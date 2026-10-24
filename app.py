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

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, ttk
from tkinter import font as tkfont

from studio import __version__
from studio.config_build import (
    BOX_STYLES,
    GROUPS,
    KEY_ICON_MODE,
    KEY_PADDING_LEFT,
    KEYED_MODULES,
    MANAGED_FILES,
    NO_KEY,
    build_config_json,
    dump_jsonc,
)
from studio.encode import LOGO_SLACK_COLS, ensure_sixels, fit_logo_cells, ladder_cells, logo_cell_px
from studio.images import convert_to_png, load_preview_image
from studio.launcher import (
    LAUNCHER_TEMPLATE as LAUNCHER_TEMPLATE,
)
from studio.launcher import (
    apply_all,
    generate_launcher,
    generate_theme_files,
    profile_snippet,
    write_config,
)
from studio.metrics import load_metrics, spawn_measure, write_measure_script
from studio.metrics import measure_argv as measure_argv
from studio.preview import (
    PREVIEW_TEMPLATE as PREVIEW_TEMPLATE,
)
from studio.preview import (
    preview_argv as preview_argv,
)
from studio.preview import (
    preview_command as preview_command,
)
from studio.preview import (
    spawn_terminal_preview,
)
from studio.preview import (
    write_preview_script as write_preview_script,
)
from studio.state import (
    DEFAULT_STATE,
    FF_DIR,
    HAVE_PIL,
    HAVE_SIXEL,
    LAUNCHER_PATH,
    METRICS_PATH,
    PNGS_DIR,
    SIXELS_DIR,
    THEMES_DIR,
    USER,
    load_state,
    save_state,
)
from studio.state import (
    MEASURE_SCRIPT as MEASURE_SCRIPT,
)
from studio.state import (
    PREVIEW_SCRIPT as PREVIEW_SCRIPT,
)
from studio.themes import palettes_for

try:
    from PIL import Image, ImageTk
except ImportError:
    Image = None  # type: ignore
    ImageTk = None  # type: ignore

try:
    from sixel_codec import ALPHA_THRESHOLD, decode_sixel_pixels, encode_sixel, quantize_rgb
except Exception:
    pass

__all__ = [
    "__version__", "LAUNCHER_TEMPLATE", "PREVIEW_TEMPLATE", "PREVIEW_SCRIPT",
    "MEASURE_SCRIPT", "apply_all", "generate_launcher", "write_preview_script",
    "preview_argv", "preview_command", "write_measure_script", "measure_argv",
    "spawn_measure", "load_state", "validate_gallery_entry",
]

STATE = load_state()


def validate_gallery_entry(path: str, w: int, h: int) -> tuple[bool, str]:
    """Validate a gallery entry before Apply. Returns (ok, message)."""
    if not path or not str(path).strip():
        return False, "Image path is empty"
    try:
        wi, hi = int(w), int(h)
    except (TypeError, ValueError):
        return False, "Size must be numbers"
    if not 1 <= wi <= 80:
        return False, "Width must be 1..80 cells"
    if not 1 <= hi <= 60:
        return False, "Height must be 1..60 cells"
    return True, "ok"


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
        cells_w = int(st["logWidth"])
        cells_h = int(st["logHeight"])
        cw, ch = logo_cell_px(st)
        with Image.open(st["gallery"][0]["path"]) as im0:
            src = fit_logo_cells(im0, cells_w, cells_h, st)
            srcq = quantize_rgb(src)
        # The invariant that stopped the logo from tearing: the raster is
        # exactly the cell box it is placed into, and it is one column short of
        # the cell count fastfetch is told about, so no glyph can be written
        # into the picture's own cells.
        # Full width, whole rows, never taller than the box - and no empty rows
        # above the picture, which is what pushed it below the frame's top line.
        if (src.width != cells_w * cw or src.height % ch or not 0 < src.height <= cells_h * ch):
            print(f"selftest: FAIL raster {src.width}x{src.height} is not "
                  f"{cells_w} cells of {cw}x{ch} px by at most {cells_h} whole rows")
            return 1
        top = src.getchannel("A").getbbox()
        if top is None or top[1] != 0:
            print(f"selftest: FAIL picture starts at y={top and top[1]}, not at the top of its canvas")
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
            if w3 != vw * cw or h3 % ch or not 0 < h3 <= vh * ch:
                print(f"selftest: FAIL ladder variant {name} is {w3}x{h3} px, "
                      f"not {vw} cells of {cw}x{ch} px by at most {vh} whole rows")
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
        def through(c):
            return (c * 100 // 255) * 255 // 100
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
            ok, msg = validate_gallery_entry(g["path"], dlg.result[0], dlg.result[1])
            if not ok:
                self.status(msg)
                return
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
        img = STATE.get("defaultImage")
        if img and not Path(img).exists():
            fallback = STATE["gallery"][0]["path"] if STATE.get("gallery") else ""
            self.status(f"Default image missing - using {Path(fallback).name or 'nothing'} instead")
            img = fallback
        if not img:
            img = STATE["gallery"][0]["path"] if STATE.get("gallery") else ""
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
    # fastfetch leaves out a row it has nothing to say for - Packages on a
    # machine with no package manager it recognises, say. That is the machine,
    # not the config, so those modules are not counted against it.
    silent: list[str] = []
    if ff is not None:
        for kind in KEYED_MODULES:
            try:
                probe = subprocess.run([ff, "--logo", "none", "--pipe", "-s", kind],
                                       capture_output=True, timeout=60)
            except subprocess.TimeoutExpired:
                continue
            if probe.returncode == 0 and not probe.stdout.strip():
                silent.append(kind)
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
                if len(rows) != keyed - len(silent):
                    bad.append(f"theme-{i:02d}: fastfetch drew {len(rows)} keyed rows, "
                               f"expected {keyed - len(silent)}")
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
    if silent:
        suffix += f" [rendered nothing on this machine: {', '.join(silent)}]"
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
