"""Local-only HTTP API and UI for FocusGuard."""
import json
import logging
import socket
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from core import GuardError

log = logging.getLogger("focusguard")

UI_FILE = Path(__file__).parent / "ui" / "index.html"


class Handler(BaseHTTPRequestHandler):
    server_version = "FocusGuard"

    def log_message(self, fmt, *args):
        pass  # keep the log for meaningful events only

    # ------------------------------------------------------------ helpers

    def _host_ok(self):
        # Rejecting other Host headers defeats DNS-rebinding from web pages.
        port = self.server.server_address[1]
        return self.headers.get("Host") in (f"127.0.0.1:{port}", f"localhost:{port}")

    def _send(self, status, body, content_type="application/json"):
        data = body if isinstance(body, bytes) else json.dumps(body).encode()
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    # ------------------------------------------------------------ routes

    def do_GET(self):
        if not self._host_ok():
            return self._send(403, {"error": "forbidden"})
        guard = self.server.guard
        if self.path in ("/", "/index.html"):
            return self._send(200, UI_FILE.read_bytes(), "text/html; charset=utf-8")
        if self.path == "/api/status":
            return self._send(200, guard.status())
        if self.path == "/api/history":
            return self._send(200, guard.history_view())
        self._send(404, {"error": "not found"})

    def do_POST(self):
        # The custom header plus JSON content type forces a CORS preflight,
        # which we never answer, so other websites can't drive this API.
        if (not self._host_ok() or self.headers.get("X-FocusGuard") != "1"
                or not self.headers.get("Content-Type", "").startswith("application/json")):
            return self._send(403, {"error": "forbidden"})
        try:
            length = min(int(self.headers.get("Content-Length", 0)), 100_000)
            body = json.loads(self.rfile.read(length) or b"{}")
        except ValueError:
            return self._send(400, {"error": "bad json"})
        if not isinstance(body, dict):
            return self._send(400, {"error": "bad json"})

        guard = self.server.guard
        routes = {
            "/api/unlock/start": lambda: guard.start_unlock(
                body.get("site"), body.get("minutes"), body.get("reason", "")),
            "/api/unlock/heartbeat": lambda: guard.heartbeat(body.get("id")),
            "/api/unlock/complete": lambda: guard.complete_unlock(body.get("id")),
            "/api/unlock/cancel": lambda: guard.cancel_unlock(body.get("id")),
            "/api/relock": lambda: guard.relock(body.get("site")),
            "/api/config": lambda: guard.save_config(body.get("config")),
            "/api/pending/cancel": lambda: guard.cancel_pending(body.get("kind")),
            "/api/uninstall/request": guard.request_uninstall,
        }
        route = routes.get(self.path)
        if not route:
            return self._send(404, {"error": "not found"})
        try:
            result = route()
        except GuardError as e:
            return self._send(e.status, {"error": str(e)})
        except Exception:
            log.exception("Error handling %s", self.path)
            return self._send(500, {"error": "internal error"})
        self._send(200, result if result is not None else {"ok": True})


class ExclusiveServer(ThreadingHTTPServer):
    # On Windows SO_REUSEADDR lets a second process bind the same port, which
    # would break the single-instance lock, so bind exclusively instead.
    allow_reuse_address = False
    daemon_threads = True

    def server_bind(self):
        if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
            self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        super().server_bind()


def make_server(port):
    server = ExclusiveServer(("127.0.0.1", port), Handler)
    server.guard = None
    return server
