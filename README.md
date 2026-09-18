# FastFetch Studio

GUI customizer + random launcher for [fastfetch](https://github.com/fastfetch-cli/fastfetch) on Windows.

Pick images, color theming, and randomization in a small desktop app — every new terminal
window then shows a **random logo + random color theme** automatically.

![FastFetch Studio's Gallery tab, showing four imported logo images with one marked as the default](docs/screenshot.png)

## Download

Grab the standalone exe from [Releases](../../releases) — no Python or other dependencies
needed. Windows SmartScreen may warn on first run for unsigned exes: **More info → Run anyway**.

## Set up a fresh machine (one command)

No fastfetch yet, or don't want to touch `$PROFILE` by hand? One script installs fastfetch,
puts it on your `PATH`, and hooks it into Windows PowerShell 5.1 and PowerShell 7+ — no
Administrator rights needed, safe to re-run:

```powershell
irm https://raw.githubusercontent.com/Kirisos-Guna/FastFetchStudio/main/setup.ps1 | iex
```

It detects which shells you actually have, skips anything already installed, and refuses to
add a second fastfetch call if your profile already has one. See **[SETUP.md](SETUP.md)** for
options, verification, and uninstall.

## Features

- **Gallery tab** — upload PNG / JPG / WEBP / GIF / BMP images (auto-converted to PNG into
  `~\.config\fastfetch\pngs\`), thumbnail grid, per-image size in terminal cells,
  set a default/fallback logo.
- **Theme tab** — key colors per module group (OS, packages, terminal, CPU, driver, title),
  box border style (rounded / double / heavy / ASCII / none) and color, separator text,
  default logo size, palette-block toggle.
- **Random tab** — choose what randomizes per terminal open (logo, theme), how often
  (every window or once per day), and how the logo is drawn (auto / always draw the image /
  built-in ASCII only).
- **Live preview** — renders the logo to a real sixel image and opens it in a Windows
  Terminal window, with the same theme and module list the launcher will draw. The window stays
  open until you close it, so the fetch does not flash past. It opens at whatever size Windows
  Terminal last used, and a narrow one will wrap the fetch over the logo — resize the window and
  click **Preview in terminal** again. The app deliberately does not resize your terminal for
  you: Windows Terminal remembers the last window size, so doing that would change the width of
  every terminal you open afterwards.
- **8 palettes** — your exact colors plus 7 hue-rotated variants are generated so
  randomization has variety.
- **An icon on every row** — the generated config switches fastfetch's key icons on, so all
  thirteen labeled modules (chassis, OS, kernel, packages, display, terminal, WM, CPU, GPU,
  driver, memory, OS age, uptime) get a Nerd Font glyph, tinted with that row's key color.
  The glyph comes from fastfetch's own per-type default rather than being typed into each
  label, so adding a module can never leave a row icon-less. Needs a Nerd Font in your
  terminal; `fastfetch --key-type string` prints plain labels instead.

## What it generates

| File | Purpose |
|---|---|
| `~\.config\fastfetch\config.jsonc` | main fastfetch config (original backed up once to `config.backup.jsonc`) |
| `~\.config\fastfetch\themes\theme-01..08.jsonc` | the 8 palettes |
| `~\.config\fastfetch\fastfetch-random.ps1` | picks a random theme + logo each run, and draws once per shell session. It detects the terminal first — sixel on Windows Terminal (1.22+), WezTerm, foot, contour, mlterm, yaft; the kitty protocol on kitty, WezTerm and Ghostty; block art from `arts\` anywhere else; fastfetch's tinted ASCII logo as the last resort. Written by **Apply & Generate**; `setup.ps1` writes a plain bootstrap here so the profile hook works before the app has ever run |
| `~\.config\fastfetch\arts\*.art` | your logos pre-rendered as truecolour block art, for terminals that can display no image protocol |
| `~\.config\fastfetch\gui\studio-state.json` | app state (gallery, colors, settings) |
| `~\.config\fastfetch\gui\preview.ps1` | the payload the **Preview in terminal** button runs; regenerated on every click |

## Hook it into PowerShell (one time)

Paste this into your `$PROFILE`, replacing any old fastfetch block:

```powershell
$ffLauncher = "$env:USERPROFILE\.config\fastfetch\fastfetch-random.ps1"
if (Test-Path -LiteralPath $ffLauncher) { & $ffLauncher } else { fastfetch.exe }
```

The `Test-Path` guard matters: `fastfetch-random.ps1` is a *generated* file. A bare
`& "$env:USERPROFILE\.config\fastfetch\fastfetch-random.ps1"` works once the file exists, but
on a machine where it does not yet, every new shell opens with

> `& : The term '...\fastfetch-random.ps1' is not recognized as the name of a cmdlet, function, script file, or operable program.`

A copy-pasteable snippet with a Copy button is also available inside the app (Theme tab).

Or let [setup.ps1](SETUP.md) do the download, the profile edit, *and* write the launcher for
you — including PowerShell 7+, which the app itself does not configure.

### My logo doesn't show — I get the ASCII Windows logo

Only some terminals can display an image, and image bytes sent to one that cannot render them
print garbage. So the launcher works out which terminal it is in first:

| Terminal | How your logo is drawn |
|---|---|
| Windows Terminal (1.22+), WezTerm, foot, contour, mlterm, yaft | your image, over sixel |
| kitty, WezTerm, Ghostty | your image, over the kitty protocol |
| everything else — classic conhost, and the VS Code terminal with `terminal.integrated.gpuAcceleration` off | your image as **block art** |
| no images uploaded, or *Never* selected on the Random tab | fastfetch's built-in ASCII Windows logo |

The terminal is identified from its environment variables first (`WT_SESSION`, `TERM_PROGRAM`,
`WEZTERM_PANE`, …) and, failing those, from the process tree — see
[the logo is sharp in one window and blocky in another](#the-logo-is-sharp-in-one-window-and-blocky-in-another).

Block art is your picture drawn with half-block characters in true colour — one terminal row
per two image rows, so it carries twice the resolution a single character per pixel would. It
is coarser than a real image, but it is *your* logo, and it is the only thing that can appear
in a terminal that supports no image protocol at all.

If even the block art is unavailable, the launcher says so rather than swapping your logo out
in silence:

> FastFetch Studio: no image support detected in the VS Code terminal, so the built-in logo was drawn.
> force it: $env:FASTFETCH_STUDIO_LOGO = 'image'   (Windows Terminal 1.22+ draws it as-is)

Ways out:

- **Use Windows Terminal 1.22 or newer** for the real image, with no configuration.
- **Force it** — *Random* tab → **How the logo is drawn** → *Always draw the image*. For a
  single shell without changing the setting, set `$env:FASTFETCH_STUDIO_LOGO` to `image`
  (force the picture) or `builtin` (force the ASCII logo) before the fetch runs.

### The logo is sharp in one window and blocky in another

Same terminal, same picture, two different renders: the crisp image in one window, block art in
the next, with the `Terminal` line reading *Windows Terminal* in both.

Windows Terminal sets `WT_SESSION` in the shells **it starts**, but not in every session it
*shows*. When Windows Terminal is the default terminal application, a console created by some
other process is merely displayed by it and never gets the variable; an elevated shell, or one
started by a tool that rebuilds the environment, loses it as well. The terminal is still Windows
Terminal in all of those cases — and fastfetch, which identifies its host from the process tree,
says so on the `Terminal` line. An environment-only check therefore made the launcher disagree
with the very fetch it was drawing: it concluded "no image support" and fell back to block art.

The launcher now also walks the process tree for `WindowsTerminal.exe`, so a Windows Terminal
session without `WT_SESSION` still gets the sixel. That check runs **after** the terminals that
name themselves (VS Code, Zed, WezTerm, kitty, Ghostty), because an editor opened from a Windows
Terminal tab keeps `WindowsTerminal.exe` in its process tree while its own terminal still cannot
draw sixel. A console Windows Terminal merely *hosts* has no `WindowsTerminal.exe` above it, so
it keeps the block art — the same verdict fastfetch reaches when it calls that terminal `dumb`.

### The fetch prints twice, in two different colours

Your `$PROFILE` has two fastfetch calls in it — most often `setup.ps1`'s managed block *plus*
the snippet above pasted in afterwards without removing it. Each call picks its own random
theme, so you get the fetch twice in two colour schemes.

The launcher now draws **once per shell session**, so a second call is a no-op. To stop it
being called at all, delete whichever of the two you do not want — `setup.ps1 -Uninstall`
removes its own block, or delete the pasted lines by hand.

### Preview in terminal fails with `Error 2147942402 (0x80070002)`

Fixed in v1.1.11. The button used to hand Windows Terminal a
`powershell -Command "$env:WT_SESSION=…; & fastfetch …"` one-liner, and Windows Terminal
splits its command line on `;` **even inside a quoted argument** — it cut the line in two and
tried to launch the second half, which began with `&`, as a program:

```
[error 2147942402 (0x80070002) when launching `" & "$env:USERPROFILE\.local\bin\fastfetch.exe" …`]
The system cannot find the file specified.
```

The payload is now a generated file (`gui\preview.ps1`) launched with `-File`, so there is
nothing on the `wt` command line for that parser to split. Update to v1.1.11 or later.

### Prerequisite: the execution policy must allow scripts

On a fresh Windows install the effective policy is `Restricted`, which blocks `.ps1` files
**and profiles**. Both the pasted line and the profile hook stay silent until you allow
signed-local scripts. This is user-scoped — no Administrator rights needed:

```powershell
Set-ExecutionPolicy -Scope CurrentUser RemoteSigned
```

Check it with `Get-ExecutionPolicy`. `setup.ps1 -FixExecutionPolicy` will do it for you.
If the policy is enforced by Group Policy, only your administrator can change it.

## Build from source

Requires Python 3.10+ on Windows.

```bat
build.bat
```

That installs `pillow` + `pyinstaller` and produces `dist\FastFetchStudio.exe`
(one portable file). The GitHub Actions workflow (`.github/workflows/build.yml`) builds
and attaches the exe to releases automatically.

## Uninstall

`.\setup.ps1 -Uninstall` removes the managed `$PROFILE` hook and the bootstrap launcher
(add `-RemoveBinary` to delete the fastfetch binary too). It leaves a launcher that
FastFetch Studio generated, since that is your own configuration.

To do it by hand: delete `~\.config\fastfetch\themes\`, `fastfetch-random.ps1`, the `gui\`
folder, and restore `config.backup.jsonc` over `config.jsonc`. Uploaded images stay in
`pngs\`.

## License

MIT — see [LICENSE](LICENSE).
