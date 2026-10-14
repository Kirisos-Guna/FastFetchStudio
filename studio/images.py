"""Gallery image handling: PNG conversion and preview thumbnails."""
from __future__ import annotations

import shutil
from pathlib import Path

from studio.state import HAVE_PIL, PNGS_DIR

if HAVE_PIL:
    from PIL import Image


def _same_file(a: Path, b: Path) -> bool:
    try:
        return a.samefile(b)
    except OSError:
        return False


def convert_to_png(src: Path) -> Path:
    """Copy/convert an uploaded image into pngs\\ as PNG. Returns new path."""
    src = Path(src)
    if not src.exists():
        raise FileNotFoundError(f"Image not found: {src}")
    PNGS_DIR.mkdir(parents=True, exist_ok=True)
    try:
        if src.suffix.lower() == ".png" and src.parent.resolve() == PNGS_DIR.resolve():
            return src
    except OSError:
        pass
    base = src.stem.replace(" ", "_") or "img"
    dest = PNGS_DIR / f"{base}.png"
    n = 1
    while dest.exists() and not _same_file(src, dest):
        dest = PNGS_DIR / f"{base}-{n}.png"
        n += 1
    if not dest.exists():
        if HAVE_PIL:
            with Image.open(src) as im:
                im.save(dest, "PNG")
        else:
            if src.suffix.lower() != ".png":
                raise RuntimeError(
                    "Pillow is required to convert non-PNG images (pip install pillow)"
                )
            shutil.copyfile(src, dest)
    return dest


def load_preview_image(path: str, max_px: int = 220):
    """PIL image resized for the gallery preview, or None."""
    if not HAVE_PIL or not path or not Path(path).exists():
        return None
    try:
        with Image.open(path) as im:
            im = im.convert("RGBA")
            im.thumbnail((max_px, max_px))
            return im.copy()
    except Exception:
        return None
