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
                             encode_sixel, fit_image_cells, quantize_rgb,
                             render_ansi_art)
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
# The Preview button's payload. It is a generated file rather than a
# `powershell -Command "..."` one-liner for a reason - see PREVIEW_TEMPLATE.
PREVIEW_SCRIPT = GUI_DIR / "preview.ps1"
SIXELS_DIR = FF_DIR / "sixels"
# Block-art logos for terminals that cannot display an image protocol at all.
ARTS_DIR = FF_DIR / "arts"

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
    # How the launcher draws the logo: "auto" trusts its terminal detection,
    # "image" forces the picture even in a terminal it does not recognise,
    # "builtin" always uses fastfetch's tinted ASCII logo.
    "logoMode": "auto",
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
        {"type": "chassis", "key": "Chassis", "format": "{1} {2} {3}",
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
        {"type": "cpu", "format": "{1} @ {7}", "key": "CPU",
         "keyColor": st["groups"]["cpu"]},
        {"type": "gpu", "format": "{1} {2}", "key": "GPU",
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
$sixels = @(Get-ChildItem -LiteralPath (Join-Path $ffRoot 'sixels') -Filter '*.sixel' -File -ErrorAction SilentlyContinue)
$arts   = @(Get-ChildItem -LiteralPath (Join-Path $ffRoot 'arts') -Filter '*.art' -File -ErrorAction SilentlyContinue)
$pngDirs = @((Join-Path $ffRoot 'pngs'), (Join-Path $ffRoot 'images'))
$pngs    = @($pngDirs | ForEach-Object { Get-ChildItem -LiteralPath $_ -Filter '*.png' -File -Recurse -ErrorAction SilentlyContinue } | Sort-Object FullName -Unique)
$statePath = Join-Path $ffRoot 'gui\studio-state.json'

$randLogo = $true; $randTheme = $true; $freq = 'every'; $w = @@W@@; $h = @@H@@; $defaultImg = ''
$logoMode = 'auto'
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

# The .sixel files are pre-encoded by FastFetch Studio (this fastfetch build
# cannot encode PNG to sixel itself); fastfetch passes the bytes through.
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
    # 'raw' passes the pre-encoded bytes straight through; width/height tell
    # fastfetch the cell size so the fetch is drawn beside (not below) the image.
    $logoArg = @('--logo-type', 'raw', '--logo', $six.FullName,
                 '--logo-width', $w, '--logo-height', $h)
} elseif ($wantImage -and $ffKitty -and $pngs.Count -gt 0) {
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

# Run fastfetch exactly ONCE. The previous version called it twice whenever no
# image protocol was available (classic conhost), and again whenever the first
# call returned a non-zero exit code - so the fetch was printed twice.
if ($logoArg.Count -gt 0) {
    & $ffExe @themeArg @logoArg
    # $LASTEXITCODE is $null when the call never reached a native command (the
    # exe vanished between the Test-Path above and this line, say). "$null -ne 0"
    # is TRUE, so a bare comparison here fired the fallback and drew twice.
    $ffRc = $LASTEXITCODE
    if ($null -ne $ffRc -and $ffRc -ne 0) {
        # Image attempt failed - retry once with the built-in tinted logo.
        & $ffExe @themeArg --logo 'Windows11' @colorArgs
    }
} else {
    # No image protocol here. fastfetch prints a *text* logo file verbatim, ANSI
    # escapes and all, so the block art FastFetch Studio pre-rendered gives the
    # user their own picture at block resolution rather than a Windows logo.
    # fastfetch's tinted ASCII logo is the last resort.
    $fbLogo = 'Windows11'
    if ($wantImage -and $arts.Count -gt 0) {
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
        if ($art) { $fbLogo = $art.FullName }
    }
    & $ffExe @themeArg --logo $fbLogo @colorArgs
    # Say why - but only when a picture was clearly expected (a pinned default
    # image, randomisation off) and the block art could not be used either.
    # Otherwise every shell that cannot draw images would print this, which is
    # noise. This is the message whose absence made "I set a logo and got a
    # Windows logo" look like a broken app.
    if ($fbLogo -eq 'Windows11' -and $wantImage -and -not $randLogo -and $defaultImg) {
        Write-Host ('FastFetch Studio: no image support detected in ' + $ffHost + ', so the built-in logo was drawn.') -ForegroundColor DarkGray
        Write-Host '  force it: $env:FASTFETCH_STUDIO_LOGO = ''image''   (Windows Terminal 1.22+ draws it as-is)' -ForegroundColor DarkGray
    }
}

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
                            .replace("@@H@@", str(int(st.get("logHeight", 24))))
    LAUNCHER_PATH.write_text(text, "utf-8", newline="\n")


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
        dst = ARTS_DIR / f"{art_key(src)}.art"
        try:
            with Image.open(src) as im:
                rows = render_ansi_art(im, gw, gh)
            dst.write_text("\n".join(rows) + "\n", "utf-8", newline="\n")
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
# terminal the user opens afterwards. A narrow window wrapping the fetch over the
# logo is a smaller price than that. See the guard in the CI preview step.
$ffExe = Join-Path $env:USERPROFILE '.local\bin\fastfetch.exe'
if (-not (Test-Path -LiteralPath $ffExe)) { $ffExe = 'fastfetch.exe' }
if (-not (Get-Command -Name $ffExe -ErrorAction SilentlyContinue)) {
    Write-Host 'FastFetch Studio: fastfetch.exe was not found - run setup.ps1 first.' -ForegroundColor Yellow
    return
}

$themeArg = @('--config', '@@THEME@@')
$sixel = '@@SIXEL@@'
$art   = '@@ART@@'

if (-not $BlockArt -and (Test-Path -LiteralPath $sixel)) {
    & $ffExe @themeArg --logo-type raw --logo $sixel --logo-width @@W@@ --logo-height @@H@@
} elseif (Test-Path -LiteralPath $art) {
    # No image protocol to draw into - a bare console, say. fastfetch prints a
    # *text* logo file verbatim, so the block art still shows the user's picture.
    & $ffExe @themeArg --logo $art
} else {
    & $ffExe @themeArg --logo Windows11
}

Write-Host ''
Write-Host 'FastFetch Studio preview - close this window when you are done.' -ForegroundColor DarkGray
# <<< FastFetch Studio :: preview <<<
"""


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
            .replace("@@H@@", str(int(st.get("logHeight", 24)))))
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

    def _random_changed(self):
        STATE["randomLogo"] = bool(self.rand_logo.get())
        STATE["randomTheme"] = bool(self.rand_theme.get())
        STATE["frequency"] = self.freq.get()
        STATE["logoMode"] = self.logo_mode.get()
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
