#!/usr/bin/env python3
"""FastFetch Studio - GUI customizer + random launcher for fastfetch on Windows.

Tabs:
  Gallery - upload images (converted to PNG), thumbnails, per-image size, default
  Theme   - per-group key colors, separator, box border style, colors block
  Random  - what randomizes per terminal open, how often

Apply writes:
  config.jsonc            (backed up once to config.backup.jsonc)
  themes/theme-XX.jsonc   (one file per palette)
  fastfetch-random.ps1    (random logo + theme launcher, sixel on WT / kitty on WezTerm)

Requires: Python 3.10+, Pillow (pip install pillow), fastfetch on PATH.
Build exe: build.bat (PyInstaller --onefile --noconsole).
"""

from __future__ import annotations

import base64
import colorsys
import io
import json
import os
import random
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import webbrowser
from pathlib import Path

import tkinter as tk
from tkinter import filedialog, font as tkfont, messagebox, ttk

try:
    from PIL import Image, ImageTk
    HAVE_PIL = True
except ImportError:
    HAVE_PIL = False

try:
    from sixel_codec import (ALPHA_THRESHOLD, CELL_PX_DEFAULT, decode_sixel_pixels,
                             encode_sixel, fit_image_cells, quantize_rgb,
                             render_ansi_art)
    HAVE_SIXEL = True
except Exception:
    HAVE_SIXEL = False
    CELL_PX_DEFAULT = (10, 20)

# --------------------------------------------------------------------------- paths
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
# The Preview button's payload. It is a generated file rather than a
# `powershell -Command "..."` one-liner for a reason - see PREVIEW_TEMPLATE.
PREVIEW_SCRIPT = GUI_DIR / "preview.ps1"
# The Random tab's "Measure terminal" payload, plus the probe images it draws to
# learn how many rows - and therefore how many pixels - one cell is worth.
MEASURE_SCRIPT = GUI_DIR / "measure-cells.ps1"
SIXELS_DIR = FF_DIR / "sixels"
# Block-art logos for terminals that cannot display an image protocol at all.
ARTS_DIR = FF_DIR / "arts"
# Where setup.ps1 puts fastfetch. The app only needs it to measure the longest
# rendered row (see measure_content_width) - everything else defers to the
# launcher, which resolves the executable at run time.
FF_EXE = USER / ".local" / "bin" / "fastfetch.exe"

IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp"}

# --------------------------------------------------------------------------- frame
#
# The module frame is a literal run of horizontal rule, so its width is baked
# into every theme. Two ways to get it wrong, and both are visible: a frame
# narrower than the longest rendered row leaves that row hanging outside the
# border, and a frame wider than the window wraps - which lands the border on the
# next row and pulls the whole fetch apart. So the width is measured from the
# machine's own output at Apply time (measure_content_width) and clamped to a
# range a terminal can actually show.
BOX_WIDTH = 44          # fallback when fastfetch cannot be asked
BOX_WIDTH_MIN = 40
BOX_WIDTH_MAX = 64
# fastfetch puts this many blank columns after the logo before the text starts
# (its `logo.padding.right`, default 4). Measured, not assumed: a 28-column
# sixel puts the frame's corner at column 29, and a 24-column one at column 25.
# The logo is drawn *beside* the frame, so the room left for it is
# window - frame - 1 - this. Forgetting it is what clipped the frame's last
# three columns beside the logo even though the arithmetic "fitted".
LOGO_GAP = 4

# --------------------------------------------------------------------------- logo cells
#
# The logo tearing into bands had one root cause, and it was ours: a sixel is
# placed by its *raster* size (pixels), while --logo-width/--logo-height only
# tell fastfetch how many cells it thinks the picture occupies. The app wrote
# 10x20 px per cell into every .sixel and then handed fastfetch whatever cell
# count the window happened to leave room for - two numbers that had no reason
# to agree, and did not. The picture is 28x24 cells of pixels; when fastfetch
# was told 10x9, its text was laid out where the picture still is, and Windows
# Terminal reacts to text written over a sixel by redrawing the image in
# bands (the alpha-blended rows come back as strips). Scrolling the old picture
# away and redrawing - the v1.1.15 repair - made it worse, because [Console]
# ::Clear() does not remove a sixel image at all: the repair left the torn
# picture on screen and painted a second one over it.
#
# So the size is now derived instead of assumed: the raster is the truth, the
# cell count is read back out of it, and both come from the terminal's real
# cell size when it has been measured (Random tab -> "Measure terminal").
# `sixels\*.sixel` is written as cells_w x cells_h cells of exactly this size.
METRICS_PATH = GUI_DIR / "term-metrics.json"
# Room for the text beside the picture: the picture is drawn one column
# narrower than the cell count fastfetch is told about, so no glyph can ever
# land in the picture's columns even if the terminal rounds the image's width
# up a cell.
LOGO_SLACK_COLS = 1

# The picture is pre-encoded at a ladder of sizes, largest first, so a window
# that cannot hold the full size still gets the *same picture* smaller - instead
# of a lie about the full one. That lie is the everyday half of this bug: on a
# 1440x900 screen a terminal snapped to half the width is 70 columns, the frame
# takes 45 of them, so 21 are left for a picture that needs 28. v1.1.15 handed
# fastfetch `--logo-width 21`, fastfetch then wrote its text from column 25 - and
# the picture is drawn where it always was, over columns 0..27. Text inside a
# sixel's own cells is what makes Windows Terminal redraw the image in bands,
# which is exactly what the screenshots showed: a logo with glyphs punched
# through it. Choosing a smaller file keeps the one invariant - raster size ==
# cells x cell size - that the numbers have to agree on.
#
# Measured on this machine (Windows Terminal 1.24, 2880x1800 at 200%):
#   70x42 window -> room 21 columns -> the 20x17 variant, text starts at column 24
#   180x42 window -> the full 28x24 picture, text starts at column 32
# The smallest step is what still reads as a picture; below that the block-art
# render (pure text, which cannot tear) takes over.
LOGO_LADDER = (1.0, 0.86, 0.72, 0.58, 0.44)
LOGO_MIN_CELLS = (10, 8)
BORDER_CHARS = set("\u2500\u2501\u2550-\u250c\u2510\u2514\u2518\u2554\u2557\u255a\u255d"
                   "\u250f\u2513\u2517\u251b+ ")
ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")

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

GROUPS = [
    ("os", "OS / Kernel", "red keys"),
    ("pkg", "Packages / Display", "green keys"),
    ("term", "Terminal / WM", "yellow keys"),
    ("cpu", "CPU / GPU", "blue keys"),
    ("drv", "GPU Driver / Memory / Disks", "magenta keys"),
    ("title", "Title / Accent", "second block keys"),
]

BOX_STYLES = {
    "rounded": ("\u250c", "\u2500", "\u2510", "\u2502", "\u2514", "\u2518"),
    "double": ("\u2554", "\u2550", "\u2557", "\u2551", "\u255a", "\u255d"),
    "heavy": ("\u250f", "\u2501", "\u2513", "\u2503", "\u2517", "\u251b"),
    "ascii": ("+", "-", "+", "|", "+", "+"),
    "none": None,
}

BOX_WIDTH = 44
SIXEL_DCS = "\x1bPq"

# --------------------------------------------------------------------------- icons
#
# Every module's icon is delivered by fastfetch itself: `display.key.type` below
# turns on the key icon, and fastfetch then fills in its own built-in `keyIcon`
# default for that module type. No glyph is written into any `key` string.
#
# The previous approach - a hand-typed glyph inside each key, e.g. "   OS" with a
# private-use codepoint in front - is why icons went missing. A hand-maintained
# list has to be kept in sync with the module list, and it silently drifted:
# 6 of the 14 rows (kernel, terminal, title, cpu, GPU Driver, memory) had no
# glyph at all, so they rendered label-only. Deriving the icon from the module
# type cannot drift, because fastfetch always has a default for every type.
#
# Do NOT put a glyph in a `key` string. With key.type "both" that renders two
# icons - fastfetch's default *and* the hand-typed one - which is what happened
# to the hand-written config this replaced.
KEY_ICON_MODE = "both"
KEY_PADDING_LEFT = 1     # the indent the keys used to carry as literal spaces

# A whitespace-only key is fastfetch's "no key" sentinel - no label, no
# separator, no icon. Used by the title row so it stays a bare "user @ host".
NO_KEY = " "

# The labeled rows the generated config must contain, in order (gpu twice: once
# for the device, once for its driver). --smoke-icons compares against this, so
# dropping a module fails the guard instead of quietly checking fewer rows.
KEYED_MODULES = ("chassis", "os", "kernel", "packages", "display", "terminal",
                 "wm", "cpu", "gpu", "gpu", "memory", "disk", "uptime")

# Shown on the Random tab. Kept as data so the columns can be aligned by grid
# instead of a monospace text blob.
MANAGED_FILES = [
    ("config.jsonc", "your main fastfetch config (backed up once)"),
    ("themes\\theme-01..08.jsonc", "generated color palettes"),
    ("fastfetch-random.ps1", "picks a random theme + logo on each run"),
    ("pngs\\", "your uploaded images (converted to PNG)"),
    ("sixels\\", "pre-encoded logos for image-capable terminals"),
    ("arts\\", "block-art logos for terminals that can show no image"),
]


def esc(s: str) -> str:
    """Escape a string for embedding inside a fastfetch JSON string value."""
    return s.replace("\\", "\\\\").replace('"', '\\"')


# --------------------------------------------------------------------------- state
def load_state() -> dict:
    st = json.loads(json.dumps(DEFAULT_STATE))  # deep copy
    if STATE_PATH.exists():
        try:
            data = json.loads(STATE_PATH.read_text("utf-8"))
            if isinstance(data, dict):
                st.update(data)
        except Exception:
            pass
    # The measured cell size wins over whatever the state file remembers: it is
    # the size the sixels in sixels\ were actually encoded for, and the size the
    # launcher will read back out of them. Read inline rather than through
    # load_metrics(): this runs at import time, before that function exists.
    try:
        m = json.loads(METRICS_PATH.read_text("utf-8"))
        cw = int(m.get("cellW", 0)); ch = int(m.get("cellH", 0))
        if 4 <= cw <= 80 and 6 <= ch <= 160:
            st["cellW"], st["cellH"] = cw, ch
            st["cellSource"] = str(m.get("source", "measured"))
    except Exception:
        pass
    return st


def save_state(st: dict) -> None:
    GUI_DIR.mkdir(parents=True, exist_ok=True)
    STATE_PATH.write_text(json.dumps(st, indent=2), "utf-8")


STATE = load_state()


# --------------------------------------------------------------------------- images
def convert_to_png(src: Path) -> Path:
    """Copy/convert an uploaded image into pngs\\ as PNG. Returns new path."""
    PNGS_DIR.mkdir(parents=True, exist_ok=True)
    if src.suffix.lower() == ".png" and src.parent.resolve() == PNGS_DIR.resolve():
        return src
    base = src.stem.replace(" ", "_")
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
                raise RuntimeError("Pillow is required to convert non-PNG images (pip install pillow)")
            shutil.copyfile(src, dest)
    return dest


def _same_file(a: Path, b: Path) -> bool:
    try:
        return a.samefile(b)
    except OSError:
        return False


def load_preview_image(path: str, max_px: int = 220):
    """PIL image resized for the gallery preview, or None."""
    if not HAVE_PIL or not path or not Path(path).exists():
        return None
    try:
        with Image.open(path) as im:
            im = im.convert("RGBA")
            im.thumbnail((max_px, max_px))
            return im
    except Exception:
        return None


# --------------------------------------------------------------------------- config build
def hex_to_sgr(hex_color: str) -> str:
    """'#e04a3f' -> '224;74;63' (decimal RGB components for SGR 38;2)."""
    h = hex_color.lstrip("#")
    if len(h) != 6:
        return "255;255;255"
    return ";".join(str(int(h[i:i + 2], 16)) for i in (0, 2, 4))


def box_width(st: dict) -> int:
    """Inner width of the frame, clamped to something a terminal can show."""
    try:
        w = int(st.get("boxWidth", BOX_WIDTH))
    except (TypeError, ValueError):
        w = BOX_WIDTH
    return max(BOX_WIDTH_MIN, min(BOX_WIDTH_MAX, w))


def frame_width(st: dict) -> int:
    """Columns the whole frame occupies: the inner width plus both corners."""
    return box_width(st) + 2


def box_lines(st: dict) -> tuple[str, str]:
    style = BOX_STYLES.get(st.get("boxStyle", "rounded"), BOX_STYLES["rounded"])
    if style is None:
        return ("", "")
    tl, hz, tr, vt, bl, br = style
    inner = box_width(st)
    top = tl + hz * inner + tr
    bot = bl + hz * inner + br
    prefix = f"\x1b[38;2;{hex_to_sgr(st['boxColor'])}m" if st.get("boxColor") else ""
    suffix = "\x1b[0m" if prefix else ""
    return (f"{prefix}{top}{suffix}", f"{prefix}{bot}{suffix}")


def measure_content_width(st: dict) -> int:
    """Longest rendered module row, in columns (0 when it cannot be measured).

    The frame has to be at least this wide, or the longest row hangs outside the
    border. fastfetch is asked for the base config with `--logo none` so the row
    widths are module text alone, and `--pipe` so it prints without needing a
    terminal. Rows that are nothing but border are skipped on purpose: the frame
    must not be measured against itself, or it would grow a column every Apply.
    """
    if not FF_EXE.exists() or not CONFIG_PATH.exists():
        return 0
    try:
        proc = subprocess.run(
            [str(FF_EXE), "--config", str(CONFIG_PATH), "--logo", "none", "--pipe"],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=20, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    except Exception:
        return 0
    widest = 0
    for line in (proc.stdout or "").splitlines():
        plain = ANSI_RE.sub("", line).rstrip()
        stripped = plain.strip()
        if not stripped or all(ch in BORDER_CHARS for ch in stripped):
            continue
        widest = max(widest, len(plain))
    return widest


def fitted_box_width(st: dict) -> int:
    """Frame width that contains the rendered rows, never narrower than the default.

    Only ever grows: a machine whose module rows are wide (a long GPU name, say)
    needs a wider frame to contain them, but there is no reason to draw a frame
    narrower than the design width just because this machine's rows are short -
    and shrinking it would make the fetch jump around between machines.
    """
    measured = measure_content_width(st)
    if measured <= 0:
        return box_width(st)
    return max(BOX_WIDTH, min(BOX_WIDTH_MAX, measured + 2))


def build_config_json(st: dict, accent: str) -> dict:
    """Fastfetch config as a Python dict (JSONC-ready)."""
    top, bot = box_lines(st)
    custom = st.get("boxStyle", "rounded") != "none"
    mods: list = []
    if top:
        mods.append({"type": "custom", "format": top})
    mods += [
        # {2} {3} is vendor + model. {1} is the chassis type ("Convertible",
        # "Notebook"), which the model name usually already states - and it was
        # the longest row in the fetch (53 columns), so the frame had to be
        # wider than most windows to contain it.
        {"type": "chassis", "key": "Chassis", "format": "{2} {3}",
         "keyColor": st["groups"]["os"]},
        {"type": "os", "key": "OS", "format": "{2}", "keyColor": st["groups"]["os"]},
        {"type": "kernel", "key": "Kernel", "format": "{2}", "keyColor": st["groups"]["os"]},
        {"type": "packages", "key": "Packages", "keyColor": st["groups"]["pkg"]},
        {"type": "display", "key": "Display", "format": "{1}x{2} @ {3}Hz [{7}]",
         "keyColor": st["groups"]["pkg"]},
        {"type": "terminal", "key": "Terminal", "keyColor": st["groups"]["term"]},
        {"type": "wm", "key": "WM", "format": "{2}", "keyColor": st["groups"]["term"]},
    ]
    if bot:
        mods.append({"type": "custom", "format": bot})
    mods += [
        "break",
        # NO_KEY (a lone space) is fastfetch's "no key" sentinel: the title row
        # then draws as a bare "user @ host" with no label and no separator.
        {"type": "title", "key": NO_KEY, "format": "{6} {7} {8}",
         "keyColor": st["groups"]["title"]},
    ]
    if top:
        mods.append({"type": "custom", "format": top})
    mods += [
        # {1} alone. "{1} @ {7}" (the boost clock) made this the widest row in
        # the fetch - wider than the frame - and a row wider than the frame has
        # to come out of the room the logo gets. The frame is a fixed rule and
        # the logo is drawn beside it, so the narrower the rows, the more of the
        # picture survives on a small screen.
        {"type": "cpu", "format": "{1}", "key": "CPU",
         "keyColor": st["groups"]["cpu"]},
        # {2} alone: {1} is the vendor, which the device name already starts
        # with, so "{1} {2}" printed "Intel Intel(R) Arc(TM) ...".
        {"type": "gpu", "format": "{2}", "key": "GPU",
         "keyColor": st["groups"]["cpu"]},
        {"type": "gpu", "format": "{3}", "key": "GPU Driver",
         "keyColor": st["groups"]["drv"]},
        {"type": "memory", "key": "Memory", "keyColor": st["groups"]["drv"]},
        {"type": "disk", "key": "OS Age", "folders": "/", "keyColor": st["groups"]["drv"],
         "format": "{days} days"},
        {"type": "uptime", "key": "Uptime", "keyColor": st["groups"]["drv"]},
    ]
    if bot:
        mods.append({"type": "custom", "format": bot})
    if st.get("colorsBlock", True):
        mods += [
            {"type": "colors", "paddingLeft": 2, "symbol": "circle"},
        ]
    mods.append("break")
    return {
        "$schema": "https://github.com/fastfetch-cli/fastfetch/raw/dev/doc/json_schema.json",
        # NOTE: no "logo" key here on purpose - fastfetch gives the config's
        # logo section precedence over --logo on the command line, so any
        # logo entry (even "none") would suppress the launcher's image logo.
        "display": {
            "separator": st.get("separator", " : "),
            # Icons come from fastfetch's own per-type keyIcon default rather
            # than a glyph baked into each key string - see KEY_ICON_MODE.
            # paddingLeft is the old hand-typed "   " indent, done properly.
            "key": {"type": KEY_ICON_MODE, "paddingLeft": KEY_PADDING_LEFT},
        },
        "modules": mods,
    }


def dump_jsonc(cfg: dict) -> str:
    return json.dumps(cfg, indent=2)


# --------------------------------------------------------------------------- themes
def shift_color(hex_color: str, hue_delta: float, sat_mul: float, val_mul: float) -> str:
    h = hex_color.lstrip("#")
    if len(h) != 6:
        return hex_color
    r, g, b = (int(h[i:i + 2], 16) / 255.0 for i in (0, 2, 4))
    hue, lig, sat = colorsys.rgb_to_hls(r, g, b)
    hue = (hue + hue_delta / 360.0) % 1.0
    lig = max(0.0, min(1.0, lig * val_mul))
    sat = max(0.0, min(1.0, sat * sat_mul))
    r2, g2, b2 = colorsys.hls_to_rgb(hue, lig, sat)
    return "#{:02x}{:02x}{:02x}".format(round(r2 * 255), round(g2 * 255), round(b2 * 255))


def palettes_for(base: dict) -> list[dict]:
    """8 palettes: exact base + 7 hue rotations around the accent color.

    The returned dicts always contain an 'accent' key even when the input
    lacks one (derived from title/os, else the default) - callers rely on it.
    """
    base = dict(base)
    if not base.get("accent"):
        base["accent"] = base.get("title") or base.get("os") or "#e04a3f"
    out = [dict(base)]
    accent = base["accent"]
    for deg, sm, vm in ((40, 1.0, 1.0), (80, 1.0, 1.0), (140, 1.0, 1.0),
                        (180, 1.0, 1.0), (220, 1.0, 1.0), (280, 0.9, 1.05), (320, 1.1, 0.95)):
        out.append({k: shift_color(v, deg, sm, vm) for k, v in base.items()})
    return out

# --------------------------------------------------------------------------- apply
def generate_theme_files(st: dict) -> list[Path]:
    """Write themes\\theme-XX.jsonc (one per palette). Returns written paths."""
    THEMES_DIR.mkdir(parents=True, exist_ok=True)
    for old in THEMES_DIR.glob("theme-*.jsonc"):
        old.unlink()
    written = []
    for i, pal in enumerate(palettes_for(st["groups"]), start=1):
        cfg = build_config_json({**st, "groups": pal}, pal["accent"])
        p = THEMES_DIR / f"theme-{i:02d}.jsonc"
        p.write_text(dump_jsonc(cfg), "utf-8")
        written.append(p)
    return written


# --------------------------------------------------------------------------- resize guard
#
# Windows has no SIGWINCH. The only resize signal the console API offers is a
# WINDOW_BUFFER_SIZE_EVENT read out of the input buffer (microsoft/terminal#305),
# and in an interactive PowerShell that buffer belongs to PSReadLine - so a
# resize cannot be subscribed to, only noticed later. The prompt is that later:
# it runs between commands, which is when the grid the fetch was drawn at can be
# compared with the grid now.
#
# Why a redraw rather than "drawing it better": the logo is a raster image
# anchored to the cells it was drawn over. Alt+Enter (Windows Terminal's
# toggleFullscreen), F11, dragging an edge and Ctrl+scroll zooming the font all
# change that grid, and the terminal then reflows the text around the image,
# which comes apart into the bands that look like a broken logo. The fetch's own
# text reflows harmlessly; the picture cannot. Erasing the cells is the only way
# to take an image out of the buffer, so the repair is: erase the viewport, then
# draw the same fetch again - same theme, same picture, re-fitted to the new
# window.
#
# This block is shared verbatim by fastfetch-random.ps1 and gui\preview.ps1
# (both generated by this app) so the two can never drift apart. Each file
# supplies its own Get-FFSGrid / Invoke-FFSRun / Show-FFSFetch.
RESIZE_GUARD_TEMPLATE = r"""# --- keep the fetch straight when the window is resized ---------------------
# There is no resize event to subscribe to on Windows (see Get-FFSGrid), so the
# prompt is where a resize is noticed: it compares the grid this fetch was drawn
# at with the grid now, and on a difference erases the torn copy and draws the
# fetch again at the new size.
function global:Repair-FFSFetch {
    param([hashtable]$Plan)
    # Take the torn copy off the screen before drawing a new one - and for a
    # sixel that means *scrolling it away*, not clearing. Measured on Windows
    # Terminal 1.24: [Console]::Clear() and CSI 2J both leave a sixel image
    # sitting on the screen (it lives in its own layer, not in the text
    # buffer), while scrolling the lines it was drawn on out of the viewport
    # removes it completely. v1.1.15 cleared first, so every resize left the
    # broken picture behind and painted another one over it - which is exactly
    # the banded, doubled logo the repair was supposed to fix.
    $rows = [int]$Plan.Rows
    if ($rows -le 0) { $rows = 40 }
    [Console]::Write(("`n" * ($rows + 2)))
    try { [Console]::Clear() } catch { }
    [Console]::Write("`e[3J`e[1;1H")
    Show-FFSFetch -Plan $Plan
}

function global:Redraw-Fetch {
    # Draw the fetch again, right now. Typing this is the impatient version of
    # waiting for the next prompt (which repairs it on its own).
    if ($global:FFSPlan) { Repair-FFSFetch -Plan $global:FFSPlan }
}

function global:Install-FFSResizeGuard {
    if ($global:FFSGuardInstalled) { return }
    $grid = Get-FFSGrid
    if ([int]$grid[0] -le 0) { return }   # no console to resize - nothing to watch
    $global:FFSGuardInstalled = $true
    if (-not $global:FFSPrevPrompt) {
        # Keep whatever prompt is already there (oh-my-posh, a hand-written one)
        # and put it back in front afterwards, so this adds a check instead of
        # replacing the user's prompt. Never capture a guard's own wrapper: it
        # chains through the same global, so the two would call each other until
        # the call stack ran out.
        $prev = $function:prompt
        if ($prev -and $prev.ToString() -notmatch 'FFSPlan') { $global:FFSPrevPrompt = $prev }
    }
    function global:prompt {
        try {
            $plan = $global:FFSPlan
            if ($plan -and $plan.Redraw) {
                $now = Get-FFSGrid
                if ([int]$now[0] -gt 0 -and
                    ([int]$now[0] -ne [int]$plan.Grid[0] -or [int]$now[1] -ne [int]$plan.Grid[1])) {
                    Repair-FFSFetch -Plan $plan
                }
            }
        } catch { }
        if ($global:FFSPrevPrompt) { & $global:FFSPrevPrompt }
        else { "PS $($executionContext.SessionState.Path.CurrentLocation)$('>' * ($nestedPromptLevel + 1)) " }
    }
}
"""


LAUNCHER_TEMPLATE = r"""# >>> FastFetch Studio :: launcher >>>
# Generated by FastFetch Studio - random logo + theme each run.
# Rewritten by "Apply & Generate" in the app; hand edits will be lost.
#
# Runs on every shell start, so it must be quiet on success and only speak up
# when something is actually wrong.
$ErrorActionPreference = 'SilentlyContinue'

# --- draw once per shell session -------------------------------------------
# A profile can end up with two fastfetch calls: setup.ps1's managed block, and
# the documented snippet pasted in afterwards without removing it. Both run, so
# the fetch is printed twice, each with its own random theme - which looks like
# a broken launcher. The marker is a global variable rather than an environment
# variable on purpose: $env: is inherited by child processes, so a nested shell
# would inherit it and draw nothing at all.
if ($global:FastFetchStudioDrawn) { return }
$global:FastFetchStudioDrawn = $true

# --- where things live -----------------------------------------------------
# $env:USERPROFILE is normally set, but it is missing in some hosts (services,
# scheduled tasks, a shell started under a different profile). Fall back rather
# than silently reading the wrong home directory.
$ffHome = $env:USERPROFILE
if (-not $ffHome) { $ffHome = $env:HOME }
if (-not $ffHome) { $ffHome = [Environment]::GetFolderPath('UserProfile') }

$ffRoot = Join-Path $ffHome '.config\fastfetch'
# Normal case is the documented location. If that folder does not exist but the
# script was started from somewhere that does hold the config, use that instead
# ($PSScriptRoot is empty when the file is dot-sourced or piped into iex).
if (-not (Test-Path -LiteralPath $ffRoot) -and $PSScriptRoot) { $ffRoot = $PSScriptRoot }

# --- locate fastfetch ------------------------------------------------------
# Resolve to a real file so a failure can name the paths it tried. The PATH
# lookup goes through Get-Command because that honours PATHEXT, which is what
# the call operator below actually requires.
$ffExe  = $null
$ffTried = @((Join-Path $ffHome '.local\bin\fastfetch.exe'),
             (Join-Path $ffRoot 'fastfetch.exe'))
foreach ($cand in $ffTried) {
    if (Test-Path -LiteralPath $cand -PathType Leaf) { $ffExe = $cand; break }
}
if (-not $ffExe) {
    $hit = Get-Command 'fastfetch.exe' -CommandType Application -ErrorAction SilentlyContinue |
           Select-Object -First 1
    if ($hit) { $ffExe = $hit.Source }
}
if (-not $ffExe) {
    # The old version swallowed this and simply did nothing, which looked like
    # a broken install with no explanation.
    Write-Host 'FastFetch Studio: fastfetch.exe was not found, so nothing was drawn.' -ForegroundColor Yellow
    Write-Host ('  looked in : ' + ($ffTried -join '  |  ')) -ForegroundColor DarkGray
    Write-Host '  looked on : PATH' -ForegroundColor DarkGray
    Write-Host '  fix       : re-run setup.ps1 (it installs into %USERPROFILE%\.local\bin)' -ForegroundColor DarkGray
    return
}

$themes = @(Get-ChildItem -LiteralPath (Join-Path $ffRoot 'themes') -Filter 'theme-*.jsonc' -File -ErrorAction SilentlyContinue)
$sixels = @(Get-ChildItem -LiteralPath (Join-Path $ffRoot 'sixels') -Filter '*.sixel' -File -ErrorAction SilentlyContinue |
           Where-Object { $_.BaseName -notmatch '-\d+x\d+$' })     # the ladder, not the pictures
$arts   = @(Get-ChildItem -LiteralPath (Join-Path $ffRoot 'arts') -Filter '*.art' -File -ErrorAction SilentlyContinue |
           Where-Object { $_.BaseName -notmatch '-\d+x\d+$' })
$pngDirs = @((Join-Path $ffRoot 'pngs'), (Join-Path $ffRoot 'images'))
$pngs    = @($pngDirs | ForEach-Object { Get-ChildItem -LiteralPath $_ -Filter '*.png' -File -Recurse -ErrorAction SilentlyContinue } | Sort-Object FullName -Unique)
$statePath = Join-Path $ffRoot 'gui\studio-state.json'

$randLogo = $true; $randTheme = $true; $freq = 'every'; $w = @@W@@; $h = @@H@@; $defaultImg = ''
$logoMode = 'auto'
# Baked in, then overridden from the state file below - like the logo size, this
# is what a launcher that has never met a state file falls back to.
$redrawOnResize = @@REDRAW@@
if (Test-Path -LiteralPath $statePath) {
    try {
        $s = Get-Content -LiteralPath $statePath -Raw | ConvertFrom-Json
        # Guard each field: a missing key used to become 0 / $false and quietly
        # disable randomisation or collapse the logo size.
        if ($null -ne $s.randomLogo)  { $randLogo  = [bool]$s.randomLogo }
        if ($null -ne $s.randomTheme) { $randTheme = [bool]$s.randomTheme }
        if ($s.frequency) { $freq = [string]$s.frequency }
        if ($s.logWidth)  { $w = [int]$s.logWidth }
        if ($s.logHeight) { $h = [int]$s.logHeight }
        if ($s.defaultImage) { $defaultImg = [string]$s.defaultImage }
        if ($s.logoMode) { $logoMode = [string]$s.logoMode }
        if ($null -ne $s.redrawOnResize) { $redrawOnResize = [bool]$s.redrawOnResize }
    } catch {}
}

# --- 'once per day' --------------------------------------------------------
$stamp = (Get-Date).ToString('yyyyMMdd')
$flagPath = Join-Path $ffRoot 'gui\.last-random-run'
if ($freq -eq 'daily' -and (Test-Path -LiteralPath $flagPath)) {
    $last = Get-Content -LiteralPath $flagPath -Raw -ErrorAction SilentlyContinue
    if ($last -and $last.Trim() -eq $stamp) { return }
}

$themeArg = @()
$accent = ''
if ($randTheme -and $themes.Count -gt 0) {
    $theme = Get-Random -InputObject $themes
    $themeArg = @('--config', $theme.FullName)
    try {
        $cfg = Get-Content -LiteralPath $theme.FullName -Raw | ConvertFrom-Json
        $accent = ($cfg.modules | Where-Object { $_.type -eq 'os' } | Select-Object -First 1).keyColor
    } catch {}
}

$logoArg = @()
$wantImage = $true
$ffHost = 'this terminal'
$ffSixel = $false
$ffKitty = $false

# Windows Terminal sets WT_SESSION in the shells it starts - but not in every
# session it *shows*. When it is the default terminal application, a console
# created by some other process is merely displayed by WT and WT never gets to
# add the variable; an elevated shell, or one started by a tool that rebuilds
# the environment, loses it as well. The terminal is still Windows Terminal in
# all of those cases, and fastfetch - which identifies its host from the process
# tree rather than the environment - says so. An env-only check therefore made
# the launcher disagree with the fetch it was drawing: the same terminal got the
# crisp sixel logo in one window and the block-art fallback in another. Ask the
# same question the same way.
function Test-WindowsTerminalHost {
    $proc = Get-CimInstance Win32_Process -Filter "ProcessId=$PID" -ErrorAction SilentlyContinue
    $depth = 0
    while ($proc -and $depth -lt 8) {
        if ($proc.Name -like 'WindowsTerminal*') { return $true }
        if (-not $proc.ParentProcessId) { return $false }
        $proc = Get-CimInstance Win32_Process -Filter ("ProcessId=" + $proc.ParentProcessId) -ErrorAction SilentlyContinue
        $depth++
    }
    return $false
}

function global:Get-TextLogoWidth {
    # How many columns a text logo file takes on screen, ANSI colours excluded.
    # fastfetch prints such a file verbatim - --logo-width has no effect on it -
    # so measuring the file is the only way to know whether it fits beside the
    # frame before it is drawn.
    #
    # Global, like the rest of the draw helpers: the resize guard calls this long
    # after the script's own scope has gone, and a script-scoped function is not
    # there any more.
    param([string]$Path)
    $widest = 0
    try {
        foreach ($line in [IO.File]::ReadAllLines($Path)) {
            $plain = [Text.RegularExpressions.Regex]::Replace($line, '\x1b\[[0-9;]*m', '')
            $plain = $plain.TrimEnd()
            if ($plain.Length -gt $widest) { $widest = $plain.Length }
        }
    } catch { }
    return $widest
}

function global:Get-FFSCellPx {
    # Pixel size of one cell, as the sixels in sixels\ were encoded for: the
    # terminal's measured size when it has one, the encoder's default otherwise.
    # Baked in at Apply time, so the launcher and the encoder can never drift.
    param([hashtable]$Plan)
    $cw = @@CELLW@@
    $ch = @@CELLH@@
    if ($Plan -and [int]$Plan.CellW -gt 0 -and [int]$Plan.CellH -gt 0) {
        $cw = [int]$Plan.CellW
        $ch = [int]$Plan.CellH
    }
    if ($cw -le 0) { $cw = 10 }
    if ($ch -le 0) { $ch = 20 }
    return @($cw, $ch)
}

function global:Get-FFSRasterSize {
    # The pixel size a sixel declares in its raster attributes ("1;1;W;H), which
    # is the size the terminal places it at. 0,0 when the file is missing or is
    # not a sixel, and the caller then draws no logo rather than guess one.
    param([string]$Path)
    if (-not $Path -or -not (Test-Path -LiteralPath $Path -PathType Leaf)) { return @(0, 0) }
    try {
        $fs = [IO.File]::OpenRead($Path)
        try {
            $head = New-Object byte[] 64
            $n = $fs.Read($head, 0, 64)
        } finally { $fs.Dispose() }
        $txt = -join ($head[0..([Math]::Max(0, $n - 1))] | ForEach-Object { [char]$_ })
        $m = [Text.RegularExpressions.Regex]::Match($txt, '"1;1;(\d+);(\d+)')
        if ($m.Success) { return @([int]$m.Groups[1].Value, [int]$m.Groups[2].Value) }
    } catch { }
    return @(0, 0)
}

# --- can this terminal draw an image, and how? ------------------------------
# Sending image bytes to a terminal that cannot render them prints garbage, so
# capability is detected rather than assumed. Getting this list wrong is what
# made a chosen logo silently turn into the built-in ASCII one.
#   sixel - Windows Terminal / OpenConsole (WT 1.22+), WezTerm, foot, contour,
#           mlterm, yaft
#   kitty - kitty, WezTerm, Ghostty
# Classic conhost draws neither. It is what you get from a bare powershell.exe
# outside Windows Terminal, a scheduled task, or a redirected run.
# VS Code and Zed are named only so the fallback can say where it happened.
# They ship image rendering off (VS Code needs terminal.integrated.gpuAcceleration),
# so claiming capability there would trade a visible logo for no logo at all.
if ($env:WT_SESSION) {
    $ffSixel = $true; $ffHost = 'Windows Terminal'
} elseif ($env:TERM_PROGRAM -eq 'Windows_Terminal') {
    $ffSixel = $true; $ffHost = 'Windows Terminal'
} elseif ($env:WEZTERM_PANE -or $env:TERM_PROGRAM -eq 'WezTerm') {
    $ffSixel = $true; $ffKitty = $true; $ffHost = 'WezTerm'
} elseif ($env:KITTY_WINDOW_ID -or $env:TERM -match 'kitty') {
    $ffKitty = $true; $ffHost = 'kitty'
} elseif ($env:TERM_PROGRAM -eq 'ghostty') {
    $ffKitty = $true; $ffHost = 'Ghostty'
} elseif ($env:TERM -match 'foot|contour|mlterm|yaft') {
    $ffSixel = $true; $ffHost = [string]$env:TERM
} elseif ($env:TERM_PROGRAM -eq 'vscode') {
    $ffHost = 'the VS Code terminal'
} elseif ($env:TERM_PROGRAM -eq 'zed') {
    $ffHost = 'the Zed terminal'
} elseif (Test-WindowsTerminalHost) {
    # Deliberately after the terminals that name themselves: an editor opened
    # *from* a Windows Terminal tab keeps WindowsTerminal.exe in its process
    # tree, and its own terminal still cannot draw sixel.
    $ffSixel = $true; $ffHost = 'Windows Terminal'
} elseif ($env:TERM_PROGRAM) {
    $ffHost = [string]$env:TERM_PROGRAM
}

# --- the user gets the last word -------------------------------------------
# Detection cannot cover every terminal, so this overrides it:
#   image   - draw the picture even when the terminal was not recognised
#   builtin - never draw it, always the tinted ASCII logo
# Comes from "Logo rendering" in the app; $env:FASTFETCH_STUDIO_LOGO wins over
# it so a single shell can be tested without touching the saved setting.
$ffLogoMode = $logoMode
if (-not $ffLogoMode) { $ffLogoMode = 'auto' }
if ($env:FASTFETCH_STUDIO_LOGO) { $ffLogoMode = [string]$env:FASTFETCH_STUDIO_LOGO }
if ($ffLogoMode -eq 'builtin') {
    $wantImage = $false
} elseif ($ffLogoMode -eq 'image') {
    # Keep whatever was detected; if nothing was, try the pre-encoded sixel
    # first (that is the format the app writes) and kitty-direct after it.
    if (-not $ffSixel -and -not $ffKitty) { $ffSixel = $true; $ffKitty = $true }
}

# --- the window, and one place that draws ----------------------------------
# Everything the draw needs is read from $global:FFSPlan rather than from this
# script's variables, because the launcher is a script: its scope is gone by the
# time the resize guard redraws at the next prompt.
function global:Get-FFSLogoFit {
    # The largest pre-encoded size of this picture the window can hold, as a
    # hashtable of W, H, Path and Px - or $null when even the smallest does not
    # fit.
    #
    # Every size in the ladder was encoded from the same image against the
    # terminal's measured cell size, so a variant's raster is exactly its cell
    # box. Picking a file keeps that true; rewriting --logo-width to whatever the
    # window allowed (what v1.1.15 did) broke it, and the fetch's text then
    # landed in the picture's own columns - which is what the terminal reacts to
    # by redrawing the picture in bands.
    #
    # Rows matter as much as columns: a 24-row picture in a 20-row window can
    # only be drawn by scrolling, and a scrolled sixel is exactly the torn one.
    param([string]$Path, [string]$Dir, [hashtable]$Plan, [int]$Room, [int]$Rows)
    if (-not $Path -or -not $Dir) { return $null }
    $cell = Get-FFSCellPx -Plan $Plan
    $base = [IO.Path]::GetFileNameWithoutExtension($Path)
    $files = @(Get-Item -LiteralPath $Path -ErrorAction SilentlyContinue)
    $files += @(Get-ChildItem -LiteralPath $Dir -Filter ($base + '-*x*.sixel') -File -ErrorAction SilentlyContinue)
    $best = $null
    foreach ($f in $files) {
        $px = @(Get-FFSRasterSize -Path $f.FullName)
        if ($px[0] -le 0 -or $px[1] -le 0) { continue }
        $w = [int][Math]::Ceiling($px[0] / [double]$cell[0])
        $h = [int][Math]::Ceiling($px[1] / [double]$cell[1])
        if ($w -gt $Room -or $h -gt $Rows) { continue }
        if ($null -eq $best -or ($w * $h) -gt ([int]$best.W * [int]$best.H)) {
            $best = @{ W = $w; H = $h; Path = $f.FullName; Px = $px }
        }
    }
    return $best
}

function global:Get-FFSArtFit {
    # The same choice for the block art: a text logo cannot tear, so it is what a
    # window too narrow or too short for the picture gets. The full-size file is
    # a candidate like any other - it is measured (that is what makes it drawable
    # at all) and the variants carry their size in the name, written by the
    # encoder from the very cells it rendered.
    param([string]$Path, [string]$Dir, [int]$Room, [int]$Rows)
    if (-not $Path -or -not $Dir) { return $null }
    $base = [IO.Path]::GetFileNameWithoutExtension($Path)
    $files = @(Get-Item -LiteralPath $Path -ErrorAction SilentlyContinue)
    $files += @(Get-ChildItem -LiteralPath $Dir -Filter ($base + '-*x*.art') -File -ErrorAction SilentlyContinue)
    $best = $null
    foreach ($f in $files) {
        $w = 0; $h = 0
        if ($f.BaseName -match '-(\d+)x(\d+)$') {
            $w = [int]$Matches[1]; $h = [int]$Matches[2]
        } else {
            $w = Get-TextLogoWidth -Path $f.FullName
            $h = @([IO.File]::ReadAllLines($f.FullName)).Count
        }
        if ($w -le 0 -or $h -le 0 -or $w -gt $Room -or $h -gt $Rows) { continue }
        if ($null -eq $best -or ($w * $h) -gt ([int]$best.W * [int]$best.H)) {
            $best = @{ W = $w; H = $h; Path = $f.FullName }
        }
    }
    return $best
}

function global:Get-FFSGrid {
    # The current grid as @(columns, rows). There is no resize event to
    # subscribe to on Windows - ReadConsoleInput's WINDOW_BUFFER_SIZE_EVENT is
    # the only signal, and PSReadLine owns the input buffer - so the size is
    # polled, and the answer changing is how a resize is noticed.
    $c = 0; $r = 0
    try { $c = [int][Console]::WindowWidth; $r = [int][Console]::WindowHeight } catch { }
    if ($c -le 0) {
        try { $c = [int]$Host.UI.RawUI.WindowSize.Width; $r = [int]$Host.UI.RawUI.WindowSize.Height } catch { }
    }
    return @($c, $r)
}

function global:Invoke-FFSRun {
    # Run fastfetch exactly ONCE, with at most one fallback - an image attempt
    # can fail on a terminal that cannot draw one, and the built-in logo is the
    # retry. The previous version called it twice on a successful run:
    # $LASTEXITCODE is $null when the call never reached a native command (the
    # exe vanished between the Test-Path and the call, say), and "$null -ne 0"
    # is TRUE.
    param([hashtable]$Plan, [string[]]$FFArgs, [string[]]$FFRetry)
    $attempts = @(,$FFArgs)
    if ($FFRetry) { $attempts += ,$FFRetry }
    foreach ($a in $attempts) {
        & $Plan.Exe @a
        $ffRc = $LASTEXITCODE
        if ($null -eq $ffRc -or $ffRc -eq 0) { return }
    }
}

function global:Show-FFSFetch {
    # Draw the plan against the window that is there *now*. The fit is
    # recomputed on every draw instead of being captured, because a repair runs
    # after the window changed size - that is the whole point of it.
    #
    # The logo is drawn beside a frame that is a fixed number of columns wide,
    # so the two together are wider than most windows. fastfetch does not know
    # the frame is there: when the terminal is narrower than the pair it wraps
    # the line, the frame's border lands on the next row and the whole fetch
    # falls apart - which is what a small screen or a tiled window looked like.
    # Give the logo only what is left after the frame. That used to mean
    # rewriting --logo-width/--logo-height with whatever the window allowed,
    # which is a lie: those two flags only say how many cells the picture
    # occupies, so the picture stayed where it was and the text was laid out on
    # top of it. The size now comes from the file (Get-FFSRasterSize) and a
    # window too narrow for it gets the block art instead.
    #
    # @@LOGOGAP@@ of those columns are fastfetch's own logo padding, which sits
    # between the logo and the text and is easy to forget: without reserving it
    # the pair "fits" on paper while the frame's right corner is still pushed
    # three columns off the edge.
    param([hashtable]$Plan)
    $grid = Get-FFSGrid
    if ([int]$grid[0] -gt 0) { $Plan.Grid = @([int]$grid[0], [int]$grid[1]) }
    $cols = [int]$Plan.Grid[0]
    if ($cols -le 0) { $cols = 80 }
    $rows = [int]$Plan.Grid[1]
    if ($rows -le 0) { $rows = 24 }
    # How far the repair has to scroll to be rid of the picture - the whole
    # window is clearer than a few lines, and it is what takes a sixel off the
    # screen (clearing the text buffer does not).
    $Plan.Rows = $rows
    # One row is left for the prompt: a picture on the last row is only kept by
    # scrolling, and a scrolled sixel is the torn one.
    $rowRoom = $rows - 1
    $room = $cols - [int]$Plan.Band - 1 - [int]$Plan.Gap
    $logo = @()
    $retry = @()
    $w = 0
    $h = 0
    if ($Plan.Kind -eq 'sixel' -or $Plan.Kind -eq 'kitty') {
        # Which *file* to draw, and what to tell fastfetch about it.
        #
        # A sixel is placed by its raster size, so the picture's cell count is
        # read out of the file that is about to be drawn and passed on unchanged:
        # --logo-width does not scale an image, it only says how much room to
        # leave beside it, and a number that disagrees with the raster is what
        # tore the logo in the first place.
        #
        # Fitting therefore means picking a smaller file, not rewriting the size.
        # The ladder holds the same picture encoded at several cell sizes; the
        # largest one this window can hold wins.
        $fit = $null
        if ($Plan.Kind -eq 'sixel') {
            $fit = Get-FFSLogoFit -Path $Plan.Logo -Dir $Plan.SixelsDir `
                                  -Plan $Plan -Room $room -Rows $rowRoom
        } else {
            # kitty-direct hands the *PNG* to the terminal, which draws it into
            # the cell box fastfetch declares - so here the declaration is the
            # size, and the file's own raster is not consulted: a PNG declares
            # none (read as a sixel it measured 0x0, and the logo vanished from
            # every kitty and WezTerm window). The box is the configured one,
            # scaled down when the room the frame leaves cannot hold it, and
            # dropped when there is no room at all - a picture wider than the
            # window is what wrapped the fetch over itself.
            $w = [int]$Plan.W
            $h = [int]$Plan.H
            if ($w -gt 0 -and $h -gt 0) {
                if ($w -gt $room -or $h -gt $rowRoom) {
                    if ($room -lt 10 -or $rowRoom -lt 4) {
                        $w = 0; $h = 0
                    } else {
                        $scale = [Math]::Min($room / [double]$w, $rowRoom / [double]$h)
                        $w = [int][Math]::Max(4, [Math]::Round($w * $scale))
                        $h = [int][Math]::Max(4, [Math]::Round($h * $scale))
                    }
                }
                if ($w -gt 0 -and $h -gt 0) {
                    $fit = @{ W = $w; H = $h; Path = $Plan.Logo }
                }
            }
        }
        if (-not $fit) {
            # Nothing in the picture's ladder fits. The block art is the same
            # picture at block resolution - pure text, so it cannot tear - and
            # when even that is too wide the frame is drawn on its own.
            $w = 0
            $art = Get-FFSArtFit -Path $Plan.Art -Dir $Plan.ArtsDir -Room $room -Rows $rowRoom
            if ($art) { $logo = @('--logo', $art.Path) }
            else { $logo = @('--logo', 'none') }
        } elseif ($Plan.Kind -eq 'sixel') {
            $w = [int]$fit.W
            $h = [int]$fit.H
            # 'raw' passes the pre-encoded bytes straight through.
            # --logo-print-remaining false because fastfetch otherwise pads the
            # logo out to logo-height lines when the key list is shorter than
            # the picture, and those blank lines are written over the picture's
            # own rows - the other half of the banding.
            $logo  = @('--logo-type', 'raw', '--logo', $fit.Path,
                       '--logo-width', $w, '--logo-height', $h,
                       '--logo-print-remaining', 'false')
            $retry = @('--logo', 'Windows11')
        } else {
            $w = [int]$fit.W
            $h = [int]$fit.H
            $logo  = @('--logo-type', 'kitty-direct', '--logo', $fit.Path,
                       '--logo-width', $w, '--logo-height', $h)
            $retry = @('--logo', 'Windows11')
        }
    } elseif ($Plan.Kind -eq 'art') {
        # fastfetch prints a *text* logo file verbatim - --logo-width has no
        # effect on it - so the block art is drawn only when it fits, and
        # nothing is drawn when it does not rather than fall back to a *wider*
        # logo: the built-in ASCII one is 40 columns and would be worse. The
        # ladder means a narrow window gets a smaller render instead of nothing.
        $art = Get-FFSArtFit -Path $Plan.Logo -Dir $Plan.ArtsDir -Room $room -Rows $rowRoom
        if ($art) { $logo = @('--logo', $art.Path) }
        else { $logo = @('--logo', 'none') }
    } else {
        $logo = @('--logo', 'Windows11')    # the built-in tinted ASCII logo
    }
    $ffArgs = @()
    if ($Plan.Theme) { $ffArgs += $Plan.Theme }
    $ffArgs += $logo
    # --logo-color only tints fastfetch's own logo, so it is passed on the
    # fallback path instead of beside an image.
    if ($Plan.Kind -ne 'sixel' -and $Plan.Kind -ne 'kitty') { $ffArgs += $Plan.Color }
    $ffArgs += $Plan.Fit
    $ffRetry = @()
    if ($retry.Count -gt 0) {
        if ($Plan.Theme) { $ffRetry += $Plan.Theme }
        $ffRetry += $retry
        $ffRetry += $Plan.Color
        $ffRetry += $Plan.Fit
    }
    Invoke-FFSRun -Plan $Plan -FFArgs $ffArgs -FFRetry $ffRetry
    # What this draw actually did: the window it found, the room it had, the file
    # it chose and the size it told fastfetch. Written every time, so "my logo is
    # missing" (or smaller than it used to be) can be explained from a file
    # instead of from a screenshot - and so a test can check the choice without
    # reading pixels.
    try {
        $pick = 'none'
        if ($logo.Count -gt 1) { $pick = [IO.Path]::GetFileName([string]$logo[1]) }
        $how = 'none'
        if ($w -gt 0) { $how = 'picture' }
        elseif ($pick -like '*.art') { $how = 'block art' }
        Set-Content -LiteralPath (Join-Path $Plan.GuiDir 'last-draw.txt') -Encoding UTF8 -Value @(
            ('when  : ' + (Get-Date).ToString('s')),
            ('window: ' + $cols + ' x ' + $rows + ' cells'),
            ('room  : ' + $room + ' columns, ' + $rowRoom + ' rows'),
            ('drawn : ' + $how + ' (' + $pick + ')'),
            ('cells : ' + $(if ($w -gt 0) { '' + $w + ' x ' + $h } else { '-' }))
        )
    } catch { }
    # Say why the picture is missing - but only when one was clearly expected (a
    # pinned image with randomisation off), the same condition the fallback note
    # below uses. Otherwise every narrow window would print this, and that
    # silence is what once made "I set a logo and got a Windows logo" look like a
    # broken app.
    if ($Plan.WantImage -and -not $Plan.RandLogo -and $Plan.DefaultImg) {
        if ($w -le 0 -and ($Plan.Kind -eq 'sixel' -or $Plan.Kind -eq 'kitty')) {
            Write-Host ('FastFetch Studio: a ' + $cols + '-column window leaves no room for the logo beside the frame, so the frame was drawn on its own.') -ForegroundColor DarkGray
            Write-Host '  widen the window to bring the picture back.' -ForegroundColor DarkGray
        } elseif ($Plan.Kind -eq 'art' -and $logo[1] -eq 'none') {
            Write-Host ('FastFetch Studio: a ' + $cols + '-column window is too narrow for the logo beside the frame, so the frame was drawn on its own.') -ForegroundColor DarkGray
            Write-Host '  widen the window to bring the picture back.' -ForegroundColor DarkGray
        } elseif ($Plan.Kind -eq 'builtin') {
            Write-Host ('FastFetch Studio: no image support detected in ' + $Plan.Host + ', so the built-in logo was drawn.') -ForegroundColor DarkGray
            Write-Host '  force it: $env:FASTFETCH_STUDIO_LOGO = ''image''   (Windows Terminal 1.22+ draws it as-is)' -ForegroundColor DarkGray
        }
    }
}

# --- which logo, and which theme -------------------------------------------
# Chosen once per shell, then *recorded*: a repair after a resize redraws this
# same fetch, so neither the picture nor the palette may be re-rolled - a resize
# must never look like a new machine. Only the size is recomputed.
#
# No image protocol here means fastfetch prints a *text* logo file verbatim,
# ANSI escapes and all, so the block art FastFetch Studio pre-rendered gives the
# user their own picture at block resolution rather than a Windows logo.
# fastfetch's tinted ASCII logo is the last resort.
$ffKind = 'builtin'
$ffLogoPath = ''
# The block-art render of the same picture. It is what a window too narrow for
# the sixel gets: pure text, so it cannot be drawn wrong.
$ffArtPath = ''
if ($wantImage -and $ffSixel -and $sixels.Count -gt 0) {
    $six = $null
    if (-not $randLogo) {
        # Random logo OFF: always the chosen default image - never a random substitution.
        if ($defaultImg) {
            $stem = [IO.Path]::GetFileNameWithoutExtension($defaultImg)
            $key  = ($stem -replace '[^A-Za-z0-9]+', '-').Trim('-')
            if (-not $key) { $key = 'img' }
            $cand = Join-Path (Join-Path $ffRoot 'sixels') ($key + '.sixel')
            if (Test-Path -LiteralPath $cand) { $six = Get-Item -LiteralPath $cand }
        }
        if (-not $six) { $six = $sixels | Select-Object -First 1 }
    } else {
        $six = Get-Random -InputObject $sixels
    }
    $ffKind = 'sixel'; $ffLogoPath = $six.FullName
    $artCand = Join-Path (Join-Path $ffRoot 'arts') ($six.BaseName + '.art')
    if (Test-Path -LiteralPath $artCand) { $ffArtPath = $artCand }
} elseif ($wantImage -and $ffKitty -and $pngs.Count -gt 0) {
    $png = $null
    if (-not $randLogo -and $defaultImg -and (Test-Path -LiteralPath $defaultImg)) {
        $png = Get-Item -LiteralPath $defaultImg
    }
    if (-not $png) {
        if ($randLogo) { $png = Get-Random -InputObject $pngs }
        else { $png = $pngs | Select-Object -First 1 }
    }
    $ffKind = 'kitty'; $ffLogoPath = $png.FullName
} elseif ($wantImage -and $arts.Count -gt 0) {
    $art = $null
    if (-not $randLogo) {
        if ($defaultImg) {
            $stem = [IO.Path]::GetFileNameWithoutExtension($defaultImg)
            $key  = ($stem -replace '[^A-Za-z0-9]+', '-').Trim('-')
            if (-not $key) { $key = 'img' }
            $cand = Join-Path (Join-Path $ffRoot 'arts') ($key + '.art')
            if (Test-Path -LiteralPath $cand) { $art = Get-Item -LiteralPath $cand }
        }
        if (-not $art) { $art = $arts | Select-Object -First 1 }
    } else {
        $art = Get-Random -InputObject $arts
    }
    if ($art) { $ffKind = 'art'; $ffLogoPath = $art.FullName; $ffArtPath = $art.FullName }
}

$colorArgs = @()
if ($accent) { $colorArgs = @('--logo-color-1', $accent, '--logo-color-2', $accent) }

# fastfetch cannot know a frame is drawn beside the text, so a row longer than
# the window wraps and the frame's border lands on the next line. Refusing to
# wrap keeps every row on its own line: an over-long value is clipped at the edge
# instead of pulling the fetch apart. Without this, a window narrower than
# logo + frame wrapped every border and every long value.
$fitArgs = @('--disable-linewrap', 'true')

# --- the plan --------------------------------------------------------------
# One record of everything a draw needs, kept in a global on purpose: this file
# runs as a script, so its own scope is gone by the time the resize guard
# redraws at the next prompt.
$global:FFSPlan = @{
    Exe        = $ffExe
    Theme      = $themeArg
    Fit        = $fitArgs
    Color      = $colorArgs
    Kind       = $ffKind
    Logo       = $ffLogoPath
    Art        = $ffArtPath
    W          = $w
    H          = $h
    CellW      = @@CELLW@@
    CellH      = @@CELLH@@
    Band       = @@BOX@@
    Gap        = @@LOGOGAP@@
    WantImage  = $wantImage
    RandLogo   = $randLogo
    DefaultImg = $defaultImg
    Host       = $ffHost
    Redraw     = $redrawOnResize
    Grid       = @(0, 0)
    Rows       = 0
    # Carried in the plan rather than read from the script scope inside
    # Show-FFSFetch: the repair runs from the prompt, long after this script's
    # own scope is gone, and a script-scoped path would be empty by then.
    SixelsDir  = Join-Path $ffRoot 'sixels'
    ArtsDir    = Join-Path $ffRoot 'arts'
    GuiDir     = Join-Path $ffRoot 'gui'
}

@@RESIZEGUARD@@

# Draw it, then keep it straight: a resize that comes later is noticed by the
# prompt, which redraws this same fetch at the new size.
Show-FFSFetch -Plan $global:FFSPlan
if ($redrawOnResize) { Install-FFSResizeGuard }

# Remember the day. Without this the 'daily' check above could never match, so
# "Once per day (same look all day)" silently behaved like "every window".
if ($freq -eq 'daily') {
    try {
        $guiDir = Join-Path $ffRoot 'gui'
        if (-not (Test-Path -LiteralPath $guiDir)) {
            New-Item -ItemType Directory -Path $guiDir -Force | Out-Null
        }
        Set-Content -LiteralPath $flagPath -Value $stamp -Encoding ASCII -ErrorAction Stop
    } catch {}
}
# <<< FastFetch Studio :: launcher <<<
"""


def generate_launcher(st: dict) -> None:
    # Ensure the folder exists: the Random tab regenerates the launcher on every
    # toggle, which can be the first thing that ever writes into ~\.config\fastfetch.
    LAUNCHER_PATH.parent.mkdir(parents=True, exist_ok=True)
    text = LAUNCHER_TEMPLATE.replace("@@W@@", str(int(st.get("logWidth", 28)))) \
                            .replace("@@H@@", str(int(st.get("logHeight", 24)))) \
                            .replace("@@CELLW@@", str(logo_cell_px(st)[0])) \
                            .replace("@@CELLH@@", str(logo_cell_px(st)[1])) \
                            .replace("@@BOX@@", str(frame_width(st))) \
                            .replace("@@LOGOGAP@@", str(LOGO_GAP)) \
                            .replace("@@REDRAW@@", redraw_flag(st)) \
                            .replace("@@RESIZEGUARD@@", RESIZE_GUARD_TEMPLATE)
    LAUNCHER_PATH.write_text(text, "utf-8", newline="\n")


def redraw_flag(st: dict) -> str:
    """'$true' / '$false' for the generated launcher's resize guard.

    A bare `True` in PowerShell is parsed as a command name, not a boolean, so
    the dollar sign is not optional here - without it the launcher's default
    would be $null and the guard would never install.
    """
    return "$true" if st.get("redrawOnResize", True) else "$false"


def profile_snippet() -> str:
    """The block users paste into $PROFILE.

    Deliberately guarded rather than a bare `& <path>`: the launcher is a
    generated file, so pasting a hard call into a profile on a machine where it
    has not been written yet produced
    "The term '...fastfetch-random.ps1' is not recognized as the name of a
    cmdlet, function, script file, or operable program." on every shell start.
    """
    return ('$ffLauncher = "$env:USERPROFILE\\.config\\fastfetch\\fastfetch-random.ps1"\r\n'
            'if (Test-Path -LiteralPath $ffLauncher) { & $ffLauncher } else { fastfetch.exe }')


def write_config(st: dict) -> Path:
    """Write the base palette to config.jsonc - fastfetch's own user config.

    The launcher passes --config <theme>, so this file is what a bare
    `fastfetch` picks up (and what any run falls back to if a theme file is
    missing). Without it fastfetch silently renders whatever else it finds
    earlier in its search path, which is how a config with a different module
    list and only some of the icons ended up on screen.
    """
    base = palettes_for(st["groups"])[0]
    CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    CONFIG_PATH.write_text(
        dump_jsonc(build_config_json({**st, "groups": base}, base["accent"])), "utf-8")
    return CONFIG_PATH


def apply_all(st: dict) -> str:
    """Backup config, encode sixels, write config + themes + launcher, persist state."""
    GUI_DIR.mkdir(parents=True, exist_ok=True)
    if CONFIG_PATH.exists() and not BACKUP_PATH.exists():
        shutil.copyfile(CONFIG_PATH, BACKUP_PATH)
    write_config(st)
    # The frame must be at least as wide as the widest row this machine renders,
    # and only fastfetch knows that. Measure against the config just written,
    # then rewrite it if the frame has to change - one extra write on the run
    # that changes it, and none on the runs after that.
    fitted = fitted_box_width(st)
    if fitted != box_width(st):
        st["boxWidth"] = fitted
        write_config(st)
    sixels = ensure_sixels(st)
    arts = ensure_arts(st)
    themes = generate_theme_files(st)
    generate_launcher(st)
    save_state(st)
    return (f"config + {len(themes)} themes + {len(sixels)} sixels + "
            f"{len(arts)} art + launcher written")


# --------------------------------------------------------------------------- sixel
#
# This fastfetch build cannot encode PNG to sixel itself (it silently falls
# back to the built-in ASCII logo), and Pillow has no SIXEL plugin. So we
# pre-encode each gallery image to a .sixel file and let fastfetch pass the
# bytes straight through - the same approach as the original logo.sixel setup.

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
    """The picture on a `cells_w` x `cells_h` cell canvas, one column clear.

    Two numbers matter here and they are not the same one:

    * the **canvas** is exactly `cells_w` x `cells_h` cells, because that is
      what the terminal reserves for the raster and what the launcher reads
      back out of it - a smaller canvas leaves a hole, a bigger one is drawn
      over the text beside it;
    * the **picture** is fitted into `cells_w - LOGO_SLACK_COLS` columns, so at
      least one empty column separates the last painted pixel from the first
      glyph of the fetch text. fastfetch places that text at
      `logo.width + logo.padding.right` and the terminal rounds the image's own
      width up to whole cells; without the slack those two roundings can meet,
      and text written into a sixel's cells is exactly what makes Windows
      Terminal redraw the picture in bands.
    """
    cw, ch = logo_cell_px(st)
    cells_w = max(1, int(cells_w))
    cells_h = max(1, int(cells_h))
    inner_w = max(1, cells_w - LOGO_SLACK_COLS)
    inner = fit_image_cells(im, inner_w, cells_h, cell_px=(cw, ch))
    canvas = Image.new("RGBA", (cells_w * cw, cells_h * ch), (0, 0, 0, 0))
    canvas.alpha_composite(inner, (0, 0))
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
        stale.unlink()   # never leave stale/broken encodes from older versions
    cells_w = int(st.get("logWidth", 28))
    cells_h = int(st.get("logHeight", 24))
    written = []
    for g in st.get("gallery", []):
        src = Path(g.get("path", ""))
        if not src.exists():
            continue
        gw = int(g.get("w", cells_w)); gh = int(g.get("h", cells_h))
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
        stale.unlink()   # never leave stale encodes from older versions
    cells_w = int(st.get("logWidth", 28))
    cells_h = int(st.get("logHeight", 24))
    written = []
    for g in st.get("gallery", []):
        src = Path(g.get("path", ""))
        if not src.exists():
            continue
        gw = int(g.get("w", cells_w)); gh = int(g.get("h", cells_h))
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


PREVIEW_TEMPLATE = r"""# >>> FastFetch Studio :: preview >>>
# Generated by FastFetch Studio - the window the Preview button opens.
#
# Why this is a file instead of a `powershell -Command "..."` one-liner:
# Windows Terminal's command line parser treats ';' as a separator between
# subcommands, *even inside a quoted argument*. The old one-liner set
# $env:WT_SESSION and then ran fastfetch, so Windows Terminal split it at the
# ';' and tried to launch the second half - which began with '&' - as if it were
# a program:
#     Error 2147942402 (0x80070002) when launching `" & "$env:USERPROFILE\..."`
# The system cannot find the file specified.
# Passing the payload as a file leaves nothing on the wt command line for that
# parser to split, and it is far easier to inspect when something goes wrong.
param([switch]$BlockArt)
$ErrorActionPreference = 'SilentlyContinue'

# Claim to be Windows Terminal so the 'terminal' module names the same host the
# fetch will name when it really runs inside one.
$env:WT_SESSION = 'set-by-wt'

# The window is left exactly as Windows Terminal opened it. Do not resize it:
# SetWindowSize() works, but Windows Terminal persists the size of the last
# window it closed, so widening the preview silently rewrites the width of every
# terminal the user opens afterwards. The logo is sized to the window instead.
# See the guard in the CI preview step.
$ffExe = Join-Path $env:USERPROFILE '.local\bin\fastfetch.exe'
if (-not (Test-Path -LiteralPath $ffExe)) { $ffExe = 'fastfetch.exe' }
if (-not (Get-Command -Name $ffExe -ErrorAction SilentlyContinue)) {
    Write-Host 'FastFetch Studio: fastfetch.exe was not found - run setup.ps1 first.' -ForegroundColor Yellow
    return
}

$themeArg = @('--config', '@@THEME@@')
$sixel = '@@SIXEL@@'
$art   = '@@ART@@'

function global:Get-TextLogoWidth {
    # How many columns a text logo file takes on screen, ANSI colours excluded.
    # fastfetch prints such a file verbatim - --logo-width has no effect on it -
    # so measuring the file is the only way to know whether it fits beside the
    # frame before it is drawn.
    #
    # Global, like the rest of the draw helpers: the resize guard calls this long
    # after the script's own scope has gone, and a script-scoped function is not
    # there any more.
    param([string]$Path)
    $widest = 0
    try {
        foreach ($line in [IO.File]::ReadAllLines($Path)) {
            $plain = [Text.RegularExpressions.Regex]::Replace($line, '\x1b\[[0-9;]*m', '')
            $plain = $plain.TrimEnd()
            if ($plain.Length -gt $widest) { $widest = $plain.Length }
        }
    } catch { }
    return $widest
}

function global:Get-FFSCellPx {
    # Pixel size of one cell, as the sixels were encoded for - the same baked-in
    # numbers the launcher uses, so the preview and the shell agree.
    param([hashtable]$Plan)
    $cw = @@CELLW@@
    $ch = @@CELLH@@
    if ($Plan -and [int]$Plan.CellW -gt 0 -and [int]$Plan.CellH -gt 0) {
        $cw = [int]$Plan.CellW
        $ch = [int]$Plan.CellH
    }
    if ($cw -le 0) { $cw = 10 }
    if ($ch -le 0) { $ch = 20 }
    return @($cw, $ch)
}

function global:Get-FFSRasterSize {
    # The pixel size a sixel declares in its raster attributes, which is the size
    # the terminal places it at. 0,0 when the file cannot be read.
    param([string]$Path)
    if (-not $Path -or -not (Test-Path -LiteralPath $Path -PathType Leaf)) { return @(0, 0) }
    try {
        $fs = [IO.File]::OpenRead($Path)
        try {
            $head = New-Object byte[] 64
            $n = $fs.Read($head, 0, 64)
        } finally { $fs.Dispose() }
        $txt = -join ($head[0..([Math]::Max(0, $n - 1))] | ForEach-Object { [char]$_ })
        $m = [Text.RegularExpressions.Regex]::Match($txt, '"1;1;(\d+);(\d+)')
        if ($m.Success) { return @([int]$m.Groups[1].Value, [int]$m.Groups[2].Value) }
    } catch { }
    return @(0, 0)
}

# --- the window, and one place that draws ----------------------------------
function global:Get-FFSLogoFit {
    # The largest pre-encoded size of this picture the window can hold, as a
    # hashtable of W, H, Path and Px - or $null when even the smallest does not
    # fit.
    #
    # Every size in the ladder was encoded from the same image against the
    # terminal's measured cell size, so a variant's raster is exactly its cell
    # box. Picking a file keeps that true; rewriting --logo-width to whatever the
    # window allowed (what v1.1.15 did) broke it, and the fetch's text then
    # landed in the picture's own columns - which is what the terminal reacts to
    # by redrawing the picture in bands.
    #
    # Rows matter as much as columns: a 24-row picture in a 20-row window can
    # only be drawn by scrolling, and a scrolled sixel is exactly the torn one.
    param([string]$Path, [string]$Dir, [hashtable]$Plan, [int]$Room, [int]$Rows)
    if (-not $Path -or -not $Dir) { return $null }
    $cell = Get-FFSCellPx -Plan $Plan
    $base = [IO.Path]::GetFileNameWithoutExtension($Path)
    $files = @(Get-Item -LiteralPath $Path -ErrorAction SilentlyContinue)
    $files += @(Get-ChildItem -LiteralPath $Dir -Filter ($base + '-*x*.sixel') -File -ErrorAction SilentlyContinue)
    $best = $null
    foreach ($f in $files) {
        $px = @(Get-FFSRasterSize -Path $f.FullName)
        if ($px[0] -le 0 -or $px[1] -le 0) { continue }
        $w = [int][Math]::Ceiling($px[0] / [double]$cell[0])
        $h = [int][Math]::Ceiling($px[1] / [double]$cell[1])
        if ($w -gt $Room -or $h -gt $Rows) { continue }
        if ($null -eq $best -or ($w * $h) -gt ([int]$best.W * [int]$best.H)) {
            $best = @{ W = $w; H = $h; Path = $f.FullName; Px = $px }
        }
    }
    return $best
}

function global:Get-FFSArtFit {
    # The same choice for the block art: a text logo cannot tear, so it is what a
    # window too narrow or too short for the picture gets. The full-size file is
    # a candidate like any other - it is measured (that is what makes it drawable
    # at all) and the variants carry their size in the name, written by the
    # encoder from the very cells it rendered.
    param([string]$Path, [string]$Dir, [int]$Room, [int]$Rows)
    if (-not $Path -or -not $Dir) { return $null }
    $base = [IO.Path]::GetFileNameWithoutExtension($Path)
    $files = @(Get-Item -LiteralPath $Path -ErrorAction SilentlyContinue)
    $files += @(Get-ChildItem -LiteralPath $Dir -Filter ($base + '-*x*.art') -File -ErrorAction SilentlyContinue)
    $best = $null
    foreach ($f in $files) {
        $w = 0; $h = 0
        if ($f.BaseName -match '-(\d+)x(\d+)$') {
            $w = [int]$Matches[1]; $h = [int]$Matches[2]
        } else {
            $w = Get-TextLogoWidth -Path $f.FullName
            $h = @([IO.File]::ReadAllLines($f.FullName)).Count
        }
        if ($w -le 0 -or $h -le 0 -or $w -gt $Room -or $h -gt $Rows) { continue }
        if ($null -eq $best -or ($w * $h) -gt ([int]$best.W * [int]$best.H)) {
            $best = @{ W = $w; H = $h; Path = $f.FullName }
        }
    }
    return $best
}

function global:Get-FFSGrid {
    # The current grid as @(columns, rows). There is no resize event to
    # subscribe to on Windows - ReadConsoleInput's WINDOW_BUFFER_SIZE_EVENT is
    # the only signal, and PSReadLine owns the input buffer - so the size is
    # polled, and the answer changing is how a resize is noticed.
    $c = 0; $r = 0
    try { $c = [int][Console]::WindowWidth; $r = [int][Console]::WindowHeight } catch { }
    if ($c -le 0) {
        try { $c = [int]$Host.UI.RawUI.WindowSize.Width; $r = [int]$Host.UI.RawUI.WindowSize.Height } catch { }
    }
    return @($c, $r)
}

function global:Invoke-FFSRun {
    # Same shape as the launcher's, minus the fallback: a preview has nothing to
    # fall back to, so what it draws is what the launcher would draw.
    param([hashtable]$Plan, [string[]]$FFArgs, [string[]]$FFRetry)
    foreach ($a in @(,$FFArgs)) { & $Plan.Exe @a }
}

function global:Show-FFSFetch {
    # Draw the plan against the window that is there *now*: the preview is the
    # one place a user resizes on purpose, so the fit has to be recomputed on
    # every draw rather than captured once.
    #
    # The logo is drawn beside a frame that is a fixed number of columns wide, so
    # the pair is wider than most windows and fastfetch wraps whatever does not
    # fit - landing the frame's border on the next row. Give the logo only what
    # is left after the frame. Its size is read out of the sixel itself rather
    # than rewritten to fit: --logo-width says how many cells the picture
    # occupies, it does not resize one, so a "fitted" number only moved the text
    # on top of the picture (see Show-FFSFetch in the launcher).
    #
    # @@LOGOGAP@@ of those columns are fastfetch's own logo padding, which sits
    # between the logo and the text: without reserving it the pair "fits" on
    # paper while the frame's right corner is still pushed off the edge.
    param([hashtable]$Plan)
    $grid = Get-FFSGrid
    if ([int]$grid[0] -gt 0) { $Plan.Grid = @([int]$grid[0], [int]$grid[1]) }
    $cols = [int]$Plan.Grid[0]
    if ($cols -le 0) { $cols = 80 }
    $rows = [int]$Plan.Grid[1]
    if ($rows -le 0) { $rows = 24 }
    $Plan.Rows = $rows
    $rowRoom = $rows - 1
    $room = $cols - [int]$Plan.Band - 1 - [int]$Plan.Gap
    # Which pre-encoded size of the picture this window holds. The size is read
    # out of the file that is about to be drawn and never rewritten to fit (see
    # Show-FFSFetch in the launcher); a smaller file is what "fitting" means.
    $fit = $null
    if (-not $Plan.BlockArt -and (Test-Path -LiteralPath $Plan.Sixel)) {
        $fit = Get-FFSLogoFit -Path $Plan.Sixel -Dir (Split-Path -Parent $Plan.Sixel) `
                              -Plan $Plan -Room $room -Rows $rowRoom
    }
    $w = 0
    $h = 0
    if ($fit) { $w = [int]$fit.W; $h = [int]$fit.H }
    # A text logo cannot be scaled - --logo-width does nothing to a file - so the
    # only way to keep the frame intact is to draw it only when it fits, and to
    # draw nothing when it does not rather than fall back to a wider logo.
    $art = Get-FFSArtFit -Path $Plan.Art -Dir (Split-Path -Parent $Plan.Art) -Room $room -Rows $rowRoom
    $ffArgs = @()
    if ($Plan.Theme) { $ffArgs += $Plan.Theme }
    $narrow = ''
    if ($Plan.BlockArt) {
        # A console with no image protocol at all: fastfetch prints a *text* logo
        # file verbatim, so the block art is the only way this terminal can show
        # the user's own picture - and it cannot tear.
        if ($art) { $ffArgs += @('--logo', $art.Path) }
        elseif (Test-Path -LiteralPath $Plan.Art) {
            $ffArgs += @('--logo', 'none')
            $narrow = 'is too narrow for the logo beside the frame.'
        } else { $ffArgs += @('--logo', 'none') }
    } elseif ($fit) {
        $ffArgs += @('--logo-type', 'raw', '--logo', $fit.Path, '--logo-width', $w, '--logo-height', $h, '--logo-print-remaining', 'false')
    } elseif ($art) {
        # No room for the picture, or no image protocol to draw it into (a bare
        # console). fastfetch prints a *text* logo file verbatim, so the block art
        # - the same picture at block resolution - is drawn when one fits.
        $ffArgs += @('--logo', $art.Path)
    } elseif (Test-Path -LiteralPath $Plan.Art) {
        # The art is there but wider than the room the frame leaves. Drawing it
        # anyway is what pushed the frame off the edge.
        $ffArgs += @('--logo', 'none')
        $narrow = 'is too narrow for the logo beside the frame.'
    } else {
        $ffArgs += @('--logo', 'Windows11')
    }
    $ffArgs += $Plan.Fit
    Invoke-FFSRun -Plan $Plan -FFArgs $ffArgs
    if ($narrow) {
        Write-Host ''
        Write-Host ('FastFetch Studio: a ' + $cols + '-column window ' + $narrow + ' Widen the window and press Preview again.') -ForegroundColor DarkGray
    }
}

$fitArgs = @('--disable-linewrap', 'true')

# One record of what to draw, so the resize guard can draw it again after the
# window changes size. Same shape as the launcher's plan.
$global:FFSPlan = @{
    Exe      = $ffExe
    Theme    = $themeArg
    Fit      = $fitArgs
    Sixel    = $sixel
    Art      = $art
    BlockArt = [bool]$BlockArt
    W        = @@W@@
    H        = @@H@@
    CellW    = @@CELLW@@
    CellH    = @@CELLH@@
    Band     = @@BOX@@
    Gap      = @@LOGOGAP@@
    Redraw   = @@REDRAW@@
    Grid     = @(0, 0)
    Rows     = 0
}

@@RESIZEGUARD@@

Show-FFSFetch -Plan $global:FFSPlan
if ($global:FFSPlan.Redraw) { Install-FFSResizeGuard }

Write-Host ''
Write-Host 'FastFetch Studio preview - close this window when you are done.' -ForegroundColor DarkGray
# <<< FastFetch Studio :: preview <<<
"""


def load_metrics() -> dict:
    """The measured terminal cell size, or {} when it has not been measured."""
    try:
        data = json.loads(METRICS_PATH.read_text("utf-8"))
    except Exception:
        return {}
    if not isinstance(data, dict):
        return {}
    try:
        cw = int(data.get("cellW", 0)); ch = int(data.get("cellH", 0))
    except (TypeError, ValueError):
        return {}
    if not (4 <= cw <= 80) or not (6 <= ch <= 160):
        return {}
    data["cellW"] = cw
    data["cellH"] = ch
    return data


def save_metrics(cell_w: int, cell_h: int, **extra) -> None:
    """Record the measured cell size so the encoder and launcher can both use it."""
    payload = {"cellW": int(cell_w), "cellH": int(cell_h)}
    payload.update(extra)
    METRICS_PATH.parent.mkdir(parents=True, exist_ok=True)
    METRICS_PATH.write_text(json.dumps(payload, indent=2) + "\n", "utf-8")


# The probe the Random tab runs to learn the terminal's real cell size. It is a
# generated file for the same reason the preview is (see PREVIEW_TEMPLATE):
# Windows Terminal splits a `powershell -Command "..."` argument on ';'.
#
# Both numbers are measured, never assumed:
#   cellH - from two images of known height: the terminal advances
#           ceil(height / cellH) rows, so each probe brackets cellH
#           (H/rows <= cellH < H/(rows-1)) and the two brackets are intersected.
#           Measured this way on a 2880x1800 display at 200%: 20.5..21.1 px.
#   cellW - from the window's client width, its column count and its DPI scale,
#           which needs no guessing about fonts.
# Terms such as "cell height" do not reach the user, so the probe says what it
# found in plain words and the app turns that into sixels.
MEASURE_TEMPLATE = r'''# >>> FastFetch Studio :: measure cells >>>
# Generated by FastFetch Studio - measures the terminal's cell size in pixels.
# The window closes on its own, and nothing is left on screen, because every
# probe runs on the alternate screen buffer.
$ErrorActionPreference = 'SilentlyContinue'
Add-Type -AssemblyName System.Drawing
Add-Type -TypeDefinition @'
using System;
using System.Runtime.InteropServices;
public static class FFSMeasure {
  [StructLayout(LayoutKind.Sequential)] public struct RECT { public int Left; public int Top; public int Right; public int Bottom; }
  [DllImport("user32.dll")] public static extern bool SetProcessDPIAware();
  [DllImport("user32.dll", CharSet=CharSet.Unicode)] public static extern IntPtr FindWindow(string cls, string title);
  [DllImport("user32.dll")] public static extern bool GetClientRect(IntPtr h, out RECT r);
  [DllImport("user32.dll")] public static extern uint GetDpiForWindow(IntPtr h);
}
'@
[void][FFSMeasure]::SetProcessDPIAware()

$ffsTitle = 'FastFetch Studio measuring'
$Host.UI.RawUI.WindowTitle = $ffsTitle
Start-Sleep -Milliseconds 600

# Every step is logged next to the result: what this measures is what the
# terminal does with an image, so when a terminal answers oddly the log is the
# only way to see why.
$ffsLog = '@@LOGPATH@@'
Remove-Item -LiteralPath $ffsLog -Force -ErrorAction SilentlyContinue
function Say([string]$m) { Add-Content -LiteralPath $ffsLog -Value $m -Encoding UTF8 }
Say ('grid=' + [Console]::WindowWidth + 'x' + [Console]::WindowHeight + ' buffer=' + [Console]::BufferWidth + 'x' + [Console]::BufferHeight)

function Get-FFSProbeRows([int]$Height) {
    # Rows the terminal spends on an image of this many pixels.
    #
    # Each probe runs on the alternate screen buffer: that puts the cursor back
    # at the top for every one of them (a fresh window would otherwise be needed
    # for each, because nothing moves the cursor up) and it throws the images
    # away when the buffer is left, so no magenta block is left behind.
    $file = Join-Path '@@PROBEDIR@@' ('probe-' + $Height + '.sixel')
    if (-not (Test-Path -LiteralPath $file)) { Say ('probe ' + $Height + ': file missing'); return 0 }
    $bytes = [IO.File]::ReadAllBytes($file)
    $out = [Console]::OpenStandardOutput()
    [Console]::Write("`e[?1049h")
    Start-Sleep -Milliseconds 250
    $before = [int][Console]::CursorTop
    $out.Write($bytes, 0, $bytes.Length)
    $out.Flush()
    Start-Sleep -Milliseconds 450
    $after = [int][Console]::CursorTop
    $rows = $after - $before
    Say ('probe ' + $Height + 'px: rows ' + $before + ' -> ' + $after + ' = ' + $rows)
    [Console]::Write("`e[?1049l")
    Start-Sleep -Milliseconds 250
    # A reading that stopped on the last row was clamped by the bottom of the
    # window and says nothing about the cell size.
    if ($rows -le 1) { return 0 }
    if ($after -ge ([int][Console]::WindowHeight - 1)) { Say ('probe ' + $Height + ': clamped at the last row'); return 0 }
    return [int]$rows
}

$lo = 0.0
$hi = 999.0
$used = 0
foreach ($h in @@PROBEHEIGHTS@@) {
    $rows = Get-FFSProbeRows $h
    if ($rows -le 1) { continue }
    $a = $h / [double]$rows            # cellH >= a  (the terminal rounds up)
    $b = $h / [double]($rows - 1)      # cellH <  b
    if ($a -gt $lo) { $lo = $a }
    if ($b -lt $hi) { $hi = $b }
    $used++
}
Say ('cellH bracket: ' + [Math]::Round($lo, 2) + ' .. ' + [Math]::Round($hi, 2) + ' from ' + $used + ' probes')

$cellH = 0.0
if ($used -gt 0 -and $hi -gt $lo) { $cellH = ($lo + $hi) / 2.0 }
elseif ($used -gt 0) { $cellH = $lo }

$cellW = 0.0
$exactW = $false
$hw = [FFSMeasure]::FindWindow($null, $ffsTitle)
$cols = [int][Console]::WindowWidth
if ($hw -ne [IntPtr]::Zero -and $cols -gt 0) {
    $cr = New-Object FFSMeasure+RECT
    if ([FFSMeasure]::GetClientRect($hw, [ref]$cr)) {
        $dpi = [FFSMeasure]::GetDpiForWindow($hw)
        if (-not $dpi -or $dpi -le 0) {
            # GetDpiForWindow needs a real top-level handle; the desktop DC knows
            # the scaling either way.
            $dpi = [System.Drawing.Graphics]::FromHwnd([IntPtr]::Zero).DpiX
        }
        if ($dpi -gt 0) {
            $dipW = ($cr.Right - $cr.Left) * 96.0 / $dpi
            $cellW = $dipW / [double]$cols
            $exactW = $true
            Say ('client=' + ($cr.Right - $cr.Left) + 'px dpi=' + $dpi + ' cols=' + $cols + ' -> cellW=' + [Math]::Round($cellW, 2))
        }
    }
}
if (-not $exactW -and $cellH -gt 0) {
    # No window handle to measure from. The fonts these terminals ship with are
    # all about half as wide as they are tall, which is close enough to fall
    # back on, and the result is recorded as the estimate it is.
    $cellW = $cellH / 2.0
    Say ('cellW from the font aspect: ' + [Math]::Round($cellW, 2))
}

$json = ''
$cw = 0
$ch = 0
if ($cellW -gt 0 -and $cellH -gt 0) {
    # Deliberately rounded *down*: the encoder and the launcher both work from
    # these numbers, and a cell that is smaller than the real one makes the
    # picture smaller than the cells fastfetch reserved for it. That is the safe
    # direction - the picture can then never reach the text beside it, whatever
    # the terminal rounds its own footprint up to.
    $cw = [int][Math]::Floor($cellW)
    $ch = [int][Math]::Floor($cellH)
    if ($cw -lt 4) { $cw = 4 }
    if ($ch -lt 6) { $ch = 6 }
    $inv = [Globalization.CultureInfo]::InvariantCulture
    $json = '{' +
        '"cellW": ' + $cw + ', "cellH": ' + $ch + ', ' +
        '"cols": ' + $cols + ', "rows": ' + [int][Console]::WindowHeight + ', ' +
        '"cellWall": ' + $cellW.ToString('F2', $inv) + ', ' +
        '"cellHall": ' + $cellH.ToString('F2', $inv) + ', ' +
        '"cellWMeasured": ' + $(if ($exactW) { 'true' } else { 'false' }) + ', ' +
        '"source": "measured"}' + "`n"
    Set-Content -LiteralPath '@@METRICS@@' -Value $json -Encoding UTF8 -ErrorAction SilentlyContinue
    Say ('metrics written: ' + $json.Trim())
}

if ($json) {
    Write-Host ('FastFetch Studio: one logo cell in this terminal is ' + $cw + 'x' + $ch + ' pixels.') -ForegroundColor DarkGray
    Write-Host '  Press Apply & Generate to draw the logos at that size.' -ForegroundColor DarkGray
} else {
    Write-Host 'FastFetch Studio: could not measure this terminal - the logo keeps its default cell.' -ForegroundColor Yellow
}
Start-Sleep -Seconds 3
# <<< FastFetch Studio :: measure cells <<<
'''


def write_measure_script(st: dict | None = None) -> Path | None:
    """Write gui\\measure-cells.ps1 plus its probe images."""
    if not (HAVE_PIL and HAVE_SIXEL):
        return None
    metrics = load_metrics() or {}
    cw, ch = CELL_PX_DEFAULT
    if metrics:
        cw, ch = metrics["cellW"], metrics["cellH"]
    # Tallest first: it is drawn at the top of a fresh window, and the one after
    # it starts wherever that left the cursor - a reading that runs into the
    # last row is discarded rather than trusted.
    heights = [600, 300]
    try:
        for h in heights:
            im = Image.new("RGBA", (max(20, 20 * cw), h), (255, 0, 255, 255))
            (GUI_DIR / f"probe-{h}.sixel").write_bytes(encode_sixel(im))
    except Exception:
        return None
    body = (MEASURE_TEMPLATE
            .replace("@@PROBEDIR@@", str(GUI_DIR))
            .replace("@@LOGPATH@@", str(GUI_DIR / "measure-cells.log"))
            .replace("@@PROBEHEIGHTS@@", ",".join(str(h) for h in heights))
            .replace("@@METRICS@@", str(METRICS_PATH)))
    MEASURE_SCRIPT.parent.mkdir(parents=True, exist_ok=True)
    MEASURE_SCRIPT.write_text(body, "utf-8", newline="\n")
    return MEASURE_SCRIPT


def write_preview_script(st: dict, image_path: str) -> Path | None:
    """Generate gui\\preview.ps1, the payload the Preview button runs."""
    themes = sorted(THEMES_DIR.glob("theme-*.jsonc"))
    if not themes:
        return None
    sixel = write_preview_sixel(st, image_path)
    if sixel is None:
        # Don't let a stale cache from an earlier run stand in for this image.
        PREVIEW_CACHE.unlink(missing_ok=True)
    body = (PREVIEW_TEMPLATE
            .replace("@@THEME@@", str(themes[0]))
            .replace("@@SIXEL@@", str(PREVIEW_CACHE))
            .replace("@@ART@@", str(ARTS_DIR / f"{art_key(image_path)}.art"))
            .replace("@@W@@", str(int(st.get("logWidth", 28))))
            .replace("@@H@@", str(int(st.get("logHeight", 24))))
            .replace("@@CELLW@@", str(logo_cell_px(st)[0]))
            .replace("@@CELLH@@", str(logo_cell_px(st)[1]))
            .replace("@@BOX@@", str(frame_width(st)))
            .replace("@@REDRAW@@", redraw_flag(st))
            .replace("@@RESIZEGUARD@@", RESIZE_GUARD_TEMPLATE)
            .replace("@@LOGOGAP@@", str(LOGO_GAP)))
    PREVIEW_SCRIPT.parent.mkdir(parents=True, exist_ok=True)
    PREVIEW_SCRIPT.write_text(body, "utf-8", newline="\n")
    return PREVIEW_SCRIPT


def preview_command(script: Path, block_art: bool = False) -> list[str]:
    """The `powershell ... -File <script>` half of the preview command line.

    -NoExit has to sit *before* -File: everything after `-File <path>` is handed
    to the script as arguments rather than read by PowerShell, so putting the
    switch there would silently do nothing. It matters because fastfetch exits in
    well under a second and Windows Terminal closes a window the moment its
    process does - without it the preview flashes up and vanishes unread.
    """
    cmd = ["powershell", "-NoExit", "-NoProfile", "-ExecutionPolicy", "Bypass",
           "-File", str(script)]
    if block_art:
        cmd.append("-BlockArt")
    return cmd


def preview_argv(script: Path) -> list[str]:
    """The full wt.exe command line that opens the preview window.

    Kept as its own function so the invariant behind a real bug can be asserted:
    Windows Terminal splits its command line on ';' *even inside a quoted
    argument*, so nothing on this line may contain one. The payload therefore
    lives in a generated file and this line only ever names it.
    """
    return ["wt.exe", "-w", "-1", "nt", "--title", "FastFetch Preview",
            *preview_command(script)]


def spawn_terminal_preview(st: dict, image_path: str) -> tuple[bool, str]:
    """Open a window showing fastfetch drawing the chosen logo, as it will live."""
    script = write_preview_script(st, image_path)
    if script is None:
        return False, "No themes yet - click Apply & Generate first"
    try:
        subprocess.Popen(preview_argv(script),
                         creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        return True, "Preview opened in Windows Terminal"
    except FileNotFoundError:
        pass   # no wt.exe on this box - Windows 10 without Windows Terminal
    except Exception as e:
        return False, f"Could not open Windows Terminal: {e}"
    # A bare console cannot draw sixel, so ask the script for the block-art logo
    # rather than spraying escape codes into the window.
    try:
        subprocess.Popen(preview_command(script, block_art=True),
                         creationflags=getattr(subprocess, "CREATE_NEW_CONSOLE", 0))
        return True, "Preview opened in a console window"
    except Exception as e:
        return False, f"Could not open a preview window: {e}"


def measure_argv(script: Path) -> list[str]:
    """The wt.exe line that opens the cell-size probe.

    Same rule as preview_argv: nothing on this line may contain ';' - Windows
    Terminal splits its command line on it even inside a quoted argument, which
    is what once produced `Error 2147942402`. The probe lives in its own file
    for exactly that reason.
    """
    return ["wt.exe", "-w", "-1", "nt", "--title", "FastFetch Studio measuring",
            "powershell", "-NoProfile", "-ExecutionPolicy", "Bypass",
            "-File", str(script)]


def spawn_measure(script: Path) -> tuple[bool, str]:
    """Open the terminal window that measures its own cell size."""
    try:
        subprocess.Popen(measure_argv(script),
                         creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        return True, "Measuring your terminal in a new window"
    except FileNotFoundError:
        # No wt.exe: a bare console window cannot be measured the same way (it
        # has no sixel support at all), so say so instead of guessing.
        return False, "Windows Terminal (wt.exe) was not found - the picture uses a 10x20 cell here"
    except Exception as e:
        return False, f"Could not open Windows Terminal: {e}"


# --------------------------------------------------------------------------- selftest
def selftest() -> int:
    st = load_state()
    if not st["gallery"]:
        for d in (PNGS_DIR, FF_DIR / "images"):
            if d.is_dir():
                for p in sorted(d.glob("*.png")):
                    st["gallery"].append({"path": str(p), "w": st["logWidth"], "h": st["logHeight"]})
        if st["gallery"]:
            st["defaultImage"] = st["gallery"][0]["path"]
    summary = apply_all(st)
    ok = bool(list(THEMES_DIR.glob("theme-*.jsonc"))) and LAUNCHER_PATH.exists()
    # Regression guard: after JSON decode, custom formats must contain a real
    # ESC control char - never a literal backslash-u001b text (double-escaped).
    for t in THEMES_DIR.glob("theme-*.jsonc"):
        data = json.loads(t.read_text("utf-8"))
        for m in data.get("modules", []):
            fmt = m.get("format") if isinstance(m, dict) else None
            if isinstance(fmt, str) and "\\u001b" in fmt:
                print(f"selftest: FAIL {t.name} has literal \\u001b text in a format")
                return 1
            if isinstance(fmt, str) and fmt.startswith("\x1b[38;2;") is False and "\x1b[" in fmt:
                print(f"selftest: FAIL {t.name} has non-truecolor ESC sequence")
                return 1
        # Regression guard: a config-level logo section (even "none") overrides
        # the CLI --logo and silently kills the image logo.
        if "logo" in data:
            print(f"selftest: FAIL {t.name} contains a logo section (suppresses CLI --logo)")
            return 1
        # Regression guard: every gallery image must have an encoded sixel.
        for g in st.get("gallery", []):
            key = re.sub(r"[^A-Za-z0-9]+", "-", Path(g["path"]).stem).strip("-") or "img"
            if not (SIXELS_DIR / f"{key}.sixel").exists():
                print(f"selftest: FAIL missing sixel for {Path(g['path']).name}")
                return 1
    # Pixel-level regression guard: re-encode gallery[0], decode it back,
    # compare every painted pixel against the quantized source.
    if HAVE_PIL and HAVE_SIXEL and st.get("gallery"):
        cells_w = int(st["logWidth"]); cells_h = int(st["logHeight"])
        cw, ch = logo_cell_px(st)
        with Image.open(st["gallery"][0]["path"]) as im0:
            src = fit_logo_cells(im0, cells_w, cells_h, st)
            srcq = quantize_rgb(src)
        # The invariant that stopped the logo from tearing: the raster is
        # exactly the cell box it is placed into, and it is one column short of
        # the cell count fastfetch is told about, so no glyph can be written
        # into the picture's own cells.
        if (src.width, src.height) != (cells_w * cw, cells_h * ch):
            print(f"selftest: FAIL raster {src.width}x{src.height} != "
                  f"{cells_w}x{cells_h} cells of {cw}x{ch} px")
            return 1
        inner_w = (cells_w - LOGO_SLACK_COLS) * cw
        als = src.convert("RGBA").getchannel("A").load()
        painted_right = max((xx for xx in range(src.width) for yy in range(src.height)
                             if als[xx, yy] >= ALPHA_THRESHOLD), default=-1)
        if painted_right >= inner_w:
            print(f"selftest: FAIL picture reaches column {painted_right}, past the "
                  f"{inner_w}px of clear space the text needs")
            return 1
        print(f"selftest: logo canvas {src.width}x{src.height} px, picture ends at "
              f"x={painted_right} of {inner_w} px of room")
        # The ladder a narrow window picks from: every variant has to hold the
        # same invariant as the full size (raster == cells x cell px), because a
        # variant whose raster and declared cells disagree is the bug again,
        # just at a smaller size.
        key = re.sub(r"[^A-Za-z0-9]+", "-", Path(st["gallery"][0]["path"]).stem).strip("-") or "img"
        for vw, vh in ladder_cells(cells_w, cells_h):
            name = (f"{key}.sixel" if (vw, vh) == (cells_w, cells_h)
                    else f"{key}-{vw}x{vh}.sixel")
            vfile = SIXELS_DIR / name
            if not vfile.exists():
                print(f"selftest: FAIL ladder variant {name} was not written")
                return 1
            w3, h3, _ = decode_sixel_pixels(vfile.read_bytes())
            if (w3, h3) != (vw * cw, vh * ch):
                print(f"selftest: FAIL ladder variant {name} is {w3}x{h3} px, "
                      f"not {vw}x{vh} cells of {cw}x{ch} px ({vw * cw}x{vh * ch})")
                return 1
        steps = ladder_cells(cells_w, cells_h)
        print(f"selftest: logo ladder ok ({len(steps)} sizes: "
              + ", ".join(f"{w}x{h}" for w, h in steps) + ")")
        w2, h2, grid = decode_sixel_pixels(encode_sixel(src))
        if (w2, h2) != (src.width, src.height):
            print(f"selftest: FAIL round-trip size {w2}x{h2} != {src.width}x{src.height}")
            return 1
        sp = srcq.load()
        ap = src.convert("RGBA").getchannel("A").load()
        through = lambda c: (c * 100 // 255) * 255 // 100
        mismatch = total = 0
        for yy, row in grid.items():
            for xx, rgb in row.items():
                total += 1
                if ap[xx, yy] < ALPHA_THRESHOLD or rgb != tuple(through(c) for c in sp[xx, yy]):
                    mismatch += 1   # painted a transparent pixel, or wrong color
        n_opaque = sum(1 for yy in range(src.height) for xx in range(src.width)
                       if ap[xx, yy] >= ALPHA_THRESHOLD)
        tol = n_opaque // 1000
        if n_opaque == 0 or mismatch > tol or total < n_opaque - tol:
            print(f"selftest: FAIL round-trip mismatch {mismatch}/{total} pixels (opaque {n_opaque})")
            return 1
        print(f"selftest: sixel round-trip ok ({n_opaque} painted of {total}, {mismatch} mismatched)")
    # The generated scripts have to agree with the encoder about one number: the
    # cell size. When they drift, the launcher derives a different cell count out
    # of the raster than the picture needs - which is the bug this all exists for.
    if HAVE_PIL and HAVE_SIXEL:
        cw, ch = logo_cell_px(st)
        launcher = LAUNCHER_PATH.read_text("utf-8", errors="replace") if LAUNCHER_PATH.exists() else ""
        if "@@" in launcher:
            print("selftest: FAIL the launcher still has an unsubstituted @@placeholder@@")
            return 1
        for needle, why in (("--logo-print-remaining", "fastfetch would pad blank lines over the picture"),
                            ("Get-FFSRasterSize", "the logo size is not read from the file"),
                            ("Get-FFSLogoFit", "a window that cannot hold the full size has no smaller one"),
                            ("Get-FFSArtFit", "no block-art size is chosen for a narrow window"),
                            (f"$cw = {cw}", "the launcher's cell width disagrees with the encoder"),
                            (f"$ch = {ch}", "the launcher's cell height disagrees with the encoder"),
                            ("`n\" * ($rows + 2)", "the resize repair does not scroll the torn copy away")):
            if needle not in launcher:
                print(f"selftest: FAIL launcher is missing {needle!r} - {why}")
                return 1
        print(f"selftest: launcher agrees on a {cw}x{ch} px cell and derives the logo size")
    print(f"selftest: {summary}; launcher={'ok' if LAUNCHER_PATH.exists() else 'MISSING'}")
    return 0 if ok else 1


# --------------------------------------------------------------------------- GUI
def norm_hex(s: str, fallback: str) -> str:
    s = (s or "").strip()
    if re.fullmatch(r"#[0-9a-fA-F]{6}", s):
        return s.lower()
    if re.fullmatch(r"[0-9a-fA-F]{6}", s):
        return "#" + s.lower()
    return fallback


# --- UI theme: dark, calm, accent-driven ---------------------------------
BG      = "#0e1016"   # window background
PANEL   = "#151a26"   # cards
FIELD   = "#1d2434"   # inputs / ghost buttons
FIELD_H = "#26304a"   # hover
FG      = "#e7eaf3"
MUTED   = "#969eb5"
BORDER  = "#28304a"
EDGE    = "#3f4b6e"   # outline of unselected check/radio indicators
HOVER_EDGE = "#5a6a94"
ACCENT  = "#e04a3f"   # matches the user's theme accent
ACCENT_H = "#f2604f"
OKC     = "#41c46f"
WARNC   = "#e6a83c"

FONT_UI    = ("Segoe UI", 10)
FONT_SEMI  = ("Segoe UI Semibold", 10)
FONT_HEAD  = ("Segoe UI Semibold", 11)
FONT_SMALL = ("Segoe UI", 9)
FONT_MONO  = ("Consolas", 9)

THUMB_PX = 170   # gallery thumbnail box: square, image letterboxed inside


def lerp_hex(a: str, b: str, t: float) -> str:
    ca = tuple(int(a[i:i + 2], 16) for i in (1, 3, 5))
    cb = tuple(int(b[i:i + 2], 16) for i in (1, 3, 5))
    return "#{:02x}{:02x}{:02x}".format(*(round(x + (y - x) * t) for x, y in zip(ca, cb)))


def animate_highlight(widget, end: str, steps: int = 7, ms: int = 13) -> None:
    """Smoothly animate a widget's highlightbackground toward end color."""
    try:
        cur = widget["highlightbackground"]
    except Exception:
        return
    state = {"i": 0}
    def tick():
        state["i"] += 1
        try:
            widget["highlightbackground"] = lerp_hex(cur, end, state["i"] / steps)
        except Exception:
            return
        if state["i"] < steps:
            widget.after(ms, tick)
    tick()


def setup_style() -> None:
    st = ttk.Style()
    try:
        st.theme_use("clam")
    except tk.TclError:
        pass
    st.configure(".", background=BG, foreground=FG, fieldbackground=FIELD,
                 bordercolor=BORDER, lightcolor=BG, darkcolor=BG,
                 troughcolor=FIELD, insertcolor=FG, font=FONT_UI)
    st.configure("TFrame", background=BG)
    st.configure("Card.TFrame", background=PANEL)
    st.configure("TLabel", background=BG, foreground=FG)
    st.configure("TNotebook", background=BG, borderwidth=0, tabmargins=(14, 12, 14, 0))
    st.configure("TNotebook.Tab", padding=(16, 8), background=BG,
                 foreground=MUTED, borderwidth=0, font=FONT_SEMI)
    st.map("TNotebook.Tab",
           background=[("selected", PANEL)], foreground=[("selected", FG)])
    st.map("TNotebook.Tab", expand=[("selected", (1, 1, 1, 0))])
    # Checkbutton / Radiobutton are deliberately NOT styled here: clam ignores
    # `indicatorcolor`, so ttk drew them as white squares with an X. Use the
    # FFToggle widget instead (canvas-drawn, exact colors, real label gap).
    st.configure("TCombobox", fieldbackground=FIELD, background=FIELD,
                 foreground=FG, arrowcolor=FG, bordercolor=BORDER,
                 lightcolor=FIELD, darkcolor=FIELD, borderwidth=1)
    st.map("TCombobox", fieldbackground=[("readonly", FIELD)],
           foreground=[("readonly", FG)], arrowcolor=[("active", FG)])
    st.configure("TSpinbox", fieldbackground=FIELD, background=FIELD, foreground=FG,
                 arrowcolor=FG, bordercolor=BORDER, lightcolor=FIELD,
                 darkcolor=FIELD, borderwidth=1)
    st.configure("TEntry", fieldbackground=FIELD, foreground=FG, bordercolor=BORDER,
                 insertcolor=FG, borderwidth=1, padding=3)
    st.map("TEntry", bordercolor=[("focus", ACCENT)])
    st.configure("TSeparator", background=BORDER)


class AnimatedButton(tk.Button):
    """Flat button with a smooth hover color animation."""

    def __init__(self, master, kind: str = "ghost", **kw):
        if kind == "accent":
            base, hover, fg = ACCENT, ACCENT_H, "#ffffff"
        else:
            base, hover, fg = FIELD, FIELD_H, FG
        kw.setdefault("font", FONT_UI)
        kw.setdefault("pady", 6)
        kw.setdefault("padx", 12)
        super().__init__(master, bg=base, fg=fg, activebackground=hover,
                         activeforeground=fg, relief="flat", bd=0,
                         cursor="hand2", takefocus=0, **kw)
        self._base, self._hover, self._task = base, hover, None
        self.bind("<Enter>", self._hover_to(1.0), add="+")
        self.bind("<Leave>", self._hover_to(0.0), add="+")

    def _hover_to(self, target: float):
        def go():
            if self._task:
                try:
                    self.after_cancel(self._task)
                except Exception:
                    pass
                self._task = None
            start, end = self["bg"], (self._hover if target else self._base)
            steps, state = 7, {"i": 0}
            def tick():
                state["i"] += 1
                self.configure(bg=lerp_hex(start, end, state["i"] / steps))
                if state["i"] < steps:
                    self._task = self.after(13, tick)
                else:
                    self._task = None
            tick()
        return go


class ColorEntry(ttk.Frame):
    """Hex color entry + swatch + picker button."""

    def __init__(self, master, initial: str, on_change=None):
        super().__init__(master, style="Card.TFrame")
        self.on_change = on_change
        self.var = tk.StringVar(value=initial)
        self.swatch = tk.Label(self, width=3, background=initial, relief="flat",
                               highlightthickness=1, highlightbackground=BORDER,
                               cursor="hand2")
        self.swatch.pack(side="left", padx=(0, 6))
        self.swatch.bind("<Button-1>", lambda e: self.pick())
        self.entry = ttk.Entry(self, textvariable=self.var, width=9)
        self.entry.pack(side="left")
        AnimatedButton(self, text="Pick", command=self.pick, font=FONT_SMALL,
                       width=4, padx=4, pady=1).pack(side="left", padx=(6, 0))
        self.var.trace_add("write", lambda *_: self._changed())

    def _changed(self):
        h = norm_hex(self.var.get(), "")
        if h:
            self.swatch.configure(background=h)
        if self.on_change:
            self.on_change()

    def pick(self):
        from tkinter import colorchooser
        current = norm_hex(self.var.get(), ACCENT)
        rgb = colorchooser.askcolor(color=current, parent=self)[0]
        if rgb:
            h = "#{:02x}{:02x}{:02x}".format(*(round(c) for c in rgb))
            self.var.set(h)

    def get(self) -> str:
        return norm_hex(self.var.get(), ACCENT)


class SizeDialog(tk.Toplevel):
    """Per-image terminal-cell size editor."""

    def __init__(self, master, title: str, w: int, h: int):
        super().__init__(master)
        self.title(title)
        self.resizable(False, False)
        self.configure(bg=PANEL)
        self.result = None
        frm = tk.Frame(self, bg=PANEL, padx=16, pady=14)
        frm.pack(fill="both", expand=True)
        tk.Label(frm, text="Width (cells):", bg=PANEL, fg=FG).grid(row=0, column=0, sticky="e", pady=4)
        wv = tk.StringVar(value=str(w))
        ttk.Spinbox(frm, from_=10, to=80, textvariable=wv, width=6).grid(row=0, column=1, padx=8)
        tk.Label(frm, text="Height (cells):", bg=PANEL, fg=FG).grid(row=1, column=0, sticky="e", pady=4)
        hv = tk.StringVar(value=str(h))
        ttk.Spinbox(frm, from_=8, to=60, textvariable=hv, width=6).grid(row=1, column=1, padx=8)
        btns = tk.Frame(frm, bg=PANEL)
        btns.grid(row=2, column=0, columnspan=2, pady=(12, 0))

        def ok():
            try:
                self.result = (max(10, min(80, int(wv.get()))), max(8, min(60, int(hv.get()))))
            except ValueError:
                return
            self.destroy()

        AnimatedButton(btns, "accent", text="OK", command=ok, width=10).pack(side="left", padx=4)
        AnimatedButton(btns, text="Cancel", command=self.destroy, width=10).pack(side="left", padx=4)
        self.transient(master)
        self.grab_set()
        self.wait_window()


class AnimatedTab(tk.Canvas):
    """Pill-style tab with an animated accent underline."""

    def __init__(self, master, name: str, on_click):
        super().__init__(master, width=92, height=34, bg=BG, highlightthickness=0)
        self.name, self._on_click, self._frac = name, on_click, 0.0
        self._active = False
        self.bind("<Button-1>", lambda e: self._on_click(name))
        self.bind("<Enter>", lambda e: self._anim_to(1.0 if self._active else 0.35))
        self.bind("<Leave>", lambda e: self._anim_to(1.0 if self._active else 0.0))
        self._draw()

    def set_active(self, active: bool):
        self._active = active
        self._anim_to(1.0 if active else 0.0)

    def _anim_to(self, target: float):
        if getattr(self, "_task", None):
            try:
                self.after_cancel(self._task)
            except Exception:
                pass
        start, state = self._frac, {"i": 0}
        def tick():
            state["i"] += 1
            self._frac = start + (target - start) * state["i"] / 8
            self._draw()
            if state["i"] < 8:
                self._task = self.after(13, tick)
        tick()

    def _draw(self):
        self.delete("all")
        f = self._frac
        tk.Canvas.create_text(self, 46, 14, text=self.name, fill=FG if f > 0.5 else MUTED,
                              font=FONT_SEMI)
        if f > 0.01:
            self.create_rectangle(24, 28, 24 + 44 * f, 31, fill=ACCENT, outline="")


class FFToggle(tk.Canvas):
    """Canvas-drawn checkbox / radio button.

    ttk's clam theme silently ignores `indicatorcolor`, so Checkbutton and
    Radiobutton rendered as a plain white square (an X inside a box, or a dot
    inside a box) pressed right up against the label. Drawing the indicator
    ourselves gives exact colors, a real gap before the text, and a hover
    state that matches the rest of the UI.
    """

    BOX, GAP = 15, 10

    def __init__(self, master, text: str, variable, value=None, command=None,
                 kind: str = "check", bg: str = PANEL):
        font = tkfont.Font(font=FONT_UI)
        width = self.BOX + self.GAP + font.measure(text) + 2
        height = max(24, font.metrics("linespace") + 8)
        super().__init__(master, width=width, height=height, bg=bg,
                         highlightthickness=0, bd=0, cursor="hand2", takefocus=1)
        self._var, self._value, self._command = variable, value, command
        self._kind, self._text = kind, text
        self._hover = self._focused = False
        self._task = None
        self._frac = 1.0 if self._checked() else 0.0
        self._var.trace_add("write", lambda *_: self._sync(True))
        self.bind("<Button-1>", self._toggle)
        self.bind("<space>", self._toggle)
        self.bind("<Return>", self._toggle)
        self.bind("<Enter>", lambda e: self._set_hover(True))
        self.bind("<Leave>", lambda e: self._set_hover(False))
        self.bind("<FocusIn>", lambda e: self._set_focus(True))
        self.bind("<FocusOut>", lambda e: self._set_focus(False))
        self._draw()

    # -- state ------------------------------------------------------------
    def _checked(self) -> bool:
        if self._value is None:
            return bool(self._var.get())
        return self._var.get() == self._value

    def _toggle(self, _event=None):
        if self._value is None:
            self._var.set(not bool(self._var.get()))
        else:
            self._var.set(self._value)
        if self._command:
            self._command()
        return "break"

    def _set_hover(self, on: bool):
        self._hover = on
        self._draw()

    def _set_focus(self, on: bool):
        self._focused = on
        self._draw()

    def _sync(self, animate: bool = False):
        target = 1.0 if self._checked() else 0.0
        if not animate or abs(target - self._frac) < 0.01:
            self._frac = target
            self._draw()
            return
        if self._task:
            try:
                self.after_cancel(self._task)
            except Exception:
                pass
        start, state = self._frac, {"i": 0}

        def tick():
            state["i"] += 1
            self._frac = start + (target - start) * state["i"] / 7
            self._draw()
            if state["i"] < 7:
                self._task = self.after(13, tick)
            else:
                self._task = None

        tick()

    # -- painting ---------------------------------------------------------
    def _draw(self):
        self.delete("all")
        f = self._frac
        height = int(self["height"])
        cy = height / 2
        x0, y0 = 1.5, cy - self.BOX / 2
        x1, y1 = 1.5 + self.BOX, cy + self.BOX / 2
        if self._focused:
            self.create_rectangle(0, 0, int(self["width"]) - 1, height - 1,
                                  outline=FIELD_H, dash=(2, 2))
        outline = ACCENT if f > 0.5 else (HOVER_EDGE if self._hover else EDGE)
        fill = lerp_hex(FIELD, ACCENT, f) if f > 0.01 else FIELD
        mid = (x0 + x1) / 2
        if self._kind == "radio":
            self.create_oval(x0, y0, x1, y1, fill=fill, outline=outline, width=1.5)
            if f > 0.15:
                r = 3.6 * min(1.0, f)
                self.create_oval(mid - r, cy - r, mid + r, cy + r,
                                 fill="#ffffff", outline="")
        else:
            self.create_rectangle(x0, y0, x1, y1, fill=fill, outline=outline, width=1.5)
            if f > 0.2:
                self.create_line(mid - 3.6, cy + 0.3, mid - 1.2, cy + 2.9,
                                 mid + 3.4, cy - 3.3, fill="#ffffff", width=2,
                                 capstyle="round", joinstyle="round")
        self.create_text(self.BOX + self.GAP, cy, text=self._text, anchor="w",
                         fill=FG, font=FONT_UI)


def section(master, title: str, pady=(0, 0)) -> tk.Frame:
    """Titled, bordered panel that groups related controls inside a tab.

    Packs itself into `master` and returns the padded inner frame, so callers
    add their rows straight into it.
    """
    box = tk.Frame(master, bg=PANEL, highlightthickness=1,
                   highlightbackground=BORDER, highlightcolor=BORDER)
    box.pack(fill="x", pady=pady)
    inner = tk.Frame(box, bg=PANEL, padx=14, pady=9)
    inner.pack(fill="both", expand=True)
    tk.Label(inner, text=title, bg=PANEL, fg=MUTED, font=FONT_SEMI,
             anchor="w").pack(anchor="w", pady=(0, 7))
    return inner


def clamp_scroll(cv: tk.Canvas) -> None:
    """Pin a scroll canvas' origin back inside its scroll region.

    Tk does NOT clamp `yview_scroll` when the scroll region is SHORTER than the
    viewport - i.e. when the content fits and there is nothing to scroll to.
    Scrolling up then walks the canvas origin negative, and because the embedded
    content frame sits at canvas y=0 it is drawn that many pixels *down* the
    canvas: the tab shows a blank PANEL band above its content with the bottom
    of the content pushed out of the card. Measured on this Tk build, six
    wheel-up notches with `yscrollincrement=40` put the origin at -240.

    Two details make it hard to catch:

    * `yview()` keeps reporting `(0.0, 1.0)` in that state, so anything that
      reads the view fraction (e.g. `refresh_gallery`'s `keep`) believes the
      canvas is at the top. `canvasy(0)` is the honest reading.
    * Re-setting the same `scrollregion` does not repair it; Tk only re-clamps
      when the region shrinks past a *positive* origin.

    `yview_moveto` *is* clamped, so it is the repair path. Cheap enough to run
    on every layout pass.
    """
    try:
        x0, y0, x1, y1 = (float(v) for v in str(cv.cget("scrollregion")).split())
    except (ValueError, tk.TclError):
        return
    top = cv.canvasy(0)
    lo = y0
    hi = max(y0, y1 - cv.winfo_height())
    if lo <= top <= hi:
        return
    cv.yview_moveto((min(max(top, lo), hi) - y0) / max(1.0, y1 - y0))


class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("FastFetch Studio")
        self.geometry("880x720")
        self.minsize(760, 620)
        self.configure(bg=BG)
        setup_style()
        self.selected_path: str | None = None
        self._thumb_refs: dict[str, ImageTk.PhotoImage] = {}
        self._tab_objs: list[AnimatedTab] = []
        self._tab_frames: dict[str, tk.Frame] = {}

        head = tk.Frame(self, bg=BG)
        head.pack(fill="x", padx=16, pady=(12, 2))
        dot = tk.Canvas(head, width=10, height=10, bg=BG, highlightthickness=0)
        dot.create_oval(1, 1, 9, 9, fill=ACCENT, outline="")
        dot.pack(side="left")
        tk.Label(head, text=" FastFetch Studio", bg=BG, fg=FG,
                 font=("Segoe UI Semibold", 13)).pack(side="left")
        tk.Label(head, text="random fetch, your way", bg=BG, fg=MUTED,
                 font=FONT_SMALL).pack(side="left", padx=(10, 0), pady=(5, 0))

        body = tk.Frame(self, bg=BG)
        body.pack(fill="both", expand=True, padx=16)
        bar = tk.Frame(body, bg=BG)
        bar.pack(anchor="w", pady=(6, 0))
        for name in ("Gallery", "Theme", "Random"):
            t = AnimatedTab(bar, name, self._show_tab)
            t.pack(side="left", padx=(0, 4))
            self._tab_objs.append(t)
        self._card = tk.Frame(body, bg=PANEL, highlightthickness=1,
                              highlightbackground=BORDER, highlightcolor=BORDER)
        self._card.pack(fill="both", expand=True, pady=(0, 10))
        inner = tk.Frame(self._card, bg=PANEL)
        inner.pack(fill="both", expand=True)
        self._grid_scroll: tk.Canvas | None = None  # gallery image grid only
        self._scroll_canvases: dict[str, tk.Canvas] = {}
        self._tab_frames = {}
        for name in ("Gallery", "Theme", "Random"):
            outer = tk.Frame(inner, bg=PANEL)
            # place() overlays the three pages; tkraise() switches them.
            # (pack() would stack them vertically - the v1.1.0 bug.)
            outer.place(x=0, y=0, relwidth=1, relheight=1)
            # Plain frames everywhere: no per-tab scroll area (and no
            # scrollbar widget at all). The gallery grid gets its own
            # wheel-scrollable canvas inside _build_gallery_tab.
            content = tk.Frame(outer, bg=PANEL, padx=16, pady=14)
            content.pack(fill="both", expand=True)
            self._tab_frames[name] = outer
            setattr(self, "tab_" + name.lower(), content)

        self.status_var = tk.StringVar(value="Ready")
        sb = tk.Frame(self, bg=PANEL, height=30)
        sb.pack(fill="x", side="bottom")
        sb.pack_propagate(False)
        self.status_dot = tk.Canvas(sb, width=8, height=8, bg=PANEL, highlightthickness=0)
        self.status_dot.pack(side="left", padx=(16, 6))
        self.status_dot.create_oval(1, 1, 7, 7, fill=OKC, outline="")
        tk.Label(sb, textvariable=self.status_var, bg=PANEL, fg=MUTED,
                 font=FONT_SMALL, anchor="w").pack(side="left", fill="x", expand=True)

        self._build_gallery_tab()
        self._build_theme_tab()
        self._build_random_tab()
        self.bind_all("<MouseWheel>", self._on_mousewheel)
        self._show_tab("Gallery")
        if not STATE["gallery"]:
            self.after(0, self._seed_gallery, True)
        self.after(0, self.refresh_gallery)

    def _show_tab(self, name: str):
        self._current_tab = name
        for obj, nm in zip(self._tab_objs, ("Gallery", "Theme", "Random")):
            obj.set_active(nm == name)
        for nm, f in self._tab_frames.items():
            if nm == name:
                f.tkraise()

    def _make_scroll_area(self, parent, tab_name: str) -> tk.Frame:
        """Wheel-scrollable content column, registered against `tab_name`.

        Deliberately scrollbar-free: the ttk scrollbar rendered as a bright
        white bar on Windows. The Theme tab needs it because its control list
        is taller than a small window - without it the profile snippet and the
        button row were simply clipped off the bottom of the card.
        """
        canvas = tk.Canvas(parent, bg=PANEL, highlightthickness=0, bd=0,
                           yscrollincrement=40)
        canvas.pack(side="left", fill="both", expand=True)
        content = tk.Frame(canvas, bg=PANEL)
        win = canvas.create_window((0, 0), window=content, anchor="nw")

        def sync_scroll():
            # Size the region from the frame's REQUIRED size, not bbox("all"):
            # right after a rebuild the embedded window still reports its old
            # height, which left a stale oversized region and blank scroll
            # space. after_idle lets the child's geometry settle first.
            canvas.itemconfigure(win, width=canvas.winfo_width())
            canvas.configure(scrollregion=(0, 0, max(1, content.winfo_reqwidth()),
                                           max(1, content.winfo_reqheight())))
            # Setting the region is not enough on its own: it does not repair
            # an origin that already walked negative, so heal it here too.
            clamp_scroll(canvas)

        content.bind("<Configure>", lambda e: canvas.after_idle(sync_scroll))
        canvas.bind("<Configure>", lambda e: canvas.after_idle(sync_scroll))
        self._scroll_canvases[tab_name] = canvas
        return content

    def _make_grid_scroll(self, parent) -> tk.Frame:
        """Wheel-scrollable canvas for the gallery image grid."""
        self._grid_scroll = None
        content = self._make_scroll_area(parent, "Gallery")
        self._grid_scroll = self._scroll_canvases["Gallery"]
        return content

    def _on_mousewheel(self, e):
        # Scroll only the canvas owned by the visible tab; the wheel must not
        # move (or blank) anything else.
        cv = self._scroll_canvases.get(getattr(self, "_current_tab", ""))
        if cv is None or not e.delta:
            return
        # Direction from the sign, magnitude from whole notches with a floor of
        # one increment: `e.delta // 120` truncated toward -inf, so a wheel or
        # touchpad reporting less than a full 120 notch (common on high-res
        # devices) either did nothing or inverted the direction.
        notches = int(abs(e.delta) // 120) or 1
        cv.yview_scroll(-notches if e.delta > 0 else notches, "units")
        # Tk leaves the origin unclamped when the content fits, which is what
        # pushed the tab content down the card; put it back in range.
        clamp_scroll(cv)

    # ---------------------------------------------------------------- status
    def status(self, msg: str):
        self.status_var.set(msg)

    # ---------------------------------------------------------------- gallery tab
    def _build_gallery_tab(self):
        f = self.tab_gallery
        # Toolbar + hint stay fixed at the top; only the image grid scrolls.
        bar = tk.Frame(f, bg=PANEL)
        bar.pack(fill="x", pady=(0, 10))
        AnimatedButton(bar, "accent", text="Add images...", command=self.add_images).pack(side="left", padx=(0, 8))
        AnimatedButton(bar, text="Remove selected", command=self.remove_selected).pack(side="left", padx=(0, 8))
        AnimatedButton(bar, text="Set as default", command=self.set_default).pack(side="left", padx=(0, 8))
        AnimatedButton(bar, text="Edit size...", command=self.edit_size).pack(side="left")
        tk.Label(f, text="Click to select \u00b7 double-click to edit size \u00b7 uploads become the default logo",
                 bg=PANEL, fg=MUTED, font=FONT_SMALL).pack(anchor="w", pady=(0, 10))

        holder = tk.Frame(f, bg=PANEL)
        holder.pack(fill="both", expand=True)
        self.gallery_inner = self._make_grid_scroll(holder)
        self.gallery_cols = int(STATE.get("galleryCols", 4))

    def _seed_gallery(self, silent: bool = False):
        found = []
        for d in (PNGS_DIR, FF_DIR / "images"):
            if d.is_dir():
                for p in sorted(d.glob("*.png")):
                    found.append({"path": str(p), "w": STATE["logWidth"], "h": STATE["logHeight"]})
        if found:
            STATE["gallery"] = found
            STATE["defaultImage"] = found[0]["path"]
            save_state(STATE)
            self.after(0, lambda: self.status(f"Imported {len(found)} existing PNG(s) - click Apply to use them"))

    def add_images(self):
        files = filedialog.askopenfilenames(
            parent=self, title="Choose images",
            filetypes=[("Images", "*.png *.jpg *.jpeg *.webp *.gif *.bmp"), ("All files", "*.*")])
        if not files:
            return
        added = skipped = failed = 0
        for f in files:
            try:
                dest = convert_to_png(Path(f))
            except Exception as e:
                failed += 1
                self.status(f"Failed: {Path(f).name} ({e})")
                continue
            if any(g["path"] == str(dest) for g in STATE["gallery"]):
                skipped += 1
                continue
            STATE["gallery"].append({"path": str(dest), "w": STATE["logWidth"], "h": STATE["logHeight"]})
            last_added = str(dest)
            added += 1
        if added:
            # Newest upload becomes the default logo immediately.
            STATE["defaultImage"] = last_added
        self.refresh_gallery()
        self._sync_sixels()
        save_state(STATE)
        msg = f"Added {added}, already present {skipped}, failed {failed}"
        if added:
            msg += f" - default logo: {Path(last_added).name}"
        self.status(msg)

    def remove_selected(self):
        if not self.selected_path:
            self.status("Select an image first")
            return
        STATE["gallery"] = [g for g in STATE["gallery"] if g["path"] != self.selected_path]
        if STATE.get("defaultImage") == self.selected_path:
            STATE["defaultImage"] = STATE["gallery"][0]["path"] if STATE["gallery"] else ""
        self.selected_path = None
        self._sync_sixels()
        save_state(STATE)
        self.refresh_gallery()
        self.status("Removed from gallery (file kept on disk)")

    def set_default(self):
        if not self.selected_path:
            self.status("Select an image first")
            return
        STATE["defaultImage"] = self.selected_path
        self.refresh_gallery()
        self._sync_sixels()
        save_state(STATE)
        self.status(f"Default logo: {Path(self.selected_path).name} (live in new terminals)")

    def edit_size(self):
        if not self.selected_path:
            self.status("Select an image first")
            return
        g = next((x for x in STATE["gallery"] if x["path"] == self.selected_path), None)
        if not g:
            return
        dlg = SizeDialog(self, "Image size in terminal cells", g["w"], g["h"])
        if dlg.result:
            g["w"], g["h"] = dlg.result
            self.refresh_gallery()
            self._sync_sixels()
            save_state(STATE)
            self.status(f"Size for {Path(g['path']).name}: {g['w']}x{g['h']} cells (applied)")

    def _sync_sixels(self):
        """Re-encode sixels so add/default/size changes work without Apply."""
        try:
            ensure_sixels(STATE)
        except Exception:
            pass

    def refresh_gallery(self):
        # Remember the view position so rebuilds (select / set default /
        # size change recreate every cell) don't snap the grid to the top.
        keep = self._grid_scroll.yview()[0] if self._grid_scroll is not None else 0.0
        for c in self.gallery_inner.winfo_children():
            c.destroy()
        self._thumb_refs.clear()
        gallery = STATE.get("gallery", [])
        if not gallery:
            tk.Label(self.gallery_inner, fg=MUTED, bg=PANEL, font=FONT_UI, pady=40,
                     text="No images yet. Click 'Add images...' to upload PNG / JPG / WEBP / GIF.").pack()
            return
        cols = max(1, self.gallery_cols)
        for i, g in enumerate(gallery):
            r, c = divmod(i, cols)
            # sticky="n" matters: without it Tk centres each cell vertically in
            # its row, so a cell with a shorter thumbnail floated down and the
            # row lost its baseline.
            cell = tk.Frame(self.gallery_inner, bg=PANEL)
            cell.grid(row=r, column=c, padx=8, pady=8, sticky="n")
            is_sel = self.selected_path == g["path"]
            is_def = STATE.get("defaultImage") == g["path"]
            ring = ACCENT if is_sel else BORDER

            # Fixed-size box holding the thumbnail. load_preview_image() keeps
            # the source aspect ratio, so a wide logo and a tall portrait used
            # to produce different-sized labels and stagger the row; the box
            # gives every cell the same footprint and letterboxes the image.
            # The box (not the image label) carries the selection ring, so the
            # highlight is a constant rectangle.
            # Filled with PANEL, not FIELD: most uploads are transparent PNGs
            # meant for a dark terminal, so matching the card surface lets them
            # sit flush instead of inside a visibly lighter tile.
            box = tk.Frame(cell, bg=PANEL, width=THUMB_PX, height=THUMB_PX,
                           highlightthickness=2, highlightbackground=ring,
                           highlightcolor=ring)
            box.pack()
            box.pack_propagate(False)

            ph = load_preview_image(g["path"], THUMB_PX)
            if ph is not None:
                self._thumb_refs[g["path"]] = ImageTk.PhotoImage(ph)
                img_label = tk.Label(box, image=self._thumb_refs[g["path"]],
                                     bg=PANEL, bd=0)
            else:
                img_label = tk.Label(box, text="?", bg=PANEL, fg=MUTED,
                                     font=FONT_UI, bd=0)
            img_label.place(relx=0.5, rely=0.5, anchor="center")

            name = Path(g["path"]).name
            info = f"{name}\n{g['w']}x{g['h']} cells"
            tk.Label(cell, text=info, justify="center", bg=PANEL,
                     fg=FG if is_sel else MUTED, font=FONT_SMALL).pack()
            if is_def:
                tk.Label(cell, text="\u2605 default logo", bg=PANEL, fg=ACCENT,
                         font=FONT_SMALL).pack()
            path = g["path"]
            for w in (box, img_label):
                w.bind("<Button-1>", lambda e, p=path: self._select(p))
                w.bind("<Double-Button-1>", lambda e, p=path: (self._select(p), self.edit_size()))
                w.bind("<Enter>", lambda e, bx=box, sel=is_sel: animate_highlight(bx, ACCENT if sel else FIELD_H), add="+")
                w.bind("<Leave>", lambda e, bx=box, sel=is_sel: animate_highlight(bx, ACCENT if sel else BORDER), add="+")
        cv = self._grid_scroll
        if cv is not None:
            self.after_idle(lambda: cv.yview_moveto(keep))

    def _select(self, path: str):
        self.selected_path = path
        self.refresh_gallery()

    # ---------------------------------------------------------------- theme tab
    def _build_theme_tab(self):
        f = self.tab_theme
        # The button row stays pinned to the bottom (packed first with
        # side="bottom"), exactly like the gallery toolbar; everything above it
        # lives in a scroll area so a short window never clips the snippet.
        btns = tk.Frame(f, bg=PANEL)
        btns.pack(side="bottom", fill="x", pady=(10, 0))
        self.apply_btn = AnimatedButton(btns, "accent", text="Apply & Generate",
                                        command=self.on_apply)
        self.apply_btn.pack(side="left", padx=(0, 8))
        AnimatedButton(btns, text="Preview in terminal", command=self.on_preview).pack(side="left", padx=(0, 8))
        AnimatedButton(btns, text="Open config folder",
                       command=lambda: os.startfile(str(FF_DIR))).pack(side="left")
        tk.Frame(f, bg=BORDER, height=1).pack(side="bottom", fill="x", pady=(10, 0))

        holder = tk.Frame(f, bg=PANEL)
        holder.pack(fill="both", expand=True)
        body = self._make_scroll_area(holder, "Theme")

        top = tk.Frame(body, bg=PANEL)
        top.pack(fill="x")
        left = tk.Frame(top, bg=PANEL)
        left.pack(side="left", fill="both", expand=True, padx=(0, 14))
        right = tk.Frame(top, bg=PANEL)
        right.pack(side="left", fill="y")

        # One grid for all seven rows, so every label shares column 0 and the
        # ColorEntry widgets line up - a per-row frame let the longest label
        # ("GPU Driver / Memory / Disks") push its swatch out of alignment and
        # the fixed width=24 labels were clipped.
        colors = section(left, "KEY COLORS \u00b7 per module group")
        grid = tk.Frame(colors, bg=PANEL)
        grid.pack(fill="x")
        grid.columnconfigure(0, minsize=205)
        self.color_entries: dict[str, ColorEntry] = {}
        for i, (key, label, _hint) in enumerate(GROUPS):
            tk.Label(grid, text=label, bg=PANEL, fg=FG, font=FONT_UI,
                     anchor="w").grid(row=i, column=0, sticky="w", pady=1)
            ce = ColorEntry(grid, STATE["groups"].get(key, ACCENT),
                            on_change=self._on_color_changed)
            ce.grid(row=i, column=1, sticky="w", pady=1)
            self.color_entries[key] = ce

        border = section(left, "BOX BORDER", pady=(12, 0))
        brow = tk.Frame(border, bg=PANEL)
        brow.pack(fill="x")
        brow.columnconfigure(0, minsize=205)
        tk.Label(brow, text="Style", bg=PANEL, fg=FG, font=FONT_UI,
                 anchor="w").grid(row=0, column=0, sticky="w", pady=3)
        self.box_style = tk.StringVar(value=STATE.get("boxStyle", "rounded"))
        ttk.Combobox(brow, textvariable=self.box_style, state="readonly", width=12,
                     values=list(BOX_STYLES.keys())).grid(row=0, column=1, sticky="w", pady=3)
        crow = tk.Frame(border, bg=PANEL)
        crow.pack(fill="x")
        crow.columnconfigure(0, minsize=205)
        tk.Label(crow, text="Border color (blank = default)", bg=PANEL, fg=FG,
                 font=FONT_UI, anchor="w").grid(row=0, column=0, sticky="w", pady=3)
        self.box_color = ColorEntry(crow, STATE.get("boxColor") or ACCENT)
        self.box_color.grid(row=0, column=1, sticky="w", pady=3)

        size = section(right, "LOGO SIZE \u00b7 default")
        sz = tk.Frame(size, bg=PANEL)
        sz.pack(anchor="w")
        self.log_w = tk.StringVar(value=str(STATE.get("logWidth", 28)))
        self.log_h = tk.StringVar(value=str(STATE.get("logHeight", 24)))
        tk.Label(sz, text="W", bg=PANEL, fg=FG).grid(row=0, column=0)
        ttk.Spinbox(sz, from_=10, to=80, textvariable=self.log_w, width=5).grid(row=0, column=1, padx=(2, 10))
        tk.Label(sz, text="H", bg=PANEL, fg=FG).grid(row=0, column=2)
        ttk.Spinbox(sz, from_=8, to=60, textvariable=self.log_h, width=5).grid(row=0, column=3, padx=2)
        tk.Label(size, text="terminal cells", bg=PANEL, fg=MUTED,
                 font=FONT_SMALL).pack(anchor="w", pady=(6, 0))

        prev = section(right, "ACCENT PALETTE PREVIEW", pady=(12, 0))
        self.palette_canvas = tk.Canvas(prev, width=8 * 28 + 8, height=28, bg=PANEL,
                                        highlightthickness=0)
        self.palette_canvas.pack(anchor="w")
        self._draw_palette()

        # OPTIONS lives in the right column: the left column already carries
        # the tallest block (seven colour rows), and stacking a third section
        # under it pushed the profile snippet out of the card entirely.
        opts = section(right, "OPTIONS", pady=(12, 0))
        srow = tk.Frame(opts, bg=PANEL)
        srow.pack(fill="x")
        srow.columnconfigure(0, minsize=205)
        tk.Label(srow, text="Separator (key / value)", bg=PANEL, fg=FG,
                 font=FONT_UI, anchor="w").grid(row=0, column=0, sticky="w", pady=3)
        self.sep_var = tk.StringVar(value=STATE.get("separator", " : "))
        ttk.Entry(srow, textvariable=self.sep_var, width=8).grid(row=0, column=1, sticky="w", pady=3)
        self.colors_block = tk.BooleanVar(value=STATE.get("colorsBlock", True))
        FFToggle(opts, "Show palette block at bottom",
                 self.colors_block).pack(anchor="w", pady=(8, 0))

        # Full width on purpose: the snippet's second line is 63 characters,
        # which clipped inside the narrow right-hand column. The Copy button
        # sits beside the code rather than under it, which keeps the whole
        # section ~36px shorter - enough for the tab to fit without scrolling
        # at the default window size.
        snip_box = section(body, "PROFILE SNIPPET \u00b7 paste into your PowerShell $PROFILE",
                           pady=(12, 0))
        srow = tk.Frame(snip_box, bg=PANEL)
        srow.pack(fill="x")
        # Pack the button first: expand=True on the text would otherwise eat
        # the whole row and push the button out of view.
        AnimatedButton(srow, text="Copy snippet", command=self.copy_snippet,
                       font=FONT_SMALL, pady=4).pack(side="right", padx=(8, 0))
        snippet = tk.Text(srow, height=2, width=46, wrap="none", font=FONT_MONO,
                          bg=FIELD, fg=FG, insertbackground=FG, relief="flat",
                          highlightthickness=1, highlightbackground=BORDER, padx=8, pady=6)
        snippet.insert("1.0", profile_snippet())
        snippet.configure(state="disabled")
        snippet.pack(side="left", fill="x", expand=True)

    def _on_color_changed(self):
        """Live-update the accent palette strip while colors are edited."""
        try:
            self._draw_palette()
        except Exception:
            pass

    def _draw_palette(self):
        cv = self.palette_canvas
        cv.delete("all")
        groups = {k: ce.get() for k, ce in self.color_entries.items()}
        for i, pal in enumerate(palettes_for(groups)):
            x0 = 4 + i * 28
            cv.create_rectangle(x0, 4, x0 + 24, 24, fill=pal.get("accent", ACCENT), outline="")

    # ---------------------------------------------------------------- random tab
    def _build_random_tab(self):
        f = self.tab_random
        holder = tk.Frame(f, bg=PANEL)
        holder.pack(fill="both", expand=True)
        f = self._make_scroll_area(holder, "Random")

        what = section(f, "WHAT CHANGES EVERY TIME YOU OPEN A TERMINAL")
        self.rand_logo = tk.BooleanVar(value=STATE.get("randomLogo", True))
        self.rand_theme = tk.BooleanVar(value=STATE.get("randomTheme", True))
        FFToggle(what, "Random logo (off = use default image)", self.rand_logo,
                 command=self._random_changed).pack(anchor="w", pady=3)
        FFToggle(what, "Random color theme (8 palettes generated from your colors)",
                 self.rand_theme, command=self._random_changed).pack(anchor="w", pady=3)

        how = section(f, "HOW OFTEN", pady=(12, 0))
        self.freq = tk.StringVar(value=STATE.get("frequency", "every"))
        FFToggle(how, "Every new terminal window", self.freq, value="every",
                 kind="radio", command=self._random_changed).pack(anchor="w", pady=3)
        FFToggle(how, "Once per day (same look all day)", self.freq, value="daily",
                 kind="radio", command=self._random_changed).pack(anchor="w", pady=3)

        # The launcher can only guess whether a terminal renders images, and a
        # wrong guess used to swap a chosen logo for the built-in ASCII one with
        # no explanation. This is the override for when the guess is wrong.
        logo = section(f, "HOW THE LOGO IS DRAWN", pady=(12, 0))
        self.logo_mode = tk.StringVar(value=STATE.get("logoMode", "auto"))
        FFToggle(logo, "Auto - use the image wherever the terminal can show it",
                 self.logo_mode, value="auto", kind="radio",
                 command=self._random_changed).pack(anchor="w", pady=3)
        FFToggle(logo, "Always draw the image, even in an unrecognised terminal",
                 self.logo_mode, value="image", kind="radio",
                 command=self._random_changed).pack(anchor="w", pady=3)
        FFToggle(logo, "Never - always fastfetch's built-in ASCII logo",
                 self.logo_mode, value="builtin", kind="radio",
                 command=self._random_changed).pack(anchor="w", pady=3)

        # The picture is drawn once, at shell start. A resize after that reflows
        # the text under it and tears the image into bands, and Windows offers no
        # resize event to react to - so the launcher's prompt notices the change
        # and redraws at the new size. Repairing means erasing the torn copy
        # first, which is why this is a switch rather than simply always on.
        self.redraw_resize = tk.BooleanVar(value=STATE.get("redrawOnResize", True))
        FFToggle(logo, "Re-draw the fetch when the window is resized",
                 self.redraw_resize, command=self._random_changed).pack(anchor="w", pady=3)
        tk.Label(logo, text="scrolls the torn copy away and draws the fetch again after a "
                            "resize (scrollback is kept); off = a resized window keeps the "
                            "torn picture until a new shell is opened",
                 bg=PANEL, fg=MUTED, font=FONT_SMALL, anchor="w", justify="left",
                 wraplength=560).pack(anchor="w", pady=(0, 4))

        # How big one cell is decides how big the pre-encoded picture is: a
        # sixel is placed at its pixel size, so a logo encoded for the wrong
        # cell lands beside the text by fractions of a cell - and text written
        # into the picture's own cells is what made Windows Terminal redraw it
        # in bands. 10x20 is the shipped default; one measurement makes it exact.
        self.cell_label = tk.Label(logo, bg=PANEL, fg=MUTED, font=FONT_SMALL,
                                   anchor="w", justify="left", wraplength=560)
        self.cell_label.pack(anchor="w", pady=(6, 2))
        self._refresh_cell_label()
        measure = tk.Frame(logo, bg=PANEL)
        measure.pack(anchor="w", pady=(0, 2))
        AnimatedButton(measure, text="Measure this terminal", command=self._measure_terminal,
                       padx=10, pady=4).pack(side="left")
        tk.Label(measure, text="  opens a short-lived window, reads its real cell size, "
                               "and re-encodes the logos",
                 bg=PANEL, fg=MUTED, font=FONT_SMALL).pack(side="left")

        # Aligned columns beat the old flat tk.Text blob, which sat dark-on-dark
        # and relied on hand-counted spaces for its "columns".
        files = section(f, "FILES FASTFETCH STUDIO MANAGES", pady=(12, 0))
        table = tk.Frame(files, bg=PANEL)
        table.pack(fill="x")
        table.columnconfigure(1, minsize=190)
        for i, (name, desc) in enumerate(MANAGED_FILES):
            tk.Label(table, text="\u2022", bg=PANEL, fg=ACCENT,
                     font=FONT_MONO).grid(row=i, column=0, sticky="w", padx=(0, 8), pady=1)
            tk.Label(table, text=name, bg=PANEL, fg=FG, font=FONT_MONO,
                     anchor="w").grid(row=i, column=1, sticky="w", pady=1)
            tk.Label(table, text=desc, bg=PANEL, fg=MUTED, font=FONT_SMALL,
                     anchor="w").grid(row=i, column=2, sticky="w", padx=(10, 0), pady=1)

    def _refresh_cell_label(self):
        cw, ch = logo_cell_px(STATE)
        measured = STATE.get("cellSource") == "measured"
        if measured:
            text = (f"logo cells: {cw}x{ch} pixels - measured in this terminal, so the "
                    f"picture lands on whole cells")
        else:
            text = (f"logo cells: {cw}x{ch} pixels (default guess) - measure this terminal "
                    f"if the picture sits a little off beside the text")
        try:
            self.cell_label.configure(text=text)
        except Exception:
            pass

    def _measure_terminal(self):
        script = write_measure_script(STATE)
        if script is None:
            self.status("Measuring needs Pillow (pip install pillow) - keeping the default cell")
            return
        METRICS_PATH.unlink(missing_ok=True)
        ok, msg = spawn_measure(script)
        self.status(msg)
        if ok:
            threading.Thread(target=self._await_metrics, daemon=True).start()

    def _await_metrics(self, timeout: float = 30.0):
        """Wait for the probe window to report, then re-draw everything at its size."""
        end = time.time() + timeout
        found = {}
        while time.time() < end:
            found = load_metrics()
            if found:
                break
            time.sleep(0.4)
        try:
            self.after(0, self._apply_metrics, found)
        except Exception:
            pass

    def _apply_metrics(self, metrics: dict):
        if not metrics:
            self.status("Could not measure this terminal - the logos keep the default cell")
            return
        STATE["cellW"] = metrics["cellW"]
        STATE["cellH"] = metrics["cellH"]
        STATE["cellSource"] = "measured"
        save_state(STATE)
        # The sixels are the thing that has to change: they are encoded at this
        # cell size, and the launcher reads the cell count back out of them.
        self._sync_sixels()
        write_config(STATE)
        generate_theme_files(STATE)
        generate_launcher(STATE)
        self._refresh_cell_label()
        self.status(f"One cell is {metrics['cellW']}x{metrics['cellH']} pixels - "
                    f"logos re-encoded; press Apply & Generate to keep it")

    def _random_changed(self):
        STATE["randomLogo"] = bool(self.rand_logo.get())
        STATE["randomTheme"] = bool(self.rand_theme.get())
        STATE["frequency"] = self.freq.get()
        STATE["logoMode"] = self.logo_mode.get()
        STATE["redrawOnResize"] = bool(self.redraw_resize.get())
        save_state(STATE)
        # Toggling rewrites the launcher, and the launcher passes
        # --config <theme>. Writing it while the themes it names were absent is
        # what left fastfetch falling back to a foreign config (the icon bug),
        # so the config and themes are refreshed here too. Sixels are left
        # alone: re-encoding every gallery image on a toggle is slow and
        # changes nothing.
        write_config(STATE)
        generate_theme_files(STATE)
        generate_launcher(STATE)
        self.status("Random settings updated in launcher")

    # ---------------------------------------------------------------- actions
    def _collect(self) -> bool:
        groups = {k: ce.get() for k, ce in self.color_entries.items()}
        groups.setdefault("accent", groups.get("title") or ACCENT)
        STATE["groups"] = groups
        STATE["separator"] = self.sep_var.get() or " : "
        STATE["boxStyle"] = self.box_style.get()
        bc = self.box_color.var.get().strip()
        STATE["boxColor"] = norm_hex(bc, "") if bc else ""
        STATE["colorsBlock"] = bool(self.colors_block.get())
        try:
            STATE["logWidth"] = max(10, min(80, int(self.log_w.get())))
            STATE["logHeight"] = max(8, min(60, int(self.log_h.get())))
        except ValueError:
            return False
        return True

    def on_apply(self):
        if not self._collect():
            self.status("Logo size must be numbers")
            return
        try:
            summary = apply_all(STATE)
        except Exception as e:
            messagebox.showerror("Apply failed", str(e), parent=self)
            return
        self.status(f"Applied: {summary}. Open a new terminal or click Preview.")
        messagebox.showinfo(
            "Applied",
            "Configs generated:\n"
            f"  \u2022 8 themes in {THEMES_DIR}\n"
            f"  \u2022 {LAUNCHER_PATH}\n\n"
            "Make sure your PowerShell profile calls fastfetch-random.ps1\n"
            "(copy the snippet from the Theme tab into $PROFILE).",
            parent=self)

    def on_preview(self):
        img = STATE.get("defaultImage") or (STATE["gallery"][0]["path"] if STATE.get("gallery") else "")
        ok, msg = spawn_terminal_preview(STATE, img)
        self.status(msg)
        if not ok:
            messagebox.showwarning("Preview", msg, parent=self)

    def copy_snippet(self):
        self.clipboard_clear()
        self.clipboard_append(profile_snippet())
        self.status("Profile snippet copied - paste it into your PowerShell $PROFILE")


def smoke_gui() -> int:
    """Headless GUI check: build the window, exercise palette + collect + apply."""
    app = App()
    app.update_idletasks()
    app._draw_palette()  # raised KeyError before the accent fix
    if not app._collect():
        print("smoke-gui: _collect failed")
        return 1
    if "accent" not in STATE["groups"]:
        print("smoke-gui: accent missing from collected groups")
        return 1
    summary = apply_all(STATE)
    app.destroy()
    print(f"smoke-gui: collect=ok; {summary}")
    return 0


def smoke_scroll() -> int:
    """Regression guard for the unclamped scroll-origin bug.

    Tk does not clamp `yview_scroll` when the scroll region is shorter than the
    viewport, so a wheel-up nudge with nothing to scroll to walked the canvas
    origin negative and pushed the whole tab content down the card. The
    invariant is simply that the origin never sits above the scroll region.
    """
    app = App()
    for _ in range(8):
        app.update_idletasks()
        app.update()

    bad: list[str] = []
    exercised = 0

    def origin_ok(cv, tag):
        try:
            x0, y0, x1, y1 = (float(v) for v in str(cv.cget("scrollregion")).split())
        except ValueError:
            return
        inc = max(1, int(cv.cget("yscrollincrement")))
        top = cv.canvasy(0)
        if top < y0 - 0.5:
            bad.append(f"{tag}: origin {top:.0f} above region top {y0:.0f} "
                       f"(content pushed {-top:.0f}px down)")
        elif top > max(y0, y1 - cv.winfo_height()) + inc:
            bad.append(f"{tag}: origin {top:.0f} past region bottom")

    def spin(delta, n=8):
        for _ in range(n):
            app.event_generate("<MouseWheel>", delta=delta, x=50, y=50)
            app.update_idletasks()
            app.update()

    for name in ("Gallery", "Theme", "Random"):
        app._show_tab(name)
        for _ in range(6):
            app.update_idletasks()
            app.update()
        cv = app._scroll_canvases[name]

        spin(120)
        origin_ok(cv, f"{name}: after 8 x wheel-up")
        spin(-120)
        origin_ok(cv, f"{name}: after 8 x wheel-down")

        # A sub-notch delta (high-resolution wheel / touchpad) must not invert
        # the direction the way `e.delta // 120` did for negative values.
        cv.yview_moveto(0.0)
        app.update_idletasks()
        before = cv.canvasy(0)
        app.event_generate("<MouseWheel>", delta=1, x=50, y=50)
        app.update_idletasks()
        app.update()
        if cv.canvasy(0) > before + 0.5:
            bad.append(f"{name}: delta=+1 inverted the scroll direction")

        # clamp_scroll must repair an origin that already went bad. Forcing the
        # state directly keeps this meaningful even if this runner's window is
        # too short for the content to fit (Tk then clamps on its own).
        cv.yview_scroll(-6, "units")
        if cv.canvasy(0) < 0:
            exercised += 1
            clamp_scroll(cv)
            if cv.canvasy(0) < -0.5:
                bad.append(f"{name}: clamp_scroll left origin at {cv.canvasy(0):.0f}")

    app.destroy()
    if bad:
        print("smoke-scroll: FAIL")
        for b in bad:
            print("  ", b)
        return 1
    print(f"smoke-scroll: ok (origin in range on all 3 tabs; "
          f"repair path exercised on {exercised}/3)")
    return 0


def _is_pua(ch: str) -> bool:
    """True for the private-use codepoints Nerd Font glyphs live in."""
    if not ch:
        return False
    o = ord(ch)
    return (0xE000 <= o <= 0xF8FF          # BMP PUA
            or 0xF0000 <= o <= 0xFFFFD     # plane 15
            or 0x100000 <= o <= 0x10FFFD)  # plane 16


def _find_fastfetch() -> str | None:
    cand = USER / ".local" / "bin" / "fastfetch.exe"
    if cand.exists():
        return str(cand)
    return shutil.which("fastfetch") or shutil.which("fastfetch.exe")


def smoke_icons() -> int:
    """Regression guard for the missing-icons bug.

    Two independent checks, because they catch opposite halves of the same
    mistake. A hand-typed glyph in a key renders *beside* fastfetch's own
    default, so a config can be wrong in both directions - no icon at all, or
    two stacked icons - and only one of them is obvious in a screenshot.

    1. Shape: the generated config must switch key icons on, and no `key`
       string may carry a glyph of its own. Cheap, and needs no fastfetch.

    2. Render: every row fastfetch actually draws with a key must begin with a
       private-use glyph. This is the end-to-end proof. It needs a real
       fastfetch and is skipped with a note when there is none - the CI build
       job has no fastfetch, the setup-verification job does.
    """
    st = json.loads(json.dumps(DEFAULT_STATE))
    themes = [build_config_json({**st, "groups": pal}, pal["accent"])
              for pal in palettes_for(st["groups"])]

    bad: list[str] = []
    keyed = len(KEYED_MODULES)
    for i, cfg in enumerate(themes, start=1):
        dk = cfg.get("display", {}).get("key", {})
        if dk.get("type") != KEY_ICON_MODE:
            bad.append(f"theme-{i:02d}: display.key.type is {dk.get('type')!r}, "
                       f"expected {KEY_ICON_MODE!r} - icons would be off entirely")
        if int(dk.get("paddingLeft", 0)) != KEY_PADDING_LEFT:
            bad.append(f"theme-{i:02d}: display.key.paddingLeft is "
                       f"{dk.get('paddingLeft')!r}, expected {KEY_PADDING_LEFT}")
        if i > 1:
            continue
        kinds: list[str] = []
        for m in cfg["modules"]:
            if not isinstance(m, dict) or "key" not in m:
                continue
            key, kind = m["key"], m["type"]
            if any(_is_pua(c) for c in key):
                bad.append(f"theme-01: {kind} key {key!r} carries its own glyph, "
                           "so it draws a second icon beside fastfetch's default")
            if kind == "title":
                if key != NO_KEY:
                    bad.append(f"theme-01: title key {key!r} is not the no-key "
                               f"sentinel {NO_KEY!r}")
            elif not key.strip():
                bad.append(f"theme-01: {kind} has an empty key")
            else:
                kinds.append(kind)
        keyed = len(kinds)
        # Without this the guard passes vacuously on a config that lost its
        # modules: zero keyed rows would match zero icons to check.
        if tuple(kinds) != KEYED_MODULES:
            bad.append(f"theme-01: keyed modules are {tuple(kinds)}, "
                       f"expected {KEYED_MODULES}")

    ff = _find_fastfetch()
    if ff is not None:
        sep = st.get("separator", " : ")
        ansi = re.compile(r"\x1b\[[0-9;]*m")

        def render(path: Path) -> tuple[list[str] | None, str]:
            """fastfetch's keyed rows for a config, or (None, why) if it would not run."""
            try:
                proc = subprocess.run([ff, "--config", str(path), "--logo", "none", "--pipe"],
                                      capture_output=True, timeout=60)
            except subprocess.TimeoutExpired:
                return None, "timed out after 60s"
            if proc.returncode != 0:
                err = proc.stderr.decode("utf-8", "replace").strip()
                return None, f"exit {proc.returncode}: {err[:120]}"
            lines = proc.stdout.decode("utf-8", "replace").splitlines()
            return [ansi.sub("", ln).rstrip() for ln in lines if sep in ansi.sub("", ln)], ""

        tmp = Path(tempfile.mkdtemp(prefix="ffstudio-icons-"))
        try:
            for i, cfg in enumerate(themes, start=1):
                f = tmp / f"theme-{i:02d}.jsonc"
                f.write_text(dump_jsonc(cfg), "utf-8")
                # A cold fastfetch on a busy machine occasionally renders nothing
                # at all - seen once in the wild as "0 keyed rows" on one theme,
                # then never again. Retrying once keeps an environment hiccup from
                # failing a build. A genuinely broken config renders nothing twice,
                # so this cannot hide a real regression.
                rows, why = render(f)
                if not rows:
                    rows, why = render(f)
                if rows is None:
                    bad.append(f"theme-{i:02d}: fastfetch would not render it ({why})")
                    continue
                if len(rows) != keyed:
                    bad.append(f"theme-{i:02d}: fastfetch drew {len(rows)} keyed rows, "
                               f"expected {keyed}")
                for r in rows:
                    if not _is_pua(next((c for c in r if c != " "), "")):
                        bad.append(f"theme-{i:02d}: row {r.strip()[:40]!r} rendered no icon")
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    if bad:
        print("smoke-icons: FAIL")
        for b in bad[:12]:
            print("  ", b)
        return 1
    suffix = "" if ff else " [render skipped: no fastfetch on PATH]"
    print(f"smoke-icons: ok ({len(themes)} themes; all {keyed} keyed modules carry "
          f"an icon){suffix}")
    return 0


def main() -> int:
    if "--selftest" in sys.argv:
        return selftest()
    if "--smoke-gui" in sys.argv:
        return smoke_gui()
    if "--smoke-scroll" in sys.argv:
        return smoke_scroll()
    if "--smoke-icons" in sys.argv:
        return smoke_icons()
    app = App()
    app.mainloop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
