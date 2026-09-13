"""Command-line interface."""
from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from pathlib import Path
from typing import Any, Optional

from .constants import DEFAULT_SKIP_PERSONS
from .digikam import DigikamDB
from .logging_setup import setup_logging
from .models import NextcloudBackend, SyncReport
from .nextcloud_db import NextcloudDB
from .nextcloud_http import NextcloudHTTP
from .session_state import (
    DEFAULT_STATE_DIR,
    SessionParams,
    SessionState,
    print_sessions,
)
from .sync import SyncCancelled, sync

LOG = logging.getLogger(__name__)

def load_config(path: Optional[str]) -> dict[str, Any]:
    if not path:
        return {}
    p = Path(path)
    if not p.is_file():
        raise FileNotFoundError(f"Config not found: {p}")
    text = p.read_text(encoding="utf-8")
    if p.suffix.lower() in (".yaml", ".yml"):
        try:
            import yaml
        except ImportError as e:
            raise SystemExit(
                "PyYAML is required for YAML config. pip install PyYAML"
            ) from e
        data = yaml.safe_load(text) or {}
    else:
        data = json.loads(text)
    if not isinstance(data, dict):
        raise ValueError("Config root must be a mapping")
    return data


def parse_path_maps(items: list[str] | None, cfg_maps: Any) -> list[tuple[str, str]]:
    maps: list[tuple[str, str]] = []
    if isinstance(cfg_maps, list):
        for m in cfg_maps:
            if isinstance(m, dict) and "from" in m and "to" in m:
                maps.append((str(m["from"]), str(m["to"])))
            elif isinstance(m, (list, tuple)) and len(m) == 2:
                maps.append((str(m[0]), str(m[1])))
    if items:
        for raw in items:
            if "=" not in raw:
                raise SystemExit(f"Invalid --path-map {raw!r}; expected FROM=TO")
            src, dst = raw.split("=", 1)
            maps.append((src, dst))
    return maps


def build_ssh_config(args: argparse.Namespace, cfg: dict[str, Any]) -> Optional[dict[str, Any]]:
    ssh_cfg = dict(cfg.get("ssh") or {})
    if args.ssh_host:
        ssh_cfg["host"] = args.ssh_host
        ssh_cfg["enabled"] = True
    if args.ssh_user:
        ssh_cfg["user"] = args.ssh_user
    if args.ssh_port is not None:
        ssh_cfg["port"] = args.ssh_port
    if args.ssh_identity:
        ssh_cfg["identity_file"] = args.ssh_identity
    if args.ssh_remote_host:
        ssh_cfg["remote_bind_host"] = args.ssh_remote_host
    if args.ssh_remote_port is not None:
        ssh_cfg["remote_bind_port"] = args.ssh_remote_port
    if args.ssh_local_port is not None:
        ssh_cfg["local_bind_port"] = args.ssh_local_port
    if not ssh_cfg.get("host"):
        return None
    ssh_cfg.setdefault("enabled", True)
    if ssh_cfg.get("enabled") is False:
        return None
    return ssh_cfg


def print_human_report(report: SyncReport, apply: bool) -> None:
    mode = "APPLY" if apply else "DRY-RUN"
    s = report.to_dict()["summary"]
    print(f"\n=== Face sync report ({mode}) ===")
    print(f"  digiKam images with faces : {s['files_digikam']}")
    print(f"  Nextcloud image files     : {s['files_nextcloud']}")
    print(f"  Matched files             : {s['files_matched']}")
    print(f"  Unmatched digiKam files   : {s['files_unmatched_digikam']}")
    print(f"  digiKam face regions      : {s['faces_digikam']}")
    print(f"  Nextcloud face regions    : {s['faces_nextcloud']}")
    print(f"  Assignments               : {s['assigned']}")
    print(f"  Inserts                   : {s['inserted']}")
    print(f"  Skipped (already OK)      : {s['skipped']}")
    print(f"  Conflicts                 : {s['conflicts']}")
    if report.prior_conflicts:
        print(
            f"    (includes {report.prior_conflicts} from earlier session segments; "
            f"{len(report.conflicts)} detailed this run)"
        )

    if report.conflicts:
        print("\n--- Conflicts (overlapping regions, different people) ---")
        for c in report.conflicts:
            print(
                f"  {c.path}\n"
                f"    digiKam : {c.digikam_person!r} rect={c.digikam_rect}\n"
                f"    Nextcloud: {c.nextcloud_person!r} rect={c.nextcloud_rect}\n"
                f"    IoU={c.iou:.3f}  nc_detection_id={c.nc_detection_id}"
            )

    if report.unmatched_paths:
        print(
            f"\n--- Unmatched digiKam paths "
            f"(showing up to 30 of {len(report.unmatched_paths)}) ---"
        )
        for p in report.unmatched_paths[:30]:
            print(f"  {p}")

    interesting = [a for a in report.actions if a.action in ("assign", "insert", "conflict")]
    if interesting:
        print(f"\n--- Actions (showing up to 40 of {len(interesting)}) ---")
        for a in interesting[:40]:
            print(f"  [{a.action}] {a.path} → {a.person!r}  {a.detail}")

    if report.warnings:
        uniq = list(dict.fromkeys(report.warnings))
        print(f"\n--- Warnings ({len(uniq)}) ---")
        for w in uniq[:20]:
            print(f"  ! {w}")

    if not apply:
        print("\nNo changes written. Re-run with --apply to update Nextcloud.")


def build_arg_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="Sync digiKam face regions → Nextcloud Recognize (one-way).",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Backends:
  db    SQL into Nextcloud DB (full read/write including inserts).
        Use --ssh-* to reach PostgreSQL/MySQL on a remote host over SSH.
  http  Nextcloud WebDAV + Recognize DAV (app password). Assigns/renames
        people and, when the digiKam Face Sync app is installed, creates boxes.

Examples:
  # Preferred: HTTP / WebDAV (app password)
  python sync_faces.py --config config.yaml
  python sync_faces.py --backend http \\
      --digikam-db ~/digikam4.db \\
      --nextcloud-url https://cloud.example.com \\
      --nc-user alice --nc-password 'app-password' \\
      --http-workers 24 --batch-size 500 --no-insert-missing

  # Optional: direct SQL (+ SSH tunnel) when you have DB access
  python sync_faces.py --backend db --config config.yaml \\
      --ssh-host bastion.example.com --ssh-user cliff

  # Apply
  python sync_faces.py --config config.yaml --apply

  # Single person (test / quick update)
  python sync_faces.py --config config.yaml --only-person Alice
  python sync_faces.py --config config.yaml --only-person Alice --apply

  # Resume after Ctrl+C (session id is printed at start / on cancel)
  python sync_faces.py --config config.yaml --apply
  python sync_faces.py --config config.yaml --apply --resume 20260722-120000-abc123
  python sync_faces.py --list-sessions
""",
    )
    p.add_argument("--config", help="YAML/JSON config file")
    p.add_argument(
        "--backend",
        choices=("http", "db"),
        default=None,
        help="Nextcloud access: http (WebDAV, preferred) or db (SQL). Default: http",
    )
    p.add_argument("--digikam-db", help="Path to digikam4.db")
    p.add_argument(
        "--nextcloud-dsn",
        help="DB DSN (postgresql://... mysql://... sqlite:///...). Host is the "
        "DB address as seen from the SSH target when tunneling.",
    )
    p.add_argument(
        "--nextcloud-url",
        help="Nextcloud base URL for HTTP backend (https://cloud.example.com)",
    )
    p.add_argument("--nc-user", help="Nextcloud user id")
    p.add_argument(
        "--nc-password",
        help="Nextcloud password or app password (HTTP). "
        "Prefer env NEXTCLOUD_PASSWORD.",
    )
    p.add_argument(
        "--recognize-api-key",
        help="X-Recognize-Api-Key header if the server requires it for DAV mutations",
    )
    p.add_argument(
        "--no-verify-ssl",
        action="store_true",
        help="Disable TLS certificate verification (HTTP backend)",
    )
    p.add_argument("--table-prefix", default=None, help="DB table prefix (default oc_)")
    p.add_argument(
        "--nc-path-prefix",
        default=None,
        help="Path prefix to scan (default files/). DB: filecache path; "
        "HTTP: same form, files/ is stripped for WebDAV.",
    )
    p.add_argument(
        "--storage-filter",
        default=None,
        help="DB only: SQL LIKE filter on oc_storages.id",
    )
    p.add_argument(
        "--path-map",
        action="append",
        default=None,
        metavar="FROM=TO",
        help="Map digiKam path prefix to Nextcloud path (after stripping files/).",
    )
    p.add_argument("--iou-threshold", type=float, default=None)
    p.add_argument("--skip-person", action="append", default=None)
    p.add_argument(
        "--only-person",
        default=None,
        metavar="NAME",
        help=(
            "Only process this digiKam person (case-insensitive). "
            "Targets images tagged with that person — useful for tests and "
            "quick single-person updates."
        ),
    )
    p.add_argument(
        "--no-insert-missing",
        action="store_true",
        help="Do not insert new face boxes (always true for HTTP backend)",
    )
    p.add_argument(
        "--leave-conflicts",
        action="store_true",
        help="Report conflicts but do not overwrite Nextcloud person names",
    )
    p.add_argument("--apply", action="store_true", help="Write changes")
    p.add_argument("--json-out", help="Write full JSON report")
    p.add_argument("-v", "--verbose", action="store_true")
    p.add_argument(
        "--batch-size",
        type=int,
        default=None,
        help="Images per batch (default 500). Keeps memory O(batch) for 500k+ faces.",
    )
    p.add_argument(
        "--limit-images",
        type=int,
        default=None,
        help="Only process first N faced images (smoke tests / partial runs)",
    )
    p.add_argument(
        "--http-workers",
        type=int,
        default=None,
        help="Concurrent WebDAV workers for HTTP backend (default 16)",
    )
    p.add_argument(
        "--max-report-actions",
        type=int,
        default=None,
        help="Max detailed actions retained in report (default 2000)",
    )

    # Session / resume
    sess = p.add_argument_group("Session state (cancel / resume)")
    sess.add_argument(
        "--session-id",
        default=None,
        metavar="ID",
        help=(
            "Session id for progress file. Auto-generated when omitted "
            "(unless --no-session). Re-use the same id to resume."
        ),
    )
    sess.add_argument(
        "--resume",
        default=None,
        metavar="ID",
        help="Resume an existing session (must already exist under --state-dir)",
    )
    sess.add_argument(
        "--state-dir",
        default=None,
        help=(
            f"Directory for session JSON files "
            f"(default: {DEFAULT_STATE_DIR} or env "
            f"DIGIKAM_NEXTCLOUD_STATE_DIR)"
        ),
    )
    sess.add_argument(
        "--no-session",
        action="store_true",
        help="Do not write session state (cannot resume after cancel)",
    )
    sess.add_argument(
        "--list-sessions",
        action="store_true",
        help="List known sessions in --state-dir and exit",
    )

    # SSH tunnel (DB backend)
    ssh = p.add_argument_group("SSH tunnel (DB backend)")
    ssh.add_argument("--ssh-host", help="SSH jump host")
    ssh.add_argument("--ssh-user", help="SSH username")
    ssh.add_argument("--ssh-port", type=int, default=None, help="SSH port (default 22)")
    ssh.add_argument(
        "--ssh-identity",
        help="SSH private key path (BatchMode; agent/keys only, no password prompts)",
    )
    ssh.add_argument(
        "--ssh-remote-host",
        help="DB host as seen from the SSH host (default: DSN host or 127.0.0.1)",
    )
    ssh.add_argument(
        "--ssh-remote-port",
        type=int,
        default=None,
        help="DB port as seen from the SSH host (default: DSN port)",
    )
    ssh.add_argument(
        "--ssh-local-port",
        type=int,
        default=None,
        help="Local bind port for the tunnel (default: ephemeral)",
    )
    return p


def open_nextcloud_backend(
    backend: str,
    *,
    dsn: Optional[str],
    url: Optional[str],
    user_id: str,
    password: Optional[str],
    table_prefix: str,
    ssh: Optional[dict[str, Any]],
    recognize_api_key: Optional[str],
    verify_ssl: bool,
    http_workers: int = 16,
) -> NextcloudBackend:
    if backend == "db":
        if not dsn:
            raise SystemExit("DB backend requires --nextcloud-dsn / nextcloud_dsn")
        return NextcloudDB(
            dsn, table_prefix=table_prefix, user_id=user_id, ssh=ssh
        )

    if backend == "http":
        if not url:
            raise SystemExit("HTTP backend requires --nextcloud-url / nextcloud_url")
        pw = password or os.environ.get("NEXTCLOUD_PASSWORD") or os.environ.get(
            "NC_PASSWORD"
        )
        if not pw:
            raise SystemExit(
                "HTTP backend requires --nc-password or env NEXTCLOUD_PASSWORD"
            )
        return NextcloudHTTP(
            base_url=url,
            user_id=user_id,
            password=pw,
            recognize_api_key=recognize_api_key
            or os.environ.get("RECOGNIZE_API_KEY"),
            verify_ssl=verify_ssl,
            http_workers=http_workers,
        )

    raise SystemExit(f"Unknown backend: {backend}")


def main(argv: Optional[list[str]] = None) -> int:
    args = build_arg_parser().parse_args(argv)
    # Always show progress at INFO; -v adds DEBUG (HTTP paths, PROPFIND bodies, …)
    setup_logging(verbose=args.verbose)

    if args.list_sessions:
        # Prefer CLI; else config state_dir if a config is provided
        state_dir = args.state_dir
        if state_dir is None and args.config:
            try:
                state_dir = load_config(args.config).get("state_dir")
            except FileNotFoundError:
                state_dir = None
        print_sessions(state_dir)
        return 0

    cfg = load_config(args.config)
    state_dir = args.state_dir or cfg.get("state_dir")

    backend = (args.backend or cfg.get("backend") or "http").lower()
    digikam_db = args.digikam_db or cfg.get("digikam_db")
    nc_dsn = args.nextcloud_dsn or cfg.get("nextcloud_dsn")
    nc_url = args.nextcloud_url or cfg.get("nextcloud_url")
    nc_user = args.nc_user or cfg.get("nc_user")
    nc_password = args.nc_password or cfg.get("nc_password")
    table_prefix = (
        args.table_prefix
        if args.table_prefix is not None
        else cfg.get("table_prefix", "oc_")
    )
    nc_path_prefix = (
        args.nc_path_prefix
        if args.nc_path_prefix is not None
        else cfg.get("nc_path_prefix", "files/")
    )
    storage_filter = args.storage_filter or cfg.get("storage_filter")
    iou = (
        args.iou_threshold
        if args.iou_threshold is not None
        else float(cfg.get("iou_threshold", 0.4))
    )
    path_maps = parse_path_maps(args.path_map, cfg.get("path_maps"))
    ssh = build_ssh_config(args, cfg)
    recognize_api_key = args.recognize_api_key or cfg.get("recognize_api_key")
    verify_ssl = not args.no_verify_ssl
    if "verify_ssl" in cfg:
        verify_ssl = bool(cfg["verify_ssl"]) and not args.no_verify_ssl

    skip = set(DEFAULT_SKIP_PERSONS)
    for name in cfg.get("skip_persons") or []:
        skip.add(str(name).lower())
    if args.skip_person:
        skip.update(s.lower() for s in args.skip_person)

    only_person = args.only_person or cfg.get("only_person")
    if only_person is not None:
        only_person = str(only_person).strip() or None

    insert_missing = not args.no_insert_missing
    if "insert_missing" in cfg:
        insert_missing = bool(cfg["insert_missing"]) and not args.no_insert_missing

    prefer_dk = not args.leave_conflicts
    if "prefer_digikam_on_conflict" in cfg:
        prefer_dk = bool(cfg["prefer_digikam_on_conflict"]) and not args.leave_conflicts

    if not digikam_db:
        print("error: --digikam-db or config digikam_db is required", file=sys.stderr)
        return 2
    if not nc_user:
        print("error: --nc-user or config nc_user is required", file=sys.stderr)
        return 2

    if args.resume and args.no_session:
        print("error: --resume and --no-session are mutually exclusive", file=sys.stderr)
        return 2
    if args.resume and args.session_id and args.resume != args.session_id:
        print(
            "error: --resume and --session-id disagree "
            f"({args.resume!r} vs {args.session_id!r})",
            file=sys.stderr,
        )
        return 2

    LOG.info("Backend    : %s", backend)
    LOG.info("digiKam DB : %s", digikam_db)
    if backend == "db":
        LOG.info("Nextcloud  : DSN …@%s (user=%s)", (nc_dsn or "").split("@")[-1], nc_user)
        if ssh:
            LOG.info(
                "SSH tunnel : %s@%s → %s:%s",
                ssh.get("user"),
                ssh.get("host"),
                ssh.get("remote_bind_host") or "(from DSN)",
                ssh.get("remote_bind_port") or "(from DSN)",
            )
    else:
        LOG.info("Nextcloud  : %s (user=%s)", nc_url, nc_user)
    batch_size = (
        args.batch_size
        if args.batch_size is not None
        else int(cfg.get("batch_size", 500))
    )
    limit_images = (
        args.limit_images
        if args.limit_images is not None
        else cfg.get("limit_images")
    )
    if limit_images is not None:
        limit_images = int(limit_images)
    http_workers = (
        args.http_workers
        if args.http_workers is not None
        else int(cfg.get("http_workers", 16))
    )
    max_actions = (
        args.max_report_actions
        if args.max_report_actions is not None
        else int(cfg.get("max_report_actions", 2000))
    )

    LOG.info("Path maps  : %s", path_maps or "(none)")
    LOG.info("Mode       : %s", "APPLY" if args.apply else "DRY-RUN")
    LOG.info(
        "Scale      : batch_size=%d limit_images=%s only_person=%s http_workers=%d",
        batch_size,
        limit_images if limit_images is not None else "none",
        only_person if only_person else "all",
        http_workers,
    )

    session: Optional[SessionState] = None
    if not args.no_session:
        session_id = args.resume or args.session_id
        params = SessionParams(
            digikam_db=str(digikam_db),
            backend=backend,
            apply=bool(args.apply),
            only_person=only_person,
            batch_size=batch_size,
            iou_threshold=iou,
            insert_missing=insert_missing,
            prefer_digikam_on_conflict=prefer_dk,
            limit_images=limit_images,
            nc_path_prefix=str(nc_path_prefix),
            path_maps=[[a, b] for a, b in path_maps],
        )
        try:
            session = SessionState.open_or_create(
                session_id=session_id,
                state_dir=state_dir,
                params=params,
                resume=bool(args.resume),
            )
        except FileNotFoundError as e:
            print(f"error: {e}", file=sys.stderr)
            return 2
        except ValueError as e:
            print(f"error: {e}", file=sys.stderr)
            return 2
        LOG.info(
            "Session    : %s (%s) — resume with --resume %s",
            session.session_id,
            session.path,
            session.session_id,
        )
        print(
            f"Session id: {session.session_id}\n"
            f"  state: {session.path}\n"
            f"  resume: python sync_faces.py --config … --resume {session.session_id}",
            flush=True,
        )
    else:
        LOG.info("Session    : disabled (--no-session)")

    with DigikamDB(digikam_db) as dk:
        nc = open_nextcloud_backend(
            backend,
            dsn=nc_dsn,
            url=nc_url,
            user_id=nc_user,
            password=nc_password,
            table_prefix=table_prefix,
            ssh=ssh,
            recognize_api_key=recognize_api_key,
            verify_ssl=verify_ssl,
            http_workers=http_workers,
        )
        try:
            report = sync(
                dk,
                nc,
                path_maps=path_maps,
                nc_path_prefix=nc_path_prefix,
                storage_filter=storage_filter,
                iou_threshold=iou,
                apply=args.apply,
                skip_persons=frozenset(skip),
                only_person=only_person,
                insert_missing=insert_missing,
                prefer_digikam_on_conflict=prefer_dk,
                batch_size=batch_size,
                limit_images=limit_images,
                max_actions=max_actions,
                http_workers=http_workers,
                session=session,
            )
        except SyncCancelled as e:
            LOG.warning("%s", e)
            if session is not None:
                print(
                    f"\nCancelled. Resume with:\n"
                    f"  python sync_faces.py --config {args.config or 'config.yaml'} "
                    f"--resume {session.session_id}"
                    + (" --apply" if args.apply else ""),
                    flush=True,
                )
            return 130
        finally:
            nc.close()

    print_human_report(report, apply=args.apply)

    if session is not None:
        print(
            f"\nSession {session.session_id}: status={session.status} "
            f"images_done={session.images_done}"
            f" → {session.path}"
        )

    if args.json_out:
        out = Path(args.json_out)
        out.write_text(
            json.dumps(report.to_dict(), indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
        print(f"\nJSON report written to {out}")

    return 1 if report.conflict_count else 0
