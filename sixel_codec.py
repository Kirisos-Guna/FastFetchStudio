"""Sixel encode/decode helpers for FastFetch Studio.

Conventions match the known-good logo.sixel files shipped with fastfetch:
DCS 'P0;1' (P2=1 -> unset pixels stay transparent), raster attributes,
0-100 palette entries, ONE 6-pixel-packed char per column, CR before every
color run, '-' between 6-row bands, ST terminator.
"""
from __future__ import annotations

from PIL import Image


ALPHA_THRESHOLD = 128  # >= paints, < stays transparent (sixel has no partial alpha)

UPPER_HALF = "\u2580"  # fg paints the top pixel of a cell, bg paints the bottom
LOWER_HALF = "\u2584"
RESET = "\x1b[0m"


def render_ansi_art(im: Image.Image, cells_w: int, cells_h: int) -> list[str]:
    """Render an image as truecolor half-block art: one string per terminal row.

    Some terminals cannot display any image protocol - the Windows console host
    is the common one - and there fastfetch can only draw its built-in ASCII
    logo. A text logo file, on the other hand, is printed verbatim, ANSI escapes
    and all, so this is what FastFetch Studio hands fastfetch on those terminals:
    the user's own picture, in colour, at block resolution.

    One character cell carries two image pixels (foreground paints the upper,
    background the lower), which doubles the vertical resolution. A cell is
    about twice as tall as it is wide, so the aspect fit works in screen units
    of 1x2 per cell.
    """
    im = im.convert("RGBA")
    src_ratio = im.width / max(1, im.height)
    avail_w, avail_h = cells_w, cells_h * 2
    if src_ratio >= avail_w / float(avail_h):
        draw_w, draw_h = avail_w, max(2, int(round(avail_w / src_ratio)))
    else:
        draw_h, draw_w = avail_h, max(1, int(round(avail_h * src_ratio)))
    draw_h -= draw_h % 2                      # whole cell rows only
    small = im.resize((draw_w, max(2, draw_h)), Image.LANCZOS)
    pad = max(0, (avail_w - draw_w) // 2)

    rows = []
    for y in range(0, small.height - 1, 2):
        # A row starts in the default state: the previous row ended with RESET.
        parts = [" " * pad]
        painted = False
        for x in range(small.width):
            top = small.getpixel((x, y))
            bot = small.getpixel((x, y + 1))
            top_on, bot_on = top[3] >= ALPHA_THRESHOLD, bot[3] >= ALPHA_THRESHOLD
            if top_on and bot_on:
                # Sets both channels, so no reset is needed first.
                parts.append("\x1b[38;2;%d;%d;%dm\x1b[48;2;%d;%d;%dm%s"
                             % (top[0], top[1], top[2], bot[0], bot[1], bot[2], UPPER_HALF))
                painted = True
            elif top_on or bot_on:
                # Only one channel is known, so clear the other one first.
                px, glyph = (top, UPPER_HALF) if top_on else (bot, LOWER_HALF)
                parts.append(RESET + "\x1b[38;2;%d;%d;%dm%s" % (px[0], px[1], px[2], glyph))
                painted = True
            else:
                # A space would inherit whatever background is still active.
                if painted:
                    parts.append(RESET)
                    painted = False
                parts.append(" ")
        rows.append("".join(parts) + RESET)
    return rows


def quantize_rgb(im: Image.Image) -> Image.Image:
    """Quantize the RGB channels to <=256 colors (returns RGB image)."""
    return im.convert("RGB").quantize(colors=256, method=Image.MEDIANCUT).convert("RGB")


def fit_image_cells(im: Image.Image, cells_w: int, cells_h: int) -> Image.Image:
    """Aspect-fit into the cell box (cell = 10x20 px), centered, transparency kept."""
    box_w, box_h = cells_w * 10, cells_h * 20
    im = im.convert("RGBA")
    im.thumbnail((box_w, box_h), Image.LANCZOS)
    canvas = Image.new("RGBA", (box_w, box_h), (0, 0, 0, 0))
    canvas.alpha_composite(im, ((box_w - im.width) // 2, (box_h - im.height) // 2))
    return canvas


def encode_sixel(im: Image.Image) -> bytes:
    """Encode an RGB PIL image to sixel bytes with a transparent background."""
    im = im.convert("RGBA")
    opaque = im.getchannel("A").point(lambda a: 255 if a >= ALPHA_THRESHOLD else 0)
    im = quantize_rgb(im)
    w, h = im.size
    px = im.load()
    op = opaque.load()

    colors: dict[tuple[int, int, int], int] = {}
    bands: list[dict[int, list[int]]] = []   # per 6-row band: colorIdx -> 6 row masks
    for by in range(0, h, 6):
        d: dict[int, list[int]] = {}
        for r in range(6):
            if by + r >= h:
                break
            for x in range(w):
                if not op[x, by + r]:
                    continue   # transparent: leave unpainted -> terminal bg shows
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
            rw = rh = 0
            for k in range(3):
                assert i < n and body[i] == 0x3B, "bad raster attrs"
                v, i = pnum(i + 1)
                if k == 1:
                    rw = v
                elif k == 2:
                    rh = v
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
    # Prefer the declared canvas size: trailing/leading transparent bands
    # (unpainted by design) would otherwise shrink the reported extent.
    if rw and rh:
        width, height = rw, rh
    else:
        height = max((yy + 1 for yy in grid), default=0)
    return width, height, grid
