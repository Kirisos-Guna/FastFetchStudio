"""Task 5: version is single-sourced from studio."""


def test_version_single_source():
    import studio
    assert studio.__version__.count(".") == 2


def test_app_exposes_version():
    import app
    import studio
    assert app.__version__ == studio.__version__
