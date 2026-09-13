"""Person-name normalization for Nextcloud / WebDAV compatibility."""
from __future__ import annotations


def sanitize_person_name(name: str) -> str:
    """
    Map a digiKam person tag to a Nextcloud/Recognize-safe folder title.

    WebDAV collection names are path segments: ``/`` (and ``\\``) would be
    interpreted as separators and are rejected by the server. Replace them
    with ``-`` so digiKam tags like ``Anique/Roel`` become ``Anique-Roel``.

    Applied consistently for create, assign, list lookup, and name matching.
    """
    if not name:
        return ""
    # Path separators cannot appear in a single DAV collection segment
    out = name.replace("/", "-").replace("\\", "-")
    # Avoid empty or dot-only segment names
    out = out.strip()
    return out


def person_names_match(a: str, b: str) -> bool:
    """Case-insensitive equality after sanitizing both sides."""
    sa = sanitize_person_name(a or "").strip().lower()
    sb = sanitize_person_name(b or "").strip().lower()
    return bool(sa) and sa == sb
