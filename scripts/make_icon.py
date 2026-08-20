#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Procedurally generate AppIcon.icns for Clash SpeedBench (zero third-party deps).

Draws a rounded-square dark gradient tile with a yellow lightning bolt,
writes PNGs in an .iconset and compiles them with iconutil.

Usage: python3 scripts/make_icon.py <output.icns>
"""

import io
import math
import struct
import subprocess
import sys
import tempfile
import zlib
from pathlib import Path

# Lightning bolt polygon, in unit square coordinates (x right, y down).
BOLT = [
    (0.58, 0.08),
    (0.26, 0.56),
    (0.45, 0.56),
    (0.38, 0.92),
    (0.74, 0.42),
    (0.54, 0.42),
]

CORNER_RADIUS = 0.225  # macOS-style squircle approximation


def point_in_poly(x: float, y: float, poly) -> bool:
    inside = False
    n = len(poly)
    j = n - 1
    for i in range(n):
        xi, yi = poly[i]
        xj, yj = poly[j]
        if (yi > y) != (yj > y) and x < (xj - xi) * (y - yi) / (yj - yi) + xi:
            inside = not inside
        j = i
    return inside


def rounded_rect_alpha(u: float, v: float) -> float:
    """Signed-distance-ish coverage for a rounded rect in unit space (1px AA)."""
    r = CORNER_RADIUS
    # distance from point to the rounded-rect edge (negative inside)
    qx = abs(u - 0.5) - (0.5 - r)
    qy = abs(v - 0.5) - (0.5 - r)
    dx = max(qx, 0.0)
    dy = max(qy, 0.0)
    dist = math.hypot(dx, dy) + min(max(qx, qy), 0.0) - r
    aa = 0.004
    if dist <= -aa:
        return 1.0
    if dist >= aa:
        return 0.0
    return (aa - dist) / (2 * aa)


def render(size: int) -> bytes:
    rows = bytearray()
    for py in range(size):
        rows.append(0)  # PNG filter: none
        v = py / (size - 1)
        # vertical gradient: deep blue -> near black
        top = (18, 34, 64)
        bot = (10, 12, 20)
        base = tuple(round(top[c] + (bot[c] - top[c]) * v) for c in range(3))
        for px in range(size):
            u = px / (size - 1)
            a = rounded_rect_alpha(u, v)
            if a == 0.0:
                rows += b"\x00\x00\x00\x00"
                continue
            if point_in_poly(u, v, BOLT):
                rgb = (250, 204, 21)  # bolt yellow
            else:
                # subtle radial highlight toward the top
                hl = max(0.0, 1.0 - math.hypot(u - 0.5, v - 0.15))
                rgb = tuple(min(255, round(c + 40 * hl)) for c in base)
            rows += bytes((rgb[0], rgb[1], rgb[2], round(a * 255)))

    raw = bytes(rows)

    def chunk(tag: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + tag + data + struct.pack(">I", zlib.crc32(tag + data))

    png = b"\x89PNG\r\n\x1a\n"
    png += chunk(b"IHDR", struct.pack(">IIBBBBB", size, size, 8, 6, 0, 0, 0))
    png += chunk(b"IDAT", zlib.compress(raw, 9))
    png += chunk(b"IEND", b"")
    return png


def main() -> int:
    out = Path(sys.argv[1] if len(sys.argv) > 1 else "AppIcon.icns")
    sizes = [16, 32, 64, 128, 256, 512, 1024]
    with tempfile.TemporaryDirectory() as td:
        iconset = Path(td) / "AppIcon.iconset"
        iconset.mkdir()
        for s in sizes:
            (iconset / f"icon_{s}x{s}.png").write_bytes(render(s))
        subprocess.run(["iconutil", "-c", "icns", str(iconset), "-o", str(out)], check=True)
    print(f"icon written: {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
