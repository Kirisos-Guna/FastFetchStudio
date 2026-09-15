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
    from sixel_codec import (ALPHA_THRESHOLD, decode_sixel_pixels,
                             encode_sixel, fit_image_cells, quantize_rgb)
    HAVE_SIXEL = True
except Exception:
    HAVE_SIXEL = False

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
SIXELS_DIR = FF_DIR / "sixels"

IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp"}

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


def box_lines(st: dict) -> tuple[str, str]:
    style = BOX_STYLES.get(st.get("boxStyle", "rounded"), BOX_STYLES["rounded"])
    if style is None:
        return ("", "")
    tl, hz, tr, vt, bl, br = style
    top = tl + hz * BOX_WIDTH + tr
    bot = bl + hz * BOX_WIDTH + br
    prefix = f"\x1b[38;2;{hex_to_sgr(st['boxColor'])}m" if st.get("boxColor") else ""
    suffix = "\x1b[0m" if prefix else ""
    return (f"{prefix}{top}{suffix}", f"{prefix}{bot}{suffix}")


def build_config_json(st: dict, accent: str) -> dict:
    """Fastfetch config as a Python dict (JSONC-ready)."""
    top, bot = box_lines(st)
    custom = st.get("boxStyle", "rounded") != "none"
    mods: list = []
    if top:
        mods.append({"type": "custom", "format": top})
    mods += [
        {"type": "chassis", "key": "   Chassis", "format": "{1} {2} {3}",
         "keyColor": st["groups"]["os"]},
        {"type": "os", "key": "   OS", "format": "{2}", "keyColor": st["groups"]["os"]},
        {"type": "kernel", "key": "   Kernel", "format": "{2}", "keyColor": st["groups"]["os"]},
        {"type": "packages", "key": "   Packages", "keyColor": st["groups"]["pkg"]},
        {"type": "display", "key": "   Display", "format": "{1}x{2} @ {3}Hz [{7}]",
         "keyColor": st["groups"]["pkg"]},
        {"type": "terminal", "key": "   Terminal", "keyColor": st["groups"]["term"]},
        {"type": "wm", "key": "   WM", "format": "{2}", "keyColor": st["groups"]["term"]},
    ]
    if bot:
        mods.append({"type": "custom", "format": bot})
    mods += [
        "break",
        {"type": "title", "key": "  ", "format": "{6} {7} {8}",
         "keyColor": st["groups"]["title"]},
    ]
    if top:
        mods.append({"type": "custom", "format": top})
    mods += [
        {"type": "cpu", "format": "{1} @ {7}", "key": "   CPU",
         "keyColor": st["groups"]["cpu"]},
        {"type": "gpu", "format": "{1} {2}", "key": "   GPU",
         "keyColor": st["groups"]["cpu"]},
        {"type": "gpu", "format": "{3}", "key": "   GPU Driver",
         "keyColor": st["groups"]["drv"]},
        {"type": "memory", "key": "   Memory ", "keyColor": st["groups"]["drv"]},
        {"type": "disk", "key": "   OS Age ", "folders": "/", "keyColor": st["groups"]["drv"],
         "format": "{days} days"},
        {"type": "uptime", "key": "   Uptime ", "keyColor": st["groups"]["drv"]},
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
        "display": {"separator": st.get("separator", " : ")},
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


LAUNCHER_TEMPLATE = r"""# Generated by FastFetch Studio - random logo + theme each run
$ErrorActionPreference = 'SilentlyContinue'
$ffRoot   = Join-Path $env:USERPROFILE '.config\fastfetch'
$ffExe    = Join-Path $env:USERPROFILE '.local\bin\fastfetch.exe'
if (-not (Test-Path $ffExe)) { $ffExe = 'fastfetch.exe' }
$themes   = @(Get-ChildItem -Path (Join-Path $ffRoot 'themes') -Filter 'theme-*.jsonc' -File -ErrorAction SilentlyContinue)
$sixels = @(Get-ChildItem -Path (Join-Path $ffRoot 'sixels') -Filter '*.sixel' -File -ErrorAction SilentlyContinue)
$pngDirs  = @((Join-Path $ffRoot 'pngs'), (Join-Path $ffRoot 'images'))
$pngs     = @($pngDirs | ForEach-Object { Get-ChildItem -Path $_ -Filter '*.png' -File -Recurse -ErrorAction SilentlyContinue } | Sort-Object FullName -Unique)
$statePath = Join-Path $ffRoot 'gui\studio-state.json'

$randLogo = $true; $randTheme = $true; $freq = 'every'; $w = @@W@@; $h = @@H@@; $defaultImg = ''
if (Test-Path $statePath) {
    try {
        $s = Get-Content $statePath -Raw | ConvertFrom-Json
        $randLogo   = [bool]$s.randomLogo
        $randTheme  = [bool]$s.randomTheme
        $freq       = [string]$s.frequency
        $w = [int]$s.logWidth; $h = [int]$s.logHeight
        if ($s.defaultImage) { $defaultImg = [string]$s.defaultImage }
    } catch {}
}

$stamp = (Get-Date).ToString('yyyyMMdd')
$flagPath = Join-Path $ffRoot 'gui\.last-random-run'
if ($freq -eq 'daily' -and (Test-Path $flagPath) -and (Get-Content $flagPath -Raw).Trim() -eq $stamp) { return }

$themeArg = @()
$accent = ''
if ($randTheme -and $themes.Count -gt 0) {
    $theme = Get-Random -InputObject $themes
    $themeArg = @('--config', $theme.FullName)
    try {
        $cfg = Get-Content $theme.FullName -Raw | ConvertFrom-Json
        $accent = ($cfg.modules | Where-Object { $_.type -eq 'os' } | Select-Object -First 1).keyColor
    } catch {}
}

$logoArg = @()
# Only attempt image protocols in terminals that can render them:
# Windows Terminal sets WT_SESSION; WezTerm sets TERM_PROGRAM=WezTerm.
# Classic conhost supports neither - skip straight to the fallback logo.
# The .sixel files are pre-encoded by FastFetch Studio (this fastfetch build
# cannot encode PNG to sixel itself); fastfetch passes the bytes through.
if ($env:WT_SESSION -and $sixels.Count -gt 0) {
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
    # 'raw' passes the pre-encoded bytes straight through; width/height tell
    # fastfetch the cell size so the fetch is drawn beside (not below) the image.
    $logoArg = @('--logo-type', 'raw', '--logo', $six.FullName,
                 '--logo-width', $w, '--logo-height', $h)
} elseif ($env:TERM_PROGRAM -eq 'WezTerm' -and $pngs.Count -gt 0) {
    $png = $null
    if (-not $randLogo -and $defaultImg -and (Test-Path -LiteralPath $defaultImg)) {
        $png = Get-Item -LiteralPath $defaultImg
    }
    if (-not $png) {
        if ($randLogo) { $png = Get-Random -InputObject $pngs }
        else { $png = $pngs | Select-Object -First 1 }
    }
    $logoArg = @('--logo-type', 'kitty-direct', '--logo', $png.FullName,
                 '--logo-width', $w, '--logo-height', $h)
}

$colorArgs = @()
if ($accent) { $colorArgs = @('--logo-color-1', $accent, '--logo-color-2', $accent) }

& $ffExe @themeArg @logoArg
if ($LASTEXITCODE -ne 0 -and $logoArg.Count -gt 0) {
    # Image attempt failed - fall back to the built-in logo (accent-tinted).
    & $ffExe @themeArg --logo 'Windows11' @colorArgs
}
if ($logoArg.Count -eq 0) {
    # No image support (e.g. classic conhost): tint the built-in ASCII logo
    # with this theme's accent so the look still changes every run.
    & $ffExe @themeArg --logo 'Windows11' @colorArgs
}
"""


def generate_launcher(st: dict) -> None:
    text = LAUNCHER_TEMPLATE.replace("@@W@@", str(int(st.get("logWidth", 28)))) \
                            .replace("@@H@@", str(int(st.get("logHeight", 24))))
    LAUNCHER_PATH.write_text(text, "utf-8", newline="\n")


def profile_snippet() -> str:
    return ('# -- FastFetch Studio: random fastfetch on shell start --\r\n'
            '& "$env:USERPROFILE\\.config\\fastfetch\\fastfetch-random.ps1"')


def apply_all(st: dict) -> str:
    """Backup config, encode sixels, write themes + launcher, persist state."""
    GUI_DIR.mkdir(parents=True, exist_ok=True)
    if CONFIG_PATH.exists() and not BACKUP_PATH.exists():
        shutil.copyfile(CONFIG_PATH, BACKUP_PATH)
    sixels = ensure_sixels(st)
    themes = generate_theme_files(st)
    generate_launcher(st)
    save_state(st)
    return f"{len(themes)} themes + {len(sixels)} sixels + launcher written"


# --------------------------------------------------------------------------- sixel
#
# This fastfetch build cannot encode PNG to sixel itself (it silently falls
# back to the built-in ASCII logo), and Pillow has no SIXEL plugin. So we
# pre-encode each gallery image to a .sixel file and let fastfetch pass the
# bytes straight through - the same approach as the original logo.sixel setup.

def ensure_sixels(st: dict) -> list[Path]:
    """Encode every gallery image to sixels\\*.sixel (fastfetch consumes these)."""
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
        dst = SIXELS_DIR / f"{key}.sixel"
        try:
            with Image.open(src) as im:
                dst.write_bytes(encode_sixel(fit_image_cells(im, gw, gh)))
            written.append(dst)
        except Exception:
            continue
    return written


def write_preview_sixel(st: dict, image_path: str) -> Path | None:
    """Encode the chosen image to a .sixel file for the terminal preview."""
    if not (HAVE_PIL and HAVE_SIXEL) or not image_path or not Path(image_path).exists():
        return None
    cells_w = int(st.get("logWidth", 28))
    cells_h = int(st.get("logHeight", 24))
    try:
        with Image.open(image_path) as im:
            enc = encode_sixel(fit_image_cells(im, cells_w, cells_h))
        PREVIEW_CACHE.parent.mkdir(parents=True, exist_ok=True)
        PREVIEW_CACHE.write_bytes(enc)
        return PREVIEW_CACHE
    except Exception:
        return None


def spawn_terminal_preview(st: dict, image_path: str) -> tuple[bool, str]:
    """Open a small Windows Terminal window showing fastfetch + the image (sixel)."""
    sixel = write_preview_sixel(st, image_path)
    themes = sorted(THEMES_DIR.glob("theme-*.jsonc"))
    if not themes:
        return False, "No themes yet - click Apply & Generate first"
    theme = themes[0]
    ff = "$env:USERPROFILE\\.local\\bin\\fastfetch.exe"
    ps = "$env:WT_SESSION='set-by-wt'; "
    if sixel:
        ps += (f"& \"{ff}\" --config '{theme}' --logo-type raw "
               f"--logo '{sixel}' --logo-width {st.get('logWidth', 28)} "
               f"--logo-height {st.get('logHeight', 24)}")
    else:
        ps += f"& \"{ff}\" --config '{theme}' --logo 'Windows11'"
    try:
        subprocess.Popen(["wt.exe", "-w", "-1", "nt", "--title", "FastFetch Preview",
                          "powershell", "-NoProfile", "-Command", ps],
                         creationflags=subprocess.CREATE_NO_WINDOW if hasattr(subprocess, "CREATE_NO_WINDOW") else 0)
        return True, "Preview opened in Windows Terminal"
    except FileNotFoundError:
        try:
            subprocess.Popen(["powershell", "-NoProfile", "-Command", ps],
                             creationflags=subprocess.CREATE_NEW_CONSOLE)
            return True, "Preview opened in PowerShell console"
        except Exception as e:
            return False, f"Could not open terminal: {e}"
    except Exception as e:
        return False, f"Could not open terminal: {e}"


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
        with Image.open(st["gallery"][0]["path"]) as im0:
            src = fit_image_cells(im0, int(st["logWidth"]), int(st["logHeight"]))
            srcq = quantize_rgb(src)
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
ACCENT  = "#e04a3f"   # matches the user's theme accent
ACCENT_H = "#f2604f"
OKC     = "#41c46f"
WARNC   = "#e6a83c"

FONT_UI    = ("Segoe UI", 10)
FONT_SEMI  = ("Segoe UI Semibold", 10)
FONT_HEAD  = ("Segoe UI Semibold", 11)
FONT_SMALL = ("Segoe UI", 9)


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
    st.configure("TCheckbutton", background=PANEL, foreground=FG, indicatorcolor=FIELD,
                 indicatormargin=0, padding=2)
    st.map("TCheckbutton", indicatorcolor=[("selected", ACCENT)],
           background=[("active", PANEL)], foreground=[("active", FG)])
    st.configure("TRadiobutton", background=PANEL, foreground=FG, indicatorcolor=FIELD,
                 indicatormargin=0, padding=2)
    st.map("TRadiobutton", indicatorcolor=[("selected", ACCENT)],
           background=[("active", PANEL)], foreground=[("active", FG)])
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
    """Hex color entry + pick button."""

    def __init__(self, master, initial: str, on_change=None):
        super().__init__(master)
        self.on_change = on_change
        self.var = tk.StringVar(value=initial)
        self.swatch = tk.Label(self, width=3, background=initial,
                               relief="flat", highlightthickness=1,
                               highlightbackground=BORDER)
        self.swatch.pack(side="left", padx=(0, 4))
        self.entry = ttk.Entry(self, textvariable=self.var, width=10)
        self.entry.pack(side="left")
        ttk.Button(self, text="...", width=3, command=self.pick).pack(side="left", padx=(4, 0))
        self.var.trace_add("write", lambda *_: self._changed())

    def _changed(self):
        h = norm_hex(self.var.get(), "")
        if h:
            self.swatch.configure(background=h)
        if self.on_change:
            self.on_change()

    def pick(self):
        from tkinter import colorchooser
        current = norm_hex(self.var.get(), "#e04a3f")
        rgb = colorchooser.askcolor(color=current, parent=self)[0]
        if rgb:
            h = "#{:02x}{:02x}{:02x}".format(*(round(c) for c in rgb))
            self.var.set(h)

    def get(self) -> str:
        return norm_hex(self.var.get(), "#e04a3f")


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

    def _make_grid_scroll(self, parent) -> tk.Frame:
        """Wheel-scrollable canvas for the gallery image grid.

        Deliberately scrollbar-free: the ttk scrollbar rendered as a bright
        white bar on Windows, and the toolbar above the grid already
        explains the interaction.
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

        content.bind("<Configure>", lambda e: canvas.after_idle(sync_scroll))
        canvas.bind("<Configure>", lambda e: canvas.after_idle(sync_scroll))
        self._grid_scroll = canvas
        return content

    def _on_mousewheel(self, e):
        # Only the gallery image grid scrolls; the other tabs fit their
        # window, so the wheel must not move (or blank) anything else.
        cv = self._grid_scroll
        if cv is not None and getattr(self, "_current_tab", "") == "Gallery":
            cv.yview_scroll(-1 * (e.delta // 120), "units")

    def status(self, msg: str):
        self.status_var.set(msg)

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
            cell = tk.Frame(self.gallery_inner, bg=PANEL)
            cell.grid(row=r, column=c, padx=8, pady=8)
            ph = load_preview_image(g["path"], 170)
            if ph is not None:
                self._thumb_refs[g["path"]] = ImageTk.PhotoImage(ph)
                img_label = tk.Label(cell, image=self._thumb_refs[g["path"]],
                                     bg=FIELD, bd=0, highlightthickness=2,
                                     highlightbackground=BORDER, highlightcolor=BORDER)
            else:
                img_label = tk.Label(cell, text="?", width=20, height=10, bg=FIELD, fg=MUTED,
                                     bd=0, highlightthickness=2,
                                     highlightbackground=BORDER, highlightcolor=BORDER)
            is_sel = self.selected_path == g["path"]
            is_def = STATE.get("defaultImage") == g["path"]
            ring = ACCENT if is_sel else BORDER
            img_label.configure(highlightbackground=ring, highlightcolor=ring)
            img_label.pack()
            name = Path(g["path"]).name
            info = f"{name}\n{g['w']}x{g['h']} cells"
            tk.Label(cell, text=info, justify="center", bg=PANEL,
                     fg=FG if is_sel else MUTED, font=FONT_SMALL).pack()
            if is_def:
                tk.Label(cell, text="\u2605 default logo", bg=PANEL, fg=ACCENT,
                         font=FONT_SMALL).pack()
            path = g["path"]
            img_label.bind("<Button-1>", lambda e, p=path: self._select(p))
            img_label.bind("<Double-Button-1>", lambda e, p=path: (self._select(p), self.edit_size()))
            img_label.bind("<Enter>", lambda e, w=img_label, sel=is_sel: animate_highlight(w, ACCENT if sel else FIELD_H), add="+")
            img_label.bind("<Leave>", lambda e, w=img_label, sel=is_sel: animate_highlight(w, ACCENT if sel else BORDER), add="+")
        cv = self._grid_scroll
        if cv is not None:
            self.after_idle(lambda: cv.yview_moveto(keep))

    def _select(self, path: str):
        self.selected_path = path
        self.refresh_gallery()

    # ---------------------------------------------------------------- theme tab
    def _build_theme_tab(self):
        f = self.tab_theme
        top = ttk.Frame(f)
        top.pack(fill="both", expand=True)
        left = ttk.Frame(top)
        left.pack(side="left", fill="both", expand=True, padx=(0, 12))
        right = ttk.Frame(top)
        right.pack(side="left", fill="y")

        tk.Label(left, text="KEY COLORS \u00b7 per module group", bg=PANEL, fg=MUTED,
                 font=FONT_SEMI).pack(anchor="w", pady=(0, 8))
        self.color_entries: dict[str, ColorEntry] = {}
        for key, label, _hint in GROUPS:
            row = tk.Frame(left, bg=PANEL)
            row.pack(fill="x", pady=3)
            tk.Label(row, text=label, width=24, bg=PANEL, fg=FG, font=FONT_UI,
                     anchor="w").pack(side="left")
            ce = ColorEntry(row, STATE["groups"].get(key, "#e04a3f"),
                            on_change=self._on_color_changed)
            ce.pack(side="left")
            self.color_entries[key] = ce

        ttk.Separator(left).pack(fill="x", pady=12)
        tk.Label(left, text="BOX BORDER", bg=PANEL, fg=MUTED,
                 font=FONT_SEMI).pack(anchor="w", pady=(0, 8))
        brow = tk.Frame(left, bg=PANEL)
        brow.pack(fill="x", pady=3)
        tk.Label(brow, text="Style", width=24, bg=PANEL, fg=FG,
                 anchor="w").pack(side="left")
        self.box_style = tk.StringVar(value=STATE.get("boxStyle", "rounded"))
        ttk.Combobox(brow, textvariable=self.box_style, state="readonly", width=10,
                     values=list(BOX_STYLES.keys())).pack(side="left")
        crow = tk.Frame(left, bg=PANEL)
        crow.pack(fill="x", pady=3)
        tk.Label(crow, text="Border color (blank = default)", width=24, bg=PANEL, fg=FG,
                 anchor="w").pack(side="left")
        self.box_color = ColorEntry(crow, STATE.get("boxColor") or "#e04a3f")
        self.box_color.pack(side="left")

        ttk.Separator(left).pack(fill="x", pady=10)
        srow = tk.Frame(left, bg=PANEL)
        srow.pack(fill="x", pady=3)
        tk.Label(srow, text="Separator", width=24, bg=PANEL, fg=FG,
                 anchor="w").pack(side="left")
        self.sep_var = tk.StringVar(value=STATE.get("separator", " : "))
        ttk.Entry(srow, textvariable=self.sep_var, width=10).pack(side="left")
        self.colors_block = tk.BooleanVar(value=STATE.get("colorsBlock", True))
        ttk.Checkbutton(left, text="Show palette (colors) block at bottom",
                        variable=self.colors_block).pack(anchor="w", pady=6)

        tk.Label(right, text="LOGO SIZE \u00b7 default", bg=PANEL, fg=MUTED,
                 font=FONT_SEMI).pack(anchor="w", pady=(0, 8))
        sz = tk.Frame(right, bg=PANEL)
        sz.pack(anchor="w")
        self.log_w = tk.StringVar(value=str(STATE.get("logWidth", 28)))
        self.log_h = tk.StringVar(value=str(STATE.get("logHeight", 24)))
        tk.Label(sz, text="W", bg=PANEL, fg=FG).grid(row=0, column=0)
        ttk.Spinbox(sz, from_=10, to=80, textvariable=self.log_w, width=5).grid(row=0, column=1, padx=(2, 10))
        tk.Label(sz, text="H", bg=PANEL, fg=FG).grid(row=0, column=2)
        ttk.Spinbox(sz, from_=8, to=60, textvariable=self.log_h, width=5).grid(row=0, column=3, padx=2)
        tk.Label(right, text="terminal cells", bg=PANEL, fg=MUTED,
                 font=FONT_SMALL).pack(anchor="w")

        ttk.Separator(right).pack(fill="x", pady=12)
        tk.Label(right, text="ACCENT PALETTE PREVIEW", bg=PANEL, fg=MUTED,
                 font=FONT_SEMI).pack(anchor="w", pady=(0, 8))
        self.palette_canvas = tk.Canvas(right, width=8 * 28 + 8, height=28, bg=PANEL,
                                        highlightthickness=0)
        self.palette_canvas.pack(anchor="w")
        self._draw_palette()

        ttk.Separator(right).pack(fill="x", pady=12)
        tk.Label(right, text="PROFILE SNIPPET \u00b7 paste into your PowerShell $PROFILE",
                 bg=PANEL, fg=MUTED, font=FONT_SEMI, justify="left").pack(anchor="w", pady=(0, 8))
        snippet = tk.Text(right, height=2, width=46, wrap="none", font=("Consolas", 9),
                          bg=FIELD, fg=FG, insertbackground=FG, relief="flat",
                          highlightthickness=1, highlightbackground=BORDER, padx=8, pady=6)
        snippet.insert("1.0", profile_snippet())
        snippet.configure(state="disabled")
        snippet.pack(anchor="w", fill="x")
        AnimatedButton(right, text="Copy snippet", command=self.copy_snippet).pack(anchor="w", pady=6)

        btns = tk.Frame(f, bg=PANEL)
        btns.pack(fill="x", pady=(14, 0))
        self.apply_btn = AnimatedButton(btns, "accent", text="Apply & Generate",
                                        command=self.on_apply)
        self.apply_btn.pack(side="left", padx=(0, 8))
        AnimatedButton(btns, text="Preview in terminal", command=self.on_preview).pack(side="left", padx=(0, 8))
        AnimatedButton(btns, text="Open config folder",
                       command=lambda: os.startfile(str(FF_DIR))).pack(side="left")

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
            cv.create_rectangle(x0, 4, x0 + 24, 24, fill=pal.get("accent", "#e04a3f"), outline="")

    # ---------------------------------------------------------------- random tab
    def _build_random_tab(self):
        f = self.tab_random
        tk.Label(f, text="WHAT CHANGES EVERY TIME YOU OPEN A TERMINAL",
                 bg=PANEL, fg=MUTED, font=FONT_SEMI).pack(anchor="w", pady=(0, 10))
        self.rand_logo = tk.BooleanVar(value=STATE.get("randomLogo", True))
        self.rand_theme = tk.BooleanVar(value=STATE.get("randomTheme", True))
        ttk.Checkbutton(f, text="Random logo (off = use default image)", variable=self.rand_logo,
                        command=self._random_changed).pack(anchor="w", pady=2)
        ttk.Checkbutton(f, text="Random color theme (8 palettes generated from your colors)",
                        variable=self.rand_theme, command=self._random_changed).pack(anchor="w", pady=2)

        ttk.Separator(f).pack(fill="x", pady=14)
        tk.Label(f, text="HOW OFTEN", bg=PANEL, fg=MUTED,
                 font=FONT_SEMI).pack(anchor="w", pady=(0, 8))
        self.freq = tk.StringVar(value=STATE.get("frequency", "every"))
        ttk.Radiobutton(f, text="Every new terminal window", variable=self.freq, value="every",
                        command=self._random_changed).pack(anchor="w", pady=2)
        ttk.Radiobutton(f, text="Once per day (same look all day)", variable=self.freq, value="daily",
                        command=self._random_changed).pack(anchor="w", pady=2)

        ttk.Separator(f).pack(fill="x", pady=12)
        info = ("Files FastFetch Studio manages:\n"
                "  \u2022 config.jsonc            \u2014 your main fastfetch config (a backup is saved once)\n"
                "  \u2022 themes\\theme-01..08.jsonc \u2014 generated color palettes\n"
                "  \u2022 fastfetch-random.ps1    \u2014 picks a random theme + logo on each run\n"
                "  \u2022 pngs\\                   \u2014 your uploaded images (converted to PNG)")
        snip = tk.Text(f, height=5, wrap="none", font=("Consolas", 9), bg=PANEL, fg=MUTED,
                       relief="flat", highlightthickness=0, bd=0)
        snip.insert("1.0", info)
        snip.configure(state="disabled")
        snip.pack(anchor="w", fill="x")

    def _random_changed(self):
        STATE["randomLogo"] = bool(self.rand_logo.get())
        STATE["randomTheme"] = bool(self.rand_theme.get())
        STATE["frequency"] = self.freq.get()
        save_state(STATE)
        generate_launcher(STATE)
        self.status("Random settings updated in launcher")

    # ---------------------------------------------------------------- actions
    def _collect(self) -> bool:
        groups = {k: ce.get() for k, ce in self.color_entries.items()}
        groups.setdefault("accent", groups.get("title") or "#e04a3f")
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


def main() -> int:
    if "--selftest" in sys.argv:
        return selftest()
    if "--smoke-gui" in sys.argv:
        return smoke_gui()
    app = App()
    app.mainloop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
