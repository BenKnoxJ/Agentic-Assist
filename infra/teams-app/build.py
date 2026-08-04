"""Build the Teams app package.

    uv run python infra/teams-app/build.py

Produces gojo-teams-app.zip next to this file: manifest plus the two icons
Teams requires. Upload it via Teams > Apps > Manage your apps > Upload an app.

Icons are generated rather than committed as binaries so the package is
reproducible from source. Pillow is not a dependency - these are small enough
to write as raw PNG, and adding an image library to a service that renders no
images would be hard to justify.
"""

import json
import struct
import zlib
from pathlib import Path
from zipfile import ZIP_DEFLATED, ZipFile

HERE = Path(__file__).parent

NAVY = (27, 58, 107)
WHITE = (255, 255, 255)


def _png(width: int, height: int, pixels: list[list[tuple[int, int, int, int]]]) -> bytes:
    """Encode RGBA pixels as a PNG. Enough of the spec to write one image."""
    raw = b"".join(
        b"\x00" + b"".join(struct.pack("BBBB", *px) for px in row) for row in pixels
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
        + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 6, 0, 0, 0))
        + chunk(b"IDAT", zlib.compress(raw, 9))
        + chunk(b"IEND", b"")
    )


def colour_icon(size: int = 192) -> bytes:
    """Navy square with a white ring — a nod to Six Eyes (7.3)."""
    c, outer, inner = size / 2, size * 0.34, size * 0.22
    rows = []
    for y in range(size):
        row = []
        for x in range(size):
            d = ((x - c) ** 2 + (y - c) ** 2) ** 0.5
            row.append((*WHITE, 255) if inner < d < outer else (*NAVY, 255))
        rows.append(row)
    return _png(size, size, rows)


def outline_icon(size: int = 32) -> bytes:
    """Transparent background, white ring. Teams tints this itself."""
    c, outer, inner = size / 2, size * 0.40, size * 0.26
    rows = []
    for y in range(size):
        row = []
        for x in range(size):
            d = ((x - c) ** 2 + (y - c) ** 2) ** 0.5
            row.append((*WHITE, 255) if inner < d < outer else (255, 255, 255, 0))
        rows.append(row)
    return _png(size, size, rows)


def main() -> None:
    manifest = (HERE / "manifest.json").read_text()
    json.loads(manifest)  # fail here rather than at upload

    out = HERE / "gojo-teams-app.zip"
    with ZipFile(out, "w", ZIP_DEFLATED) as z:
        z.writestr("manifest.json", manifest)
        z.writestr("color.png", colour_icon())
        z.writestr("outline.png", outline_icon())

    print(f"built {out} ({out.stat().st_size:,} bytes)")


if __name__ == "__main__":
    main()
