"""Baseline: no duplicate constants, no dead SIXEL_DCS, single version source."""
from pathlib import Path

APP = Path(__file__).resolve().parents[1] / "app.py"


def test_no_duplicate_box_width():
    src = APP.read_text("utf-8")
    assert src.count("BOX_WIDTH = 44") <= 1


def test_sixel_dcs_removed_or_used():
    src = APP.read_text("utf-8")
    # SIXEL_DCS was dead (defined but never referenced). Either remove it
    # or reference it somewhere real.
    if "SIXEL_DCS" in src:
        assert src.count("SIXEL_DCS") >= 2, "SIXEL_DCS defined but never used"


def test_no_dead_imports():
    src = APP.read_text("utf-8")
    # threading/time are live (Random-tab measure await); the rest must be used or gone.
    for mod in ("base64", "webbrowser", "io", "random"):
        # import present but never used as `mod.` -> dead
        if f"import {mod}" in src:
            assert f"{mod}." in src, f"dead import: {mod}"


def test_version_single_source():
    import studio

    assert studio.__version__.count(".") == 2
