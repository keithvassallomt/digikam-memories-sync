"""Local-only HTTP server for the desktop browser interface."""
from __future__ import annotations

import argparse
import json
import secrets
import threading
import webbrowser
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from .app_service import AppService, InvalidDigikamLibrary, discover_digikam_databases
from .nextcloud_http import NextcloudConnectionError, RecognizeNotInstalledError
from .settings import SettingsStore
from .state_store import StateStore

WEB_ROOT = Path(__file__).with_name("web")
CONTENT_TYPES = {".css": "text/css; charset=utf-8", ".js": "text/javascript; charset=utf-8"}


class FaceSyncHTTPServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, address: tuple[str, int], app: AppService):
        super().__init__(address, FaceSyncHandler)
        self.app = app
        self.api_token = secrets.token_urlsafe(24)


class FaceSyncHandler(BaseHTTPRequestHandler):
    server: FaceSyncHTTPServer

    def log_message(self, format: str, *args: Any) -> None:
        return

    def do_GET(self) -> None:  # noqa: N802
        path = urlparse(self.path).path
        if path.startswith("/api/") and not self._authorized():
            self._json(HTTPStatus.FORBIDDEN, {"error": "Invalid local session."})
            return
        if path == "/api/health":
            self._json(HTTPStatus.OK, {"status": "ok"})
        elif path == "/api/settings":
            self._json(HTTPStatus.OK, self.server.app.public_settings())
        elif path == "/api/digikam/discover":
            self._json(HTTPStatus.OK, {"databases": discover_digikam_databases()})
        elif path == "/api/notifications":
            self._json(
                HTTPStatus.OK,
                {"notifications": self.server.app.state.unread_notifications()},
            )
        elif path == "/" or path == "/index.html":
            raw = (WEB_ROOT / "index.html").read_text(encoding="utf-8")
            raw = raw.replace("__FACE_SYNC_TOKEN__", self.server.api_token)
            self._bytes(HTTPStatus.OK, raw.encode(), "text/html; charset=utf-8")
        else:
            self._static(path)

    def do_POST(self) -> None:  # noqa: N802
        path = urlparse(self.path).path
        if not self._authorized():
            self._json(HTTPStatus.FORBIDDEN, {"error": "Invalid local session."})
            return
        try:
            payload = self._payload()
            if path == "/api/connection/test":
                self._json(HTTPStatus.OK, self.server.app.test_connection(payload))
            elif path == "/api/settings":
                self._json(HTTPStatus.OK, self.server.app.save_settings(payload))
            else:
                self._json(HTTPStatus.NOT_FOUND, {"error": "Not found."})
        except RecognizeNotInstalledError as error:
            self._json(
                HTTPStatus.UNPROCESSABLE_ENTITY,
                {"code": "recognize_missing", "error": str(error)},
            )
        except (InvalidDigikamLibrary, NextcloudConnectionError, ValueError) as error:
            self._json(HTTPStatus.BAD_REQUEST, {"code": "invalid_setup", "error": str(error)})
        except json.JSONDecodeError:
            self._json(HTTPStatus.BAD_REQUEST, {"error": "Invalid request."})

    def _authorized(self) -> bool:
        return self.headers.get("X-Face-Sync-Token", "") == self.server.api_token

    def _payload(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length", "0"))
        if length > 64 * 1024:
            raise ValueError("Request is too large.")
        value = json.loads(self.rfile.read(length) or b"{}")
        if not isinstance(value, dict):
            raise ValueError("Invalid request.")
        return value

    def _static(self, path: str) -> None:
        name = path.lstrip("/")
        if name not in {"app.css", "app.js"}:
            self._json(HTTPStatus.NOT_FOUND, {"error": "Not found."})
            return
        file_path = WEB_ROOT / name
        self._bytes(
            HTTPStatus.OK,
            file_path.read_bytes(),
            CONTENT_TYPES[file_path.suffix],
        )

    def _json(self, status: HTTPStatus, payload: dict[str, Any]) -> None:
        self._bytes(
            status,
            (json.dumps(payload, ensure_ascii=False) + "\n").encode(),
            "application/json; charset=utf-8",
        )

    def _bytes(self, status: HTTPStatus, body: bytes, content_type: str) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Content-Security-Policy", "default-src 'self'; style-src 'self'; script-src 'self'; base-uri 'none'; frame-ancestors 'none'")
        self.end_headers()
        self.wfile.write(body)


def create_server(config_dir: Path | None = None, port: int = 0) -> FaceSyncHTTPServer:
    settings = SettingsStore(config_dir)
    root = settings.root
    state = StateStore(root / "state.sqlite3")
    return FaceSyncHTTPServer(("127.0.0.1", port), AppService(settings, state))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run the Face Sync desktop interface")
    parser.add_argument("--port", type=int, default=0)
    parser.add_argument("--no-browser", action="store_true")
    parser.add_argument("--config-dir", type=Path)
    args = parser.parse_args(argv)
    server = create_server(args.config_dir, args.port)
    url = f"http://127.0.0.1:{server.server_port}/"
    print(f"Face Sync is running at {url}")
    if not args.no_browser:
        threading.Timer(0.2, webbrowser.open, args=(url,)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.app.state.close()
        server.server_close()
    return 0
