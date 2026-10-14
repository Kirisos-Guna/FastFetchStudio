"""Task 3: measure/fitted guards."""


def test_measure_no_exe_returns_zero(monkeypatch):
    from pathlib import Path

    from studio import config_build
    monkeypatch.setattr(config_build, "FF_EXE", Path("C:/nonexistent-ff.exe"))
    assert config_build.measure_content_width({}) == 0


def test_fitted_never_below_default(monkeypatch):
    from studio import config_build
    monkeypatch.setattr(config_build, "measure_content_width", lambda st: 0)
    assert config_build.fitted_box_width({"boxWidth": 44}) == 44


def test_box_width_clamped():
    from studio.config_build import box_width, frame_width
    assert box_width({"boxWidth": 999}) == 64
    assert box_width({"boxWidth": 1}) == 40
    assert box_width({"boxWidth": "bad"}) == 44
    assert frame_width({"boxWidth": 44}) == 46


def test_hex_to_sgr_fallback():
    from studio.config_build import hex_to_sgr
    assert hex_to_sgr("#e04a3f") == "224;74;63"
    assert hex_to_sgr("bad") == "255;255;255"
