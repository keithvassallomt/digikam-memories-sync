"""WebDAV + Recognize DAV backend for Nextcloud (preferred access path)."""
from __future__ import annotations

import base64
import json
import logging
import re
import ssl
import threading
import urllib.parse
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path, PurePosixPath
from typing import Any, Callable, Iterable, Optional
from urllib.parse import quote, unquote

from .constants import DAV_NS, IMAGE_EXT
from .http_client import KeepAliveHttpClient
from .matching import digikam_path_candidates
from .models import (
    DigikamImage,
    FaceRegion,
    FileMatch,
    NextcloudFile,
    NextcloudNamedFace,
    NextcloudRequirements,
    Rect,
)
from .names import sanitize_person_name
from .paths import normalize_path, strip_nc_files_prefix

LOG = logging.getLogger(__name__)

FACE_SYNC_APP_INSTALL_URL = "https://keithvassallo.com"


class NextcloudConnectionError(RuntimeError):
    """The supplied Nextcloud connection cannot be used."""


class RecognizeNotInstalledError(NextcloudConnectionError):
    """The Nextcloud Recognize app is not installed or enabled."""

# Photos app embeds: <input type="hidden" id="initial-state-photos-recognizeApiKey" value="…">
_INITIAL_STATE_RE = re.compile(
    r'id=["\']initial-state-photos-recognizeApiKey["\'][^>]*value=["\']([^"\']+)["\']'
    r'|value=["\']([^"\']+)["\'][^>]*id=["\']initial-state-photos-recognizeApiKey["\']',
    re.IGNORECASE,
)

RECOGNIZE_403_HELP = """\
Recognize DAV returned HTTP 403 (API key required){person_part}.

Modern Recognize protects /remote.php/dav/recognize/… with header:
  X-Recognize-Api-Key: <server-generated key>

Fix (pick one):

  A) Auto-key from Photos (this tool retries automatically if Photos is installed).
     Ensure the Photos app is enabled and your user can open /apps/photos/.

  B) Disable the gate on the server (admin):
     occ config:app:set recognize require_api_key --value false

  C) Provide a key in config (from a logged-in browser Photos page source,
     input#initial-state-photos-recognizeApiKey — keys expire after 24h):
     recognize_api_key: "…"
     # or env RECOGNIZE_API_KEY / --recognize-api-key

Note: listing file face-detections under /dav/files/ works without this key;
only people create/rename/assign (MKCOL/MOVE under /recognize) needs it.
"""


def _recognize_api_key_help(person: str | None = None) -> str:
    person_part = f" for person {person!r}" if person else ""
    return RECOGNIZE_403_HELP.format(person_part=person_part)


def _body_suggests_api_key(body: str) -> bool:
    b = body.lower()
    return (
        "x-recognize-api-key" in b
        or "recognize-api-key" in b
        or "valid x-recognize" in b
        or "you must provide a valid" in b
    )


def _body_is_recognize_duplicate_names(body: str) -> bool:
    """
    Recognize (Sabre) non-RFC quirk: createDirectory throws Forbidden (403)
    with message 'Not allowed to create duplicate names' when the person
    folder already exists. RFC 4918 would use 405 for that case.
    """
    b = body.lower()
    return "duplicate name" in b or "create duplicate" in b


def _recognize_person_title(title: str, cluster_id: object) -> str:
    """Return an actual person title, excluding DAV's numeric cluster fallback."""
    if cluster_id is not None:
        try:
            if title == str(int(cluster_id)):
                return ""
        except (TypeError, ValueError):
            pass
    return title

_PROPFIND_FILE_AND_FACES = """<?xml version="1.0"?>
<d:propfind xmlns:d="DAV:" xmlns:oc="http://owncloud.org/ns" xmlns:nc="http://nextcloud.org/ns">
  <d:prop>
    <d:resourcetype/>
    <d:getcontentlength/>
    <d:getcontenttype/>
    <oc:fileid/>
    <nc:face-detections/>
  </d:prop>
</d:propfind>
""".encode()

_PROPFIND_FILE_ONLY = """<?xml version="1.0"?>
<d:propfind xmlns:d="DAV:" xmlns:oc="http://owncloud.org/ns">
  <d:prop>
    <d:resourcetype/>
    <d:getcontentlength/>
    <d:getcontenttype/>
    <oc:fileid/>
  </d:prop>
</d:propfind>
""".encode()


class NextcloudHTTP:
    """
    Preferred Nextcloud backend: WebDAV + Recognize DAV.

    Capabilities:
      - Targeted PROPFIND per digiKam path (with face-detections)
      - Concurrent keep-alive HTTP (thread pool + connection reuse)
      - MKCOL people / MOVE detections for assignment

    Limitations:
      - Brand-new face boxes require the digiKam Face Sync Nextcloud app
      - Unclustered detections (cluster_id NULL) are not MOVE-able
      - Some installs require X-Recognize-Api-Key for /recognize DAV
    """

    supports_insert = False
    supports_export = False
    generates_face_vectors = False

    def __init__(
        self,
        base_url: str,
        user_id: str,
        password: str,
        *,
        recognize_api_key: Optional[str] = None,
        verify_ssl: bool = True,
        timeout: float = 60.0,
        http_workers: int = 16,
    ):
        self.user_id = user_id
        self.password = password
        self.recognize_api_key = (recognize_api_key or "").strip()
        self.timeout = timeout
        self.base_url = base_url.rstrip("/")
        self.http_workers = max(1, int(http_workers))

        if verify_ssl:
            ssl_ctx: Optional[ssl.SSLContext] = ssl.create_default_context()
        else:
            ssl_ctx = ssl._create_unverified_context()  # noqa: SLF001

        token = base64.b64encode(f"{user_id}:{password}".encode()).decode()
        self._auth_header = f"Basic {token}"
        self._client = KeepAliveHttpClient(
            self.base_url,
            default_headers={
                "Authorization": self._auth_header,
                "User-Agent": "digikam-nextcloud-sync_faces/1.1",
            },
            timeout=timeout,
            ssl_context=ssl_ctx,
        )
        self._cluster_cache: dict[str, int] = {}
        self._synthetic_cluster_id = 10_000_000
        self._created_people: set[str] = set()
        self._cluster_lock = threading.Lock()
        # Learned path-candidate index that succeeds most often (0 = first)
        self._preferred_candidate_index = 0
        self._candidate_success: dict[int, int] = {}
        self._recognize_ready: Optional[bool] = None
        self._recognize_error: Optional[str] = None

        self.recognize_installed = self._probe_recognize_installation()
        if not self.recognize_installed:
            raise RecognizeNotInstalledError(
                "This Nextcloud instance does not have the Recognize app "
                "installed and enabled. Face Sync requires Recognize."
            )

        # Prefer a server-issued key (Photos initial-state) when not configured
        if not self.recognize_api_key:
            try:
                self.recognize_api_key = self.fetch_recognize_api_key_from_photos() or ""
            except Exception as e:
                LOG.debug("Could not auto-fetch Recognize API key: %s", e)

        capabilities = self._probe_face_sync_app()
        self.supports_insert = capabilities["create"]
        self.supports_export = capabilities["list"]
        self.generates_face_vectors = self.supports_insert

    def _probe_recognize_installation(self) -> bool:
        """Confirm that Recognize has registered its DAV collection."""
        status, _, _ = self._request(
            "PROPFIND",
            f"{self.recognize_dav_base()}/faces",
            headers={
                "Depth": "0",
                "Content-Type": "application/xml; charset=utf-8",
            },
            body=b"""<?xml version="1.0"?>
<d:propfind xmlns:d="DAV:"><d:prop><d:resourcetype/></d:prop></d:propfind>""",
        )
        if status == 404:
            return False
        if status == 401:
            raise NextcloudConnectionError(
                "Nextcloud rejected the username or app password."
            )
        if status not in (200, 207, 403):
            raise NextcloudConnectionError(
                f"Could not check the Recognize app (HTTP {status})."
            )
        return True

    def connection_requirements(self) -> NextcloudRequirements:
        """Return the install state needed by the first-run connection UI."""
        return NextcloudRequirements(
            recognize_installed=self.recognize_installed,
            face_sync_installed=self.supports_insert and self.supports_export,
            face_sync_install_url=FACE_SYNC_APP_INSTALL_URL,
        )

    def face_import_url(self) -> str:
        return "index.php/apps/digikam_face_sync/api/v1/face-import"

    def _probe_face_sync_app(self) -> dict[str, bool]:
        """Detect the authenticated companion app capabilities."""
        try:
            status, _, raw = self._request(
                "GET",
                self.face_import_url(),
                headers={
                    "Accept": "application/json",
                    "OCS-APIRequest": "true",
                },
            )
            if status != 200:
                LOG.debug("Recognize face-import API unavailable (HTTP %s)", status)
                return {"create": False, "list": False}
            payload = json.loads(raw.decode("utf-8"))
            capabilities = {
                "create": payload.get("createFaceDetection") is True,
                "list": payload.get("listFaceDetections") is True,
            }
            if capabilities["create"]:
                LOG.info("Recognize face-import API available")
            if capabilities["list"]:
                LOG.info("Recognize face-list API available")
            return capabilities
        except Exception as e:
            LOG.debug("Could not probe Face Sync companion API: %s", e)
            return {"create": False, "list": False}

    def face_list_url(self) -> str:
        return "index.php/apps/digikam_face_sync/api/v1/faces"

    def people_list_url(self) -> str:
        return "index.php/apps/digikam_face_sync/api/v1/people"

    def list_named_people(self) -> list[str]:
        if not self.supports_export:
            raise NextcloudConnectionError(
                "The Nextcloud Face Sync companion app must be updated before "
                "Memories faces can be read."
            )
        status, _, raw = self._request(
            "GET",
            self.people_list_url(),
            headers={"Accept": "application/json", "OCS-APIRequest": "true"},
        )
        if status != 200:
            raise NextcloudConnectionError(
                f"Could not list Memories people (HTTP {status})."
            )
        payload = json.loads(raw.decode("utf-8"))
        people = payload.get("people")
        if not isinstance(people, list):
            raise NextcloudConnectionError("The Memories people response was invalid.")
        return [
            sanitize_person_name(str(person))
            for person in people
            if str(person).strip()
        ]

    def list_named_faces(
        self,
        person: Optional[str] = None,
        *,
        page_size: int = 1000,
        progress_callback: Optional[Callable[[int], None]] = None,
    ) -> list[NextcloudNamedFace]:
        """Read every named Recognize detection through the companion app."""
        if not self.supports_export:
            raise NextcloudConnectionError(
                "The Nextcloud Face Sync companion app must be updated before "
                "Memories faces can be read."
            )
        cleaned = sanitize_person_name(person or "").strip()
        after = 0
        out: list[NextcloudNamedFace] = []
        while True:
            query: dict[str, str | int] = {
                "after": after,
                "limit": max(1, min(1000, int(page_size))),
            }
            if cleaned:
                query["person"] = cleaned
            path = f"{self.face_list_url()}?{urllib.parse.urlencode(query)}"
            status, _, raw = self._request(
                "GET",
                path,
                headers={"Accept": "application/json", "OCS-APIRequest": "true"},
            )
            if status != 200:
                raise NextcloudConnectionError(
                    f"Could not list Memories faces (HTTP {status})."
                )
            payload = json.loads(raw.decode("utf-8"))
            records = payload.get("detections")
            if not isinstance(records, list):
                raise NextcloudConnectionError("The Memories face response was invalid.")
            for item in records:
                if not isinstance(item, dict):
                    continue
                try:
                    nc_file = NextcloudFile(
                        file_id=int(item["fileId"]),
                        path=normalize_path(str(item["path"])),
                        name=str(item["name"]),
                        size=0,
                        webdav_path=normalize_path(str(item["path"])),
                    )
                    face = FaceRegion(
                        person=sanitize_person_name(str(item["person"])),
                        rect=Rect(
                            float(item["x"]),
                            float(item["y"]),
                            float(item["width"]),
                            float(item["height"]),
                        ).clamp(),
                        source="nextcloud",
                        nc_file_id=nc_file.file_id,
                        nc_detection_id=int(item["id"]),
                        nc_cluster_id=int(item["clusterId"]),
                        file_name=nc_file.name,
                        threshold=float(item.get("threshold") or 0.0),
                    )
                except (KeyError, TypeError, ValueError) as error:
                    raise NextcloudConnectionError(
                        "The Memories face response contained an invalid detection."
                    ) from error
                if face.person and face.rect.area() > 0:
                    out.append(NextcloudNamedFace(nc_file, face))
            if progress_callback is not None:
                progress_callback(len(out))
            next_after = payload.get("nextAfter")
            if next_after is None:
                break
            next_value = int(next_after)
            if next_value <= after:
                raise NextcloudConnectionError(
                    "The Memories face response repeated its pagination cursor."
                )
            after = next_value
        return out

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> "NextcloudHTTP":
        return self

    def __exit__(self, *args: Any) -> None:
        self.close()

    def commit(self) -> None:
        return

    def rollback(self) -> None:
        return

    def _url(self, path: str) -> str:
        if path.startswith("http://") or path.startswith("https://"):
            return path
        return f"{self.base_url}/{path.lstrip('/')}"

    def fetch_recognize_api_key_from_photos(self) -> Optional[str]:
        """
        Load a short-lived Recognize API key from the Photos app page.

        Nextcloud Photos injects initial-state ``recognizeApiKey`` generated via
        OCA\\Recognize\\Public\\ApiKeyManager (valid ~24h). Requires Photos app
        enabled for this user.
        """
        LOG.info("Fetching Recognize API key from Photos initial-state…")
        # Prefer the people/faces entry if available; /apps/photos/ is enough
        for path in (
            "/apps/photos/",
            "/index.php/apps/photos/",
            "/apps/photos/faces",
            "/index.php/apps/photos/faces",
        ):
            status, _, raw = self._request("GET", path, recognize=False)
            if status not in (200, 302, 303):
                LOG.debug("Photos page %s → %s", path, status)
                continue
            html = raw.decode("utf-8", errors="replace")
            key = self._parse_recognize_api_key_from_html(html)
            if key:
                LOG.info("Obtained Recognize API key from Photos (%s)", path)
                return key
        LOG.warning(
            "Could not find photos recognizeApiKey initial-state. "
            "Is the Photos app installed and enabled?"
        )
        return None

    @staticmethod
    def _parse_recognize_api_key_from_html(html: str) -> Optional[str]:
        m = _INITIAL_STATE_RE.search(html)
        if not m:
            # broader fallback: scan all initial-state inputs
            for m2 in re.finditer(
                r'id=["\']initial-state-photos-recognizeApiKey["\'][^>]*>',
                html,
                re.I,
            ):
                # look back/forward for value=
                window = html[max(0, m2.start() - 200) : m2.end() + 200]
                vm = re.search(r'value=["\']([^"\']+)["\']', window)
                if vm:
                    m = vm
                    break
            else:
                # value= before id=
                m = re.search(
                    r'value=["\']([^"\']+)["\'][^>]*id=["\']initial-state-photos-recognizeApiKey["\']',
                    html,
                    re.I,
                )
        if not m:
            return None
        b64 = m.group(1) if m.lastindex and m.group(1) else (m.group(2) if m.lastindex and m.lastindex >= 2 else m.group(1))
        if not b64:
            # last pattern has group 1 only
            b64 = m.group(1)
        try:
            # Photos uses base64(JSON.stringify(keyString))
            decoded = base64.b64decode(b64)
            value = json.loads(decoded.decode("utf-8"))
            if isinstance(value, str) and value:
                return value
        except Exception as e:
            LOG.debug("Failed to decode recognizeApiKey initial-state: %s", e)
        return None

    def ensure_recognize_access(self) -> None:
        """
        Probe Recognize DAV once. On 403, try refreshing API key, then fail
        with actionable help instead of spamming MKCOL for every person.
        """
        if self._recognize_ready is True:
            return
        if self._recognize_ready is False:
            raise RuntimeError(self._recognize_error or _recognize_api_key_help())

        def probe() -> int:
            status, _, raw = self._request(
                "PROPFIND",
                f"{self.recognize_dav_base()}/faces",
                headers={
                    "Depth": "0",
                    "Content-Type": "application/xml; charset=utf-8",
                },
                body=b"""<?xml version="1.0"?>
<d:propfind xmlns:d="DAV:"><d:prop><d:resourcetype/></d:prop></d:propfind>""",
                recognize=True,
            )
            LOG.debug(
                "Recognize DAV probe → %s (%s)",
                status,
                raw[:120].decode(errors="replace"),
            )
            return status

        status = probe()
        if status == 403:
            LOG.info(
                "Recognize DAV returned 403; retrying after refreshing API key…"
            )
            key = self.fetch_recognize_api_key_from_photos()
            if key:
                self.recognize_api_key = key
                status = probe()

        if status == 403:
            self._recognize_ready = False
            self._recognize_error = _recognize_api_key_help()
            raise RuntimeError(self._recognize_error)

        if status == 404:
            self._recognize_ready = False
            self._recognize_error = (
                "This Nextcloud instance does not have the Recognize app "
                "installed and enabled. Face Sync requires Recognize."
            )
            raise RecognizeNotInstalledError(self._recognize_error)

        if status not in (207, 200, 405):
            LOG.warning(
                "Unexpected Recognize DAV probe status %s — continuing anyway",
                status,
            )

        self._recognize_ready = True
        LOG.info(
            "Recognize DAV access OK%s",
            " (with API key)" if self.recognize_api_key else " (no API key required)",
        )
        # Touch so callers always see a clear transition into listing
        LOG.info("Listing existing Recognize person folders (Depth:1)…")

    def _request(
        self,
        method: str,
        path: str,
        *,
        headers: Optional[dict[str, str]] = None,
        body: Optional[bytes] = None,
        recognize: bool = False,
        timeout: Optional[float] = None,
    ) -> tuple[int, dict[str, str], bytes]:
        # KeepAliveHttpClient wants a path starting with /
        req_path = path if path.startswith("/") or path.startswith("http") else f"/{path}"
        hdrs: dict[str, str] = {}
        if recognize and self.recognize_api_key:
            hdrs["X-Recognize-Api-Key"] = self.recognize_api_key
        if headers:
            hdrs.update(headers)
        LOG.debug("HTTP %s %s", method, req_path)
        status, resp_headers, raw = self._client.request(
            method, req_path, headers=hdrs or None, body=body, timeout=timeout
        )
        LOG.debug("HTTP %s %s → %s (%s bytes)", method, req_path, status, len(raw))
        return status, resp_headers, raw

    @staticmethod
    def _encode_dav_path(path: str) -> str:
        """Encode each path segment for use in a URL path."""
        parts = [quote(p, safe="") for p in normalize_path(path).split("/") if p != ""]
        return "/".join(parts)

    def files_dav_base(self) -> str:
        return f"remote.php/dav/files/{quote(self.user_id, safe='')}"

    def recognize_dav_base(self) -> str:
        return f"remote.php/dav/recognize/{quote(self.user_id, safe='')}"

    def list_image_files(
        self,
        path_prefix: str = "files/",
        storage_filter: Optional[str] = None,
    ) -> list[NextcloudFile]:
        """Recursive WebDAV listing (expensive on large trees). Prefer resolve_digikam_images."""
        del storage_filter
        user_rel = strip_nc_files_prefix(normalize_path(path_prefix))
        LOG.info(
            "Listing Nextcloud images via WebDAV under %r "
            "(Depth:1 walk — large trees take a while)…",
            user_rel or "/",
        )
        # Depth:infinity often returns huge XML and pegs CPU while appearing hung.
        # Always walk Depth:1 with progress instead.
        return self._list_images_depth1(user_rel)

    def resolve_digikam_images(
        self,
        digikam_images: list[DigikamImage],
        path_maps: list[tuple[str, str]],
    ) -> tuple[
        list[FileMatch],
        list[DigikamImage],
        dict[int, list[FaceRegion]],
    ]:
        """
        Resolve digiKam images by concurrent keep-alive PROPFIND.

        Preferred production path for large libraries: only digiKam-faced
        images are probed (no full Nextcloud tree listing).
        """
        total = len(digikam_images)
        workers = max(1, int(self.http_workers or 16))
        LOG.info(
            "Resolving %d digiKam images via WebDAV "
            "(%d keep-alive workers, targeted paths only)…",
            total,
            workers,
        )

        prefer_idx = self._preferred_candidate_index

        def resolve_one(
            di: DigikamImage,
        ) -> tuple[DigikamImage, Optional[NextcloudFile], list[FaceRegion], int, bool]:
            """Returns (di, found, faces, win_idx, skipped)."""
            candidates = digikam_path_candidates(di, path_maps)
            if not candidates:
                # No path_map match (or no candidates) → skip, do not treat as unmatched
                return di, None, [], -1, bool(path_maps)
            # Try learned-best candidate index first
            order = list(range(len(candidates)))
            if 0 <= prefer_idx < len(candidates):
                order = [prefer_idx] + [i for i in order if i != prefer_idx]
            for idx in order:
                cand = candidates[idx]
                nf, faces = self._propfind_file_with_faces(cand)
                if nf is not None:
                    return di, nf, faces, idx, False
            return di, None, [], -1, False

        matches: list[FileMatch] = []
        unmatched: list[DigikamImage] = []
        faces_by_file: dict[int, list[FaceRegion]] = {}
        done = 0
        hits = 0
        skipped = 0
        progress_every = max(1, min(50, total // 20 or 1))

        with ThreadPoolExecutor(max_workers=workers) as pool:
            futures = [pool.submit(resolve_one, di) for di in digikam_images]
            for fut in as_completed(futures):
                di, found, faces, win_idx, was_skipped = fut.result()
                done += 1
                if done == 1 or done % progress_every == 0 or done == total:
                    LOG.info(
                        "  path resolve: %d / %d (matched=%d skipped=%d)",
                        done,
                        total,
                        hits + (1 if found else 0),
                        skipped + (1 if was_skipped else 0),
                    )
                if was_skipped:
                    skipped += 1
                    continue
                if found is None:
                    unmatched.append(di)
                    continue
                hits += 1
                if win_idx >= 0:
                    self._candidate_success[win_idx] = (
                        self._candidate_success.get(win_idx, 0) + 1
                    )
                matches.append(
                    FileMatch(digikam=di, nextcloud=found, method="path")
                )
                for fr in faces:
                    fr.file_name = found.name
                faces_by_file[found.file_id] = faces

        # Update preferred candidate for next batch
        if self._candidate_success:
            self._preferred_candidate_index = max(
                self._candidate_success, key=self._candidate_success.get
            )
            LOG.debug(
                "Path candidate success counts=%s → prefer index %d",
                self._candidate_success,
                self._preferred_candidate_index,
            )

        LOG.info(
            "Path resolve complete: %d matched, %d unmatched, %d skipped "
            "(no path_map), %d face detections",
            len(matches),
            len(unmatched),
            skipped,
            sum(len(v) for v in faces_by_file.values()),
        )
        return matches, unmatched, faces_by_file

    def ensure_people(self, names: Iterable[str]) -> None:
        """Create missing person clusters (MKCOL) concurrently."""
        # One probe first — avoids 5000× failures if API key is missing
        self.ensure_recognize_access()
        known = self.list_face_clusters()
        missing = []
        seen: set[str] = set()
        for name in names:
            n = sanitize_person_name(name or "")
            if not n:
                continue
            key = n.lower()
            if key in seen:
                continue
            seen.add(key)
            if key in known:
                continue
            missing.append(n)
        if not missing:
            LOG.info(
                "All %d digiKam people already present in Recognize listing "
                "(or empty folders will be created on assign)",
                len(seen),
            )
            return
        LOG.info(
            "Ensuring %d person folders via Recognize DAV "
            "(%d already listed with faces)…",
            len(missing),
            len(known),
        )
        workers = max(1, min(self.http_workers, 16))
        errors: list[str] = []
        done = 0
        total = len(missing)
        progress_every = max(1, min(50, total // 20 or 1))
        lock = threading.Lock()

        def mk(name: str) -> None:
            nonlocal done
            try:
                self.get_or_create_cluster(name)
            except Exception as e:
                with lock:
                    errors.append(f"{name!r}: {e}")
            finally:
                with lock:
                    done += 1
                    if done == 1 or done % progress_every == 0 or done == total:
                        LOG.info(
                            "  ensure people: %d / %d (errors=%d)",
                            done,
                            total,
                            len(errors),
                        )

        with ThreadPoolExecutor(max_workers=workers) as pool:
            list(pool.map(mk, missing))

        if errors:
            # If any look like API-key failures, surface that clearly
            api_key_errs = [e for e in errors if "X-Recognize-Api-Key" in e]
            sample = errors[:5]
            msg = (
                f"Failed to ensure {len(errors)} / {total} person folders.\n"
                + "\n".join(f"  - {s}" for s in sample)
            )
            if len(errors) > 5:
                msg += f"\n  … and {len(errors) - 5} more"
            if api_key_errs:
                msg += "\n\n" + _recognize_api_key_help()
            raise RuntimeError(msg)
        LOG.info("Person folders ready (%d ensured this run)", total)

    def _propfind_file_with_faces(
        self, user_rel: str
    ) -> tuple[Optional[NextcloudFile], list[FaceRegion]]:
        """Depth:0 PROPFIND for a single user-relative path + face-detections."""
        user_rel = normalize_path(user_rel)
        if not user_rel:
            return None, []
        dav_path = f"{self.files_dav_base()}/{self._encode_dav_path(user_rel)}"
        status, _, raw = self._request(
            "PROPFIND",
            dav_path,
            headers={
                "Depth": "0",
                "Content-Type": "application/xml; charset=utf-8",
            },
            body=_PROPFIND_FILE_AND_FACES,
        )
        if status == 404:
            return None, []
        if status not in (207, 200):
            LOG.debug("PROPFIND %s → %s", user_rel, status)
            return None, []

        files = self._parse_file_propfind(raw, user_rel)
        # Depth 0 may return the collection itself if path is a folder
        file_hits = [f for f in files if not f.webdav_path.endswith("/")]
        if not file_hits:
            # try any non-collection parse result
            file_hits = files
        if not file_hits:
            return None, []
        nf = file_hits[0]
        # Ensure webdav_path is the path we asked for if parser stripped oddly
        if not nf.webdav_path:
            nf.webdav_path = user_rel
        faces = self._parse_face_detections(raw, nf)
        return nf, faces

    def _list_images_depth1(self, user_rel: str) -> list[NextcloudFile]:
        """BFS Depth:1 listing with progress logs."""
        out: list[NextcloudFile] = []
        queue = [user_rel]
        seen_dirs: set[str] = set()
        dirs_done = 0

        while queue:
            rel = queue.pop(0)
            if rel in seen_dirs:
                continue
            seen_dirs.add(rel)
            dirs_done += 1
            if dirs_done == 1 or dirs_done % 25 == 0:
                LOG.info(
                    "  WebDAV walk: %d dirs, %d images so far (queue=%d)…",
                    dirs_done,
                    len(out),
                    len(queue),
                )
            dav_path = self.files_dav_base()
            if rel:
                dav_path = f"{dav_path}/{self._encode_dav_path(rel)}"
            status, _, raw = self._request(
                "PROPFIND",
                dav_path,
                headers={
                    "Depth": "1",
                    "Content-Type": "application/xml; charset=utf-8",
                },
                body=_PROPFIND_FILE_ONLY,
            )
            if status not in (207, 200):
                LOG.warning("PROPFIND Depth 1 failed for %s (%s)", rel or "/", status)
                continue
            LOG.debug(
                "  parsed PROPFIND for %r (%d bytes)",
                rel or "/",
                len(raw),
            )
            files, subdirs = self._parse_file_propfind_split(raw, rel)
            out.extend(files)
            queue.extend(subdirs)

        LOG.info("WebDAV listing complete: %d images in %d directories", len(out), dirs_done)
        return out

    def _href_to_user_rel(self, href: str) -> str:
        """Map a DAV href to a path relative to files/{user}/."""
        path = urllib.parse.urlparse(href).path
        path = unquote(path)
        markers = [
            f"/remote.php/dav/files/{self.user_id}/",
            f"/remote.php/webdav/",
        ]
        for m in markers:
            idx = path.find(m)
            if idx >= 0:
                return normalize_path(path[idx + len(m) :])
        # fallback: strip known prefix loosely
        parts = path.split(f"/files/{self.user_id}/", 1)
        if len(parts) == 2:
            return normalize_path(parts[1])
        return normalize_path(path.lstrip("/"))

    def _parse_file_propfind(self, raw: bytes, root_rel: str) -> list[NextcloudFile]:
        files, _ = self._parse_file_propfind_split(raw, root_rel)
        return files

    def _parse_file_propfind_split(
        self, raw: bytes, root_rel: str
    ) -> tuple[list[NextcloudFile], list[str]]:
        del root_rel
        try:
            root = ET.fromstring(raw)
        except ET.ParseError as e:
            raise RuntimeError(f"Invalid PROPFIND multistatus XML: {e}") from e

        files: list[NextcloudFile] = []
        subdirs: list[str] = []
        for resp in root.findall("d:response", DAV_NS):
            href_el = resp.find("d:href", DAV_NS)
            if href_el is None or not href_el.text:
                continue
            href = href_el.text
            prop = None
            for ok in resp.findall("d:propstat", DAV_NS):
                st = ok.find("d:status", DAV_NS)
                if st is not None and st.text and "200" in st.text:
                    prop = ok.find("d:prop", DAV_NS)
                    break
            if prop is None:
                continue

            rtype = prop.find("d:resourcetype", DAV_NS)
            is_collection = (
                rtype is not None and rtype.find("d:collection", DAV_NS) is not None
            )
            user_rel = self._href_to_user_rel(href)
            if is_collection:
                if user_rel:
                    subdirs.append(user_rel.rstrip("/"))
                continue

            name = PurePosixPath(user_rel).name
            ext = Path(name).suffix.lower()
            if ext and ext not in IMAGE_EXT:
                continue

            fileid_el = prop.find("oc:fileid", DAV_NS)
            if fileid_el is None or not (fileid_el.text or "").strip():
                continue
            length_el = prop.find("d:getcontentlength", DAV_NS)
            size = int(length_el.text) if length_el is not None and length_el.text else 0
            ctype_el = prop.find("d:getcontenttype", DAV_NS)
            mimetype = ctype_el.text if ctype_el is not None and ctype_el.text else ""

            # Mirror filecache-style path with files/ prefix for matching
            fc_path = normalize_path(f"files/{user_rel}")
            files.append(
                NextcloudFile(
                    file_id=int(fileid_el.text),
                    path=fc_path,
                    name=name,
                    size=size,
                    mimetype=mimetype,
                    webdav_path=user_rel,
                )
            )
        return files, subdirs

    def list_face_clusters(self) -> dict[str, int]:
        if self._cluster_cache:
            return dict(self._cluster_cache)

        # Fail fast with a clear message instead of 403 spam later
        self.ensure_recognize_access()

        dav_path = f"{self.recognize_dav_base()}/faces"
        body = """<?xml version="1.0"?>
<d:propfind xmlns:d="DAV:">
  <d:prop><d:resourcetype/><d:displayname/></d:prop>
</d:propfind>
""".encode()
        status, _, raw = self._request(
            "PROPFIND",
            dav_path,
            headers={
                "Depth": "1",
                "Content-Type": "application/xml; charset=utf-8",
            },
            body=body,
            recognize=True,
        )
        out: dict[str, int] = {}
        if status == 403 and _body_suggests_api_key(raw.decode(errors="replace")):
            self._recognize_ready = False
            self._recognize_error = _recognize_api_key_help()
            raise RuntimeError(self._recognize_error)
        if status not in (207, 200):
            LOG.warning(
                "Could not list Recognize face clusters via DAV (%s). "
                "People will be created on demand. Note: empty person folders "
                "are not listed by Recognize even when they exist.",
                status,
            )
            # Keep any names we already learned locally
            self._cluster_cache = dict(self._cluster_cache)
            return dict(self._cluster_cache)

        try:
            root = ET.fromstring(raw)
        except ET.ParseError:
            return dict(self._cluster_cache)

        for resp in root.findall("d:response", DAV_NS):
            href_el = resp.find("d:href", DAV_NS)
            if href_el is None or not href_el.text:
                continue
            path = unquote(urllib.parse.urlparse(href_el.text).path).rstrip("/")
            name = PurePosixPath(path).name
            if name in ("", "faces") or name.isdigit():
                continue
            key = name.lower()
            if key not in out and key not in self._cluster_cache:
                self._synthetic_cluster_id += 1
                out[key] = self._synthetic_cluster_id
            elif key in self._cluster_cache:
                out[key] = self._cluster_cache[key]
        # Merge with previously known (incl. empty clusters discovered via MKCOL)
        merged = dict(self._cluster_cache)
        merged.update(out)
        self._cluster_cache = merged
        LOG.info(
            "Listed %d named face folders from Recognize DAV "
            "(%d known including empty)",
            len(out),
            len(merged),
        )
        return dict(merged)

    def _remember_cluster(self, title: str, *, existed: bool) -> int:
        """Record a person cluster in the local cache and return its synthetic id."""
        key = title.strip().lower()
        with self._cluster_lock:
            if key in self._cluster_cache:
                return self._cluster_cache[key]
            self._synthetic_cluster_id += 1
            cid = self._synthetic_cluster_id
            self._cluster_cache[key] = cid
            self._created_people.add(title.strip())
            LOG.debug(
                "Person %r %s (local id=%s)",
                title,
                "already exists" if existed else "created",
                cid,
            )
            return cid

    def get_or_create_cluster(self, title: str) -> int:
        """
        Ensure a person collection exists under recognize/{user}/faces/{title}.

        MKCOL status handling (RFC 4918):
          201 Created — collection created
          405 Method Not Allowed — resource already exists at that URI (OK)
          409 Conflict — parent collection missing (error)
          403 Forbidden — permission/policy (or Recognize API key / rare quirks)
          207 Multi-Status — extended MKCOL (treat as success if no hard fail)

        Recognize quirk: sometimes returns 403 "Not allowed to create duplicate
        names" when the folder already exists (should be 405 per RFC).

        Person titles are sanitized (``/`` → ``-``) so digiKam tags cannot
        inject path separators into the DAV URL.
        """
        title = sanitize_person_name(title)
        if not title:
            raise ValueError("cluster title must be non-empty after sanitization")
        self.ensure_recognize_access()
        with self._cluster_lock:
            if title.lower() in self._cluster_cache:
                return self._cluster_cache[title.lower()]

        # Depth:1 listing only includes people who already have detections
        clusters = self.list_face_clusters()
        if title.lower() in clusters:
            return clusters[title.lower()]

        dav_path = f"{self.recognize_dav_base()}/faces/{self._encode_dav_path(title)}"
        status, _, raw = self._request(
            "MKCOL",
            dav_path,
            recognize=True,
        )
        body = raw.decode(errors="replace")
        body_one_line = body[:160].replace("\n", " ")

        # --- RFC 4918 success / already-exists ---
        if status == 201:
            LOG.debug("Created person folder %r (MKCOL 201 Created)", title)
            return self._remember_cluster(title, existed=False)

        if status == 207:
            # Extended MKCOL multi-status — collection generally created
            LOG.debug("Created person folder %r (MKCOL 207 Multi-Status)", title)
            return self._remember_cluster(title, existed=False)

        if status == 405:
            # RFC: Method Not Allowed when the collection already exists
            LOG.debug(
                "Person folder %r already exists (MKCOL 405 Method Not Allowed)",
                title,
            )
            return self._remember_cluster(title, existed=True)

        # --- RFC 4918 errors ---
        if status == 409:
            raise RuntimeError(
                f"MKCOL 409 Conflict for person {title!r}: parent collection "
                f"missing or intermediate path cannot be created. Body: {body_one_line}"
            )

        if status == 415:
            raise RuntimeError(
                f"MKCOL 415 Unsupported Media Type for person {title!r}: "
                f"{body_one_line}"
            )

        if status == 403:
            # API key gate (Recognize)
            if _body_suggests_api_key(body):
                self._recognize_ready = False
                self._recognize_error = _recognize_api_key_help(title)
                raise RuntimeError(self._recognize_error)
            # Recognize non-RFC: Forbidden on duplicate person folder
            if _body_is_recognize_duplicate_names(body):
                LOG.debug(
                    "Person folder %r already exists "
                    "(MKCOL 403 Recognize duplicate-names quirk)",
                    title,
                )
                return self._remember_cluster(title, existed=True)
            # Real permission / policy denial
            raise RuntimeError(
                f"MKCOL 403 Forbidden for person {title!r}: server policy or "
                f"permissions denied creating the collection. Body: {body_one_line}"
            )

        # Unexpected status — do not guess; report clearly with person name
        raise RuntimeError(
            f"MKCOL unexpected status {status} for person {title!r}: {body_one_line}"
        )

    def list_detections_for_files(
        self,
        file_ids: Iterable[int],
        files_by_id: Optional[dict[int, NextcloudFile]] = None,
    ) -> dict[int, list[FaceRegion]]:
        ids = list(file_ids)
        files_by_id = files_by_id or {}
        by_file: dict[int, list[FaceRegion]] = {i: [] for i in ids}
        total = len(ids)
        LOG.info("Fetching face-detections for %d files via WebDAV…", total)
        progress_every = max(1, min(50, total // 20 or 1))

        body = """<?xml version="1.0"?>
<d:propfind xmlns:d="DAV:" xmlns:nc="http://nextcloud.org/ns">
  <d:prop>
    <nc:face-detections/>
  </d:prop>
</d:propfind>
""".encode()

        for n, fid in enumerate(ids, start=1):
            if n == 1 or n % progress_every == 0 or n == total:
                LOG.info("  face-detections progress: %d / %d", n, total)
            nf = files_by_id.get(fid)
            if not nf or not nf.webdav_path:
                LOG.debug("No webdav path for file_id=%s; skip face fetch", fid)
                continue
            dav_path = f"{self.files_dav_base()}/{self._encode_dav_path(nf.webdav_path)}"
            status, _, raw = self._request(
                "PROPFIND",
                dav_path,
                headers={
                    "Depth": "0",
                    "Content-Type": "application/xml; charset=utf-8",
                },
                body=body,
            )
            if status not in (207, 200):
                LOG.warning(
                    "face-detections PROPFIND failed for %s (%s)",
                    nf.webdav_path,
                    status,
                )
                continue
            by_file[fid] = self._parse_face_detections(raw, nf)
        LOG.info(
            "Fetched %d face detections across %d files",
            sum(len(v) for v in by_file.values()),
            total,
        )
        return by_file

    def _parse_face_detections(
        self, raw: bytes, nc_file: NextcloudFile
    ) -> list[FaceRegion]:
        try:
            root = ET.fromstring(raw)
        except ET.ParseError:
            return []

        payload = None
        for resp in root.findall("d:response", DAV_NS):
            for ok in resp.findall("d:propstat", DAV_NS):
                st = ok.find("d:status", DAV_NS)
                if st is not None and st.text and "200" in st.text:
                    prop = ok.find("d:prop", DAV_NS)
                    if prop is None:
                        continue
                    el = prop.find("nc:face-detections", DAV_NS)
                    if el is not None and el.text:
                        payload = el.text
                        break
            if payload is not None:
                break
        if not payload:
            return []

        try:
            data = json.loads(payload)
        except json.JSONDecodeError:
            LOG.warning("Invalid face-detections JSON on %s", nc_file.webdav_path)
            return []

        faces: list[FaceRegion] = []
        if not isinstance(data, list):
            return faces

        for item in data:
            if not isinstance(item, dict):
                continue
            # Recognize toArray uses camelCase field names
            det_id = item.get("id") or item.get("detection_id")
            x = item.get("x")
            y = item.get("y")
            w = item.get("width")
            h = item.get("height")
            if None in (det_id, x, y, w, h):
                continue
            cluster_id = item.get("clusterId", item.get("cluster_id"))
            title = (item.get("title") or "").strip()
            person = _recognize_person_title(title, cluster_id)
            dav_parent = ""
            if cluster_id is None:
                dav_parent = ""
            elif int(cluster_id) < 0:
                dav_parent = "unassigned-faces"
            else:
                dav_parent = title if title else str(int(cluster_id))

            faces.append(
                FaceRegion(
                    person=person,
                    rect=Rect(float(x), float(y), float(w), float(h)).clamp(),
                    source="nextcloud",
                    nc_file_id=nc_file.file_id,
                    nc_detection_id=int(det_id),
                    nc_cluster_id=int(cluster_id) if cluster_id is not None else None,
                    dav_parent=dav_parent,
                    file_name=nc_file.name,
                )
            )
        return faces

    def assign_person(
        self,
        detection: FaceRegion,
        person: str,
        nc_file: NextcloudFile,
        cluster_id: int,
    ) -> None:
        del cluster_id
        self.ensure_recognize_access()
        person = sanitize_person_name(person)
        if not person:
            raise ValueError("person name empty after sanitization")
        if detection.nc_detection_id is None:
            raise ValueError("detection id required")
        if not detection.dav_parent:
            raise RuntimeError(
                f"Detection {detection.nc_detection_id} has no DAV parent "
                "(unclustered/null). HTTP backend cannot reassign it until "
                "Recognize has clustered the face (or use backend=db)."
            )

        # Ensure destination person collection exists
        self.get_or_create_cluster(person)

        file_name = detection.file_name or nc_file.name
        node_name = f"{detection.nc_detection_id}-{file_name}"

        if detection.dav_parent == "unassigned-faces":
            src = (
                f"{self.recognize_dav_base()}/unassigned-faces/"
                f"{self._encode_dav_path(node_name)}"
            )
        else:
            src_parent = sanitize_person_name(detection.dav_parent) or detection.dav_parent
            src = (
                f"{self.recognize_dav_base()}/faces/"
                f"{self._encode_dav_path(src_parent)}/"
                f"{self._encode_dav_path(node_name)}"
            )
        dst_path = (
            f"{self.recognize_dav_base()}/faces/"
            f"{self._encode_dav_path(person)}/"
            f"{self._encode_dav_path(node_name)}"
        )
        destination = self._url(dst_path)

        status, _, raw = self._request(
            "MOVE",
            src,
            headers={
                "Destination": destination,
                "Overwrite": "T",
            },
            recognize=True,
        )
        if status not in (201, 204):
            raise RuntimeError(
                f"DAV MOVE failed ({status}) {src} → {destination}: "
                f"{raw[:300].decode(errors='replace')}"
            )
        # Update local knowledge for subsequent ops
        detection.dav_parent = person
        detection.person = person

    def insert_detection(
        self,
        file_id: int,
        rect: Rect,
        cluster_id: int,
        person: Optional[str] = None,
        face_vector: Optional[list[float]] = None,
        threshold: float = 0.0,
    ) -> int:
        del cluster_id, face_vector, threshold
        if not self.supports_insert:
            raise NotImplementedError(
                "This server does not provide the Recognize face-import API."
            )
        person = sanitize_person_name(person or "")
        if not person:
            raise ValueError("person name empty after sanitization")

        body = json.dumps(
            {
                "fileId": file_id,
                "person": person,
                "x": rect.x,
                "y": rect.y,
                "width": rect.w,
                "height": rect.h,
            },
            separators=(",", ":"),
        ).encode("utf-8")
        status, _, raw = self._request(
            "POST",
            self.face_import_url(),
            headers={
                "Accept": "application/json",
                "Content-Type": "application/json",
                "OCS-APIRequest": "true",
            },
            body=body,
            # Pure-JS Recognize allows up to 360 seconds for model inference.
            timeout=max(self.timeout, 420.0),
        )
        try:
            payload = json.loads(raw.decode("utf-8")) if raw else {}
        except json.JSONDecodeError as e:
            raise RuntimeError(
                f"Recognize face-import returned invalid JSON (HTTP {status})"
            ) from e

        if status not in (200, 201):
            detail = payload.get("error") if isinstance(payload, dict) else None
            raise RuntimeError(
                f"Recognize face-import failed (HTTP {status}): "
                f"{detail or raw[:300].decode(errors='replace')}"
            )
        detection = payload.get("detection", {})
        detection_id = detection.get("id") if isinstance(detection, dict) else None
        if detection_id is None:
            raise RuntimeError("Recognize face-import response has no detection id")
        return int(detection_id)

    def sample_cluster_vector(self, cluster_id: int) -> Optional[list[float]]:
        del cluster_id
        return None
