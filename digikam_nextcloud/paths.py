"""Path normalization and mapping helpers."""
from __future__ import annotations

from pathlib import Path, PurePosixPath
from typing import Optional

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
