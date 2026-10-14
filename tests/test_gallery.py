"""Task 4: gallery entry validation."""


def test_validate_gallery_entry_rejects_bad_size():
    from app import validate_gallery_entry
    ok, msg = validate_gallery_entry("x.png", 0, 24)
    assert ok is False and msg != ""


def test_validate_gallery_entry_accepts_good():
    from app import validate_gallery_entry
    ok, msg = validate_gallery_entry("x.png", 28, 24)
    assert ok is True


def test_validate_gallery_entry_rejects_bad_path():
    from app import validate_gallery_entry
    ok, msg = validate_gallery_entry("", 28, 24)
    assert ok is False
