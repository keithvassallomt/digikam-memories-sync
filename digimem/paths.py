"""Path normalization and mapping helpers."""
from __future__ import annotations

from pathlib import Path, PurePosixPath
from typing import Optional

#: A path-map source that stands for the digiKam collection itself. A photo is
#: placed under the map's target by its path inside the collection (album
#: folder and file name), the same way the Memories-to-digiKam pass reads it
#: back. Where the collection sits on disk never enters into it, which matters
#: because digiKam does not store that in a form that can be compared: on
#: Windows the drive letter is missing, and a network share is stored as "/".
COLLECTION = ""


def is_collection_map(source: str) -> bool:
    """True when a path-map source is :data:`COLLECTION`."""
    return normalize_path(source) in ("", ".")


def normalize_path(path: str) -> str:
    p = path.replace("\\", "/")
    p = PurePosixPath(p).as_posix()
    while p.startswith("./"):
        p = p[2:]
    return p.lstrip("/")


def apply_path_maps(path: str, maps: list[tuple[str, str]]) -> Optional[str]:
    """Rewrite *path* using the first matching map prefix.

    Returns the mapped path on success. When *maps* is non-empty and no
    source prefix matches, returns ``None`` so callers can skip the file.
    When *maps* is empty, returns the normalized path unchanged.
    """
    p = normalize_path(path)
    if not maps:
        return p
    for src, dst in maps:
        src_n = normalize_path(src)
        dst_n = normalize_path(dst)
        if p == src_n or p.startswith(src_n + "/"):
            rest = p[len(src_n) :].lstrip("/")
            return normalize_path(f"{dst_n}/{rest}" if rest else dst_n)
    return None


def expand_path(path: str | None) -> Optional[str]:
    if not path:
        return None
    return str(Path(path).expanduser())


def strip_nc_files_prefix(path: str) -> str:
    p = normalize_path(path)
    if p.startswith("files/"):
        return p[len("files/") :]
    if p == "files":
        return ""
    return p
