"""Shared constants."""
from __future__ import annotations

import re

DEFAULT_SKIP_PERSONS = frozenset(
    {"unknown", "unconfirmed", "ignored", "people"}
)

# What to do with a face whose two libraries disagree and whose history cannot
# say which of them moved. Trusting one library writes names in bulk with no
# review, so asking stays the default.
ASK = "ask"
TRUST_DIGIKAM = "digikam"
TRUST_MEMORIES = "memories"
CONFLICT_POLICIES = frozenset({ASK, TRUST_DIGIKAM, TRUST_MEMORIES})

# The companion app on Nextcloud keeps the name it was published under. It is
# not this application, and renaming one must never quietly rename the other.
COMPANION_APP_NAME = "digiKam Face Sync"

TAG_REGION_PROPERTY = "tagRegion"
FACE_VECTOR_DIM = 128

DAV_NS = {
    "d": "DAV:",
    "oc": "http://owncloud.org/ns",
    "nc": "http://nextcloud.org/ns",
}

RECT_RE = re.compile(
    r'x\s*=\s*["\']?(?P<x>-?[\d.]+)'
    r'[^y]*y\s*=\s*["\']?(?P<y>-?[\d.]+)'
    r'[^w]*width\s*=\s*["\']?(?P<w>-?[\d.]+)'
    r'[^h]*height\s*=\s*["\']?(?P<h>-?[\d.]+)',
    re.IGNORECASE,
)

IMAGE_EXT = {
    ".jpg",
    ".jpeg",
    ".png",
    ".webp",
    ".tif",
    ".tiff",
    ".heic",
    ".heif",
    ".bmp",
    ".gif",
    ".cr2",
    ".cr3",
    ".nef",
    ".arw",
    ".dng",
    ".orf",
    ".rw2",
}
