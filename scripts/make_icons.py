#!/usr/bin/env python
"""Generate the PWA icon set with no image library.

Pillow is not a dependency of this project and adding one for six PNGs would be
a poor trade. A PNG is a handful of chunks around a zlib stream, so the encoder
below is about thirty lines and has no dependencies at all.

The mark is a pulse trace — the thing the product is named after — in the app's
own accent cyan on its own background, so the icon on the home screen and the
first screen after launch are recognisably the same object.

Two shapes are produced, because Android uses them differently:

* `any`      — the artwork fills the square. Used where the launcher shows the
               icon as supplied.
* `maskable` — the launcher may crop this to a circle, a squircle or a rounded
               square depending on the phone. Everything that matters therefore
               sits inside the middle 80%, the "safe zone" the spec defines.
               Without a maskable icon Android puts the `any` one on a white
               plate, which looks like a bookmark rather than an app.

    python scripts/make_icons.py
"""

from __future__ import annotations

import struct
import zlib
from pathlib import Path

OUT = Path(__file__).resolve().parents[1] / "frontend" / "public" / "icons"

BG = (10, 13, 18)         # --bg, the app's own ground
ACCENT = (0, 212, 255)    # --accent
DIM = (35, 42, 54)        # --border, for the baseline


def png(width: int, height: int, pixels: list[list[tuple[int, int, int]]]) -> bytes:
    """Encode RGB rows as a PNG. No filtering — these are tiny and flat."""
    raw = b"".join(
        b"\x00" + b"".join(struct.pack("3B", *px) for px in row) for row in pixels
    )

    def chunk(tag: bytes, data: bytes) -> bytes:
        return (
            struct.pack(">I", len(data))
            + tag
            + data
            + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)
        )

    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", struct.pack(">2I5B", width, height, 8, 2, 0, 0, 0))
        + chunk(b"IDAT", zlib.compress(raw, 9))
        + chunk(b"IEND", b"")
    )


def _blend(a: tuple[int, int, int], b: tuple[int, int, int], t: float) -> tuple[int, int, int]:
    return tuple(int(round(a[i] + (b[i] - a[i]) * t)) for i in range(3))  # type: ignore[return-value]


def draw(size: int, *, maskable: bool) -> list[list[tuple[int, int, int]]]:
    """A pulse trace, drawn to fit whichever crop the launcher applies."""
    px = [[BG for _ in range(size)] for _ in range(size)]

    # A maskable icon may be cropped to 80% of its width, so the artwork is
    # scaled down to survive the worst case rather than being clipped by it.
    inset = 0.30 if maskable else 0.14
    left, right = size * inset, size * (1.0 - inset)
    mid = size / 2.0
    span = right - left
    stroke = max(2.0, size * (0.055 if maskable else 0.07))

    # The trace: flat, a small dip, a tall spike, a dip, then flat again —
    # the shape of an acceleration, which is what the scanner looks for.
    nodes = [
        (0.00, 0.00), (0.26, 0.00), (0.34, 0.16), (0.44, -0.42),
        (0.54, 0.30), (0.62, 0.00), (1.00, 0.00),
    ]
    points = [(left + fx * span, mid + fy * span) for fx, fy in nodes]

    def line(x0: float, y0: float, x1: float, y1: float, colour, width: float) -> None:
        steps = int(max(abs(x1 - x0), abs(y1 - y0)) * 3) + 2
        for s in range(steps + 1):
            t = s / steps
            cx, cy = x0 + (x1 - x0) * t, y0 + (y1 - y0) * t
            r = width / 2.0
            for dy in range(int(-r) - 1, int(r) + 2):
                for dx in range(int(-r) - 1, int(r) + 2):
                    x, y = int(cx + dx), int(cy + dy)
                    if not (0 <= x < size and 0 <= y < size):
                        continue
                    d = ((cx + dx - cx) ** 2 + (cy + dy - cy) ** 2) ** 0.5
                    if d <= r:
                        px[y][x] = colour
                    elif d <= r + 1.0:  # one pixel of feathering, no more
                        px[y][x] = _blend(px[y][x], colour, max(0.0, r + 1.0 - d))

    # A dim baseline first, so the spike reads as a departure from calm.
    line(left, mid, right, mid, DIM, max(1.5, stroke * 0.35))
    for (x0, y0), (x1, y1) in zip(points, points[1:], strict=False):
        line(x0, y0, x1, y1, ACCENT, stroke)
    return px


def main() -> int:
    OUT.mkdir(parents=True, exist_ok=True)
    written = []
    for size in (192, 512):
        (OUT / f"icon-{size}.png").write_bytes(png(size, size, draw(size, maskable=False)))
        written.append(f"icon-{size}.png")
        (OUT / f"icon-maskable-{size}.png").write_bytes(png(size, size, draw(size, maskable=True)))
        written.append(f"icon-maskable-{size}.png")

    # iOS ignores the manifest's icons and reads this one from the HTML.
    (OUT / "apple-touch-icon.png").write_bytes(png(180, 180, draw(180, maskable=False)))
    written.append("apple-touch-icon.png")
    # The browser tab.
    (OUT / "favicon.png").write_bytes(png(64, 64, draw(64, maskable=False)))
    written.append("favicon.png")

    for name in written:
        print(f"  {name}  {(OUT / name).stat().st_size:,} bytes")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
