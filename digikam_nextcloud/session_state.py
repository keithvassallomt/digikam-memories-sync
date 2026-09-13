"""File-based sync session state for cancel / resume.

Each run can persist progress under a session id so a later invocation can
skip already-processed digiKam image ids and continue where it left off.
"""
from __future__ import annotations

import json
import logging
import os
import secrets
import tempfile
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Optional

LOG = logging.getLogger(__name__)

# Default under the process cwd so sessions travel with the project checkout.
DEFAULT_STATE_DIR = ".sync_sessions"

# Schema version for forward-compatible loads
STATE_VERSION = 1

VALID_STATUSES = frozenset(
    {"running", "completed", "cancelled", "failed"}
)


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def generate_session_id() -> str:
    """Return a readable unique session id: YYYYMMDD-HHMMSS-<6 hex>."""
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    return f"{stamp}-{secrets.token_hex(3)}"


def default_state_dir() -> Path:
    env = os.environ.get("DIGIKAM_NEXTCLOUD_STATE_DIR")
    if env:
        return Path(env).expanduser()
    return Path(DEFAULT_STATE_DIR)


def _validate_session_id(session_id: str) -> str:
    sid = (session_id or "").strip()
    if not sid:
        raise ValueError("session_id must be non-empty")
    # Keep filenames portable: no path separators / nulls
    if any(c in sid for c in ("/", "\\", "\0", "..")):
        raise ValueError(f"invalid session_id {session_id!r}")
    if len(sid) > 128:
        raise ValueError("session_id too long (max 128)")
    return sid


@dataclass
class SessionParams:
    """Fingerprint of run parameters (warn on resume if they diverge)."""

    digikam_db: str = ""
    backend: str = ""
    apply: bool = False
    only_person: Optional[str] = None
    batch_size: int = 500
    iou_threshold: float = 0.4
    insert_missing: bool = True
    prefer_digikam_on_conflict: bool = True
    limit_images: Optional[int] = None
    nc_path_prefix: str = "files/"
    path_maps: list[list[str]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Optional[dict[str, Any]]) -> "SessionParams":
        if not data:
            return cls()
        path_maps = data.get("path_maps") or []
        maps: list[list[str]] = []
        for m in path_maps:
            if isinstance(m, (list, tuple)) and len(m) == 2:
                maps.append([str(m[0]), str(m[1])])
        return cls(
            digikam_db=str(data.get("digikam_db") or ""),
            backend=str(data.get("backend") or ""),
            apply=bool(data.get("apply", False)),
            only_person=data.get("only_person"),
            batch_size=int(data.get("batch_size") or 500),
            iou_threshold=float(data.get("iou_threshold") or 0.4),
            insert_missing=bool(data.get("insert_missing", True)),
            prefer_digikam_on_conflict=bool(
                data.get("prefer_digikam_on_conflict", True)
            ),
            limit_images=(
                int(data["limit_images"])
                if data.get("limit_images") is not None
                else None
            ),
            nc_path_prefix=str(data.get("nc_path_prefix") or "files/"),
            path_maps=maps,
        )

    def differences(self, other: "SessionParams") -> list[str]:
        """Human-readable list of significant param mismatches."""
        diffs: list[str] = []
        pairs = [
            ("digikam_db", self.digikam_db, other.digikam_db),
            ("backend", self.backend, other.backend),
            ("apply", self.apply, other.apply),
            ("only_person", self.only_person, other.only_person),
            ("batch_size", self.batch_size, other.batch_size),
            ("iou_threshold", self.iou_threshold, other.iou_threshold),
            ("insert_missing", self.insert_missing, other.insert_missing),
            (
                "prefer_digikam_on_conflict",
                self.prefer_digikam_on_conflict,
                other.prefer_digikam_on_conflict,
            ),
            ("limit_images", self.limit_images, other.limit_images),
            ("nc_path_prefix", self.nc_path_prefix, other.nc_path_prefix),
            ("path_maps", self.path_maps, other.path_maps),
        ]
        for name, a, b in pairs:
            if a != b:
                diffs.append(f"{name}: session={a!r} current={b!r}")
        return diffs


@dataclass
class ReportSnapshot:
    """Cumulative counters restored across resume."""

    files_digikam: int = 0
    files_nextcloud: int = 0
    files_matched: int = 0
    files_unmatched_digikam: int = 0
    faces_digikam: int = 0
    faces_nextcloud: int = 0
    assigned: int = 0
    inserted: int = 0
    skipped: int = 0
    conflicts: int = 0
    warnings: list[str] = field(default_factory=list)
    unmatched_paths: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Optional[dict[str, Any]]) -> "ReportSnapshot":
        if not data:
            return cls()
        return cls(
            files_digikam=int(data.get("files_digikam") or 0),
            files_nextcloud=int(data.get("files_nextcloud") or 0),
            files_matched=int(data.get("files_matched") or 0),
            files_unmatched_digikam=int(data.get("files_unmatched_digikam") or 0),
            faces_digikam=int(data.get("faces_digikam") or 0),
            faces_nextcloud=int(data.get("faces_nextcloud") or 0),
            assigned=int(data.get("assigned") or 0),
            inserted=int(data.get("inserted") or 0),
            skipped=int(data.get("skipped") or 0),
            conflicts=int(data.get("conflicts") or 0),
            warnings=list(data.get("warnings") or []),
            unmatched_paths=list(data.get("unmatched_paths") or []),
        )


class SessionState:
    """
    Persist sync progress for one session id.

    Storage layout::

        {state_dir}/{session_id}.json

    ``processed_image_ids`` is the set of digiKam image ids fully handled
    (matched or unmatched) so resume skips them without redoing work.
    """

    def __init__(
        self,
        session_id: str,
        state_dir: Optional[str | Path] = None,
        *,
        params: Optional[SessionParams] = None,
    ):
        self.session_id = _validate_session_id(session_id)
        self.state_dir = Path(state_dir) if state_dir else default_state_dir()
        self.path = self.state_dir / f"{self.session_id}.json"

        self.version = STATE_VERSION
        self.status = "running"
        self.created_at = _utc_now_iso()
        self.updated_at = self.created_at
        self.params = params or SessionParams()
        self.total_images = 0
        self.images_done = 0
        self.faces_done = 0
        self.batches_done = 0
        self.processed_image_ids: set[int] = set()
        self.report = ReportSnapshot()
        self.message = ""
        self._dirty = False

    # ------------------------------------------------------------------ load / save

    @classmethod
    def create(
        cls,
        *,
        session_id: Optional[str] = None,
        state_dir: Optional[str | Path] = None,
        params: Optional[SessionParams] = None,
    ) -> "SessionState":
        sid = session_id or generate_session_id()
        state = cls(sid, state_dir=state_dir, params=params)
        state.state_dir.mkdir(parents=True, exist_ok=True)
        state.save(force=True)
        LOG.info("Created session %s → %s", state.session_id, state.path)
        return state

    @classmethod
    def load(
        cls,
        session_id: str,
        state_dir: Optional[str | Path] = None,
    ) -> "SessionState":
        sid = _validate_session_id(session_id)
        base = Path(state_dir) if state_dir else default_state_dir()
        path = base / f"{sid}.json"
        if not path.is_file():
            raise FileNotFoundError(
                f"Session state not found: {path} "
                f"(session_id={sid!r}, state_dir={base})"
            )
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            raise ValueError(f"Corrupt session state (not an object): {path}")

        state = cls(sid, state_dir=base)
        state.version = int(data.get("version") or STATE_VERSION)
        state.status = str(data.get("status") or "running")
        if state.status not in VALID_STATUSES:
            state.status = "running"
        state.created_at = str(data.get("created_at") or _utc_now_iso())
        state.updated_at = str(data.get("updated_at") or state.created_at)
        state.params = SessionParams.from_dict(data.get("params"))
        progress = data.get("progress") or {}
        state.total_images = int(progress.get("total_images") or 0)
        state.images_done = int(progress.get("images_done") or 0)
        state.faces_done = int(progress.get("faces_done") or 0)
        state.batches_done = int(progress.get("batches_done") or 0)
        ids = progress.get("processed_image_ids") or []
        state.processed_image_ids = {int(x) for x in ids}
        # Prefer images_done from set size if set is authoritative
        if state.processed_image_ids:
            state.images_done = max(state.images_done, len(state.processed_image_ids))
        state.report = ReportSnapshot.from_dict(data.get("report"))
        state.message = str(data.get("message") or "")
        state._dirty = False
        LOG.info(
            "Loaded session %s (%s): %d images done, status=%s",
            state.session_id,
            path,
            len(state.processed_image_ids),
            state.status,
        )
        return state

    @classmethod
    def open_or_create(
        cls,
        session_id: Optional[str] = None,
        state_dir: Optional[str | Path] = None,
        *,
        params: Optional[SessionParams] = None,
        resume: bool = False,
    ) -> "SessionState":
        """
        Open an existing session or create a new one.

        If ``resume`` is True, ``session_id`` is required and must exist.
        If ``session_id`` is given and the file exists, load it (resume).
        Otherwise create a new session (auto-id when session_id is None).
        """
        base = Path(state_dir) if state_dir else default_state_dir()
        if resume:
            if not session_id:
                raise ValueError("--resume requires a session id")
            state = cls.load(session_id, state_dir=base)
            if params is not None:
                diffs = state.params.differences(params)
                if diffs:
                    LOG.warning(
                        "Session %s params differ from current run:\n  %s",
                        state.session_id,
                        "\n  ".join(diffs),
                    )
            if state.status == "completed":
                LOG.warning(
                    "Session %s already completed (%d images). "
                    "Re-opening will skip all previously processed ids.",
                    state.session_id,
                    len(state.processed_image_ids),
                )
            state.status = "running"
            state.message = "resumed"
            state.save(force=True)
            return state

        if session_id:
            path = base / f"{_validate_session_id(session_id)}.json"
            if path.is_file():
                # Same id as an existing file → resume behaviour
                return cls.open_or_create(
                    session_id=session_id,
                    state_dir=base,
                    params=params,
                    resume=True,
                )
            return cls.create(
                session_id=session_id, state_dir=base, params=params
            )

        return cls.create(session_id=None, state_dir=base, params=params)

    def to_dict(self) -> dict[str, Any]:
        # Store ids sorted for stable diffs / smaller git noise if ever committed
        ids_sorted = sorted(self.processed_image_ids)
        return {
            "version": self.version,
            "session_id": self.session_id,
            "status": self.status,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "message": self.message,
            "params": self.params.to_dict(),
            "progress": {
                "total_images": self.total_images,
                "images_done": self.images_done,
                "faces_done": self.faces_done,
                "batches_done": self.batches_done,
                "processed_count": len(ids_sorted),
                "processed_image_ids": ids_sorted,
            },
            "report": self.report.to_dict(),
        }

    def save(self, *, force: bool = False) -> None:
        if not force and not self._dirty:
            return
        self.updated_at = _utc_now_iso()
        self.state_dir.mkdir(parents=True, exist_ok=True)
        payload = json.dumps(self.to_dict(), indent=2, ensure_ascii=False) + "\n"
        # Atomic replace so a crash mid-write never corrupts the session file
        fd, tmp_name = tempfile.mkstemp(
            prefix=f".{self.session_id}.",
            suffix=".tmp",
            dir=str(self.state_dir),
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                fh.write(payload)
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp_name, self.path)
        except Exception:
            try:
                os.unlink(tmp_name)
            except OSError:
                pass
            raise
        self._dirty = False
        LOG.debug("Session %s saved (%s)", self.session_id, self.path)

    # ------------------------------------------------------------------ mutate

    def set_total_images(self, total: int) -> None:
        self.total_images = max(0, int(total))
        self._dirty = True

    def mark_processed(
        self,
        image_ids: Iterable[int],
        *,
        faces: int = 0,
    ) -> None:
        added = 0
        for iid in image_ids:
            iid = int(iid)
            if iid not in self.processed_image_ids:
                self.processed_image_ids.add(iid)
                added += 1
        if added or faces:
            self.images_done = len(self.processed_image_ids)
            self.faces_done += max(0, int(faces))
            self._dirty = True

    def mark_batch_done(self) -> None:
        self.batches_done += 1
        self._dirty = True

    def update_report_from_sync_report(self, report: Any) -> None:
        """Copy live SyncReport counters into the snapshot."""
        self.report = ReportSnapshot(
            files_digikam=int(report.files_digikam),
            files_nextcloud=int(report.files_nextcloud),
            files_matched=int(report.files_matched),
            files_unmatched_digikam=int(report.files_unmatched_digikam),
            faces_digikam=int(report.faces_digikam),
            faces_nextcloud=int(report.faces_nextcloud),
            assigned=int(report.assigned),
            inserted=int(report.inserted),
            skipped=int(report.skipped),
            conflicts=int(getattr(report, "conflict_count", len(report.conflicts))),
            warnings=list(report.warnings)[:100],
            unmatched_paths=list(report.unmatched_paths)[:500],
        )
        self._dirty = True

    def apply_snapshot_to_report(self, report: Any) -> None:
        """Seed a SyncReport with previously accumulated counters."""
        r = self.report
        report.files_digikam = r.files_digikam
        report.files_nextcloud = r.files_nextcloud
        report.files_matched = r.files_matched
        report.files_unmatched_digikam = r.files_unmatched_digikam
        report.faces_digikam = r.faces_digikam
        report.faces_nextcloud = r.faces_nextcloud
        report.assigned = r.assigned
        report.inserted = r.inserted
        report.skipped = r.skipped
        # Detail lists start empty for this segment; keep prior conflict count
        report.prior_conflicts = int(r.conflicts or 0)
        if r.unmatched_paths and not report.unmatched_paths:
            report.unmatched_paths = list(r.unmatched_paths)
        if r.warnings and not report.warnings:
            report.warnings = list(r.warnings)

    def set_status(self, status: str, message: str = "") -> None:
        if status not in VALID_STATUSES:
            raise ValueError(f"invalid status {status!r}")
        self.status = status
        if message:
            self.message = message
        self._dirty = True

    def finish(self, status: str = "completed", message: str = "") -> None:
        self.set_status(status, message=message or status)
        self.save(force=True)
        LOG.info(
            "Session %s finished: status=%s images_done=%d path=%s",
            self.session_id,
            self.status,
            self.images_done,
            self.path,
        )


def list_sessions(state_dir: Optional[str | Path] = None) -> list[dict[str, Any]]:
    """Return summary dicts for all session files in ``state_dir``."""
    base = Path(state_dir) if state_dir else default_state_dir()
    if not base.is_dir():
        return []
    out: list[dict[str, Any]] = []
    for path in sorted(base.glob("*.json")):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as e:
            out.append(
                {
                    "session_id": path.stem,
                    "path": str(path),
                    "status": "corrupt",
                    "error": str(e),
                }
            )
            continue
        progress = data.get("progress") or {}
        out.append(
            {
                "session_id": data.get("session_id") or path.stem,
                "path": str(path),
                "status": data.get("status"),
                "created_at": data.get("created_at"),
                "updated_at": data.get("updated_at"),
                "images_done": progress.get("images_done")
                or progress.get("processed_count")
                or 0,
                "total_images": progress.get("total_images") or 0,
                "assigned": (data.get("report") or {}).get("assigned"),
                "apply": (data.get("params") or {}).get("apply"),
                "only_person": (data.get("params") or {}).get("only_person"),
                "message": data.get("message") or "",
            }
        )
    return out


def print_sessions(state_dir: Optional[str | Path] = None) -> None:
    sessions = list_sessions(state_dir)
    base = Path(state_dir) if state_dir else default_state_dir()
    if not sessions:
        print(f"No sessions in {base}")
        return
    print(f"Sessions in {base}:\n")
    for s in sessions:
        if s.get("status") == "corrupt":
            print(f"  {s['session_id']}: CORRUPT ({s.get('error')})")
            continue
        total = s.get("total_images") or 0
        done = s.get("images_done") or 0
        pct = f"{100.0 * done / total:.1f}%" if total else "?"
        print(
            f"  {s['session_id']}\n"
            f"    status={s.get('status')}  progress={done}/{total} ({pct})  "
            f"assigned={s.get('assigned')}  apply={s.get('apply')}\n"
            f"    only_person={s.get('only_person')!r}  "
            f"updated={s.get('updated_at')}\n"
            f"    {s.get('path')}"
        )
