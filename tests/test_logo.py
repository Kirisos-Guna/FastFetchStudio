"""The logo has to reach the exe and the window, not just sit in the repo."""
from pathlib import Path

from PIL import Image

ROOT = Path(__file__).resolve().parents[1]


def test_ico_holds_every_size_windows_asks_for():
    with Image.open(ROOT / "assets" / "logo.ico") as ico:
        assert {(16, 16), (32, 32), (48, 48), (256, 256)} <= set(ico.info["sizes"])


def test_spec_sets_the_exe_icon_and_bundles_it_for_the_window():
    spec = (ROOT / "FastFetchStudio.spec").read_text("utf-8")
    assert 'icon="assets/logo.ico"' in spec
    assert '("assets/logo.ico", "assets")' in spec


def test_window_icon_path_points_at_the_real_file():
    import app

    assert app.app_icon_path() == ROOT / "assets" / "logo.ico"
    assert app.app_icon_path().exists()
