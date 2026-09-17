#!/usr/bin/env python3
"""Regenerate every derived icon from assets/logo.png.

The source is one square artwork with transparency. Everything the application
ships is cut from it here, so replacing the logo is one file and one command
rather than five exports kept in step by hand.

    just icons

Needs Pillow, which is not a runtime dependency: the results are committed.
"""
from __future__ import annotations

from pathlib import Path

from PIL import Image

ROOT = Path(__file__).resolve().parent.parent
SOURCE = ROOT / "assets" / "logo.png"
ICONS = ROOT / "digimem" / "icons"
WEB = ROOT / "digimem" / "web"

# Windows reads whichever size it needs out of the one file.
ICO_SIZES = [(16, 16), (32, 32), (48, 48), (256, 256)]


def square(image: Image.Image) -> Image.Image:
    """Trim the transparent margin, then pad back to square.

    Padding rather than stretching keeps the mark's proportions whatever the
    source's margins happen to be.
    """
    mark = image.crop(image.getchannel("A").getbbox())
    side = max(mark.size)
    canvas = Image.new("RGBA", (side, side), (0, 0, 0, 0))
    canvas.paste(mark, ((side - mark.width) // 2, (side - mark.height) // 2))
    return canvas


def main() -> int:
    source = square(Image.open(SOURCE).convert("RGBA"))
    ICONS.mkdir(parents=True, exist_ok=True)

    def at(size: int) -> Image.Image:
        return source.resize((size, size), Image.LANCZOS)

    written = [
        (ICONS / "digimem.png", lambda p: at(256).save(p, optimize=True)),
        (ICONS / "digimem.ico", lambda p: at(256).save(p, sizes=ICO_SIZES)),
        (ICONS / "digimem.icns", lambda p: at(1024).save(p)),
        # Four times the 38px it is drawn at, for the densest screens.
        (WEB / "logo.png", lambda p: at(152).save(p, optimize=True)),
        (WEB / "favicon.png", lambda p: at(64).save(p, optimize=True)),
    ]
    for path, write in written:
        write(path)
        print(f"{path.relative_to(ROOT)!s:28} {path.stat().st_size / 1024:7.1f} KB")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
