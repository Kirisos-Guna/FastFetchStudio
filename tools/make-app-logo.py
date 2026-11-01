#!/usr/bin/env python3
"""Draw the FastFetch Studio logo: assets/logo.png, assets/logo.ico, docs/store/icon.png.

The mark is the fetch itself - a picture beside rows of coloured keys - on the
app's own dark background, so it says what the app does at any size. Drawn from
code rather than kept as a one-off image so every size comes from one source.

    python tools/make-app-logo.py
"""
from __future__ import annotations

from pathlib import Path

from PIL import Image, ImageChops, ImageDraw

ROOT = Path(__file__).resolve().parents[1]
SIZE = 1024
SCALE = 4       # supersampled, then reduced, for clean edges
ICO_SIZES = [(n, n) for n in (16, 24, 32, 48, 64, 128, 256)]

BG_TOP = (36, 44, 66)
BG_BOTTOM = (14, 16, 22)
PICTURE_FROM = (242, 96, 79)    # the app's accent, hovered
PICTURE_TO = (201, 94, 224)     # the default GPU driver key colour
ROWS = (((224, 74, 63), 300), ((230, 197, 60), 236), ((67, 192, 74), 300), ((74, 156, 224), 196))


def s(v: float) -> int:
    return int(round(v * SCALE))


def gradient(size: int, top: tuple, bottom: tuple, diagonal: bool = False) -> Image.Image:
    """A top-to-bottom gradient, or a corner-to-corner one when `diagonal`."""
    steps = Image.linear_gradient("L").resize((size, size))
    if diagonal:
        steps = steps.rotate(45, resample=Image.BICUBIC, expand=True)
        off = (steps.width - size) // 2
        steps = steps.crop((off, off, off + size, off + size))
    return Image.composite(Image.new("RGB", (size, size), bottom),
                           Image.new("RGB", (size, size), top), steps)


def rounded_mask(size: int, box: tuple, radius: float) -> Image.Image:
    mask = Image.new("L", (size, size), 0)
    ImageDraw.Draw(mask).rounded_rectangle([s(v) for v in box], radius=s(radius), fill=255)
    return mask


def draw() -> Image.Image:
    size = s(SIZE)
    out = Image.new("RGBA", (size, size), (0, 0, 0, 0))

    # The tile, with a hairline of light along its edge so it holds up on dark.
    tile = (24, 24, 1000, 1000)
    mask = rounded_mask(size, tile, 216)
    out.paste(gradient(size, BG_TOP, BG_BOTTOM), (0, 0), mask)
    inner = rounded_mask(size, (32, 32, 992, 992), 208)
    edge = ImageChops.subtract(mask, inner).point(lambda a: a // 6)
    out.paste(Image.new("RGB", (size, size), (255, 255, 255)), (0, 0), edge)

    # The picture, on the left: a sun over two hills.
    pic = (140, 268, 516, 756)
    pic_mask = rounded_mask(size, pic, 72)
    out.paste(gradient(size, PICTURE_FROM, PICTURE_TO, diagonal=True), (0, 0), pic_mask)
    scene = Image.new("L", (size, size), 0)
    d = ImageDraw.Draw(scene)
    d.ellipse([s(v) for v in (372, 340, 452, 420)], fill=255)
    d.polygon([(s(x), s(y)) for x, y in ((140, 756), (140, 640), (262, 500), (384, 640),
                                         (384, 756))], fill=205)
    d.polygon([(s(x), s(y)) for x, y in ((300, 756), (404, 580), (516, 700), (516, 756))],
              fill=255)
    out.paste(Image.new("RGB", (size, size), (255, 255, 255)), (0, 0),
              ImageChops.multiply(scene, pic_mask))

    # The fetch, on the right: one coloured row per key.
    d = ImageDraw.Draw(out)
    bar_h, y = 66, 298
    for colour, width in ROWS:
        d.rounded_rectangle([s(584), s(y), s(584 + width), s(y + bar_h)], radius=s(bar_h / 2),
                            fill=colour + (255,))
        y += 110
    return out.resize((SIZE, SIZE), Image.LANCZOS)


def main() -> int:
    logo = draw()
    assets = ROOT / "assets"
    assets.mkdir(exist_ok=True)
    logo.save(assets / "logo.png", optimize=True)
    logo.save(assets / "logo.ico", sizes=ICO_SIZES)
    store = ROOT / "docs" / "store"
    store.mkdir(parents=True, exist_ok=True)
    logo.resize((512, 512), Image.LANCZOS).save(store / "icon.png", optimize=True)
    print("wrote assets/logo.png, assets/logo.ico, docs/store/icon.png")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
