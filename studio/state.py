"""Paths, defaults, and persisted GUI state."""
from __future__ import annotations

import copy
import json
import os
from pathlib import Path

USER = Path(os.environ.get("USERPROFILE", str(Path.home())))
FF_DIR = USER / ".config" / "fastfetch"
GUI_DIR = FF_DIR / "gui"
THEMES_DIR = FF_DIR / "themes"
PNGS_DIR = FF_DIR / "pngs"
STATE_PATH = GUI_DIR / "studio-state.json"
LAUNCHER_PATH = FF_DIR / "fastfetch-random.ps1"
CONFIG_PATH = FF_DIR / "config.jsonc"
BACKUP_PATH = FF_DIR / "config.backup.jsonc"
PREVIEW_CACHE = GUI_DIR / "preview.sixel"
PREVIEW_SCRIPT = GUI_DIR / "preview.ps1"
MEASURE_SCRIPT = GUI_DIR / "measure-cells.ps1"
METRICS_PATH = GUI_DIR / "term-metrics.json"
SIXELS_DIR = FF_DIR / "sixels"
ARTS_DIR = FF_DIR / "arts"
FF_EXE = USER / ".local" / "bin" / "fastfetch.exe"

IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp"}

BOX_WIDTH = 44
BOX_WIDTH_MIN = 40
BOX_WIDTH_MAX = 64
LOGO_GAP = 4

try:
    from PIL import Image, ImageTk  # noqa: F401

    HAVE_PIL = True
except ImportError:
    HAVE_PIL = False

try:
    from sixel_codec import CELL_PX_DEFAULT  # noqa: F401
except Exception:
    CELL_PX_DEFAULT = (10, 20)

try:
    from sixel_codec import (  # noqa: F401
        ALPHA_THRESHOLD,
        decode_sixel_pixels,
        encode_sixel,
        fit_image_cells,
        quantize_rgb,
        render_ansi_art,
    )

    HAVE_SIXEL = True
except Exception:
    HAVE_SIXEL = False

DEFAULT_STATE = {
    "gallery": [],          # [{path, w, h}]
    "defaultImage": "",
    "galleryCols": 4,
    "groups": {
        "accent": "#e04a3f",
        "os": "#e04a3f",
        "pkg": "#43c04a",
        "term": "#e6c53c",
        "cpu": "#4a9ce0",
        "drv": "#c95ee0",
        "title": "#e04a3f",
    },
    "separator": " : ",
    "boxStyle": "rounded",
    "boxColor": "",
    "colorsBlock": True,
    "randomLogo": True,
    "randomTheme": True,
    "frequency": "every",
    "logWidth": 28,
    "logHeight": 24,
    # Inner width of the module frame. Rewritten on every Apply from the widest
    # row this machine actually renders - see measure_content_width.
    "boxWidth": BOX_WIDTH,
    # How the launcher draws the logo: "auto" trusts its terminal detection,
    # "image" forces the picture even in a terminal it does not recognise,
    # "builtin" always uses fastfetch's tinted ASCII logo.
    "logoMode": "auto",
    # Redraw the fetch when the window changes size. A resize (Alt+Enter
    # fullscreen, F11, dragging an edge, Ctrl+scroll zoom) reflows the text under
    # an image logo and tears it into bands; nothing can subscribe to a resize on
    # Windows, so the prompt notices it and redraws. Erasing the torn copy is
    # what makes the repair visible, which is why this can be turned off.
    "redrawOnResize": True,
    # Pixel size of one terminal cell, as measured in the user's own terminal
    # (gui/term-metrics.json, Random tab -> "Measure terminal"). The encoder
    # writes sixels this size and the launcher reads the cell count back out of
    # them, so a measured value makes the picture land on whole cells. The
    # default is the size the files were built with before measuring existed.
    "cellW": CELL_PX_DEFAULT[0],
    "cellH": CELL_PX_DEFAULT[1],
    "cellSource": "default",
}


_INT_KEYS = ("logWidth", "logHeight", "boxWidth", "galleryCols", "cellW", "cellH")


def load_state() -> dict:
    st = copy.deepcopy(DEFAULT_STATE)
    try:
        raw = STATE_PATH.read_text("utf-8")
    except OSError:
        raw = None
    if raw is not None:
        try:
            data = json.loads(raw)
        except (json.JSONDecodeError, ValueError):
            data = None
        if isinstance(data, dict):
            st.update(data)
    for k in _INT_KEYS:
        try:
            st[k] = int(st[k])
        except (TypeError, ValueError):
            st[k] = DEFAULT_STATE[k]
    if not isinstance(st.get("gallery"), list):
        st["gallery"] = []
    if not isinstance(st.get("groups"), dict):
        st["groups"] = copy.deepcopy(DEFAULT_STATE["groups"])
    # The measured cell size wins over whatever the state file remembers.
    try:
        m = json.loads(METRICS_PATH.read_text("utf-8"))
        cw = int(m.get("cellW", 0))
        ch = int(m.get("cellH", 0))
        if 4 <= cw <= 80 and 6 <= ch <= 160:
            st["cellW"], st["cellH"] = cw, ch
            st["cellSource"] = str(m.get("source", "measured"))
    except Exception:
        pass
    return st


def save_state(st: dict) -> None:
    GUI_DIR.mkdir(parents=True, exist_ok=True)
    STATE_PATH.write_text(json.dumps(st, indent=2), "utf-8")
