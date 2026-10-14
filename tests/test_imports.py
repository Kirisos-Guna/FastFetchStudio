"""Task 2: studio package exposes the agreed interfaces; app stays compatible."""


def test_new_package_imports():
    from studio.config_build import build_config_json
    from studio.encode import ladder_cells, logo_cell_px
    from studio.launcher import generate_launcher, redraw_flag, write_config
    from studio.metrics import measure_argv, spawn_measure, write_measure_script
    from studio.state import load_state, save_state
    from studio.themes import palettes_for

    assert callable(load_state)
    assert callable(save_state)
    assert callable(build_config_json)
    assert callable(palettes_for)
    assert callable(generate_launcher)
    assert callable(redraw_flag)
    assert callable(spawn_measure)
    assert callable(write_config)
    assert callable(logo_cell_px)
    assert callable(ladder_cells)
    assert callable(write_measure_script)
    assert callable(measure_argv)


def test_app_compat_shim():
    import app

    for name in ("generate_launcher", "write_preview_script", "preview_argv",
                 "apply_all", "load_state", "PREVIEW_SCRIPT",
                 "write_measure_script", "measure_argv", "MEASURE_SCRIPT"):
        assert hasattr(app, name), f"app.{name} missing"
