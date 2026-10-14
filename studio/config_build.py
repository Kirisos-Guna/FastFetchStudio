"""Frame geometry, fastfetch config construction, content measurement."""
from __future__ import annotations

import json
import re
import subprocess

from studio.state import BOX_WIDTH, BOX_WIDTH_MAX, BOX_WIDTH_MIN, CONFIG_PATH, FF_EXE

BORDER_CHARS = set("\u2500\u2501\u2550-\u250c\u2510\u2514\u2518\u2554\u2557\u255a\u255d"
                   "\u250f\u2513\u2517\u251b+ ")


ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")


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


KEY_ICON_MODE = "both"


KEY_PADDING_LEFT = 1     # the indent the keys used to carry as literal spaces


NO_KEY = " "


KEYED_MODULES = ("chassis", "os", "kernel", "packages", "display", "terminal",
                 "wm", "cpu", "gpu", "gpu", "memory", "disk", "uptime")


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
    try:
        if not FF_EXE.exists() or not CONFIG_PATH.exists():
            return 0
    except OSError:
        return 0
    try:
        proc = subprocess.run(
            [str(FF_EXE), "--config", str(CONFIG_PATH), "--logo", "none", "--pipe"],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=20, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    except (OSError, subprocess.TimeoutExpired, ValueError):
        return 0
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


