"""Local test server for browser/Electron tests (127.0.0.1 only, never a public address).

  /            page with a coloured banner, a text input and a button; every input/click is reported to /ev
  /ev?...      logs one JSON line to the event log
  /login       sets a session cookie `aw_session=<token>` (so a test can seed a fixture profile)
  /whoami      200 "authenticated:<token>" when the cookie is present, 401 "anonymous" otherwise
  /ping        204

Run as a script (`web_probe.py LOGFILE [PORTFILE]`) or import `WebProbe` in tests.
"""
import json
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

PAGE = """<!doctype html><html><head><meta charset=utf-8><title>AWTEST page %(banner)s</title>
<style>body{margin:0;font:20px sans-serif;background:%(color)s;color:#fff}h1{font-size:64px;margin:20px}
input,button{font-size:24px;margin:10px 20px;color:#111}</style></head><body>
<h1>%(banner)s</h1><input id=t placeholder="type here" autofocus><br>
<button id=b onclick="ev('click',++n)">Click me</button> <span id=c>0</span>
<script>var n=0;function ev(k,v){fetch('/ev?k='+k+'&v='+encodeURIComponent(v));document.getElementById('c').textContent=n}
document.getElementById('t').addEventListener('input',function(e){ev('text',e.target.value)});
fetch('/ev?k=loaded&v=1');</script></body></html>"""


class _H(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _send(self, code, body=b"", ctype="text/plain", extra=None):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        u = urlparse(self.path)
        q = {k: v[0] for k, v in parse_qs(u.query).items()}
        srv = self.server.probe
        if u.path == "/":
            body = PAGE % {"banner": q.get("b", srv.banner), "color": q.get("c", srv.color)}
            self._send(200, body.encode(), "text/html; charset=utf-8")
        elif u.path == "/ev":
            srv.record({"ev": q.get("k"), "v": q.get("v")})
            self._send(204)
        elif u.path == "/login":
            tok = q.get("token", "fixture-token")
            self._send(200, b"ok", extra={
                "Set-Cookie": f"aw_session={tok}; Max-Age=31536000; Path=/; HttpOnly"})
        elif u.path == "/whoami":
            ck = self.headers.get("Cookie", "")
            tok = next((c.split("=", 1)[1] for c in ck.split("; ") if c.startswith("aw_session=")), None)
            srv.record({"ev": "whoami", "v": tok})
            if tok:
                self._send(200, f"authenticated:{tok}".encode())
            else:
                self._send(401, b"anonymous")
        elif u.path == "/ping":
            self._send(204)
        else:
            self._send(404, b"not found")


class WebProbe:
    def __init__(self, logfile, banner="PAGE", color="#d53f8c"):
        self.logfile, self.banner, self.color = logfile, banner, color
        self._lock = threading.Lock()
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), _H)
        self.httpd.probe = self
        self.port = self.httpd.server_address[1]
        self.url = f"http://127.0.0.1:{self.port}"
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)

    def start(self):
        open(self.logfile, "a").close()
        self.thread.start()
        return self

    def stop(self):
        self.httpd.shutdown()
        self.httpd.server_close()

    def record(self, d):
        d["t"] = time.time()
        with self._lock, open(self.logfile, "a") as f:
            f.write(json.dumps(d) + "\n")

    def events(self):
        out = []
        try:
            for line in open(self.logfile):
                try:
                    out.append(json.loads(line))
                except ValueError:
                    pass
        except OSError:
            pass
        return out


if __name__ == "__main__":
    p = WebProbe(sys.argv[1]).start()
    if len(sys.argv) > 2:
        open(sys.argv[2], "w").write(str(p.port))
    print(p.url, flush=True)
    threading.Event().wait()
