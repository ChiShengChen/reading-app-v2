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
import posixpath
import re
import socket
import sys
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
