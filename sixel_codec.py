"""Sixel encode/decode helpers for FastFetch Studio.

Conventions match the known-good logo.sixel files shipped with fastfetch:
DCS 'P0;1' (P2=1 -> unset pixels stay transparent), raster attributes,
0-100 palette entries, ONE 6-pixel-packed char per column, CR before every
color run, '-' between 6-row bands, ST terminator.
"""
from __future__ import annotations

from PIL import Image


def flatten_alpha(im: Image.Image, bg=(0, 0, 0)) -> Image.Image:
    """Composite transparency onto bg (sixel has no alpha channel)."""
    im = im.convert("RGBA")
    bgim = Image.new("RGBA", im.size, bg + (255,))
    return Image.alpha_composite(bgim, im).convert("RGB")


def fit_image_cells(im: Image.Image, cells_w: int, cells_h: int) -> Image.Image:
    """Aspect-fit into the cell box (cell = 10x20 px), centered, opaque."""
    box_w, box_h = cells_w * 10, cells_h * 20
    im = flatten_alpha(im)
    im.thumbnail((box_w, box_h), Image.LANCZOS)
    canvas = Image.new("RGB", (box_w, box_h), (0, 0, 0))
    canvas.paste(im, ((box_w - im.width) // 2, (box_h - im.height) // 2))
    return canvas


def encode_sixel(im: Image.Image) -> bytes:
    """Encode an RGB PIL image to sixel bytes with a transparent background."""
    im = im.convert("RGB")
    im = im.quantize(colors=256, method=Image.MEDIANCUT).convert("RGB")
    w, h = im.size
    px = im.load()

    colors: dict[tuple[int, int, int], int] = {}
    bands: list[dict[int, list[int]]] = []   # per 6-row band: colorIdx -> 6 row masks
    for by in range(0, h, 6):
        d: dict[int, list[int]] = {}
        for r in range(6):
            if by + r >= h:
                break
            for x in range(w):
                c = px[x, by + r]
                idx = colors.setdefault(c, len(colors))
                mask = d.get(idx)
                if mask is None:
                    mask = [0] * 6
                    d[idx] = mask
                mask[r] |= 1 << x
        bands.append(d)

    items = sorted(colors.items())            # [(rgb, idx)] sorted by color

    out = bytearray(b"P0;1q\"1;1;%d;%d" % (w, h))
    for i, (rgb, _) in enumerate(items):
        out += b"#%d;2;%d;%d;%d" % (i, rgb[0] * 100 // 255,
                                    rgb[1] * 100 // 255, rgb[2] * 100 // 255)

    for d in bands:
        for i, (_, idx) in enumerate(items):
            masks = d.get(idx)
            if masks is None:
                continue
            out += b"$"          # CR before EVERY color run: each color starts at x=0
            out += b"#%d" % i
            chars = [0] * w
            for r in range(6):
                bits = masks[r]
                for x in range(w):
                    if bits >> x & 1:
                        chars[x] |= 1 << r
            col = 0
            while col < w:
                ch = 0x3F + chars[col]
                run = 1
                while col + run < w and 0x3F + chars[col + run] == ch:
                    run += 1
                if run > 3:
                    out += b"!%d%s" % (run, bytes([ch]))
                else:
                    out += bytes([ch]) * run
                col += run
        out += b"-"
    out += b"\\"
    return bytes(out)


def decode_sixel_pixels(data: bytes) -> tuple[int, int, dict]:
    """Decode sixel (as emitted by encode_sixel) to (width, height, pixels).

    pixels: {(x, y): (r, g, b)} for every painted pixel - used by the
    selftest to round-trip-verify the encoder.
    """
    hdr = b"P0;1q"
    assert data.startswith(hdr) and data.endswith(b"\\"), "malformed sixel"
    body = data[len(hdr):-2]
    n = len(body)
    pal: dict[int, tuple[int, int, int]] = {}
    grid: dict[int, dict[int, tuple[int, int, int]]] = {}
    width = 0
    cur = -1
    x = y = 0
    i = 0

    def pnum(j: int) -> tuple[int, int]:
        v = 0
        while j < n and 0x30 <= body[j] <= 0x39:
            v = v * 10 + (body[j] - 0x30)
            j += 1
        return v, j

    while i < n:
        b = body[i]
        if b == 0x22:  # raster attrs "P1;P2;W;H
            _, i = pnum(i + 1)
            for _ in range(3):
                assert i < n and body[i] == 0x3B, "bad raster attrs"
                _, i = pnum(i + 1)
        elif b == 0x23:  # palette def or color select
            v, j = pnum(i + 1)
            if j < n and body[j] == 0x3B:
                assert body[j + 1] == 0x32 and body[j + 2] == 0x3B, "expected ;2;"
                r, j2 = pnum(j + 3)
                assert j2 < n and body[j2] == 0x3B
                g, j3 = pnum(j2 + 1)
                assert j3 < n and body[j3] == 0x3B
                bl, i = pnum(j3 + 1)
                pal[v] = (r * 255 // 100, g * 255 // 100, bl * 255 // 100)
            else:
                cur = v
                i = j
        elif b == 0x21:  # ! RLE repeat
            cnt, j = pnum(i + 1)
            six = body[j] - 0x3F
            for dx in range(cnt):
                for dy in range(6):
                    if six >> dy & 1:
                        grid.setdefault(y + dy, {})[x + dx] = pal[cur]
                width = max(width, x + dx + 1)
            x += cnt
            i = j + 1
        elif b == 0x24:  # $ carriage return
            x = 0
            i += 1
        elif b == 0x2D:  # - next band
            y += 6
            x = 0
            i += 1
        elif 0x3F <= b <= 0x7E:  # sixel char
            six = b - 0x3F
            for dy in range(6):
                if six >> dy & 1:
                    grid.setdefault(y + dy, {})[x] = pal[cur]
            width = max(width, x + 1)
            x += 1
            i += 1
        else:
            i += 1
    height = max((yy + 1 for yy in grid), default=0)
    return width, height, grid
