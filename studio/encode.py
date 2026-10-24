"""Pre-encoded logo assets: sixel files and block-art files."""
from __future__ import annotations

import re
from pathlib import Path

from studio.state import ARTS_DIR, CELL_PX_DEFAULT, HAVE_PIL, HAVE_SIXEL, PREVIEW_CACHE, SIXELS_DIR

if HAVE_PIL:
    from PIL import Image
if HAVE_SIXEL:
    from sixel_codec import encode_sixel, render_ansi_art


LOGO_SLACK_COLS = 1


LOGO_LADDER = (1.0, 0.86, 0.72, 0.58, 0.44)


LOGO_MIN_CELLS = (10, 8)


def logo_cell_px(st: dict) -> tuple[int, int]:
    """Pixel size of one terminal cell, as the sixels are encoded for.

    Comes from gui/term-metrics.json when the terminal has been measured, and
    from the encoder's own long-standing default otherwise. Both the encoding
    (encode_sixels) and the launcher's read-back (--logo-width from the raster)
    use this one number, which is what keeps the picture and the text that sits
    beside it from disagreeing.
    """
    try:
        cw = int(st.get("cellW", CELL_PX_DEFAULT[0]))
        ch = int(st.get("cellH", CELL_PX_DEFAULT[1]))
    except (TypeError, ValueError):
        return CELL_PX_DEFAULT
    if not (4 <= cw <= 80) or not (6 <= ch <= 160):
        return CELL_PX_DEFAULT
    return (cw, ch)


def ladder_cells(cells_w: int, cells_h: int) -> list[tuple[int, int]]:
    """The cell sizes one picture is encoded at, largest first.

    Fractions rather than fixed numbers, so the ladder follows whatever logo
    size the user configured, and whole cells because that is the unit both
    fastfetch and the terminal count in. Duplicates are dropped: a small logo
    would otherwise produce the same variant twice.
    """
    out: list[tuple[int, int]] = []
    for f in LOGO_LADDER:
        w = max(LOGO_MIN_CELLS[0], int(round(cells_w * f)))
        h = max(LOGO_MIN_CELLS[1], int(round(cells_h * f)))
        if (w, h) not in out:
            out.append((w, h))
    return out


def fit_logo_cells(im, cells_w: int, cells_h: int, st: dict):
    """The picture on a canvas `cells_w` cells wide, one column clear.

    Two numbers matter here and they are not the same one:

    * the **canvas** is `cells_w` cells wide and a whole number of rows tall
      (at most `cells_h`), because that is what the terminal reserves for the
      raster and what the launcher reads back out of it - a canvas that
      disagrees with the cells fastfetch is told about leaves a hole or is
      drawn over the text beside it;
    * the **picture** is fitted into `cells_w - LOGO_SLACK_COLS` columns, so at
      least one empty column separates the last painted pixel from the first
      glyph of the fetch text. fastfetch places that text at
      `logo.width + logo.padding.right` and the terminal rounds the image's own
      width up to whole cells; without the slack those two roundings can meet,
      and text written into a sixel's cells is exactly what makes Windows
      Terminal redraw the picture in bands.

    The picture sits in the **top-left** corner, and the canvas is only as
    many rows tall as the picture paints. Centring it in the full box (what
    this used to do) put two or three empty rows above a tall picture, so it
    started below the frame's top line, and as many below it - the box was
    then taller than the fetch's text, so the prompt landed on the picture's
    last rows and cut it off flat. The launcher reads the cell size back out
    of the raster, so a shorter canvas is all it takes for fastfetch to be
    told the real height.
    """
    cw, ch = logo_cell_px(st)
    cells_w = max(1, int(cells_w))
    cells_h = max(1, int(cells_h))
    inner_w = max(1, cells_w - LOGO_SLACK_COLS)
    pic = im.convert("RGBA")
    pic.thumbnail((inner_w * cw, cells_h * ch), Image.LANCZOS)
    rows = max(1, min(cells_h, -(-pic.height // ch)))
    canvas = Image.new("RGBA", (cells_w * cw, rows * ch), (0, 0, 0, 0))
    canvas.alpha_composite(pic, (0, 0))
    return canvas


def ensure_sixels(st: dict) -> list[Path]:
    """Encode every gallery image to sixels\\*.sixel (fastfetch consumes these).

    Every file is cells_w x cells_h cells of the terminal's own cell size, so
    the launcher can read the cell count straight back out of the raster
    instead of guessing one.
    """
    if not (HAVE_PIL and HAVE_SIXEL):
        return []
    SIXELS_DIR.mkdir(parents=True, exist_ok=True)
    for stale in SIXELS_DIR.glob('*.sixel'):
        try:
            stale.unlink()  # never leave stale/broken encodes from older versions
        except OSError:
            continue
    cells_w = int(st.get("logWidth", 28))
    cells_h = int(st.get("logHeight", 24))
    written = []
    for g in st.get("gallery", []):
        src = Path(g.get("path", ""))
        try:
            if not src.exists():
                continue
        except OSError:
            continue
        gw = int(g.get("w", cells_w))
        gh = int(g.get("h", cells_h))
        key = re.sub(r"[^A-Za-z0-9]+", "-", src.stem).strip("-") or "img"
        try:
            with Image.open(src) as im:
                for vw, vh in ladder_cells(gw, gh):
                    # The configured size keeps the plain name: the state file and
                    # older launchers look the picture up by <key>.sixel.
                    name = f"{key}.sixel" if (vw, vh) == (gw, gh) else f"{key}-{vw}x{vh}.sixel"
                    dst = SIXELS_DIR / name
                    dst.write_bytes(encode_sixel(fit_logo_cells(im, vw, vh, st)))
                    written.append(dst)
        except Exception:
            continue
    return written


def art_key(image_path) -> str:
    """Filename stem of the .art block-art render for an image.

    Shared by ensure_arts() (which writes the file) and the terminal preview
    (which has to find it again), so the two can never drift apart.
    """
    key = re.sub(r"[^A-Za-z0-9]+", "-", Path(image_path).stem).strip("-")
    return key or "img"


def ensure_arts(st: dict) -> list[Path]:
    """Render every gallery image to arts\\*.art block art.

    Terminals that can display no image protocol at all - the Windows console
    host being the common one - would otherwise only ever show fastfetch's
    built-in ASCII logo. fastfetch prints a *text* logo file verbatim, ANSI
    escapes included, so the user's own picture can still appear there.
    """
    if not HAVE_PIL:
        return []
    ARTS_DIR.mkdir(parents=True, exist_ok=True)
    for stale in ARTS_DIR.glob('*.art'):
        try:
            stale.unlink()  # never leave stale encodes from older versions
        except OSError:
            continue
    cells_w = int(st.get("logWidth", 28))
    cells_h = int(st.get("logHeight", 24))
    written = []
    for g in st.get("gallery", []):
        src = Path(g.get("path", ""))
        try:
            if not src.exists():
                continue
        except OSError:
            continue
        gw = int(g.get("w", cells_w))
        gh = int(g.get("h", cells_h))
        try:
            with Image.open(src) as im:
                for vw, vh in ladder_cells(gw, gh):
                    rows = render_ansi_art(im, vw, vh)
                    name = f"{art_key(src)}.art" if (vw, vh) == (gw, gh) else f"{art_key(src)}-{vw}x{vh}.art"
                    dst = ARTS_DIR / name
                    dst.write_text("\n".join(rows) + "\n", "utf-8", newline="\n")
                    written.append(dst)
        except Exception:
            continue
    return written


def write_preview_sixel(st: dict, image_path: str) -> Path | None:
    """Encode the chosen image to .sixel files for the terminal preview.

    The same ladder the launcher uses, so the preview shows what a real shell
    would show in a window of that size - including the smaller sizes a narrow
    or short window picks.
    """
    if not (HAVE_PIL and HAVE_SIXEL) or not image_path or not Path(image_path).exists():
        return None
    cells_w = int(st.get("logWidth", 28))
    cells_h = int(st.get("logHeight", 24))
    try:
        with Image.open(image_path) as im:
            PREVIEW_CACHE.parent.mkdir(parents=True, exist_ok=True)
            for f in PREVIEW_CACHE.parent.glob(f"{PREVIEW_CACHE.stem}-*x*.sixel"):
                f.unlink()          # never leave a variant of an older image
            for vw, vh in ladder_cells(cells_w, cells_h):
                enc = encode_sixel(fit_logo_cells(im, vw, vh, st))
                name = (PREVIEW_CACHE.name if (vw, vh) == (cells_w, cells_h)
                        else f"{PREVIEW_CACHE.stem}-{vw}x{vh}.sixel")
                (PREVIEW_CACHE.parent / name).write_bytes(enc)
        return PREVIEW_CACHE
    except Exception:
        return None


