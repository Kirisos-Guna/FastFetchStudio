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


class ColorEntry(ttk.Frame):
    """Hex color entry + pick button."""

    def __init__(self, master, initial: str, on_change=None):
        super().__init__(master)
        self.on_change = on_change
        self.var = tk.StringVar(value=initial)
        self.swatch = tk.Label(self, width=3, background=initial, relief="groove")
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
        self.result = None
        frm = ttk.Frame(self, padding=12)
        frm.pack(fill="both", expand=True)
        ttk.Label(frm, text="Width (cells):").grid(row=0, column=0, sticky="e", pady=4)
        wv = tk.StringVar(value=str(w))
        ttk.Spinbox(frm, from_=10, to=80, textvariable=wv, width=6).grid(row=0, column=1, padx=8)
        ttk.Label(frm, text="Height (cells):").grid(row=1, column=0, sticky="e", pady=4)
        hv = tk.StringVar(value=str(h))
        ttk.Spinbox(frm, from_=8, to=60, textvariable=hv, width=6).grid(row=1, column=1, padx=8)
        btns = ttk.Frame(frm)
        btns.grid(row=2, column=0, columnspan=2, pady=(12, 0))

        def ok():
            try:
                self.result = (max(10, min(80, int(wv.get()))), max(8, min(60, int(hv.get()))))
            except ValueError:
                return
            self.destroy()

        ttk.Button(btns, text="OK", command=ok).pack(side="left", padx=4)
        ttk.Button(btns, text="Cancel", command=self.destroy).pack(side="left", padx=4)
        self.transient(master)
        self.grab_set()
        self.wait_window()


class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("FastFetch Studio")
        self.geometry("780x660")
        self.minsize(700, 560)
        try:
            ttk.Style().theme_use("vista")
        except tk.TclError:
            pass
        self.selected_path: str | None = None
        self._thumb_refs: dict[str, ImageTk.PhotoImage] = {}

        nb = ttk.Notebook(self)
        nb.pack(fill="both", expand=True, padx=8, pady=(8, 4))
        self.tab_gallery = ttk.Frame(nb, padding=10)
        self.tab_theme = ttk.Frame(nb, padding=10)
        self.tab_random = ttk.Frame(nb, padding=10)
        nb.add(self.tab_gallery, text=" Gallery ")
        nb.add(self.tab_theme, text=" Theme ")
        nb.add(self.tab_random, text=" Random ")
        self.nb = nb

        self.status_var = tk.StringVar(value="Ready")
        ttk.Label(self, textvariable=self.status_var, relief="sunken", anchor="w",
                  padding=(6, 3)).pack(fill="x", side="bottom")

        self._build_gallery_tab()
        self._build_theme_tab()
        self._build_random_tab()
        if not STATE["gallery"]:
            self._seed_gallery(silent=True)
        self.refresh_gallery()

    # ---------------------------------------------------------------- status
    def status(self, msg: str):
        self.status_var.set(msg)

    # ---------------------------------------------------------------- gallery tab
    def _build_gallery_tab(self):
        f = self.tab_gallery
        bar = ttk.Frame(f)
        bar.pack(fill="x", pady=(0, 8))
        ttk.Button(bar, text="Add images...", command=self.add_images).pack(side="left", padx=(0, 6))
        ttk.Button(bar, text="Remove selected", command=self.remove_selected).pack(side="left", padx=(0, 6))
        ttk.Button(bar, text="Set as default", command=self.set_default).pack(side="left", padx=(0, 6))
        ttk.Button(bar, text="Edit size...", command=self.edit_size).pack(side="left")
        ttk.Label(f, text="Click to select \u00b7 double-click to edit size \u00b7 PNGs are stored in ~/.config/fastfetch/pngs",
                  foreground="#777").pack(anchor="w", pady=(0, 6))

        holder = ttk.Frame(f)
        holder.pack(fill="both", expand=True)
        self.gallery_inner = ttk.Frame(holder)
        self.gallery_inner.pack(fill="both", expand=True)
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
        for c in self.gallery_inner.winfo_children():
            c.destroy()
        self._thumb_refs.clear()
        gallery = STATE.get("gallery", [])
        if not gallery:
            ttk.Label(self.gallery_inner, foreground="#777",
                      text="No images yet. Click 'Add images...' to upload PNG / JPG / WEBP / GIF.").pack(pady=30)
            return
        cols = max(1, self.gallery_cols)
        for i, g in enumerate(gallery):
            r, c = divmod(i, cols)
            cell = ttk.Frame(self.gallery_inner)
            cell.grid(row=r, column=c, padx=6, pady=6)
            ph = load_preview_image(g["path"], 170)
            if ph is not None:
                self._thumb_refs[g["path"]] = ImageTk.PhotoImage(ph)
                img_label = tk.Label(cell, image=self._thumb_refs[g["path"]], bd=2, relief="flat")
            else:
                img_label = tk.Label(cell, text="?", width=20, height=10, bd=2, relief="flat")
            is_sel = self.selected_path == g["path"]
            is_def = STATE.get("defaultImage") == g["path"]
            img_label.configure(highlightthickness=2,
                                highlightbackground="#1f6feb" if is_sel else "#dddddd",
                                highlightcolor="#1f6feb" if is_sel else "#dddddd")
            img_label.pack()
            name = Path(g["path"]).name
            mark = "  \u2605 default" if is_def else ""
            ttk.Label(cell, text=f"{name}\n{g['w']}x{g['h']} cells{mark}",
                      justify="center", foreground="#444").pack()
            path = g["path"]
            img_label.bind("<Button-1>", lambda e, p=path: self._select(p))
            img_label.bind("<Double-Button-1>", lambda e, p=path: (self._select(p), self.edit_size()))

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

        ttk.Label(left, text="Key colors (per module group)", font="TkDefaultFont 10 bold").pack(anchor="w", pady=(0, 6))
        self.color_entries: dict[str, ColorEntry] = {}
        for key, label, _hint in GROUPS:
            row = ttk.Frame(left)
            row.pack(fill="x", pady=2)
            ttk.Label(row, text=label, width=24).pack(side="left")
            ce = ColorEntry(row, STATE["groups"].get(key, "#e04a3f"),
                            on_change=self._on_color_changed)
            ce.pack(side="left")
            self.color_entries[key] = ce

        ttk.Separator(left).pack(fill="x", pady=10)
        ttk.Label(left, text="Box border", font="TkDefaultFont 10 bold").pack(anchor="w", pady=(0, 6))
        brow = ttk.Frame(left)
        brow.pack(fill="x", pady=2)
        ttk.Label(brow, text="Style", width=24).pack(side="left")
        self.box_style = tk.StringVar(value=STATE.get("boxStyle", "rounded"))
        ttk.Combobox(brow, textvariable=self.box_style, state="readonly", width=10,
                     values=list(BOX_STYLES.keys())).pack(side="left")
        crow = ttk.Frame(left)
        crow.pack(fill="x", pady=2)
        ttk.Label(crow, text="Border color (blank = default)", width=24).pack(side="left")
        self.box_color = ColorEntry(crow, STATE.get("boxColor") or "#e04a3f")
        self.box_color.pack(side="left")

        ttk.Separator(left).pack(fill="x", pady=10)
        srow = ttk.Frame(left)
        srow.pack(fill="x", pady=2)
        ttk.Label(srow, text="Separator", width=24).pack(side="left")
        self.sep_var = tk.StringVar(value=STATE.get("separator", " : "))
        ttk.Entry(srow, textvariable=self.sep_var, width=10).pack(side="left")
        self.colors_block = tk.BooleanVar(value=STATE.get("colorsBlock", True))
        ttk.Checkbutton(left, text="Show palette (colors) block at bottom",
                        variable=self.colors_block).pack(anchor="w", pady=6)

        ttk.Label(right, text="Logo size (default)", font="TkDefaultFont 10 bold").pack(anchor="w", pady=(0, 6))
        sz = ttk.Frame(right)
        sz.pack(anchor="w")
        self.log_w = tk.StringVar(value=str(STATE.get("logWidth", 28)))
        self.log_h = tk.StringVar(value=str(STATE.get("logHeight", 24)))
        ttk.Label(sz, text="W").grid(row=0, column=0)
        ttk.Spinbox(sz, from_=10, to=80, textvariable=self.log_w, width=5).grid(row=0, column=1, padx=(2, 10))
        ttk.Label(sz, text="H").grid(row=0, column=2)
        ttk.Spinbox(sz, from_=8, to=60, textvariable=self.log_h, width=5).grid(row=0, column=3, padx=2)
        ttk.Label(right, text="terminal cells", foreground="#777").pack(anchor="w")

        ttk.Separator(right).pack(fill="x", pady=10)
        ttk.Label(right, text="Accent palette preview", font="TkDefaultFont 10 bold").pack(anchor="w", pady=(0, 6))
        self.palette_canvas = tk.Canvas(right, width=8 * 26 + 8, height=26, highlightthickness=0)
        self.palette_canvas.pack(anchor="w")
        self._draw_palette()

        ttk.Separator(right).pack(fill="x", pady=10)
        ttk.Label(right, text="Profile snippet (copy into your\nPowerShell $PROFILE)",
                  font="TkDefaultFont 10 bold", justify="left").pack(anchor="w", pady=(0, 6))
        snippet = tk.Text(right, height=2, width=46, wrap="none", font=("Consolas", 9))
        snippet.insert("1.0", profile_snippet())
        snippet.configure(state="disabled")
        snippet.pack(anchor="w")
        ttk.Button(right, text="Copy snippet", command=self.copy_snippet).pack(anchor="w", pady=4)

        btns = ttk.Frame(f)
        btns.pack(fill="x", pady=(10, 0))
        self.apply_btn = ttk.Button(btns, text="Apply & Generate", command=self.on_apply)
        self.apply_btn.pack(side="left", padx=(0, 6))
        ttk.Button(btns, text="Preview in terminal", command=self.on_preview).pack(side="left", padx=(0, 6))
        ttk.Button(btns, text="Open config folder", command=lambda: os.startfile(str(FF_DIR))).pack(side="left")

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
            x0 = 4 + i * 26
            cv.create_rectangle(x0, 4, x0 + 22, 22,            fill=pal.get("accent", "#e04a3f"), outline="")

    # ---------------------------------------------------------------- random tab
    def _build_random_tab(self):
        f = self.tab_random
        ttk.Label(f, text="What changes every time you open a terminal",
                  font="TkDefaultFont 10 bold").pack(anchor="w", pady=(0, 10))
        self.rand_logo = tk.BooleanVar(value=STATE.get("randomLogo", True))
        self.rand_theme = tk.BooleanVar(value=STATE.get("randomTheme", True))
        ttk.Checkbutton(f, text="Random logo (off = use default image)", variable=self.rand_logo,
                        command=self._random_changed).pack(anchor="w", pady=2)
        ttk.Checkbutton(f, text="Random color theme (8 palettes generated from your colors)",
                        variable=self.rand_theme, command=self._random_changed).pack(anchor="w", pady=2)

        ttk.Separator(f).pack(fill="x", pady=12)
        ttk.Label(f, text="How often", font="TkDefaultFont 10 bold").pack(anchor="w", pady=(0, 6))
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
        ttk.Label(f, text=info, justify="left", foreground="#555").pack(anchor="w")

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
