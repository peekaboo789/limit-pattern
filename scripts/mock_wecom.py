"""本地 Mock 企业微信接口（gettoken / message/send / webhook），用于联调和截图。

python scripts/mock_wecom.py 9900  → 设置里「接口地址 / 代理」填 http://127.0.0.1:9900；打开 http://127.0.0.1:9900/ 看收到的消息。"""

import html
import json
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

SENT = []


class H(BaseHTTPRequestHandler):
    def _json(self, obj):
        data = json.dumps(obj).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *a):
        pass

    def do_GET(self):
        u = urlparse(self.path)
        if u.path == "/cgi-bin/gettoken":
            return self._json({"errcode": 0, "errmsg": "ok", "access_token": "mock-token", "expires_in": 7200})
        rows = "".join(
            f"<div class='msg'><div class='who'>涨停形态系统 · {html.escape(m.get('msgtype', ''))} → {html.escape(str(m.get('touser', '')))}</div>"
            f"<pre>{html.escape((m.get('markdown') or m.get('text') or {}).get('content', ''))}</pre></div>" for m in SENT)
        page = f"""<html><meta charset='utf-8'><style>body{{font-family:sans-serif;background:#eef0f3;padding:20px}}
.msg{{background:#fff;border-radius:8px;padding:12px 16px;margin:10px 0;max-width:520px;box-shadow:0 1px 3px #0002}}
.who{{color:#888;font-size:12px;margin-bottom:6px}}pre{{white-space:pre-wrap;font-family:inherit;margin:0}}</style>
<h3>Mock 企业微信 — 收到 {len(SENT)} 条</h3>{rows}</html>"""
        data = page.encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.end_headers()
        self.wfile.write(data)

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)) or b"{}")
        if urlparse(self.path).path in ("/cgi-bin/message/send", "/cgi-bin/webhook/send"):
            SENT.append(body)
            return self._json({"errcode": 0, "errmsg": "ok"})
        self._json({"errcode": 404, "errmsg": "not found"})


if __name__ == "__main__":
    ThreadingHTTPServer(("127.0.0.1", int(sys.argv[1]) if len(sys.argv) > 1 else 9900), H).serve_forever()
