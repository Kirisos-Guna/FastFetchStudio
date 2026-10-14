"""Color palettes and theme/config file generation."""
from __future__ import annotations

import colorsys
from pathlib import Path

from studio.config_build import build_config_json, dump_jsonc
from studio.state import THEMES_DIR


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
    return f"#{round(r2 * 255):02x}{round(g2 * 255):02x}{round(b2 * 255):02x}"


def palettes_for(base: dict) -> list[dict]:
    """8 palettes: exact base + 7 hue rotations around the accent color.

    The returned dicts always contain an 'accent' key even when the input
    lacks one (derived from title/os, else the default) - callers rely on it.
    """
    base = dict(base)
    if not base.get("accent"):
        base["accent"] = base.get("title") or base.get("os") or "#e04a3f"
    out = [dict(base)]
    for deg, sm, vm in ((40, 1.0, 1.0), (80, 1.0, 1.0), (140, 1.0, 1.0),
                        (180, 1.0, 1.0), (220, 1.0, 1.0), (280, 0.9, 1.05), (320, 1.1, 0.95)):
        out.append({k: shift_color(v, deg, sm, vm) for k, v in base.items()})
    return out


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


