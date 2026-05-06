"""HTTP shell server: serves shell.html and the project REST API."""
import json, os, re, threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from launcher.launch_config import load_options, project_options, save_options

SHELL_HTML = os.path.join(os.path.dirname(__file__), "shell.html")


def make_handler(pm, base_dir):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, fmt, *args):
            pass

        def _json(self, code, body):
            data = json.dumps(body, ensure_ascii=False).encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(data)

        def _err(self, code, msg):
            self._json(code, {"error": msg})

        def _read_body(self):
            n = int(self.headers.get("Content-Length") or 0)
            if not n:
                return {}
            try:
                return json.loads(self.rfile.read(n).decode("utf-8") or "{}")
            except Exception:
                return {}

        def do_GET(self):
            if self.path == "/" or self.path == "/index.html":
                with open(SHELL_HTML, "rb") as f:
                    data = f.read()
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(data)))
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self.wfile.write(data)
                return
            if self.path == "/api/projects":
                return self._json(200, pm.list())
            if self.path == "/api/options":
                return self._json(200, load_options(base_dir))
            return self._err(404, "not found")

        def do_POST(self):
            if self.path == "/api/projects":
                body = self._read_body()
                try:
                    options = body.get("options") or project_options(load_options(base_dir))
                    project = pm.create(body.get("name") or "新对话", auto_start=False, options=options)
                    warning = None
                    try:
                        pm.start(project["id"])
                    except Exception as exc:
                        warning = str(exc)
                    return self._json(200, {"project": pm.get(project["id"]), "warning": warning})
                except Exception as e:
                    return self._err(500, str(e))
            if self.path == "/api/options":
                try:
                    return self._json(200, save_options(base_dir, self._read_body()))
                except Exception as e:
                    return self._err(500, str(e))
            m = re.match(r"^/api/projects/([\w-]+)/options$", self.path)
            if m:
                try:
                    if not pm.update_options(m.group(1), self._read_body()):
                        return self._err(404, "project not found")
                    return self._json(200, {"ok": True, "project": pm.get(m.group(1))})
                except Exception as e:
                    return self._err(500, str(e))
            m = re.match(r"^/api/projects/([\w-]+)/(start|stop|rename|activate)$", self.path)
            if m:
                pid, action = m.group(1), m.group(2)
                body = self._read_body()
                try:
                    if action == "start":
                        pm.start(pid)
                    elif action == "stop":
                        pm.stop(pid)
                    elif action == "rename":
                        if not pm.rename(pid, body.get("name", "")):
                            return self._err(400, "name required")
                    elif action == "activate":
                        pm.set_active(pid)
                    return self._json(200, {"ok": True, "project": pm.get(pid)})
                except Exception as e:
                    return self._err(500, str(e))
            return self._err(404, "not found")

        def do_DELETE(self):
            m = re.match(r"^/api/projects/([\w-]+)$", self.path)
            if m:
                try:
                    pm.delete(m.group(1))
                    return self._json(200, {"ok": True})
                except Exception as e:
                    return self._err(500, str(e))
            return self._err(404, "not found")

    return Handler


def serve(pm, port, base_dir=None):
    base_dir = base_dir or getattr(pm, "base_dir", os.path.dirname(os.path.dirname(__file__)))
    server = ThreadingHTTPServer(("127.0.0.1", port), make_handler(pm, base_dir))
    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()
    return server
