"""Rebased tree: cell-size ladder, cell-px resolution, measure argv shape."""


def test_logo_cell_px_defaults_and_bounds():
    from studio.encode import logo_cell_px
    assert logo_cell_px({}) == (10, 20)
    assert logo_cell_px({"cellW": 12, "cellH": 24}) == (12, 24)
    assert logo_cell_px({"cellW": 1, "cellH": 24}) == (10, 20)
    assert logo_cell_px({"cellW": "bad", "cellH": 24}) == (10, 20)


def test_ladder_cells_fractions_largest_first():
    from studio.encode import ladder_cells
    ladder = ladder_cells(28, 24)
    assert ladder[0] == (28, 24)
    assert len(ladder) == len(set(ladder))  # no duplicates
    assert all(10 <= w <= 28 and 8 <= h <= 24 for w, h in ladder)


def test_load_metrics_invalid_is_empty(tmp_path, monkeypatch):
    import studio.metrics as m
    p = tmp_path / "term-metrics.json"
    p.write_text("{broken")
    monkeypatch.setattr(m, "METRICS_PATH", p)
    assert m.load_metrics() == {}
    p.write_text('{"cellW": 1, "cellH": 500}')
    assert m.load_metrics() == {}


def test_measure_argv_has_no_semicolon():
    import app
    cmd = app.measure_argv(app.MEASURE_SCRIPT)
    assert not any(";" in c for c in cmd)
    joined = " ".join(cmd)
    assert "-File" in joined
