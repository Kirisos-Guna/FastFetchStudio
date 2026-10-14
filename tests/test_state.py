"""Task 3: state fallbacks."""
import json


def test_corrupt_state_falls_back_to_defaults(tmp_path, monkeypatch):
    import studio.state as st
    p = tmp_path / "studio-state.json"
    p.write_text("{broken")
    monkeypatch.setattr(st, "STATE_PATH", p)
    assert st.load_state()["logWidth"] == 28


def test_non_dict_state_falls_back(tmp_path, monkeypatch):
    import studio.state as st
    p = tmp_path / "studio-state.json"
    p.write_text(json.dumps([1, 2, 3]))
    monkeypatch.setattr(st, "STATE_PATH", p)
    assert st.load_state()["logWidth"] == 28


def test_bad_int_coerced(tmp_path, monkeypatch):
    import studio.state as st
    p = tmp_path / "studio-state.json"
    p.write_text(json.dumps({"logWidth": "huge", "logHeight": None}))
    monkeypatch.setattr(st, "STATE_PATH", p)
    got = st.load_state()
    assert got["logWidth"] == 28 and got["logHeight"] == 24


def test_roundtrip(tmp_path, monkeypatch):
    import studio.state as st
    p = tmp_path / "studio-state.json"
    monkeypatch.setattr(st, "STATE_PATH", p)
    monkeypatch.setattr(st, "GUI_DIR", tmp_path)
    s = st.load_state()
    s["logWidth"] = 30
    st.save_state(s)
    assert st.load_state()["logWidth"] == 30
