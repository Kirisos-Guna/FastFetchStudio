"""Task 3: missing gallery files are skipped, never crash."""


def test_missing_gallery_file_skipped(tmp_path):
    from studio.encode import ensure_arts, ensure_sixels
    st = {"gallery": [{"path": str(tmp_path / "gone.png"), "w": 28, "h": 24}],
          "logWidth": 28, "logHeight": 24}
    assert ensure_sixels(st) == []
    assert ensure_arts(st) == []


def test_convert_missing_raises(tmp_path):
    import pytest

    from studio.images import convert_to_png
    with pytest.raises(FileNotFoundError):
        convert_to_png(tmp_path / "gone.png")


def test_art_key_fallback():
    from studio.encode import art_key
    assert art_key("foo.png") == "foo"
    assert art_key("...") == "img"
