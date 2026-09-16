"""Local-only HTTP server for the desktop browser interface."""
from __future__ import annotations

import json
import logging
import secrets
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

from .app_service import AppService, InvalidDigikamLibrary, discover_digikam_databases
from .nextcloud_http import NextcloudConnectionError, RecognizeNotInstalledError
from .settings import SettingsStore
from .state_store import StateStore

WEB_ROOT = Path(__file__).with_name("web")
CONTENT_TYPES = {
    ".css": "text/css; charset=utf-8",
    ".js": "text/javascript; charset=utf-8",
    ".svg": "image/svg+xml",
    ".png": "image/png",
    ".woff2": "font/woff2",
    ".json": "application/json; charset=utf-8",
}
LOG = logging.getLogger(__name__)


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
        elif path == "/api/people":
            try:
                self._json(HTTPStatus.OK, {"people": self.server.app.people()})
            except (NextcloudConnectionError, ValueError) as error:
                self._json(
                    HTTPStatus.BAD_REQUEST,
                    {"code": "people_unavailable", "error": str(error)},
                )
            except Exception:
                LOG.exception("Could not load people")
                self._json(
                    HTTPStatus.INTERNAL_SERVER_ERROR,
                    {"error": "People could not be loaded. Try again."},
                )
        elif path == "/api/logs":
            try:
                self._json(HTTPStatus.OK, self.server.app.logs(**self._log_filters()))
            except ValueError as error:
                self._json(HTTPStatus.BAD_REQUEST, {"error": str(error)})
        elif path == "/api/service":
            self._json(HTTPStatus.OK, self.server.app.service_info())
        elif path == "/api/ledger":
            self._json(HTTPStatus.OK, self.server.app.ledger_summary())
        elif path == "/api/notifications":
            self._json(
                HTTPStatus.OK,
                {"notifications": self.server.app.state.unread_notifications()},
            )
        elif path == "/api/status":
            self._json(HTTPStatus.OK, self.server.app.status(self._client_hint()))
        elif path == "/api/attention":
            self._json(HTTPStatus.OK, self.server.app.attention())
        elif path == "/api/runs":
            query = parse_qs(urlparse(self.path).query)
            try:
                limit = int((query.get("limit") or ["25"])[0])
                before = query.get("before")
                before_id = int(before[0]) if before and before[0] else None
            except ValueError:
                self._json(HTTPStatus.BAD_REQUEST, {"error": "Invalid activity request."})
                return
            self._json(HTTPStatus.OK, self.server.app.activity(limit, before_id))
        elif path == "/api/runs/latest":
            self._json(
                HTTPStatus.OK,
                {"run": self.server.app.state.latest_actionable_run()},
            )
        elif path.startswith("/api/runs/"):
            try:
                parts = path.strip("/").split("/")
                run_id = int(parts[2])
                if len(parts) == 3:
                    self._json(HTTPStatus.OK, self.server.app.preview_status(run_id))
                elif len(parts) == 4 and parts[3] == "apply":
                    self._json(HTTPStatus.OK, self.server.app.apply_review(run_id))
                elif len(parts) == 4 and parts[3] == "conflicts":
                    self._json(HTTPStatus.OK, self.server.app.conflicts(run_id))
                elif len(parts) == 4 and parts[3] == "failures":
                    self._json(HTTPStatus.OK, self.server.app.failures(run_id))
                elif (
                    len(parts) == 6
                    and parts[3] == "conflicts"
                    and parts[5] == "photo"
                ):
                    conflict_id = int(parts[4])
                    body, content_type = self.server.app.conflict_photo(
                        run_id, conflict_id
                    )
                    self._bytes(HTTPStatus.OK, body, content_type)
                elif (
                    len(parts) == 6
                    and parts[3] == "failures"
                    and parts[5] == "photo"
                ):
                    action_id = int(parts[4])
                    body, content_type = self.server.app.failure_photo(
                        run_id, action_id
                    )
                    self._bytes(HTTPStatus.OK, body, content_type)
                else:
                    raise ValueError("Preview resource not found.")
            except ValueError as error:
                self._json(HTTPStatus.NOT_FOUND, {"error": str(error)})
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
            elif path == "/api/preview":
                self._json(HTTPStatus.ACCEPTED, self.server.app.start_preview(payload))
            elif path == "/api/digikam/close":
                self._json(HTTPStatus.OK, self.server.app.close_digikam())
            elif path == "/api/sync":
                self._json(HTTPStatus.ACCEPTED, self.server.app.start_sync(payload))
            elif path == "/api/automation/pause":
                self._json(HTTPStatus.OK, self.server.app.pause(payload))
            elif path == "/api/automation/resume":
                self._json(HTTPStatus.OK, self.server.app.resume())
            elif path == "/api/notifications/delivered":
                self._json(HTTPStatus.OK, self.server.app.notifications_delivered(payload))
            elif path == "/api/notifications/read":
                self._json(HTTPStatus.OK, self.server.app.mark_notifications_read(payload))
            elif path == "/api/settings/sync":
                self._json(HTTPStatus.OK, self.server.app.update_sync(payload))
            elif path == "/api/settings/automation":
                self._json(HTTPStatus.OK, self.server.app.update_automation(payload))
            elif path == "/api/settings/notifications":
                self._json(HTTPStatus.OK, self.server.app.update_notifications(payload))
            elif path == "/api/settings/retention":
                self._json(HTTPStatus.OK, self.server.app.update_retention(payload))
            elif path == "/api/decisions/apply":
                self._json(HTTPStatus.OK, self.server.app.create_decisions_run())
            elif path == "/api/ledger/rebuild":
                self._json(HTTPStatus.ACCEPTED, self.server.app.rebuild_ledger())
            elif path == "/api/service/autostart":
                self._json(HTTPStatus.OK, self.server.app.set_autostart(payload))
            elif path == "/api/shortcuts/install":
                self._json(HTTPStatus.OK, self.server.app.install_shortcut())
            elif path.startswith("/api/runs/"):
                parts = path.strip("/").split("/")
                if len(parts) == 4 and parts[3] == "apply":
                    self._json(
                        HTTPStatus.ACCEPTED,
                        self.server.app.start_apply(int(parts[2]), payload),
                    )
                elif len(parts) == 4 and parts[3] == "discard":
                    self._json(
                        HTTPStatus.OK,
                        self.server.app.discard_preview(int(parts[2])),
                    )
                elif len(parts) == 5 and parts[3] == "conflicts":
                    self._json(
                        HTTPStatus.OK,
                        self.server.app.resolve_conflict(
                            int(parts[2]), int(parts[4]), payload
                        ),
                    )
                elif len(parts) == 5 and parts[3] == "failures":
                    self._json(
                        HTTPStatus.OK,
                        self.server.app.resolve_failure(
                            int(parts[2]), int(parts[4]), payload
                        ),
                    )
                else:
                    self._json(HTTPStatus.NOT_FOUND, {"error": "Not found."})
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
        except Exception:
            self._json(
                HTTPStatus.INTERNAL_SERVER_ERROR,
                {"error": "The operation failed. Check the Face Sync terminal for details."},
            )

    def _client_hint(self) -> dict[str, Any]:
        """What the page reports about itself, used to route notifications."""
        query = parse_qs(urlparse(self.path).query)
        return {
            "visible": (query.get("visible") or ["false"])[0] == "true",
            "permission": (query.get("permission") or ["default"])[0],
        }

    def _log_filters(self) -> dict[str, Any]:
        """Read and bound the log query, rejecting anything unparseable."""
        query = parse_qs(urlparse(self.path).query)

        def single(name: str) -> str | None:
            values = query.get(name)
            return values[0] if values else None

        def number(name: str) -> int | None:
            raw = single(name)
            if raw in (None, ""):
                return None
            try:
                return int(raw)
            except ValueError as error:
                raise ValueError(f"{name} must be a whole number.") from error

        return {
            "level": single("level") or None,
            "run_id": number("run_id"),
            "query": single("q") or None,
            "after_id": number("after_id"),
            "before_id": number("before_id"),
            "limit": number("limit") or 200,
        }

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
        """Serve the interface's own files, and nothing else on the disk."""
        name = path.lstrip("/")
        if not name or name.endswith("/"):
            self._json(HTTPStatus.NOT_FOUND, {"error": "Not found."})
            return
        try:
            file_path = (WEB_ROOT / name).resolve()
            file_path.relative_to(WEB_ROOT.resolve())
        except (ValueError, OSError):
            self._json(HTTPStatus.NOT_FOUND, {"error": "Not found."})
            return
        if file_path.suffix not in CONTENT_TYPES or not file_path.is_file():
            self._json(HTTPStatus.NOT_FOUND, {"error": "Not found."})
            return
        self._bytes(HTTPStatus.OK, file_path.read_bytes(), CONTENT_TYPES[file_path.suffix])

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
        self.send_header(
            "Content-Security-Policy",
            "default-src 'self'; img-src 'self' blob:; style-src 'self'; "
            "script-src 'self'; base-uri 'none'; frame-ancestors 'none'",
        )
        self.end_headers()
        self.wfile.write(body)


def create_server(config_dir: Path | None = None, port: int = 0) -> FaceSyncHTTPServer:
    settings = SettingsStore(config_dir)
    root = settings.root
    state = StateStore(root / "state.sqlite3")
    recovered = state.recover_unfinished_runs()
    if recovered["total"]:
        LOG.info("Re-queued %s unfinished runs from the last session", recovered["total"])
    return FaceSyncHTTPServer(("127.0.0.1", port), AppService(settings, state))

