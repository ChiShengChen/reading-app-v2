#!/usr/bin/env python3
"""逐段翻譯閱讀器 — 本機伺服器

用法:  python3 server.py  [埠號, 預設 8765]
然後用瀏覽器開 http://localhost:8765
手機連同一個 Wi-Fi 後,開 http://<這台電腦的IP>:8765

翻譯來源: Google 翻譯免費網頁介面 (translate.googleapis.com)
不需要 API key、不需要安裝任何 Python 套件。
"""
import io
import json
import os
import posixpath
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import urllib.parse
import urllib.request
import zipfile
from html.parser import HTMLParser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from xml.etree import ElementTree

try:
    import fitz  # PyMuPDF
except ImportError:
    fitz = None
try:
    import pypdf
except ImportError:
    pypdf = None

ROOT = Path(__file__).resolve().parent
PORT = int(sys.argv[1]) if len(sys.argv) > 1 else 8765

GOOGLE_URL = "https://translate.googleapis.com/translate_a/single"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")


def google_translate(text: str, target: str, source: str = "auto") -> dict:
    """呼叫 Google 翻譯的 gtx 端點,回傳 {'text': 譯文, 'src': 偵測到的來源語言}"""
    params = urllib.parse.urlencode({
        "client": "gtx", "sl": source, "tl": target, "dt": "t", "dj": "1",
    })
    data = urllib.parse.urlencode({"q": text}).encode("utf-8")
    req = urllib.request.Request(
        f"{GOOGLE_URL}?{params}", data=data,
        headers={"User-Agent": UA,
                 "Content-Type": "application/x-www-form-urlencoded"})
    with urllib.request.urlopen(req, timeout=30) as resp:
        payload = json.loads(resp.read().decode("utf-8"))
    # dj=1 時回傳 {"sentences": [{"trans": ...}, ...], "src": "en"}
    translated = "".join(s.get("trans", "") for s in payload.get("sentences", []))
    return {"text": translated, "src": payload.get("src", "")}


def _is_cjk(ch: str) -> bool:
    return "一" <= ch <= "鿿" or "　" <= ch <= "ヿ" \
        or "豈" <= ch <= "﫿" or "＀" <= ch <= "￯"


def _join_wrapped_lines(text: str) -> str:
    """把一個段落區塊裡被硬換行切開的行接回一行"""
    buf = ""
    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            continue
        if not buf:
            buf = line
        elif buf.endswith("-") and len(buf) > 1 and buf[-2].isalpha():
            buf = buf[:-1] + line          # 英文連字號斷行
        elif _is_cjk(buf[-1]) or _is_cjk(line[0]):
            buf += line                    # 中日韓文字直接相連
        else:
            buf += " " + line
    return buf


def extract_pdf(data: bytes) -> str:
    """PDF → 純文字,段落之間以空行分隔"""
    if fitz is not None:
        paras = []
        with fitz.open(stream=data, filetype="pdf") as doc:
            for page in doc:
                # blocks 模式會依版面把文字分成區塊,區塊≈段落
                for block in page.get_text("blocks"):
                    if block[6] != 0:      # 只要文字區塊,跳過圖片
                        continue
                    joined = _join_wrapped_lines(block[4])
                    if joined:
                        paras.append(joined)
        return "\n\n".join(paras)
    if pypdf is not None:
        reader = pypdf.PdfReader(io.BytesIO(data))
        pages = [(p.extract_text() or "") for p in reader.pages]
        blocks = re.split(r"\n\s*\n+", "\n\n".join(pages))
        return "\n\n".join(filter(None, (_join_wrapped_lines(b) for b in blocks)))
    raise RuntimeError("找不到 PDF 解析套件,請執行: pip install pymupdf")


class _HTMLTextExtractor(HTMLParser):
    """把 XHTML 內容轉成純文字,在區塊元素邊界斷段"""
    BLOCK_TAGS = {"p", "div", "h1", "h2", "h3", "h4", "h5", "h6", "li",
                  "blockquote", "section", "article", "tr", "br", "table"}
    SKIP_TAGS = {"script", "style", "head", "title"}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.chunks = [""]
        self._skip_depth = 0

    def _break(self):
        if self.chunks[-1].strip():
            self.chunks.append("")

    def handle_starttag(self, tag, attrs):
        if tag in self.SKIP_TAGS:
            self._skip_depth += 1
        elif tag in self.BLOCK_TAGS:
            self._break()

    def handle_endtag(self, tag):
        if tag in self.SKIP_TAGS:
            self._skip_depth = max(0, self._skip_depth - 1)
        elif tag in self.BLOCK_TAGS:
            self._break()

    def handle_data(self, data):
        if not self._skip_depth:
            self.chunks[-1] += data

    def text(self) -> str:
        paras = (re.sub(r"\s+", " ", c).strip() for c in self.chunks)
        return "\n\n".join(p for p in paras if p)


def extract_epub(data: bytes) -> str:
    """EPUB → 純文字,依 spine 順序串起各章,段落以空行分隔"""
    with zipfile.ZipFile(io.BytesIO(data)) as zf:
        container = ElementTree.fromstring(zf.read("META-INF/container.xml"))
        opf_path = container.find(
            ".//{urn:oasis:names:tc:opendocument:xmlns:container}rootfile"
        ).get("full-path")
        opf_dir = posixpath.dirname(opf_path)
        opf = ElementTree.fromstring(zf.read(opf_path))
        ns = {"opf": "http://www.idpf.org/2007/opf"}

        manifest = {item.get("id"): item.get("href")
                    for item in opf.findall(".//opf:manifest/opf:item", ns)}
        spine = [ref.get("idref")
                 for ref in opf.findall(".//opf:spine/opf:itemref", ns)]

        parts = []
        for idref in spine:
            href = manifest.get(idref)
            if not href:
                continue
            path = posixpath.normpath(posixpath.join(opf_dir, urllib.parse.unquote(href)))
            try:
                html_bytes = zf.read(path)
            except KeyError:
                continue
            parser = _HTMLTextExtractor()
            parser.feed(html_bytes.decode("utf-8", errors="replace"))
            chapter = parser.text()
            if chapter:
                parts.append(chapter)
        return "\n\n".join(parts)


MAX_UPLOAD = 100 * 1024 * 1024  # 100 MB

# ---------------------------------------------------------------------------
# 論文審稿模式:透過本機已登入的 Claude Code CLI (headless) 產生深度審稿報告
# ---------------------------------------------------------------------------

REVIEW_PROMPT = """\
論文檔案在目前目錄的 paper.pdf。請先用 Read 工具把整篇論文讀完(超過 10 頁就分段用 pages 參數讀,\
必須讀完全文包含附錄),讀完之後才開始寫報告。

重要:閱讀過程中不要輸出任何過場說明文字(不要說「我先來閱讀」之類的話)。\
你唯一的文字輸出就是最終那份完整的 Markdown 審稿報告本體,從第一個字開始就是報告內容。

請深度解析附檔論文。目標:我讀完你的分析後,能向同行完整講解該方法,並在 Q&A 中回答技術追問\
(conference talk 等級的理解)。

我的背景:熟悉 deep learning(Transformer/attention、diffusion、contrastive/self-supervised \
learning)、時序與頻域訊號處理(EEG/生理訊號)、基礎量子計算與 variational quantum circuits。\
請跳過基礎概念解釋,直接進入這篇論文的特定設計。

全程遵守的原則:
- 解釋「為什麼這樣設計」,而非只描述「是什麼」。
- 三層資訊標註:直接來自論文的內容為預設(關鍵 claim 請附 §/Eq./Table 編號);論文未明說、\
由你合理推論的標【推論】;論文完全沒提、你以領域知識補充的標【補充】。禁止把推論寫成論文事實;\
資訊不足就明說「論文未提供」,不要猜。
- 篇幅分配:§2–3(方法與數學)至少佔全文一半。其他節依論文性質可壓縮到幾句,不要硬湊。
- 若發現論文內部不一致(公式與文字矛盾、維度對不上、正文與附錄數字兜不攏),直接指出。

## 1. 定位與故事(簡短)
- 一句話總結:解決什麼問題、關鍵思路、達到什麼效果。
- 在 research landscape 的位置:開創新方向、突破現有 pipeline 的某個瓶頸、還是新理論框架?
- 與最相關的 2–3 篇 prior work 的具體差異:點名論文、指出差異點,不要泛泛說「過去方法不足」。
- 為什麼能中這個 venue:hook 是什麼(新問題?反直覺結果?大幅提升?優雅理論?)、\
故事線怎麼鋪陳、理論/實驗/可視化哪些元素撐起說服力。

## 2. 方法:直覺 → 架構
- 先用 2–3 句 high-level intuition:不看公式就能懂「它在做什麼、賭的是什麼假設」。
- 完整 data flow:從原始輸入到最終輸出,資料經過哪些模組、每一步的目的。
- 每個關鍵設計的動機:為什麼選這個而非替代方案?論文有無討論?若沒討論,【補充】你認為真正的原因。
- 新提出的模組或機制:特別標註並詳解運作原理。

## 3. 核心數學走查(最重要)
選出 2–3 個最核心的運算/模組,每個做到:
- 寫出公式(LaTeX),使用論文原始 notation;每個符號標註物理意義與維度。
- 用具體 tensor shape 走完整個計算流程。超參數論文有給就用論文的;沒給則標【假設】。\
例:輸入 [B=2, T=64, C=22] 的 EEG 訊號,每步操作後 shape 如何變化、為什麼。
- 若涉及 attention:Q/K/V 的來源、shape、attention score 的完整計算過程。
- 若涉及特殊數學(KL divergence、contrastive loss、variational bound 等):用 2–3 組具體數值示範計算。
- 若涉及量子電路/擴散模型/其他非標準模組:用示意流程說明經典 ↔ 特殊表示之間的轉換。
- 收尾:給出該核心模組的 PyTorch-style pseudocode(20–40 行),重點是 shape 正確、模組邏輯完整,\
不求可直接執行。目標是我看完能自己寫出 forward pass。

## 4. 證據鏈:Claim → 實驗 → 結果
先用表格對齊論文的主要 claims(通常 2–4 個):Claim | 驗證實驗 | 關鍵數字 | 支持強度(強/中/弱)+理由
再補充:
- 資料集與 baseline 的選擇是否公平充分?有沒有明顯該比而沒比的方法?
- 消融實驗:哪個模組貢獻最大、哪個可能可拿掉?
- 統計顯著性、error bars、random seeds 有無交代?
- Failure cases:論文坦承了什麼、迴避了什麼?

## 5. 審稿人視角
- 最強的 2–3 個優點,具體到能寫進 meta-review 的程度。
- 最可質疑的 2–3 個弱點;claims 與證據之間有無邏輯跳躍?
- 可重現性:是否開源、細節是否足夠、compute 需求多大?

## 6. 對我的延伸價值
- 可復用的技術模組或設計模式,結合我的方向評估:frequency-domain physiological signals、\
EEG foundation models、quantum-classical hybrid ML。
- 基於此工作最有潛力的 2–3 個延伸方向,以及值得探索的 open questions。

## 7. 收尾:模擬 Q&A 自測
出 5 題同行或審稿人最可能追問的尖銳技術問題(至少 2 題針對數學細節、1 題針對實驗弱點),\
每題附 2–3 句參考答案。這是檢驗我是否真的理解的自測題。

## 8. 正式評審結論
- 先判斷這篇論文最適合的 venue 類型(頂會如 NeurIPS/ICML/ICLR/MICCAI,或期刊)。
- 若是頂會:給出該會議格式的評分(例如 NeurIPS: Rating 1–10、Confidence 1–5、\
Soundness/Presentation/Contribution 各 1–4),附 2–3 句理由。
- 若是期刊:給出審稿結果建議(Accept / Minor Revision / Major Revision / Reject)與理由。
- 最後附上正式的 Comments to Authors(用英文,照審稿慣例):Summary、Strengths、Weaknesses、\
Questions to Authors、Minor Issues。

輸出格式:Markdown、公式用 LaTeX($...$ 與 $$...$$)。寧可深挖少數關鍵點,不要淺嚐所有細節。
"""

CLAUDE_CMD = ["claude", "-p", "--output-format", "stream-json",
              "--include-partial-messages", "--verbose",
              "--model", "opus", "--allowedTools", "Read"]


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def _send(self, code: int, body: bytes, ctype: str) -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _send_json(self, code: int, obj: dict) -> None:
        self._send(code, json.dumps(obj, ensure_ascii=False).encode("utf-8"),
                   "application/json; charset=utf-8")

    def do_GET(self):
        path = urllib.parse.urlparse(self.path).path
        if path in ("/", "/index.html"):
            try:
                body = (ROOT / "index.html").read_bytes()
            except FileNotFoundError:
                self._send(500, "index.html 不存在".encode(), "text/plain; charset=utf-8")
                return
            self._send(200, body, "text/html; charset=utf-8")
        elif path == "/ping":
            self._send_json(200, {"ok": True})
        else:
            self._send(404, b"not found", "text/plain")

    def do_POST(self):
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        if path == "/extract":
            self._handle_extract(parsed)
            return
        if path == "/review":
            self._handle_review()
            return
        if path != "/translate":
            self._send(404, b"not found", "text/plain")
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
            req = json.loads(self.rfile.read(length).decode("utf-8"))
            text = (req.get("text") or "").strip()
            target = req.get("target") or "zh-TW"
            source = req.get("source") or "auto"
            if not text:
                self._send_json(400, {"error": "文字是空的"})
                return
            result = google_translate(text, target, source)
            self._send_json(200, result)
        except Exception as exc:  # 回報給前端顯示,不讓伺服器掛掉
            self._send_json(502, {"error": f"翻譯失敗: {exc}"})

    def _handle_extract(self, parsed):
        """接收上傳的 PDF/EPUB 原始位元組,回傳抽出的純文字"""
        try:
            query = urllib.parse.parse_qs(parsed.query)
            name = (query.get("name", [""])[0]).lower()
            length = int(self.headers.get("Content-Length", "0"))
            if length <= 0:
                self._send_json(400, {"error": "沒有收到檔案內容"})
                return
            if length > MAX_UPLOAD:
                self._send_json(413, {"error": "檔案太大 (上限 100 MB)"})
                return
            data = self.rfile.read(length)
            if name.endswith(".pdf") or data[:5] == b"%PDF-":
                text = extract_pdf(data)
            elif name.endswith(".epub") or data[:2] == b"PK":
                text = extract_epub(data)
            else:
                self._send_json(400, {"error": "只支援 .pdf 和 .epub"})
                return
            if not text.strip():
                self._send_json(422, {"error": "抽不出文字 — 可能是掃描圖片版 PDF"})
                return
            self._send_json(200, {"text": text})
        except Exception as exc:
            self._send_json(500, {"error": f"解析失敗: {exc}"})

    # ---- 審稿模式:chunked 串流回應 ----

    def _chunk(self, obj) -> None:
        data = (json.dumps(obj, ensure_ascii=False) + "\n").encode("utf-8")
        self.wfile.write(f"{len(data):X}\r\n".encode() + data + b"\r\n")
        self.wfile.flush()

    def _handle_review(self):
        length = int(self.headers.get("Content-Length", "0"))
        if length <= 0 or length > MAX_UPLOAD:
            self._send_json(400, {"error": "沒有收到檔案或檔案太大"})
            return
        data = self.rfile.read(length)
        if data[:5] != b"%PDF-":
            self._send_json(400, {"error": "審稿模式只接受 PDF 檔"})
            return

        tmpdir = tempfile.mkdtemp(prefix="paper_review_")
        proc = None
        try:
            with open(os.path.join(tmpdir, "paper.pdf"), "wb") as f:
                f.write(data)

            proc = subprocess.Popen(
                CLAUDE_CMD, cwd=tmpdir,
                stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                stderr=subprocess.PIPE, text=True, encoding="utf-8")
            threading.Thread(  # 送 prompt 後關閉 stdin,避免阻塞
                target=lambda: (proc.stdin.write(REVIEW_PROMPT), proc.stdin.close()),
                daemon=True).start()

            # 開始 chunked 串流
            self.send_response(200)
            self.send_header("Content-Type", "application/x-ndjson; charset=utf-8")
            self.send_header("Transfer-Encoding", "chunked")
            self.send_header("Cache-Control", "no-store")
            self.end_headers()

            final_text, is_error = None, False
            for line in proc.stdout:
                line = line.strip()
                if not line:
                    continue
                try:
                    msg = json.loads(line)
                except ValueError:
                    continue
                mtype = msg.get("type")
                if mtype == "stream_event":
                    event = msg.get("event") or {}
                    etype = event.get("type")
                    if etype == "content_block_delta":
                        delta = event.get("delta") or {}
                        if delta.get("type") == "text_delta" and delta.get("text"):
                            self._chunk({"t": "delta", "text": delta["text"]})
                    elif etype == "content_block_start":
                        block = event.get("content_block") or {}
                        if block.get("type") == "tool_use":
                            self._chunk({"t": "status", "text": "Claude 正在閱讀論文…"})
                elif mtype == "result":
                    final_text = msg.get("result")
                    is_error = bool(msg.get("is_error"))

            proc.wait(timeout=30)
            if final_text and not is_error:
                self._chunk({"t": "final", "text": final_text})
            else:
                err = (proc.stderr.read() or "")[-500:] or "claude CLI 沒有回傳結果"
                self._chunk({"t": "error", "text": err})
            self.wfile.write(b"0\r\n\r\n")
        except (BrokenPipeError, ConnectionResetError):
            pass  # 使用者關頁面/中斷,下面 finally 會收拾子行程
        except Exception as exc:
            try:
                self._chunk({"t": "error", "text": f"審稿失敗: {exc}"})
                self.wfile.write(b"0\r\n\r\n")
            except OSError:
                pass
        finally:
            if proc and proc.poll() is None:
                proc.kill()
            shutil.rmtree(tmpdir, ignore_errors=True)

    def log_message(self, fmt, *args):  # 安靜一點,只留錯誤
        if args and str(args[1]).startswith(("4", "5")):
            super().log_message(fmt, *args)


def lan_ip() -> str:
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except OSError:
        return "127.0.0.1"


if __name__ == "__main__":
    server = ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
    print("逐段翻譯閱讀器已啟動:")
    print(f"  這台電腦:  http://localhost:{PORT}")
    print(f"  手機(同 Wi-Fi): http://{lan_ip()}:{PORT}")
    print("按 Ctrl+C 停止")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n已停止")
