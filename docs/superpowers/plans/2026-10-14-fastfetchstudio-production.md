# FastFetchStudio Production Upgrade Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make FastFetchStudio a production-level Windows tkinter app with cleaned code, fixed bugs, polished Gallery UX, and a distributable release.

**Architecture:** Keep Windows-only + tkinter + PowerShell launcher. Split `app.py` monolith (~2429 lines) into `studio/` package with thin GUI shell; keep `sixel_codec.py`, `setup.ps1`, launcher/preview templates behavior-identical.

**Tech Stack:** Python 3.10+, tkinter/ttk, Pillow, PyInstaller `--onefile --noconsole`, PowerShell 5.1/7+, fastfetch.exe, pytest + ruff.

**Spec:** `README.md` + `SETUP.md` + discovered behavior (`app.py`, `sixel_codec.py` 236 lines, `setup.ps1` 851 lines, `.github/workflows/build.yml`, `build.bat`). No separate spec doc exists; executor reads README as spec.

## Global Constraints

- Windows-only; keep tkinter, no new frontend stack.
- Python floor 3.10 (per `app.py` docstring); CI pins 3.12.
- Pillow is the only new runtime dep; never add unpinned deps.
- Launcher `fastfetch-random.ps1` and `gui/preview.ps1` output must stay byte-compatible in behavior (all existing `build.yml` probe assertions keep passing).
- `config.jsonc` backed up once to `config.backup.jsonc`; never overwrite backup.
- No terminal resizing; no `SetWindowSize` in preview/launcher.

## Review Focus

- Corrupt `gui/studio-state.json` on launch → expect app starts with defaults, no traceback.
- Default image file deleted from disk → expect fallback to first gallery item + visible warning, never crash in `ensure_sixels/ensure_arts`.
- Pillow missing → expect convert/encode paths disabled with message, app still opens.
- Narrow window (< logo+46+1+4 cols) → expect logo dropped, frame intact, explanatory note only when a pinned default exists.
- `fastfetch.exe` missing at Apply/preview → expect measured width falls back to 44, preview/launcher explain rather than fail silently.

---

## File Structure

- `studio/__init__.py` — package marker, `__version__`.
- `studio/state.py` — `load_state/save_state`, `DEFAULT_STATE`, paths (`FF_DIR`, `GUI_DIR`, etc.).
- `studio/images.py` — `convert_to_png`, `load_preview_image`, `_same_file`.
- `studio/config_build.py` — `hex_to_sgr`, `box_width/frame_width/box_lines`, `measure_content_width/fitted_box_width`, `build_config_json/dump_jsonc`, `BOX_*`, `KEY_*` constants.
- `studio/themes.py` — `shift_color`, `palettes_for`, `generate_theme_files`, `write_config`.
- `studio/encode.py` — `ensure_sixels`, `ensure_arts`, `art_key`, `write_preview_sixel`.
- `studio/launcher.py` — `LAUNCHER_TEMPLATE`, `generate_launcher`, `profile_snippet`.
- `studio/preview.py` — `PREVIEW_TEMPLATE`, `write_preview_script`, `preview_command/argv`, `spawn_terminal_preview`.
- `app.py` — thin GUI shell only (imports `studio.*`).
- `requirements.txt` + `requirements-dev.txt` — `pillow`, `pyinstaller`, `pytest`, `ruff`.
- `tests/` — new pytest suite mirroring each module.
- `FastFetchStudio.spec` — new versioned PyInstaller spec (replaces bare CLI flags in `build.bat`).

---

### Task 1: Dead-code removal + dependency baseline

**Files:**
- Modify: `app.py:1-120`, `build.bat`, `.gitignore`
- Create: `requirements.txt`, `requirements-dev.txt`, `tests/test_baseline.py`

**Interfaces:**
- Consumes: nothing.
- Produces: `requirements.txt` (pillow pinned), `tests/test_baseline.py::test_imports_clean`.

- [ ] **Step 1: Write failing test** — `tests/test_baseline.py` asserts no duplicate `BOX_WIDTH = 44` definition, `SIXEL_DCS` is used or removed, `import app` exposes `__version__`:

```python
def test_no_duplicate_box_width():
    src = open("app.py").read()
    assert src.count("BOX_WIDTH = 44") <= 1
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_baseline.py -v`
Expected: FAIL (duplicate exists, no `__version__`).

- [ ] **Step 3: Implement `studio/__init__.py::__version__` + dedupe in `app.py`**

Remove second `BOX_WIDTH = 44`, remove-or-wire `SIXEL_DCS`, drop unused imports (`base64`, `tempfile`, `threading`, `time`, `webbrowser` unless used by GUI half lines 1210-2429 — verify first), add `requirements.txt` (`pillow==<current-ci-version>`), `requirements-dev.txt` (`pytest`, `ruff`, `pyinstaller`).

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_baseline.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add requirements.txt requirements-dev.txt tests/test_baseline.py app.py studio/__init__.py
git commit -m "chore: baseline deps and remove dead code"
```

### Task 2: Split monolith into `studio/` package

**Files:**
- Create: `studio/state.py`, `studio/config_build.py`, `studio/themes.py`, `studio/images.py`, `studio/encode.py`, `studio/launcher.py`, `studio/preview.py`
- Modify: `app.py` (thin re-export + GUI only)
- Test: `tests/test_imports.py`

**Interfaces:**
- Consumes: `requirements.txt` from Task 1.
- Produces: `studio.state.load_state() -> dict`, `studio.state.save_state(st: dict) -> None`, `studio.config_build.build_config_json(st: dict, accent: str) -> dict`, `studio.themes.palettes_for(base: dict) -> list[dict]`, `studio.launcher.generate_launcher(st: dict) -> None` — later tasks import these exact names.

- [ ] **Step 1: Write failing test**

```python
def test_new_package_imports():
    from studio.state import load_state, save_state
    from studio.config_build import build_config_json
    from studio.themes import palettes_for
    assert callable(load_state)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_imports.py -v`
Expected: FAIL with "No module named 'studio'".

- [ ] **Step 3: Move code verbatim**

Move functions listed in File Structure with no behavior change; `app.py` becomes `from studio.* import *` + GUI class only. Keep `LAUNCHER_TEMPLATE`/`PREVIEW_TEMPLATE` strings byte-identical.

- [ ] **Step 4: Verify**

Run: `pytest tests/test_imports.py tests/test_baseline.py -v`
Expected: PASS. Run: `python -c "import app; print('ok')"` Expected: `ok`.

- [ ] **Step 5: Commit**

```bash
git add studio/ app.py tests/test_imports.py
git commit -m "refactor: split app.py into studio package"
```

### Task 3: Bug-fix sweep (state, gallery, sixel/art, measure)

**Files:**
- Modify: `studio/state.py`, `studio/images.py`, `studio/encode.py`, `studio/config_build.py`
- Test: `tests/test_state.py`, `tests/test_encode.py`, `tests/test_config.py`

**Interfaces:**
- Consumes: `studio.state.load_state/save_state`, `studio.encode.ensure_sixels/ensure_arts`, `studio.config_build.measure_content_width/fitted_box_width`.
- Produces: hardened behavior (same signatures, no new params).

- [ ] **Step 1: Write failing tests**

```python
def test_corrupt_state_falls_back_to_defaults(tmp_path, monkeypatch):
    monkeypatch.setattr("studio.state.STATE_PATH", tmp_path / "studio-state.json")
    (tmp_path / "studio-state.json").write_text("{broken")
    from studio.state import load_state
    assert load_state()["logWidth"] == 28

def test_missing_gallery_file_skipped(tmp_path):
    from studio.encode import ensure_sixels
    st = {"gallery": [{"path": str(tmp_path/"gone.png"), "w":28,"h":24}], "logWidth":28,"logHeight":24}
    assert ensure_sixels(st) == []

def test_measure_no_exe_returns_zero(monkeypatch):
    from studio import config_build
    monkeypatch.setattr(config_build, "FF_EXE", config_build.Path("C:/nonexistent.exe"))
    assert config_build.measure_content_width({}) == 0
```

- [ ] **Step 2: Run to verify they fail**

Run: `pytest tests/test_state.py tests/test_encode.py tests/test_config.py -v`
Expected: FAIL (traceback / exception on corrupt JSON, missing-file crash).

- [ ] **Step 3: Fix**

`load_state`: catch `JSONDecodeError`, validate `dict`, coerce `logWidth/logHeight/boxWidth` to int with fallback; `convert_to_png`: raise `FileNotFoundError` with friendly message; `ensure_sixels/ensure_arts`: skip missing src, catch per-file `Exception` and continue; `measure_content_width`: guard `FF_EXE`/`CONFIG_PATH` missing + `subprocess.TimeoutExpired`. Replace bare `except:` with `except Exception:`.

- [ ] **Step 4: Verify**

Run: `pytest tests/ -v`
Expected: PASS. Run existing CI probes locally if on Windows: `python app.py --selftest`.

- [ ] **Step 5: Commit**

```bash
git add studio/state.py studio/images.py studio/encode.py studio/config_build.py tests/test_state.py tests/test_encode.py tests/test_config.py
git commit -m "fix: harden state, gallery, and measure paths"
```

### Task 4: Gallery & UX polish (agreed new-features scope)

**Files:**
- Modify: `app.py` (Gallery tab only), `studio/encode.py`, `studio/preview.py`
- Test: `tests/test_gallery.py`

**Interfaces:**
- Consumes: `studio.images.convert_to_png(src: Path) -> Path`, `studio.preview.spawn_terminal_preview(st: dict, image_path: str) -> tuple[bool,str]`.
- Produces: `app.validate_gallery_entry(path:str,w:int,h:int) -> tuple[bool,str]` (new; GUI uses it before Apply).

- [ ] **Step 1: Write failing test**

```python
def test_validate_gallery_entry_rejects_bad_size():
    from app import validate_gallery_entry
    ok, msg = validate_gallery_entry("x.png", 0, 24)
    assert ok is False and msg != ""
```

- [ ] **Step 2: Run to verify it fails**

Run: `pytest tests/test_gallery.py -v`
Expected: FAIL "function not defined".

- [ ] **Step 3: Implement**

Bulk import via `filedialog.askopenfilenames`, per-image `w/h` spinbox validation (1..80 cols, 1..60 rows), default-logo fallback to `gallery[0]` with status-bar warning, Apply runs encode off UI thread with progress + per-file error list, Preview button surfaces `(ok,msg)` in status bar, Copy-button for `profile_snippet()`. No drag-drop lib (tkinter-dnd2 not in stack — use multi-select dialog).

- [ ] **Step 4: Verify**

Run: `pytest tests/test_gallery.py -v`
Expected: PASS. Manual: open GUI, import 3 images, delete default file, Apply → warning not crash.

- [ ] **Step 5: Commit**

```bash
git add app.py studio/encode.py studio/preview.py tests/test_gallery.py
git commit -m "feat: gallery bulk import, validation, and apply feedback"
```

### Task 5: Distributable release hardening

**Files:**
- Create: `FastFetchStudio.spec`, `assets/icon.ico` (or document missing)
- Modify: `build.bat`, `.github/workflows/build.yml`, `studio/__init__.py`
- Test: `tests/test_version.py`

**Interfaces:**
- Consumes: all `studio.*` modules.
- Produces: `dist/FastFetchStudio.exe` with embedded `__version__`.

- [ ] **Step 1: Write failing test**

```python
def test_version_single_source():
    import studio
    assert studio.__version__.count(".") == 2
```

- [ ] **Step 2: Run to verify it fails**

Run: `pytest tests/test_version.py -v`
Expected: FAIL (no `__version__`).

- [ ] **Step 3: Implement**

Single-source `__version__ = "1.2.0"` in `studio/__init__.py`, `FastFetchStudio.spec` (`onefile`, `noconsole`, `icon`, `version`), `build.bat` calls spec, CI pins `pillow`/`pyinstaller` versions, adds `pytest tests/` job before exe smokes, uploads versioned artifact.

- [ ] **Step 4: Verify**

Run: `pytest tests/ -v`
Expected: PASS. Run: `pyinstaller FastFetchStudio.spec` (Windows)
Expected: `dist/FastFetchStudio.exe` builds.

- [ ] **Step 5: Commit**

```bash
git add FastFetchStudio.spec build.bat .github/workflows/build.yml studio/__init__.py tests/test_version.py
git commit -m "chore: versioned PyInstaller release build"
```

### Task 6: Quality gates + docs

**Files:**
- Create: `ruff.toml` (or `pyproject.toml`)
- Modify: `README.md`, `SETUP.md`, `.github/workflows/build.yml`
- Test: `ruff check .`, `pytest tests/`

**Interfaces:**
- Consumes: all prior tasks.
- Produces: clean `ruff check` + updated docs.

- [ ] **Step 1: Write gate**

Add `ruff` config (line-length 120, `py310` target) and CI step; no new test file, gate is the test.

- [ ] **Step 2: Run to verify it fails**

Run: `ruff check studio/ app.py`
Expected: FAIL with unused-import/leak warnings.

- [ ] **Step 3: Fix**

Clear ruff findings, add first-run wizard text + logging to `gui/` log file, update README (requirements, dev setup, test commands).

- [ ] **Step 4: Verify**

Run: `ruff check . && pytest tests/ -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add ruff.toml README.md SETUP.md .github/workflows/build.yml
git commit -m "chore: lint, logging, and release docs"
```

---

## Self-Review

Spec coverage: Gallery polish→Task 4; dead code→Tasks 1-2; bugs→Task 3; quality/release→Tasks 5-6. Proportion: plan is interface + test focused, bodies left to implementer. Type consistency: `studio.*` signatures reused verbatim across tasks.
