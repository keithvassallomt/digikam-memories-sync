"""File and face-region matching."""
from __future__ import annotations

import logging
from typing import Optional

from .models import DigikamImage, FaceRegion, FileMatch, NextcloudFile
from .names import person_names_match
from .paths import apply_path_maps, normalize_path, strip_nc_files_prefix

LOG = logging.getLogger(__name__)


def digikam_path_candidates(
    di: DigikamImage,
    path_maps: list[tuple[str, str]],
    *,
    prefer_pattern: Optional[str] = None,
) -> list[str]:
    """
    Ordered unique WebDAV-relative path candidates for a digiKam image.

    When path_maps are configured (recommended), only mapped paths are tried
    so we do not PROPFIND host filesystem paths that will always 404.
    If no map matches the digiKam path, returns an empty list (caller should
    skip the file).
    """
    raw: list[str] = []
    if path_maps:
        for path in (di.full_path, di.relative_path):
            mapped = apply_path_maps(path, path_maps)
            if mapped is not None:
                raw.append(mapped)
        if not raw:
            # Outside configured path_maps → skip this file
            return []
    else:
        raw.extend(
            [
                di.relative_path,
                di.full_path,
                di.name,
            ]
        )

    out: list[str] = []
    seen: set[str] = set()
    for c in raw:
        key = normalize_path(c)
        if not key or key in seen:
            continue
        # Never use absolute host paths on WebDAV
        if key.startswith("/") or (len(key) > 2 and key[1] == ":"):
            continue
        if key.startswith("files/"):
            key = strip_nc_files_prefix(key)
        if not key or key in seen:
            continue
        # Skip bare basename when we have better candidates (ambiguous)
        if key == di.name and any("/" in x for x in raw if x):
            continue
        seen.add(key)
        out.append(key)

    # Learned winning pattern (e.g. "mapped_full") bubbled to front by caller
    # via prefer_pattern substring match
    if prefer_pattern and out:
        preferred = [p for p in out if prefer_pattern in p]
        rest = [p for p in out if prefer_pattern not in p]
        if preferred:
            out = preferred + rest

    return out


def match_files(
    digikam_images: list[DigikamImage],
    nc_files: list[NextcloudFile],
    path_maps: list[tuple[str, str]],
    nc_path_strip: str = "files/",
    use_name_size: bool = True,
) -> tuple[list[FileMatch], list[DigikamImage]]:
    by_rel: dict[str, list[NextcloudFile]] = {}
    by_name_size: dict[tuple[str, int], list[NextcloudFile]] = {}

    for nf in nc_files:
        if nc_path_strip and nc_path_strip != "files/":
            p = normalize_path(nf.path)
            strip = normalize_path(nc_path_strip)
            if p.startswith(strip.rstrip("/") + "/") or p == strip.rstrip("/"):
                stripped = p[len(strip.rstrip("/")) :].lstrip("/")
            else:
                stripped = strip_nc_files_prefix(p)
        else:
            stripped = strip_nc_files_prefix(nf.path)
        stripped = normalize_path(stripped)
        by_rel.setdefault(stripped.lower(), []).append(nf)
        by_rel.setdefault(normalize_path(nf.path).lower(), []).append(nf)
        if nf.webdav_path:
            by_rel.setdefault(normalize_path(nf.webdav_path).lower(), []).append(nf)
        by_name_size.setdefault((nf.name.lower(), nf.size), []).append(nf)

    LOG.info(
        "Matching %d digiKam images against %d Nextcloud files…",
        len(digikam_images),
        len(nc_files),
    )
    matches: list[FileMatch] = []
    unmatched: list[DigikamImage] = []

    for di in digikam_images:
        candidates = digikam_path_candidates(di, path_maps)
        if path_maps and not candidates:
            # No digiKam path matched a configured map — skip entirely
            continue
        found: Optional[NextcloudFile] = None
        method = ""

        for c in candidates:
            key = normalize_path(c).lower()
            hits = by_rel.get(key) or []
            uniq = {h.file_id: h for h in hits}
            if len(uniq) == 1:
                found = next(iter(uniq.values()))
                method = "path"
                break
            if len(uniq) > 1:
                sized = [h for h in uniq.values() if h.size == di.file_size]
                if len(sized) == 1:
                    found = sized[0]
                    method = "path+size"
                    break

        if not found and use_name_size and di.file_size > 0:
            hits = by_name_size.get((di.name.lower(), di.file_size), [])
            uniq = {h.file_id: h for h in hits}
            if len(uniq) == 1:
                found = next(iter(uniq.values()))
                method = "name_size"

        if found:
            matches.append(FileMatch(digikam=di, nextcloud=found, method=method))
        else:
            unmatched.append(di)

    LOG.info(
        "File match complete: %d matched, %d unmatched",
        len(matches),
        len(unmatched),
    )
    return matches, unmatched


def match_regions(
    dk_faces: list[FaceRegion],
    nc_faces: list[FaceRegion],
    iou_threshold: float,
) -> tuple[
    list[tuple[FaceRegion, FaceRegion, float]],
    list[FaceRegion],
    list[FaceRegion],
]:
    pairs: list[tuple[FaceRegion, FaceRegion, float]] = []
    used_nc: set[int] = set()
    used_dk: set[int] = set()

    scored: list[tuple[float, int, int]] = []
    for i, df in enumerate(dk_faces):
        for j, nf in enumerate(nc_faces):
            score = df.rect.iou(nf.rect)
            if score >= iou_threshold:
                scored.append((score, i, j))
    scored.sort(reverse=True)

    for score, i, j in scored:
        if i in used_dk or j in used_nc:
            continue
        used_dk.add(i)
        used_nc.add(j)
        pairs.append((dk_faces[i], nc_faces[j], score))

    unmatched_dk = [f for i, f in enumerate(dk_faces) if i not in used_dk]
    unmatched_nc = [f for j, f in enumerate(nc_faces) if j not in used_nc]
    return pairs, unmatched_dk, unmatched_nc


def overlapping_same_person(
    face: FaceRegion,
    candidates: list[FaceRegion],
    iou_threshold: float,
) -> Optional[tuple[FaceRegion, float]]:
    """Return the best overlapping candidate carrying the same person name.

    Region matching is deliberately one-to-one. Some libraries can nevertheless
    contain two overlapping rectangles for the same person. Once one rectangle
    has been paired, this check lets the other one be treated as represented
    instead of proposing a duplicate in the opposite library forever.
    """
    matches = [
        (candidate, face.rect.iou(candidate.rect))
        for candidate in candidates
        if person_names_match(face.person, candidate.person)
        and face.rect.iou(candidate.rect) >= iou_threshold
    ]
    return max(matches, key=lambda item: item[1], default=None)
