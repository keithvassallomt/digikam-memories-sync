"""Face rectangle parsing and geometry helpers."""
from __future__ import annotations

import logging
import xml.etree.ElementTree as ET
from typing import Optional

from .constants import FACE_VECTOR_DIM, RECT_RE
from .models import Rect

LOG = logging.getLogger(__name__)


def displayed_dimensions(
    width: int, height: int, orientation: int = 1
) -> tuple[int, int]:
    """Return the dimensions of the image as displayed by digiKam.

    digiKam stores pixel-based face regions after applying EXIF orientation.
    Orientations 5 through 8 turn the image by 90 degrees, reversing the
    displayed width and height.
    """
    if orientation in (5, 6, 7, 8):
        return height, width
    return width, height


def parse_tag_region(
    value: str, width: int, height: int, orientation: int = 1
) -> Optional[Rect]:
    if not value or width <= 0 or height <= 0:
        return None

    m = RECT_RE.search(value)
    if not m:
        try:
            el = ET.fromstring(value.strip())
            x = float(el.attrib["x"])
            y = float(el.attrib["y"])
            w = float(el.attrib.get("width") or el.attrib.get("w"))
            h = float(el.attrib.get("height") or el.attrib.get("h"))
        except Exception:
            LOG.debug("Unparseable tagRegion: %r", value)
            return None
    else:
        x, y, w, h = (
            float(m.group("x")),
            float(m.group("y")),
            float(m.group("w")),
            float(m.group("h")),
        )

    if 0 <= x <= 1 and 0 <= y <= 1 and 0 < w <= 1 and 0 < h <= 1 and (
        x + w <= 1.05 and y + h <= 1.05
    ):
        return Rect(x, y, w, h).clamp()

    display_width, display_height = displayed_dimensions(
        width, height, orientation
    )
    return Rect(
        x / display_width,
        y / display_height,
        w / display_width,
        h / display_height,
    ).clamp()


def zero_vector() -> list[float]:
    return [0.0] * FACE_VECTOR_DIM
