"""UI-facing application operations, independent of HTTP presentation."""
from __future__ import annotations

import os
import sqlite3
import threading
import time
import logging
import mimetypes
from datetime import datetime, timedelta, timezone
from contextlib import closing
from pathlib import Path
from typing import Any, Callable

from .constants import COMPANION_APP_NAME, CONFLICT_POLICIES
from .digikam import DigikamDB
from .digikam_writer import (
    DigikamWriter,
    create_sqlite_backup,
    digikam_database_is_free,
    digikam_is_running,
    terminate_digikam,
)
from . import checkpoint as checkpoint_module
from . import health
from . import desktop
from . import coordinator as coordinator_module
from . import notify, status as status_module
from .apply import (
    ApplyExecutor,
    build_apply_plan,
    conflict_preview_actions,
    plan_summary,
)
from .checkpoint import StateCheckpoint
from .ledger import StateLedger
from .models import SyncReport
from .nextcloud_http import NextcloudConnectionError, NextcloudHTTP, fetch_file_preview
from .reverse import compare_memories_to_digikam, selected_memories_faces
from .settings import DEFAULTS, SettingsStore
from .state_store import StateStore
from .sync import sync

REQUIRED_DIGIKAM_TABLES = {"Images", "Tags", "TagProperties", "ImageTagProperties"}
BROWSER_IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp", ".avif"}
LOG = logging.getLogger(__name__)

# How long one backup covers a sitting at the library screen.
LIBRARY_BACKUP_WINDOW = timedelta(hours=1)


class InvalidDigikamLibrary(ValueError):
    pass


def is_systemic_apply_failure(error: Exception) -> bool:
    """Return true when retrying every remaining action is likely to fail."""
    if isinstance(error, (NextcloudConnectionError, ConnectionError, TimeoutError, OSError)):
        return True
    message = str(error).lower()
    if "failed after retries" in message:
        return True
    return any(
        f"http {status}" in message
        for status in (401, 403, 408, 429, 500, 502, 503, 504)
    )


def resolve_digikam_database(value: str | Path) -> Path:
    selected = Path(value).expanduser()
    database = selected if selected.is_file() else selected / "digikam4.db"
    if not database.is_file():
        raise InvalidDigikamLibrary("No digikam4.db was found in that location.")
    try:
        uri = f"file:{database.resolve()}?mode=ro"
        with closing(sqlite3.connect(uri, uri=True)) as connection:
            tables = {
                str(row[0])
                for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")
            }
    except sqlite3.Error as error:
        raise InvalidDigikamLibrary("The digiKam database could not be read.") from error
    if not REQUIRED_DIGIKAM_TABLES.issubset(tables):
        raise InvalidDigikamLibrary("That file is not a compatible digiKam database.")
    return database.resolve()


def discover_digikam_databases() -> list[str]:
    home = Path.home()
    candidates = [
        os.environ.get("DIGIKAM_DB", ""),
        home / "Photos" / "digikam4.db",
        home / "Pictures" / "digikam4.db",
        home / ".local" / "share" / "digikam" / "digikam4.db",
        home / "Library" / "Application Support" / "digikam" / "digikam4.db",
    ]
    found: list[str] = []
    for candidate in candidates:
        if not candidate:
            continue
        path = Path(candidate).expanduser()
        if path.is_file() and str(path.resolve()) not in found:
            found.append(str(path.resolve()))
    return found


class AppService:
    def __init__(
        self,
        settings: SettingsStore,
        state: StateStore,
        *,
        backend_factory: Callable[..., NextcloudHTTP] = NextcloudHTTP,
        digikam_factory: Callable[..., DigikamDB] = DigikamDB,
        sync_function: Callable[..., Any] = sync,
    ):
        self.settings = settings
        self.state = state
        self.backend_factory = backend_factory
        self.digikam_factory = digikam_factory
        self.sync_function = sync_function
        self._job_lock = threading.Lock()
        self._jobs: dict[int, threading.Thread] = {}
        # Polling the home screen must not scan /proc or stat files every time.
        self._cache: dict[str, tuple[float, Any]] = {}
        self._client_seen: float = 0.0
        self._client_visible = False
        self._client_can_notify = False

    def _cached(self, key: str, seconds: float, produce: Callable[[], Any]) -> Any:
        now = time.monotonic()
        entry = self._cache.get(key)
        if entry is not None and now - entry[0] < seconds:
            return entry[1]
        value = produce()
        self._cache[key] = (now, value)
        return value

    def public_settings(self) -> dict[str, Any]:
        return self.settings.public_settings()

    def _explicit_config_dir(self) -> Path | None:
        """Pass the configuration directory on only when it is not the default.

        A login entry that names a non-standard directory must keep naming it;
        one using the default should not hard-code a path that may move.
        """
        from .settings import default_config_dir

        root = Path(self.settings.root).resolve()
        return None if root == default_config_dir().resolve() else root

    def logs(self, **filters: Any) -> dict[str, Any]:
        return self.state.logs(**filters)

    def service_info(self) -> dict[str, Any]:
        """What the interface shows about the background service itself."""
        from . import autostart, shortcuts
        from .service import read_service_info

        published = read_service_info(self._explicit_config_dir()) or {}
        try:
            login = autostart.status()
        except Exception:
            LOG.exception("Could not read the autostart entry")
            login = {"supported": False, "enabled": False, "mechanism": None, "path": None}
        return {
            "running": bool(published),
            "pid": published.get("pid"),
            "port": published.get("port"),
            "started_at": published.get("started_at"),
            "version": published.get("version"),
            "config_dir": str(self.settings.root),
            "autostart": login,
            "shortcut": shortcuts.status(),
        }

    def set_autostart(self, payload: dict[str, Any]) -> dict[str, Any]:
        from . import autostart

        wanted = payload.get("enabled")
        if not isinstance(wanted, bool):
            raise ValueError("Say whether DigiMem should start at login.")
        directory = self._explicit_config_dir()
        try:
            result = autostart.enable(directory) if wanted else autostart.disable(directory)
        except RuntimeError as error:
            raise ValueError(str(error)) from error
        LOG.info("Start at login turned %s", "on" if wanted else "off")
        return {**result, "supported": True}

    def install_shortcut(self) -> dict[str, Any]:
        from . import shortcuts

        try:
            return shortcuts.install(self._explicit_config_dir())
        except OSError as error:
            raise ValueError(f"The shortcut could not be created: {error}") from error

    # ----------------------------------------------------------- home status

    def _paused_until(self) -> tuple[bool, str | None]:
        """Whether a pause is in force, expiring it when its time has passed."""
        value = self.state.get_state("paused_until")
        if value is None:
            return False, None
        if value == "indefinite":
            return True, None
        try:
            until = datetime.fromisoformat(str(value))
        except ValueError:
            self.state.clear_state("paused_until")
            return False, None
        if until.tzinfo is None:
            until = until.replace(tzinfo=timezone.utc)
        if until <= datetime.now(timezone.utc):
            self.state.clear_state("paused_until")
            return False, None
        return True, until.isoformat()

    def _route_notifications(self) -> list[dict[str, Any]]:
        """Assign each new notification to exactly one channel.

        Called from the status poll because that is the moment the service
        knows whether anybody is looking.
        """
        pending = self.state.undelivered_notifications()
        if not pending:
            return []
        wanted = self.settings.load().get("notifications") or {}
        attached = (time.monotonic() - self._client_seen) < notify.CLIENT_TIMEOUT_SECONDS
        channel = notify.choose_channel(
            client_attached=attached,
            client_visible=self._client_visible,
            client_can_notify=self._client_can_notify,
        )
        suppressed: list[int] = []
        chosen: list[dict[str, Any]] = []
        for row in pending:
            event_type = str(row.get("event_type", ""))
            if not notify.is_enabled(event_type, wanted) or channel == notify.SUPPRESSED:
                suppressed.append(int(row["id"]))
            else:
                chosen.append(row)
        self.state.assign_notification_channel(suppressed, notify.SUPPRESSED)
        if not chosen:
            return []
        ids = [int(row["id"]) for row in chosen]
        self.state.assign_notification_channel(ids, channel)
        if channel != notify.BROWSER:
            return []
        return [notify.browser_payload(row) for row in chosen]

    def digikam_running(self) -> bool:
        return bool(self._cached("digikam_running", 3.0, digikam_is_running))

    def status(self, client: dict[str, Any] | None = None) -> dict[str, Any]:
        """One aggregate for the home screen, polled while the window is open."""
        client = client or {}
        self._client_seen = time.monotonic()
        self._client_visible = bool(client.get("visible"))
        self._client_can_notify = str(client.get("permission", "")) == "granted"

        settings = self.settings.load()
        configured = self.settings.is_configured()
        run = self.state.latest_actionable_run() if configured else None
        paused, paused_until = self._paused_until()
        running = self.digikam_running()
        summary = (run or {}).get("summary") or {}
        digikam_changes = int(summary.get("created_in_digikam") or 0) + int(
            summary.get("reassigned_in_digikam") or 0
        )
        # Work a run has journalled but not finished: reviewed faces, or a
        # digiKam half held back.
        applied_state = (run or {}).get("apply") or {}
        pending_changes = int(applied_state.get("pending") or 0)
        blocks = bool(
            run
            and run.get("status") in {"previewed", "deferred"}
            and digikam_changes > 0
            and running
        )
        published = self._cached("service_published", 5.0, self._published_service)
        return status_module.compute(
            configured=configured,
            settings=settings,
            run=run,
            attention=self.state.attention_counts(),
            last_completed=self.state.last_completed_run(),
            paused_until=paused_until,
            paused=paused,
            digikam_running=running,
            digikam_blocks_apply=blocks,
            pending_changes=pending_changes,
            service={"running": True, **published},
            unread=len(self.state.unread_notifications()),
            raise_notifications=self._route_notifications(),
            now=datetime.now(timezone.utc).isoformat(timespec="seconds"),
        )

    def _published_service(self) -> dict[str, Any]:
        """What the interface may know about the service. Never the token."""
        from .service import read_service_info

        published = read_service_info(self._explicit_config_dir()) or {}
        return {
            key: value
            for key, value in published.items()
            if key in {"pid", "port", "started_at", "version"}
        }

    # -------------------------------------------------------------- activity

    def activity(self, limit: int = 25, before_id: int | None = None) -> dict[str, Any]:
        return self.state.recent_runs(limit=limit, before_id=before_id)

    def attention(self) -> dict[str, Any]:
        return self.state.attention()

    # ---------------------------------------------------- the library check

    def check_library(self, *, notify_on_new: bool = False) -> dict[str, int]:
        """Look for face boxes that are wrong rather than merely unsynced.

        Cheap enough to do before every sync: about 140 ms over 13,700 faces.
        It never blocks one. A sync that refused to run until somebody had been
        through a list would stop being automatic, and these are suspicions,
        not faults.
        """
        database = self.settings.load().get("digikam_db")
        if not database:
            return {"added": 0, "closed": 0, "open": 0}
        try:
            issues = health.scan(database)
        except Exception:
            LOG.exception("Could not check the digiKam library")
            return {"added": 0, "closed": 0, "open": 0}
        result = self.state.save_library_issues([issue.to_dict() for issue in issues])
        if notify_on_new and result["added"]:
            self.state.create_notification(
                "library.issues",
                f"{result['added']} face boxes look wrong",
                "digiKam has faces that cannot sync cleanly until you look at them.",
                "/attention",
            )
        return result

    def library_issues(self) -> dict[str, Any]:
        return {"issues": self.state.open_library_issues()}

    def resolve_library_issue(
        self, issue_id: int, payload: dict[str, Any]
    ) -> dict[str, Any]:
        """Dismiss an issue, or remove the box it is about."""
        decision = str(payload.get("decision", ""))
        issue = self.state.library_issue(issue_id)
        if issue is None or issue.get("status") != "open":
            raise ValueError("That library issue is no longer open.")
        if decision == "dismiss":
            self.state.settle_library_issue(issue_id, "dismissed")
            return {"removed": False, **self.library_issues()}
        if decision != "remove":
            raise ValueError("Choose whether to remove the boxes or keep them.")

        wanted = self._boxes_in_question(issue, payload)
        settings = self.settings.load()
        database = str(settings.get("digikam_db") or "")
        if not database:
            raise ValueError("Complete the connection setup first.")
        if digikam_is_running() or not digikam_database_is_free(database):
            raise ValueError(
                "Close digiKam before removing a face box, so it does not "
                "put it back when it next saves."
            )
        self._library_backup(database)
        removed = 0
        with DigikamWriter(database) as writer:
            for person, rect in wanted:
                outcome = writer.remove_face(
                    str(issue["path"]), person, rect, image_id=issue.get("image_id"),
                )
                removed += 1 if outcome.get("changed") else 0
        if removed:
            LOG.info("Removed %s face box(es) from %s", removed, issue["path"])
        # Re-check rather than assume: removing one box can settle a second
        # issue about the same photo, and can leave one that is still wrong.
        self.check_library()
        return {"removed": removed, **self.library_issues()}

    def _library_backup(self, database: str) -> Path | None:
        """One backup per sitting, rather than one per box.

        A copy of this database is 18 MB. Taking one for every box removed
        filled the folder with near-identical copies in five minutes, and
        retention then evicted the backups taken before a sync wrote anything,
        which are the ones worth keeping. A backup is here so you can get back
        to before you started, so one covers the whole sitting.
        """
        folder = self.settings.root / "backups"
        recent = [
            path for path in folder.glob("digikam4-library-*.db")
            if datetime.now() - datetime.fromtimestamp(path.stat().st_mtime)
            < LIBRARY_BACKUP_WINDOW
        ]
        if recent:
            return max(recent, key=lambda path: path.stat().st_mtime)
        timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        return create_sqlite_backup(database, folder / f"digikam4-library-{timestamp}.db")

    @staticmethod
    def _boxes_in_question(
        issue: dict[str, Any], payload: dict[str, Any]
    ) -> list[tuple[str, tuple[float, ...]]]:
        """The boxes to remove, checked against the ones actually offered.

        Every one is matched before any is removed, so a page that has fallen
        behind cannot take away a box nobody was shown, and cannot half-finish.
        """
        raw = payload.get("boxes")
        if not isinstance(raw, list) or not raw:
            raise ValueError("Say which boxes to remove.")
        offered = {
            (str(box.get("person")), tuple(round(float(v), 6) for v in box.get("rect", [])))
            for box in issue.get("boxes", [])
        }
        wanted: list[tuple[str, tuple[float, ...]]] = []
        for entry in raw:
            if not isinstance(entry, dict):
                raise ValueError("Say which boxes to remove.")
            rect = entry.get("rect")
            person = str(entry.get("person") or "")
            if not isinstance(rect, list) or len(rect) != 4 or not person:
                raise ValueError("Say which boxes to remove.")
            key = (person, tuple(round(float(v), 6) for v in rect))
            if key not in offered:
                raise ValueError("That box is not one of the ones in question.")
            wanted.append((person, tuple(float(v) for v in rect)))
        return wanted

    def library_issue_photo(self, issue_id: int) -> tuple[bytes, str]:
        issue = self.state.library_issue(issue_id)
        if issue is None:
            raise ValueError("That library issue is no longer open.")
        return self._review_photo(issue, "library issue")

    def notifications_delivered(self, payload: dict[str, Any]) -> dict[str, Any]:
        raw = payload.get("ids")
        if not isinstance(raw, list):
            raise ValueError("Send the notification ids that were shown.")
        ids = [int(value) for value in raw]
        return {"delivered": self.state.mark_notifications_delivered(ids)}

    def mark_notifications_read(self, payload: dict[str, Any]) -> dict[str, Any]:
        raw = payload.get("ids")
        ids = [int(value) for value in raw] if isinstance(raw, list) else None
        return {"read": self.state.mark_notifications_read(ids)}

    # ------------------------------------------------------------ automation

    def pause(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Stop automatic work until a moment, or until the user resumes."""
        until = payload.get("until")
        minutes = payload.get("minutes")
        if until == "indefinite":
            value: Any = "indefinite"
        elif isinstance(minutes, (int, float)) and minutes > 0:
            moment = datetime.now(timezone.utc) + timedelta(minutes=float(minutes))
            value = moment.isoformat(timespec="seconds")
        elif isinstance(until, str) and until:
            try:
                parsed = datetime.fromisoformat(until)
            except ValueError as error:
                raise ValueError("That pause time could not be understood.") from error
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
            value = parsed.isoformat(timespec="seconds")
        else:
            raise ValueError("Say how long DigiMem should pause for.")
        self.state.set_state("paused_until", value)
        LOG.info("Automatic sync paused (%s)", value)
        self.state.create_notification(
            "automation.paused",
            "Automatic sync paused",
            "Nothing will change in either library until it resumes.",
            "/",
        )
        paused, paused_until = self._paused_until()
        return {"paused": paused, "paused_until": paused_until}

    def resume(self) -> dict[str, Any]:
        self.state.clear_state("paused_until")
        LOG.info("Automatic sync resumed")
        return {"paused": False, "paused_until": None}

    def update_sync(self, payload: dict[str, Any]) -> dict[str, Any]:
        return self._update_group("sync", payload, {
            "conflict_policy": CONFLICT_POLICIES,
            "create_in_memories": bool,
            "create_in_digikam": bool,
        })

    def update_automation(self, payload: dict[str, Any]) -> dict[str, Any]:
        return self._update_group("automation", payload, {
            "enabled": bool,
            "apply_automatically": bool,
            "quiet_period_minutes": int,
            "check_interval_minutes": int,
            "fallback_interval_hours": int,
        })

    def update_notifications(self, payload: dict[str, Any]) -> dict[str, Any]:
        return self._update_group("notifications", payload, {
            "decisions": bool, "connection": bool, "completed": bool,
        })

    def update_retention(self, payload: dict[str, Any]) -> dict[str, Any]:
        return self._update_group("retention", payload, {
            "backups_keep": int, "log_days": int,
        })

    def _update_group(
        self, group: str, payload: dict[str, Any], schema: dict[str, Any]
    ) -> dict[str, Any]:
        """Accept only known keys, coerced to the type the group expects.

        A key whose schema entry is a set of words takes one of those words.
        """
        values: dict[str, Any] = {}
        for key, kind in schema.items():
            if key not in payload:
                continue
            raw = payload[key]
            if isinstance(kind, frozenset):
                if raw not in kind:
                    allowed = ", ".join(sorted(kind))
                    raise ValueError(f"{key} must be one of: {allowed}.")
                values[key] = raw
            elif kind is bool:
                if not isinstance(raw, bool):
                    raise ValueError(f"{key} must be true or false.")
                values[key] = raw
            else:
                try:
                    number = int(raw)
                except (TypeError, ValueError) as error:
                    raise ValueError(f"{key} must be a whole number.") from error
                if number < 1:
                    raise ValueError(f"{key} must be at least 1.")
                values[key] = number
        if not values:
            raise ValueError("There was nothing to change.")
        settings = self.settings.update_group(group, values)
        LOG.info("Updated %s settings: %s", group, ", ".join(sorted(values)))
        return {group: settings.get(group), "saved": True}

    def test_connection(self, payload: dict[str, Any]) -> dict[str, Any]:
        database = resolve_digikam_database(str(payload.get("digikam_library", "")))
        user_id = str(payload.get("nc_user", "")).strip()
        password = str(payload.get("password", "")) or self.settings.password(user_id)
        if not user_id or not password:
            raise ValueError("Enter your Nextcloud username and app password.")
        url = str(payload.get("nextcloud_url", "")).strip()
        if not url:
            raise ValueError("Enter your Nextcloud address.")

        backend = self.backend_factory(url, user_id, password, http_workers=1)
        try:
            requirements = backend.connection_requirements()
            return {
                "digikam_db": str(database),
                "recognize_installed": requirements.recognize_installed,
                "face_sync_installed": requirements.face_sync_installed,
                "install_url": requirements.face_sync_install_url,
                "ready": requirements.ready,
            }
        finally:
            backend.close()

    def save_settings(self, payload: dict[str, Any]) -> dict[str, Any]:
        result = self.test_connection(payload)
        if not result["ready"]:
            return result
        settings = {
            "digikam_library": str(Path(result["digikam_db"]).parent),
            "digikam_db": result["digikam_db"],
            "nextcloud_url": str(payload["nextcloud_url"]).strip().rstrip("/"),
            "nc_user": str(payload["nc_user"]).strip(),
            "nc_photos_path": str(payload.get("nc_photos_path", "Photos")).strip("/"),
        }
        self.settings.save(settings, str(payload.get("password", "")) or None)
        return {**result, "saved": True, "settings": self.settings.public_settings()}

    def people(self) -> list[str]:
        settings = self.settings.load()
        database = settings.get("digikam_db")
        if not database:
            return []
        user_id = str(settings["nc_user"])
        password = self.settings.password(user_id)
        if not password:
            raise ValueError("The saved Nextcloud app password is unavailable.")
        backend = self.backend_factory(
            str(settings["nextcloud_url"]), user_id, password, http_workers=4
        )
        try:
            with self.digikam_factory(database) as digikam:
                digikam_people = set(digikam.person_tag_ids().values())
            memories_people = set(backend.list_named_people())
        finally:
            backend.close()
        return sorted(digikam_people | memories_people, key=str.casefold)

    @staticmethod
    def _preview_selection(payload: dict[str, Any]) -> tuple[str, str]:
        scope = str(payload.get("scope", "person"))
        if scope not in {"all", "person"}:
            raise ValueError("Choose All faces or One person.")
        person = str(payload.get("person", "")).strip() if scope == "person" else ""
        if scope == "person" and not person:
            raise ValueError("Choose a person.")
        return scope, person

    def start_preview(self, payload: dict[str, Any]) -> dict[str, Any]:
        if not self.settings.load().get("digikam_db"):
            raise ValueError("Complete the connection setup first.")
        scope, person = self._preview_selection(payload)
        trigger = str(payload.get("trigger") or "manual")
        auto_apply = payload.get("auto_apply") is True
        # Look at the library before reconciling it. A box in the wrong place
        # is not a disagreement to settle, and syncing it faithfully copies
        # the mistake. Nobody is stopped from syncing over it.
        self.check_library(notify_on_new=trigger != "manual")
        with self._job_lock:
            active = [run_id for run_id, thread in self._jobs.items() if thread.is_alive()]
            if active:
                raise ValueError(f"Preview {active[0]} is already running.")
            run_id = self.state.create_run(
                scope, trigger=trigger, auto_apply=auto_apply, person=person
            )
            thread = threading.Thread(
                target=self._preview_job,
                args=(run_id, {"scope": scope, "person": person}),
                name=f"digimem-preview-{run_id}",
                daemon=True,
            )
            self._jobs[run_id] = thread
            thread.start()
        return {"run_id": run_id, "status": "previewing"}

    def _short_backend(self) -> Any:
        """A connection for one quick question, or None when not set up."""
        settings = self.settings.load()
        user_id = str(settings.get("nc_user") or "")
        password = self.settings.password(user_id)
        if not settings.get("nextcloud_url") or not user_id or not password:
            return None
        return self.backend_factory(
            str(settings["nextcloud_url"]), user_id, password, http_workers=1
        )

    def memories_changes(self) -> Any:
        """What one look at Memories saw, or None if it could not be asked.

        None is different from "nothing changed" and must not be treated as an
        answer.
        """
        backend = self._short_backend()
        if backend is None:
            return None
        try:
            if not getattr(backend, "supports_change_fingerprint", False):
                return None
            return backend.read_changes()
        finally:
            backend.close()

    def memories_fingerprint(self) -> str | None:
        """A value that changes when a named face moves in Memories."""
        seen = self.memories_changes()
        return None if seen is None else seen.value

    def recognize_busy(self) -> bool:
        """Whether Recognize is working through its own queue."""

        def probe() -> bool:
            backend = self._short_backend()
            if backend is None:
                return False
            try:
                return bool(backend.recognize_busy())
            except Exception as error:
                LOG.debug("Could not read the Recognize status: %s", error)
                return False
            finally:
                backend.close()

        return bool(self._cached("recognize_busy", 300.0, probe))

    def deliver_desktop_notifications(self) -> int:
        """Raise anything still unannounced, for when no window is open.

        An open page gets first refusal through the status poll, so this only
        acts once nothing has polled for a while.
        """
        if (time.monotonic() - self._client_seen) < notify.CLIENT_TIMEOUT_SECONDS:
            return 0
        pending = self.state.undelivered_notifications()
        if not pending:
            return 0
        wanted = self.settings.load().get("notifications") or {}
        sent = 0
        for row in pending:
            identifier = int(row["id"])
            if not notify.is_enabled(str(row.get("event_type", "")), wanted):
                self.state.assign_notification_channel([identifier], notify.SUPPRESSED)
                continue
            self.state.assign_notification_channel([identifier], notify.OPERATING_SYSTEM)
            if desktop.send_notification(row):
                sent += 1
            # Marked either way. The inbox is the record; a desktop that
            # cannot show one must not make it repeat forever.
            self.state.mark_notifications_delivered([identifier])
        if sent:
            LOG.info("Showed %s desktop notifications", sent)
        return sent

    def prune_backups(self, keep: int) -> int:
        """Keep the newest backups, and any a run might still need."""
        folder = self.settings.root / "backups"
        if not folder.is_dir():
            return 0
        try:
            backups = sorted(
                (path for path in folder.glob("*.db") if path.is_file()),
                key=lambda path: path.stat().st_mtime,
                reverse=True,
            )
        except OSError:
            LOG.exception("Could not list the backup folder")
            return 0
        protected = self.state.protected_backups()
        removed = 0
        for path in backups[max(0, int(keep)):]:
            if str(path) in protected:
                continue
            try:
                path.unlink()
                removed += 1
            except OSError:
                LOG.debug("Could not remove the backup %s", path)
        if removed:
            LOG.info("Removed %s old digiKam backups", removed)
        return removed

    def apply_retention(self) -> dict[str, int]:
        """Housekeeping: old logs, old backups, old proposed changes."""
        retention = self.settings.load().get("retention") or {}
        result = {
            "logs": self.state.delete_old_logs(
                days=int(retention.get("log_days") or 30), debug_days=3
            ),
            "backups": self.prune_backups(int(retention.get("backups_keep") or 10)),
            "preview_actions": self.state.prune_preview_actions(days=90),
        }
        LOG.info(
            "Housekeeping removed %s log lines, %s backups and %s stored changes",
            result["logs"], result["backups"], result["preview_actions"],
        )
        return result

    def invalidate_probes(self) -> None:
        """Forget cached answers. Used after a sleep, when they may be stale."""
        self._cache.clear()

    def resume_run(self, run_id: int) -> dict[str, Any]:
        """Continue a run the coordinator has decided may proceed.

        A run with a journal resumes its apply; anything else resumes its
        preview, from the checkpoint if it has one.
        """
        run = self.state.run(run_id)
        if run is None:
            raise ValueError("Run not found.")
        with self._job_lock:
            if any(thread.is_alive() for thread in self._jobs.values()):
                raise ValueError("Another DigiMem job is already running.")
            journalled = self.state.apply_counts(run_id)["total"] > 0
            if journalled:
                self.state.resume_apply(run_id)
                target, name = self._apply_job, f"digimem-apply-{run_id}"
                arguments: tuple[Any, ...] = (run_id,)
            else:
                self.state.resume_preview(run_id)
                target, name = self._preview_job, f"digimem-preview-{run_id}"
                arguments = (
                    run_id,
                    {"scope": str(run.get("mode") or "all"), "person": run.get("person") or ""},
                )
            thread = threading.Thread(target=target, args=arguments, name=name, daemon=True)
            self._jobs[run_id] = thread
            thread.start()
        return {"run_id": run_id, "resumed": "apply" if journalled else "preview"}

    def _park_for_retry(self, run_id: int, error: Exception) -> bool:
        """Park a run that failed for a reason retrying might fix.

        Returns True when the run was parked rather than failed, so the caller
        knows not to announce a failure.
        """
        if not is_systemic_apply_failure(error):
            return False
        run = self.state.run(run_id) or {}
        attempts = int(run.get("attempts") or 0)
        when = coordinator_module.next_attempt_at(attempts)
        self.state.set_waiting(
            run_id,
            coordinator_module.WAITING_CONNECTION,
            next_attempt_at=when,
            count_attempt=True,
        )
        LOG.warning(
            "Run %s stopped on a connection problem; next attempt at %s (%s)",
            run_id, when, error,
        )
        return True

    def active_run_id(self) -> int | None:
        with self._job_lock:
            for run_id, thread in self._jobs.items():
                if thread.is_alive():
                    return run_id
        return None

    def start_sync(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Start a sync, or hand back the one already running.

        Asking twice is a normal thing for a person to do, so it attaches to
        the running job rather than failing.
        """
        running = self.active_run_id()
        if running is not None:
            return {"run_id": running, "status": "previewing", "attached": True}
        request = dict(payload)
        request.setdefault("scope", "all")
        if request.get("preview_only") is True:
            request["auto_apply"] = False
        else:
            automation = self.settings.load().get("automation") or {}
            request.setdefault(
                "auto_apply",
                bool(automation.get("enabled")) and bool(automation.get("apply_automatically")),
            )
        result = self.start_preview(request)
        LOG.info(
            "Started run %s (%s, %s)",
            result["run_id"],
            request.get("scope"),
            request.get("person") or "everyone",
        )
        return {**result, "attached": False}

    def _preview_job(self, run_id: int, payload: dict[str, Any]) -> None:
        try:
            self.preview(payload, run_id=run_id)
        except Exception:
            LOG.exception("Preview %s failed", run_id)
        finally:
            with self._job_lock:
                self._jobs.pop(run_id, None)

    def preview_status(self, run_id: int) -> dict[str, Any]:
        result = self.state.run(run_id)
        if result is None:
            raise ValueError("Preview run not found.")
        return result

    def discard_preview(self, run_id: int) -> dict[str, Any]:
        with self._job_lock:
            thread = self._jobs.get(run_id)
            if thread is not None and thread.is_alive():
                raise ValueError("This preview is still running and cannot be discarded yet.")
        self.state.discard_preview(run_id)
        return {"run_id": run_id, "status": "discarded"}

    def conflicts(self, run_id: int) -> dict[str, Any]:
        return self.state.conflicts_for_run(run_id)

    def apply_review(self, run_id: int) -> dict[str, Any]:
        run = self.state.run(run_id)
        if run is None or run.get("result") is None:
            raise ValueError("Preview run not found.")
        if run["status"] not in {
            "previewed", "applying", "deferred", "waiting", "apply_failed", "applied"
        }:
            raise ValueError("This preview is not ready to apply.")
        conflicts = self.state.conflicts_for_run(run_id)
        plan = build_apply_plan(run["result"], self.state.plan_conflicts(run_id))
        summary = (
            self.state.remaining_apply_counts(run_id)
            if run.get("apply") is not None
            else plan_summary(plan)
        )
        return {
            "run_id": run_id,
            **summary,
            "conflicts": conflicts["total"],
            "digikam_running": digikam_is_running(),
            "requires_digikam_closed": summary["digikam"] > 0,
            "status": run["status"],
            "apply": run.get("apply"),
        }

    def close_digikam(self) -> dict[str, Any]:
        result = terminate_digikam()
        if not result["supported"]:
            raise ValueError("Automatic closing is unavailable on this platform. Close digiKam normally.")
        if not result["closed"]:
            raise ValueError("digiKam did not close. Quit it manually, then try again.")
        return {"closed": True}

    def start_apply(self, run_id: int, payload: dict[str, Any]) -> dict[str, Any]:
        review = self.apply_review(run_id)
        if review["status"] == "applied":
            return self.preview_status(run_id)
        if review["total"] == 0:
            raise ValueError("There are no changes to apply.")
        # An automatic run defers the digiKam half silently. Only someone
        # clicking Apply is asked to close digiKam first.
        if review["requires_digikam_closed"] and not payload.get("automatic"):
            if payload.get("digikam_closed") is not True:
                raise ValueError("Confirm that digiKam is closed before applying changes.")
            if digikam_is_running():
                raise ValueError("digiKam is still running. Close it, then try Apply again.")
        run = self.state.run(run_id)
        assert run is not None and run["result"] is not None
        plan = build_apply_plan(run["result"], self.state.plan_conflicts(run_id))
        with self._job_lock:
            active = [job_id for job_id, thread in self._jobs.items() if thread.is_alive()]
            if active:
                raise ValueError("Another DigiMem job is already running.")
            self.state.initialize_apply(run_id, plan)
            counts = self.state.apply_counts(run_id)
            self.state.update_progress(
                run_id,
                "starting_apply",
                counts["applied"] + counts["ignored"],
                counts["total"],
                counts,
            )
            thread = threading.Thread(
                target=self._apply_job,
                args=(run_id,),
                name=f"digimem-apply-{run_id}",
                daemon=True,
            )
            self._jobs[run_id] = thread
            thread.start()
        return {"run_id": run_id, "status": "applying"}

    def _apply_job(self, run_id: int) -> None:
        backend = None
        writer = None
        try:
            settings = self.settings.load()
            database = str(settings.get("digikam_db") or "")
            if not database:
                raise ValueError("The saved digiKam database is unavailable.")
            pending = self.state.pending_apply_actions(run_id)
            counts = self.state.apply_counts(run_id)

            def digikam_is_free() -> bool:
                """Fresh enough to stop within a second of digiKam opening."""
                return not self._cached("digikam_busy", 1.0, digikam_is_running)

            def ensure_backup() -> None:
                if self.state.apply_backup(run_id):
                    return
                timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
                destination = (
                    self.settings.root / "backups" / f"digikam4-run-{run_id}-{timestamp}.db"
                )
                self.state.update_progress(
                    run_id, "backing_up_digikam", counts["applied"], counts["total"], counts
                )
                self.state.set_apply_backup(run_id, str(create_sqlite_backup(database, destination)))

            if any(item["action"]["target"] == "memories" for item in pending):
                user_id = str(settings["nc_user"])
                password = self.settings.password(user_id)
                if not password:
                    raise ValueError("The saved Nextcloud app password is unavailable.")
                backend = self.backend_factory(
                    str(settings["nextcloud_url"]), user_id, password, http_workers=4
                )
                requirements = backend.connection_requirements()
                if not requirements.ready:
                    raise ValueError(f"Nextcloud Recognize or the {COMPANION_APP_NAME} app is unavailable.")

            writer = DigikamWriter(database)
            executor = ApplyExecutor(
                backend=backend,
                digikam=writer,
                nextcloud_photos_path=str(settings.get("nc_photos_path", "Photos")),
            )
            consecutive_failures = 0
            deferred = False
            for item in pending:
                action = item["action"]
                if action["target"] == "digikam":
                    # digiKam does not notice writes made while it is open, so
                    # its half waits until it closes. The Memories half has
                    # already gone ahead.
                    if not digikam_is_free() or not digikam_database_is_free(database):
                        if not deferred:
                            LOG.info(
                                "digiKam is in use; holding its changes for run %s", run_id
                            )
                        deferred = True
                        continue
                    ensure_backup()
                try:
                    outcome = executor.execute(action)
                    self.state.finish_apply_action(item["id"], "applied", result=outcome)
                    link = executor.link_for(action, outcome)
                    if link is not None:
                        self.state.record_agreements([
                            {**link, "synced_name": link["digikam_name"]}
                        ])
                    consecutive_failures = 0
                except Exception as error:
                    LOG.exception("Apply action %s failed", item["id"])
                    self.state.finish_apply_action(item["id"], "failed", error=str(error))
                    if is_systemic_apply_failure(error):
                        consecutive_failures += 1
                    else:
                        consecutive_failures = 0
                counts = self.state.apply_counts(run_id)
                self.state.update_progress(
                    run_id,
                    "applying",
                    counts["applied"] + counts["failed"] + counts["ignored"],
                    counts["total"],
                    counts,
                )
                if consecutive_failures >= 3:
                    break

            counts = self.state.apply_counts(run_id)
            if deferred and counts["pending"]:
                # Nothing failed. The rest is simply waiting for digiKam.
                self.state.set_deferred(run_id, "digikam")
                self.state.update_progress(
                    run_id, "deferred",
                    counts["applied"] + counts["failed"] + counts["ignored"],
                    counts["total"], counts,
                )
                self.state.create_notification(
                    "sync.deferred",
                    f"{counts['pending']} changes are waiting for digiKam",
                    "They will be applied when you quit digiKam.",
                    f"/runs/{run_id}",
                    run_id=run_id,
                )
                LOG.info(
                    "Run %s deferred: %s changes wait for digiKam to close",
                    run_id, counts["pending"],
                )
                return

            final_status = "applied" if not counts["failed"] and not counts["pending"] else "apply_failed"
            final = self.state.finish_apply(run_id, final_status)
            if final_status == "applied":
                # This run has just redone the ground an earlier one covered,
                # so whatever that one left waiting is settled or unwanted.
                run = self.state.run(run_id) or {}
                retired = self.state.supersede_pending_actions(
                    run_id,
                    str(run.get("person") or "") if run.get("mode") == "person" else "",
                )
                if retired:
                    LOG.info(
                        "Run %s superseded %s change(s) left waiting on earlier runs",
                        run_id, retired,
                    )
            self.state.update_progress(
                run_id,
                "completed" if final_status == "applied" else "apply_failed",
                final["applied"] + final["failed"] + final["ignored"],
                final["total"],
                final,
                error=None if final_status == "applied" else "Some changes could not be applied.",
            )
            if final_status == "applied":
                message = (
                    f"{final['applied']} face changes completed."
                    if not final["ignored"]
                    else f"{final['applied']} face changes completed and {final['ignored']} kept in one library."
                )
                self.state.create_notification(
                    "apply.completed",
                    f"Updated {final['applied']} faces",
                    message,
                    f"/runs/{run_id}",
                    run_id=run_id,
                )
            else:
                self.state.create_notification(
                    "apply.failed",
                    f"{final['failed'] + final['pending']} changes need attention",
                    "Open DigiMem to retry the changes that did not finish.",
                    f"/runs/{run_id}",
                    run_id=run_id,
                )
        except Exception as error:
            LOG.exception("Apply %s failed", run_id)
            if self._park_for_retry(run_id, error):
                counts = self.state.apply_counts(run_id)
                self.state.update_progress(
                    run_id, "waiting",
                    counts["applied"] + counts["failed"] + counts["ignored"],
                    counts["total"], counts, error=str(error),
                )
                return
            final = self.state.finish_apply(run_id, "apply_failed")
            self.state.update_progress(
                run_id, "apply_failed", final["applied"] + final["failed"] + final["ignored"],
                final["total"], final, error=str(error),
            )
            self.state.create_notification(
                "apply.failed", "Apply failed", "Open DigiMem for details.",
                f"/runs/{run_id}", run_id=run_id,
            )
        finally:
            if writer is not None:
                writer.close()
            if backend is not None:
                backend.close()
            with self._job_lock:
                self._jobs.pop(run_id, None)

    def resolve_conflict(
        self, run_id: int, conflict_id: int, payload: dict[str, Any]
    ) -> dict[str, Any]:
        resolution = str(payload.get("resolution", ""))
        apply_to_remaining = payload.get("apply_to_remaining") is True
        return self.state.resolve_conflict(
            run_id,
            conflict_id,
            resolution,
            apply_to_remaining=apply_to_remaining,
        )

    def create_decisions_run(self) -> dict[str, Any]:
        """Gather decisions no run can carry into a follow-up run of their own.

        A conflict settled after its own run finished cannot be added to that
        run's journal, so it becomes a small run containing only decisions.
        """
        carried = self.state.runs_carrying_decisions()
        pending = self.state.pending_decisions()
        if not pending:
            return {"created": False, "decisions": 0, "carried_by": carried}
        result = conflict_preview_actions(pending)
        if not result["actions"]:
            return {"created": False, "decisions": 0, "carried_by": carried}
        run_id = self.state.create_run(
            "decisions", status="previewing", trigger="decisions"
        )
        result["run_id"] = run_id
        result["scope"] = "decisions"
        result["direction"] = "two_way"
        self.state.save_result(run_id, result)
        self.state.finish_run(run_id, "previewed", result["summary"])
        self.state.update_progress(
            run_id, "completed", len(result["actions"]), len(result["actions"]),
            result["summary"],
        )
        self.state.mark_decisions_folded([item["id"] for item in pending], run_id)
        LOG.info("Collected %s decisions into run %s", len(result["actions"]), run_id)
        return {
            "created": True,
            "run_id": run_id,
            "decisions": len(result["actions"]),
            "carried_by": carried,
        }

    def rebuild_ledger(self) -> dict[str, Any]:
        """Forget every remembered agreement and learn them again.

        The next full run writes them back. Faces that disagree at that moment
        need one decision each, exactly as they did on the first run.
        """
        removed = self.state.clear_ledger()
        LOG.info("Cleared %s remembered face names", removed)
        started = self.start_sync({"scope": "all", "preview_only": True})
        return {"cleared": removed, **started}

    def ledger_summary(self) -> dict[str, Any]:
        return {"remembered": self.state.ledger_size()}

    def failures(self, run_id: int) -> dict[str, Any]:
        run = self.state.run(run_id)
        if run is None or run["status"] != "apply_failed":
            raise ValueError("This run has no failed changes to review.")
        return self.state.failure_review(run_id)

    def _correct_source_box(
        self, run_id: int, action_id: int, decision: str, rect: list[float] | None
    ) -> int:
        """Redraw the face in digiKam where the review says it belongs.

        Moving the rectangle here means "this box is wrong", not "send a
        different box this once". Correcting only what is sent leaves the
        original where it was, with still nothing matching it, so the next run
        proposes it again and the one after that too.

        Only the digiKam direction can be corrected: the box came from there,
        and Recognize has no way to move a detection it already holds.
        """
        if decision != "retry" or not rect:
            return 0
        action = self.state.action(action_id)
        if action is None or action.get("run_id") != run_id:
            return 0
        body = action["action"]
        if body.get("operation") != "insert_memories":
            return 0
        was = body.get("rect")
        if not was or [round(v, 6) for v in was] == [round(v, 6) for v in rect]:
            return 0

        settings = self.settings.load()
        database = settings.get("digikam_db")
        if not database:
            return 0
        if digikam_is_running() or not digikam_database_is_free(database):
            raise ValueError(
                "Close digiKam before changing a face box, so it does not "
                "overwrite the change when it next saves."
            )
        timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        destination = self.settings.root / "backups" / f"digikam4-review-{timestamp}.db"
        create_sqlite_backup(database, destination)
        with DigikamWriter(database) as writer:
            outcome = writer.move_face(
                str(body.get("path") or ""),
                str(body.get("person") or ""),
                tuple(was),
                tuple(rect),
                image_id=body.get("digikam_image_id"),
            )
        if outcome.get("changed"):
            LOG.info(
                "Redrew the %s face in %s to match the review",
                body.get("person"), body.get("path"),
            )
        return 1 if outcome.get("changed") else 0

    def resolve_failure(
        self, run_id: int, action_id: int, payload: dict[str, Any]
    ) -> dict[str, Any]:
        decision = str(payload.get("decision", ""))
        rect_value = payload.get("rect")
        rect = [float(value) for value in rect_value] if isinstance(rect_value, list) else None
        corrected = self._correct_source_box(run_id, action_id, decision, rect)
        result = self.state.resolve_failed_action(
            run_id,
            action_id,
            decision,
            rect=rect,
            apply_to_remaining=payload.get("apply_to_remaining") is True,
        )
        result = {**result, "source_corrected": corrected}
        if result["remaining"] == 0 and result["pending"] == 0:
            final = self.state.finish_apply(run_id, "applied")
            self.state.update_progress(
                run_id,
                "completed",
                final["applied"] + final["ignored"],
                final["total"],
                final,
            )
            self.state.create_notification(
                "apply.completed",
                "Face decisions saved",
                f"{final['applied']} changes completed and {final['ignored']} kept in one library.",
                f"/runs/{run_id}",
                run_id=run_id,
            )
            result["status"] = "applied"
        else:
            result["status"] = "apply_failed"
        return result

    def conflict_photo(self, run_id: int, conflict_id: int) -> tuple[bytes, str]:
        conflict = self.state.conflict_for_run(run_id, conflict_id)
        return self._review_photo(conflict, "conflict")

    def failure_photo(self, run_id: int, action_id: int) -> tuple[bytes, str]:
        failure = self.state.failed_apply_action(run_id, action_id)
        return self._review_photo(failure, "failed face")

    def _review_photo(self, item: dict[str, Any], description: str) -> tuple[bytes, str]:
        settings = self.settings.load()
        library_value = settings.get("digikam_library")
        if not library_value:
            raise ValueError("The digiKam library location is unavailable.")
        library = Path(str(library_value)).expanduser().resolve()
        photo = (library / str(item.get("path", ""))).resolve()
        try:
            photo.relative_to(library)
        except ValueError as error:
            raise ValueError(f"The {description} photo path is invalid.") from error
        if not photo.is_file():
            raise ValueError(f"The {description} photo is not available locally.")
        if photo.suffix.lower() not in BROWSER_IMAGE_EXTENSIONS:
            file_id = item.get("nc_file_id")
            user_id = str(settings.get("nc_user", ""))
            password = self.settings.password(user_id)
            if file_id is None or not user_id or not password:
                raise ValueError("A browser-compatible preview is unavailable.")
            return fetch_file_preview(
                str(settings["nextcloud_url"]),
                user_id,
                password,
                int(file_id),
            )
        content_type = mimetypes.guess_type(photo.name)[0] or "application/octet-stream"
        if not content_type.startswith("image/"):
            raise ValueError("The conflict file is not a supported image.")
        return photo.read_bytes(), content_type

    def preview(self, payload: dict[str, Any], *, run_id: int | None = None) -> dict[str, Any]:
        settings = self.settings.load()
        if not settings.get("digikam_db"):
            raise ValueError("Complete the connection setup first.")
        scope, person = self._preview_selection(payload)
        # Read once, so a setting changed mid-run cannot make the two halves
        # of one preview disagree about what they were asked to do.
        sync_settings = dict(DEFAULTS["sync"], **(settings.get("sync") or {}))

        user_id = str(settings["nc_user"])
        password = self.settings.password(user_id)
        if not password:
            raise ValueError("The saved Nextcloud app password is unavailable.")
        if run_id is None:
            run_id = self.state.create_run(scope)
        saved = self.state.checkpoint(run_id)
        report = SyncReport()
        if saved is not None:
            checkpoint_module.restore(report, saved["counters"])
            LOG.info(
                "Resuming run %s from %s (%s actions already recorded)",
                run_id, saved["phase"], self.state.count_preview_actions(run_id),
            )
        backend = None
        face_ledger = StateLedger(self.state).load()
        sink = StateCheckpoint(self.state, run_id)
        try:
            backend = self.backend_factory(
                str(settings["nextcloud_url"]), user_id, password, http_workers=16
            )
            with self.digikam_factory(str(settings["digikam_db"])) as digikam:
                self.state.update_progress(run_id, "loading_memories", 0, 0)
                named_faces = backend.list_named_faces(
                    person or None,
                    progress_callback=lambda current: self.state.update_progress(
                        run_id,
                        "loading_memories",
                        current,
                        0,
                        {"loaded_faces": current},
                    ),
                )
                selected_faces = selected_memories_faces(
                    named_faces,
                    nextcloud_photos_path=str(settings.get("nc_photos_path", "Photos")),
                    only_person=person or None,
                )
                memories_files = len({relative.lower() for relative, _ in selected_faces})
                if person:
                    digikam_files = len(digikam.image_ids_for_person(person))
                else:
                    digikam_files = digikam.count_images_with_faces()
                total_work = digikam_files + memories_files

                def forward_progress(progress: dict[str, Any]) -> None:
                    detail = dict(progress)
                    detail["phase"] = "scanning_digikam"
                    detail["current"] = int(progress["current"])
                    detail["total"] = total_work
                    detail["created_in_digikam"] = 0
                    self.state.update_progress(
                        run_id,
                        "scanning_digikam",
                        int(progress["current"]),
                        total_work,
                        detail,
                    )

                phase = saved["phase"] if saved else checkpoint_module.SCANNING_DIGIKAM
                cursor = saved["cursor"] if saved else {}
                start_index = 0
                if phase == checkpoint_module.SCANNING_MEMORIES:
                    # The digiKam half is already written out and counted.
                    start_index = int(cursor.get("path_index") or 0)
                else:
                    report = self.sync_function(
                        digikam,
                        backend,
                        path_maps=[
                            (
                                str(settings["digikam_library"]),
                                str(settings.get("nc_photos_path", "Photos")),
                            )
                        ],
                        apply=False,
                        only_person=person or None,
                        insert_missing=sync_settings["create_in_memories"],
                        conflict_policy=sync_settings["conflict_policy"],
                        batch_size=250,
                        max_actions=250000,
                        session=None,
                        progress_callback=forward_progress,
                        ledger=face_ledger,
                        checkpoint=sink,
                        start_after_image_id=cursor.get("after_image_id"),
                        report=report,
                    )
                    # Move the mark before the second half starts, so a crash
                    # here does not replay the first half.
                    self.state.save_checkpoint(
                        run_id,
                        checkpoint_module.SCANNING_MEMORIES,
                        {"path_index": 0},
                        checkpoint_module.counters_of(report),
                    )
                compare_memories_to_digikam(
                    digikam,
                    selected_faces,
                    report,
                    ledger=face_ledger,
                    conflict_policy=sync_settings["conflict_policy"],
                    create_in_digikam=sync_settings["create_in_digikam"],
                    batch_size=250,
                    max_actions=250000,
                    checkpoint=sink,
                    start_index=start_index,
                    progress_callback=lambda progress: self.state.update_progress(
                        run_id,
                        "scanning_memories",
                        digikam_files + int(progress["current"]),
                        total_work,
                        {
                            **progress,
                            "phase": "scanning_memories",
                            "current": digikam_files + int(progress["current"]),
                            "total": total_work,
                        },
                    ),
                )
            # Anything still in memory belongs to the final partial batch.
            sink.batch_done(checkpoint_module.FINISHED, {}, report)
            data = report.to_dict()
            data["actions"] = self.state.preview_actions(run_id)
            data["conflicts"] = []
            actions, ignored_actions = self.state.filter_ignored_actions(data["actions"])
            data["actions"] = actions
            for action in ignored_actions:
                if action.get("action") == "insert":
                    data["summary"]["inserted"] -= 1
                elif action.get("action") == "create_digikam":
                    data["summary"]["created_in_digikam"] -= 1
                elif action.get("action") == "reassign_digikam":
                    data["summary"]["reassigned_in_digikam"] -= 1
            data["summary"]["ignored"] = len(ignored_actions)
            summary = data["summary"]
            result = {
                "run_id": run_id,
                "scope": scope,
                "person": person or None,
                "direction": "two_way",
                **data,
            }
            self.state.save_result(run_id, result)
            self.state.finish_run(run_id, "previewed", summary)
            self.state.update_progress(
                run_id, "completed", total_work, total_work, summary
            )
            # Conflicts were written out batch by batch. Only a full run has
            # seen everything, so only a full run may close what it no longer
            # reports.
            if scope == "all":
                closed = self.state.close_conflicts_not_seen(run_id)
                if closed:
                    LOG.info("Closed %s conflicts settled in one of the libraries", closed)
            self.state.clear_checkpoint(run_id)
            if summary["conflicts"]:
                self.state.create_notification(
                    "conflicts.created",
                    f"{summary['conflicts']} conflicts",
                    "Click to resolve the faces that have different names.",
                    f"/runs/{run_id}/conflicts",
                    run_id=run_id,
                )
            else:
                changes = (
                    summary["assigned"]
                    + summary["inserted"]
                    + summary["created_in_digikam"]
                    + summary.get("reassigned_in_digikam", 0)
                )
                self.state.create_notification(
                    "run.completed" if changes else "run.no_changes",
                    "Preview complete",
                    f"{changes} face changes are ready to review.",
                    f"/runs/{run_id}",
                    run_id=run_id,
                )
            return result
        except Exception as error:
            if self._park_for_retry(run_id, error):
                self.state.update_progress(
                    run_id, "waiting", 0, 0,
                    {"reason": coordinator_module.WAITING_CONNECTION},
                    error=str(error),
                )
                raise
            self.state.finish_run(run_id, "failed", {})
            self.state.update_progress(run_id, "failed", 0, 0, error=str(error))
            self.state.create_notification(
                "run.failed",
                "Preview failed",
                "Open DigiMem for details.",
                f"/runs/{run_id}",
                run_id=run_id,
            )
            raise
        finally:
            if backend is not None:
                backend.close()
