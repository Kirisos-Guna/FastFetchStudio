# FastFetch Studio

GUI customizer + random launcher for [fastfetch](https://github.com/fastfetch-cli/fastfetch) on Windows.

Pick images, color theming, and randomization in a small desktop app — every new terminal
window then shows a **random logo + random color theme** automatically.

![screenshot placeholder](docs/screenshot.png)

## Download

Grab the standalone exe from [Releases](../../releases) — no Python or other dependencies
needed. Windows SmartScreen may warn on first run for unsigned exes: **More info → Run anyway**.

## Features

- **Gallery tab** — upload PNG / JPG / WEBP / GIF / BMP images (auto-converted to PNG into
  `~\.config\fastfetch\pngs\`), thumbnail grid, per-image size in terminal cells,
  set a default/fallback logo.
- **Theme tab** — key colors per module group (OS, packages, terminal, CPU, driver, title),
  box border style (rounded / double / heavy / ASCII / none) and color, separator text,
  default logo size, palette-block toggle.
- **Random tab** — choose what randomizes per terminal open (logo, theme) and how often
  (every window or once per day).
- **Live preview** — renders the logo to a real sixel image and opens a preview in
  Windows Terminal.
- **8 palettes** — your exact colors plus 7 hue-rotated variants are generated so
  randomization has variety.

## What it generates

| File | Purpose |
|---|---|
| `~\.config\fastfetch\config.jsonc` | main fastfetch config (original backed up once to `config.backup.jsonc`) |
| `~\.config\fastfetch\themes\theme-01..08.jsonc` | the 8 palettes |
| `~\.config\fastfetch\fastfetch-random.ps1` | picks a random theme + logo each run (sixel on Windows Terminal, kitty-direct on WezTerm, built-in logo fallback) |
| `~\.config\fastfetch\gui\studio-state.json` | app state (gallery, colors, settings) |

## Hook it into PowerShell (one time)

Paste this into your `$PROFILE`, replacing any old fastfetch block:

```powershell
& "$env:USERPROFILE\.config\fastfetch\fastfetch-random.ps1"
```

A copy-pasteable snippet with a Copy button is also available inside the app (Theme tab).

## Build from source

Requires Python 3.10+ on Windows.

```bat
build.bat
```

That installs `pillow` + `pyinstaller` and produces `dist\FastFetchStudio.exe`
(one portable file). The GitHub Actions workflow (`.github/workflows/build.yml`) builds
and attaches the exe to releases automatically.

## Uninstall

Delete `~\.config\fastfetch\themes\`, `fastfetch-random.ps1`, the `gui\` folder, and restore
`config.backup.jsonc` over `config.jsonc`. Uploaded images stay in `pngs\`.

## License

MIT — see [LICENSE](LICENSE).
