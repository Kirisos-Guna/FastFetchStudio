"""PowerShell launcher generation and Apply orchestration."""
from __future__ import annotations

import shutil
from pathlib import Path

from studio.config_build import box_width, build_config_json, dump_jsonc, fitted_box_width, frame_width
from studio.encode import ensure_arts, ensure_sixels, logo_cell_px
from studio.state import BACKUP_PATH, CMD_HOOK_PATH, CONFIG_PATH, GUI_DIR, LAUNCHER_PATH, LOGO_GAP, save_state
from studio.themes import generate_theme_files, palettes_for

RESIZE_GUARD_TEMPLATE = r"""# --- keep the fetch straight when the window is resized ---------------------
# There is no resize event to subscribe to on Windows (see Get-FFSGrid), so the
# prompt is where a resize is noticed: it compares the grid this fetch was drawn
# at with the grid now, and on a difference erases the torn copy and draws the
# fetch again at the new size.
function global:Repair-FFSFetch {
    param([hashtable]$Plan)
    # Take the torn copy off the screen before drawing a new one - and for a
    # sixel that means *scrolling it away*, not clearing. Measured on Windows
    # Terminal 1.24: [Console]::Clear() and CSI 2J both leave a sixel image
    # sitting on the screen (it lives in its own layer, not in the text
    # buffer), while scrolling the lines it was drawn on out of the viewport
    # removes it completely. v1.1.15 cleared first, so every resize left the
    # broken picture behind and painted another one over it - which is exactly
    # the banded, doubled logo the repair was supposed to fix.
    $rows = [int]$Plan.Rows
    if ($rows -le 0) { $rows = 40 }
    [Console]::Write(("`n" * ($rows + 2)))
    try { [Console]::Clear() } catch { }
    # `e is a PowerShell 7-only escape; profiles also run in Windows PowerShell
    # 5.1, where it prints literally as "e[3J". Build the real ESC character
    # explicitly so both shells send the control sequence to the terminal.
    $clearScreen = [string][char]27 + '[3J' + [string][char]27 + '[1;1H'
    [Console]::Write($clearScreen)
    Show-FFSFetch -Plan $Plan
}

function global:Redraw-Fetch {
    # Draw the fetch again, right now. Typing this is the impatient version of
    # waiting for the next prompt (which repairs it on its own).
    if ($global:FFSPlan) { Repair-FFSFetch -Plan $global:FFSPlan }
}

function global:Invoke-FFSExe {
    # Run fastfetch with the console as its own stdout, and return its exit code.
    #
    # Not "& $exe": a native command called that way only writes to the screen
    # when its pipeline ends at the host. Called from inside the prompt function
    # (where the resize repair and the deferred first draw run) its output is
    # captured instead and becomes the prompt *string*, decoded through the OEM
    # code page on the way - the frame came out as cp437 mojibake and the sixel as
    # noise, or the picture alone with no text. From an event action it is
    # swallowed entirely. A child process started without redirection inherits
    # the console and writes to it directly, wherever it is called from.
    #
    # The exit code goes to $LASTEXITCODE, as it would for "& $exe", so callers
    # read it the same way - and the CI probes can swap this one statement for a
    # log write that sets it.
    param([string]$Exe, [string[]]$FFArgs)
    $quoted = foreach ($a in $FFArgs) {
        $a = [string]$a
        if ($a -and $a -notmatch '[\s"]') { $a; continue }
        # CommandLineToArgvW rules: double the backslashes before a quote or the
        # closing quote, escape the quote itself.
        '"' + (($a -replace '(\*)"', '$1$1\"') -replace '(\+)$', '$1$1') + '"'
    }
    $psi = New-Object Diagnostics.ProcessStartInfo
    $psi.FileName = $Exe
    $psi.Arguments = ($quoted -join ' ')
    $psi.UseShellExecute = $false
    $global:LASTEXITCODE = $null
    try {
        $proc = [Diagnostics.Process]::Start($psi)
        $proc.WaitForExit()
        $global:LASTEXITCODE = $proc.ExitCode
    } catch { }
}

function global:Get-FFSCursorRow {
    # The cursor's row, or -1 when there is no console to ask.
    try { return [int][Console]::CursorTop } catch { return -1 }
}

function global:Complete-FFSLogoRows {
    # Put the cursor under the picture when the fetch's text was shorter than it.
    # fastfetch is run with --logo-print-remaining false (its padding lines are
    # written over the picture and tear it), so after a short fetch the cursor
    # sits beside the picture's lower rows, and the prompt printed there cut the
    # picture off flat. A bare line feed only moves the cursor - no cell is
    # written, so no sixel pixel is erased.
    param([int]$Top, [int]$Rows)
    if ($Top -lt 0 -or $Rows -le 0) { return }
    $now = Get-FFSCursorRow
    if ($now -lt 0) { return }
    $used = $now - $Top
    if ($used -ge 0 -and $used -lt $Rows) { [Console]::Write("`n" * ($Rows - $used)) }
}

function global:Wait-FFSGridSettled {
    # Windows Terminal starts every shell at its default grid (120 x 30, or a
    # share of it for a split pane) and only resizes it to the real window a
    # few hundred milliseconds later - measured at 580-670 ms after the shell
    # process started, with the profile already running. A tiling tool or a
    # snapped/remembered window size lands even later. Drawing at the first size
    # laid the fetch out for 120 columns in a 70-column window; the terminal
    # then re-wrapped every row over the picture, which is the banded logo with
    # stray "741.0" / "GB)" tails at the left edge.
    #
    # So the first draw waits until the shell is old enough for that resize to
    # have landed and the grid has held still for a moment. A profile that is
    # already slower than that waits for nothing; the cap keeps a shell whose
    # window keeps changing from hanging.
    $start = $null
    try { $start = (Get-Process -Id $PID).StartTime } catch { }
    if (-not $start) { return }
    $last = Get-FFSGrid
    if ([int]$last[0] -le 0) { return }
    $first = $last
    $since = [DateTime]::Now
    while ($true) {
        $now = [DateTime]::Now
        $g = Get-FFSGrid
        if ([int]$g[0] -ne [int]$last[0] -or [int]$g[1] -ne [int]$last[1]) {
            $last = $g; $since = $now
        }
        $age = ($now - $start).TotalMilliseconds
        $still = ($now - $since).TotalMilliseconds
        if ($age -ge 1500) { return }
        if ($still -ge 150) {
            if ($age -ge 900) { return }
            # The resize already came: no reason to sit out the rest.
            if ([int]$last[0] -ne [int]$first[0] -or [int]$last[1] -ne [int]$first[1]) { return }
        }
        Start-Sleep -Milliseconds 20
    }
}

function global:Test-FFSNeedsRepair {
    # True when the window no longer matches the grid the fetch was drawn at
    # *and* the fetch is still the last thing on the screen. The repair clears
    # the screen, so once a command has run it would wipe that command's output
    # to redraw a logo - from then on a resize is left alone, and Redraw-Fetch
    # is there for anyone who wants the fetch back.
    $plan = $global:FFSPlan
    if (-not $plan -or -not $plan.Redraw) { return $false }
    $last = Get-History -Count 1
    if ($last -and [int]$last.Id -gt [int]$plan.HistoryId) {
        $plan.Redraw = $false
        if ($global:FFSIdleJob) {
            try { Unregister-Event -SourceIdentifier $global:FFSIdleJob.Name } catch { }
            try { Remove-Job -Job $global:FFSIdleJob -Force } catch { }
            $global:FFSIdleJob = $null
        }
        return $false
    }
    $now = Get-FFSGrid
    if ([int]$now[0] -le 0) { return $false }
    return ([int]$now[0] -ne [int]$plan.Grid[0] -or [int]$now[1] -ne [int]$plan.Grid[1])
}

function global:Restore-FFSPrompt {
    # Put the prompt back under a fetch drawn while PSReadLine was waiting for
    # input. InvokePrompt() redraws the prompt at the row PSReadLine remembered
    # from before - which the redraw has just painted over, so the prompt ended
    # up hidden inside the fetch (or scrolled away) and typing landed there too.
    # Point PSReadLine at the row the cursor is on now, then let it redraw. The
    # field is private, so every step is allowed to fail: the worst case is the
    # old behaviour.
    try {
        $t = [Microsoft.PowerShell.PSConsoleReadLine]
        $rl = $t.GetField('_singleton', 'NonPublic,Static').GetValue($null)
        $t.GetField('_initialY', 'NonPublic,Instance').SetValue($rl, [int][Console]::CursorTop)
    } catch { }
    try { [Microsoft.PowerShell.PSConsoleReadLine]::InvokePrompt() } catch { }
}

function global:Invoke-FFSIdleRepair {
    # Repair a resize while the prompt just sits there, instead of waiting for
    # Enter: snapping or tiling a fresh window, or Alt+Enter, otherwise leaves
    # the torn fetch on screen until the user types something. PSReadLine raises
    # OnIdle about every 300 ms while it waits for input.
    $plan = $global:FFSPlan
    if (-not $plan) { return }
    if ($plan.Deferred) {
        # The first draw was handed to the prompt, but the prompt was replaced
        # after the launcher ran (a theme engine set up later in the profile),
        # so it never happened. Draw it now, over the empty screen.
        $plan.Deferred = $false
        try { [Console]::Clear() } catch { }
        [Console]::Write([string][char]27 + '[3J' + [string][char]27 + '[1;1H')
        Wait-FFSGridSettled
        Show-FFSFetch -Plan $plan
        Restore-FFSPrompt
        return
    }
    if (-not (Test-FFSNeedsRepair)) { $plan.Pending = $null; return }
    # Never wipe a half-typed command line.
    $line = $null; $cursor = $null
    try { [Microsoft.PowerShell.PSConsoleReadLine]::GetBufferState([ref]$line, [ref]$cursor) } catch { return }
    if ($line) { return }
    # A window being dragged changes size on every tick; wait until it has
    # held one size for two ticks in a row, so the fetch is drawn once at the
    # end rather than at every step of the drag.
    $now = Get-FFSGrid
    $key = '' + $now[0] + 'x' + $now[1]
    if ($plan.Pending -ne $key) { $plan.Pending = $key; return }
    $plan.Pending = $null
    Repair-FFSFetch -Plan $plan
    Restore-FFSPrompt
}

function global:Install-FFSResizeGuard {
    if ($global:FFSGuardInstalled) { return }
    $grid = Get-FFSGrid
    if ([int]$grid[0] -le 0) { return }   # no console to resize - nothing to watch
    $global:FFSGuardInstalled = $true
    if (-not $global:FFSPrevPrompt) {
        # Keep whatever prompt is already there (oh-my-posh, a hand-written one)
        # and put it back in front afterwards, so this adds a check instead of
        # replacing the user's prompt. Never capture a guard's own wrapper: it
        # chains through the same global, so the two would call each other until
        # the call stack ran out.
        $prev = $function:prompt
        if ($prev -and $prev.ToString() -notmatch 'FFSPlan') { $global:FFSPrevPrompt = $prev }
    }
    function global:prompt {
        try {
            $plan = $global:FFSPlan
            if ($plan -and $plan.Deferred) {
                # The launcher left the first draw to here (see its last lines):
                # the wait no longer counts as profile time, and the fetch still
                # lands right above the first prompt.
                $plan.Deferred = $false
                Wait-FFSGridSettled
                Show-FFSFetch -Plan $plan
            } elseif (Test-FFSNeedsRepair) {
                Repair-FFSFetch -Plan $plan
            }
        } catch { }
        if ($global:FFSPrevPrompt) { & $global:FFSPrevPrompt }
        else { "PS $($executionContext.SessionState.Path.CurrentLocation)$('>' * ($nestedPromptLevel + 1)) " }
    }
    # Only where PSReadLine is the line editor: it is what raises OnIdle and
    # what can redraw the prompt afterwards.
    $plan = $global:FFSPlan
    if ($plan -and ($plan.Redraw -or $plan.Deferred) -and (Get-Module PSReadLine)) {
        try {
            $global:FFSIdleJob = Register-EngineEvent -SourceIdentifier PowerShell.OnIdle `
                -Action { Invoke-FFSIdleRepair } -ErrorAction Stop
        } catch { }
    }
}
"""


LAUNCHER_TEMPLATE = r"""# >>> FastFetch Studio :: launcher >>>
# Generated by FastFetch Studio - random logo + theme each run.
# Rewritten by "Apply & Generate" in the app; hand edits will be lost.
#
# Runs on every shell start, so it must be quiet on success and only speak up
# when something is actually wrong.
$ErrorActionPreference = 'SilentlyContinue'

# --- draw once per shell session -------------------------------------------
# A profile can end up with two fastfetch calls: setup.ps1's managed block, and
# the documented snippet pasted in afterwards without removing it. Both run, so
# the fetch is printed twice, each with its own random theme - which looks like
# a broken launcher. The marker is a global variable rather than an environment
# variable on purpose: $env: is inherited by child processes, so a nested shell
# would inherit it and draw nothing at all.
if ($global:FastFetchStudioDrawn) { return }
$global:FastFetchStudioDrawn = $true

# --- where things live -----------------------------------------------------
# $env:USERPROFILE is normally set, but it is missing in some hosts (services,
# scheduled tasks, a shell started under a different profile). Fall back rather
# than silently reading the wrong home directory.
$ffHome = $env:USERPROFILE
if (-not $ffHome) { $ffHome = $env:HOME }
if (-not $ffHome) { $ffHome = [Environment]::GetFolderPath('UserProfile') }

$ffRoot = Join-Path $ffHome '.config\fastfetch'
# Normal case is the documented location. If that folder does not exist but the
# script was started from somewhere that does hold the config, use that instead
# ($PSScriptRoot is empty when the file is dot-sourced or piped into iex).
if (-not (Test-Path -LiteralPath $ffRoot) -and $PSScriptRoot) { $ffRoot = $PSScriptRoot }

# --- locate fastfetch ------------------------------------------------------
# Resolve to a real file so a failure can name the paths it tried. The PATH
# lookup goes through Get-Command because that honours PATHEXT, which is what
# the call operator below actually requires.
$ffExe  = $null
$ffTried = @((Join-Path $ffHome '.local\bin\fastfetch.exe'),
             (Join-Path $ffRoot 'fastfetch.exe'))
foreach ($cand in $ffTried) {
    if (Test-Path -LiteralPath $cand -PathType Leaf) { $ffExe = $cand; break }
}
if (-not $ffExe) {
    $hit = Get-Command 'fastfetch.exe' -CommandType Application -ErrorAction SilentlyContinue |
           Select-Object -First 1
    if ($hit) { $ffExe = $hit.Source }
}
if (-not $ffExe) {
    # The old version swallowed this and simply did nothing, which looked like
    # a broken install with no explanation.
    Write-Host 'FastFetch Studio: fastfetch.exe was not found, so nothing was drawn.' -ForegroundColor Yellow
    Write-Host ('  looked in : ' + ($ffTried -join '  |  ')) -ForegroundColor DarkGray
    Write-Host '  looked on : PATH' -ForegroundColor DarkGray
    Write-Host '  fix       : re-run setup.ps1 (it installs into %USERPROFILE%\.local\bin)' -ForegroundColor DarkGray
    return
}

$themes = @(Get-ChildItem -LiteralPath (Join-Path $ffRoot 'themes') -Filter 'theme-*.jsonc' -File -ErrorAction SilentlyContinue)
$sixels = @(Get-ChildItem -LiteralPath (Join-Path $ffRoot 'sixels') -Filter '*.sixel' -File -ErrorAction SilentlyContinue |
           Where-Object { $_.BaseName -notmatch '-\d+x\d+$' })     # the ladder, not the pictures
$arts   = @(Get-ChildItem -LiteralPath (Join-Path $ffRoot 'arts') -Filter '*.art' -File -ErrorAction SilentlyContinue |
           Where-Object { $_.BaseName -notmatch '-\d+x\d+$' })
$pngDirs = @((Join-Path $ffRoot 'pngs'), (Join-Path $ffRoot 'images'))
$pngs    = @($pngDirs | ForEach-Object { Get-ChildItem -LiteralPath $_ -Filter '*.png' -File -Recurse -ErrorAction SilentlyContinue } | Sort-Object FullName -Unique)
$statePath = Join-Path $ffRoot 'gui\studio-state.json'

$randLogo = $true; $randTheme = $true; $freq = 'every'; $w = @@W@@; $h = @@H@@; $defaultImg = ''
$logoMode = 'auto'
# Baked in, then overridden from the state file below - like the logo size, this
# is what a launcher that has never met a state file falls back to.
$redrawOnResize = @@REDRAW@@
if (Test-Path -LiteralPath $statePath) {
    try {
        $s = Get-Content -LiteralPath $statePath -Raw | ConvertFrom-Json
        # Guard each field: a missing key used to become 0 / $false and quietly
        # disable randomisation or collapse the logo size.
        if ($null -ne $s.randomLogo)  { $randLogo  = [bool]$s.randomLogo }
        if ($null -ne $s.randomTheme) { $randTheme = [bool]$s.randomTheme }
        if ($s.frequency) { $freq = [string]$s.frequency }
        if ($s.logWidth)  { $w = [int]$s.logWidth }
        if ($s.logHeight) { $h = [int]$s.logHeight }
        if ($s.defaultImage) { $defaultImg = [string]$s.defaultImage }
        if ($s.logoMode) { $logoMode = [string]$s.logoMode }
        if ($null -ne $s.redrawOnResize) { $redrawOnResize = [bool]$s.redrawOnResize }
    } catch {}
}

# --- 'once per day' --------------------------------------------------------
$stamp = (Get-Date).ToString('yyyyMMdd')
$flagPath = Join-Path $ffRoot 'gui\.last-random-run'
if ($freq -eq 'daily' -and (Test-Path -LiteralPath $flagPath)) {
    $last = Get-Content -LiteralPath $flagPath -Raw -ErrorAction SilentlyContinue
    if ($last -and $last.Trim() -eq $stamp) { return }
}

$themeArg = @()
$accent = ''
if ($randTheme -and $themes.Count -gt 0) {
    $theme = Get-Random -InputObject $themes
    $themeArg = @('--config', $theme.FullName)
    try {
        $cfg = Get-Content -LiteralPath $theme.FullName -Raw | ConvertFrom-Json
        $accent = ($cfg.modules | Where-Object { $_.type -eq 'os' } | Select-Object -First 1).keyColor
    } catch {}
}

$logoArg = @()
$wantImage = $true
$ffHost = 'this terminal'
$ffSixel = $false
$ffKitty = $false

# Windows Terminal sets WT_SESSION in the shells it starts - but not in every
# session it *shows*. When it is the default terminal application, a console
# created by some other process is merely displayed by WT and WT never gets to
# add the variable; an elevated shell, or one started by a tool that rebuilds
# the environment, loses it as well. The terminal is still Windows Terminal in
# all of those cases, and fastfetch - which identifies its host from the process
# tree rather than the environment - says so. An env-only check therefore made
# the launcher disagree with the fetch it was drawing: the same terminal got the
# crisp sixel logo in one window and the block-art fallback in another. Ask the
# same question the same way.
function Test-WindowsTerminalHost {
    $proc = Get-CimInstance Win32_Process -Filter "ProcessId=$PID" -ErrorAction SilentlyContinue
    $depth = 0
    while ($proc -and $depth -lt 8) {
        if ($proc.Name -like 'WindowsTerminal*') { return $true }
        if (-not $proc.ParentProcessId) { return $false }
        $proc = Get-CimInstance Win32_Process -Filter ("ProcessId=" + $proc.ParentProcessId) -ErrorAction SilentlyContinue
        $depth++
    }
    return $false
}

function global:Get-TextLogoWidth {
    # How many columns a text logo file takes on screen, ANSI colours excluded.
    # fastfetch prints such a file verbatim - --logo-width has no effect on it -
    # so measuring the file is the only way to know whether it fits beside the
    # frame before it is drawn.
    #
    # Global, like the rest of the draw helpers: the resize guard calls this long
    # after the script's own scope has gone, and a script-scoped function is not
    # there any more.
    param([string]$Path)
    $widest = 0
    try {
        foreach ($line in [IO.File]::ReadAllLines($Path)) {
            $plain = [Text.RegularExpressions.Regex]::Replace($line, '\x1b\[[0-9;]*m', '')
            $plain = $plain.TrimEnd()
            if ($plain.Length -gt $widest) { $widest = $plain.Length }
        }
    } catch { }
    return $widest
}

function global:Get-FFSCellPx {
    # Pixel size of one cell, as the sixels in sixels\ were encoded for: the
    # terminal's measured size when it has one, the encoder's default otherwise.
    # Baked in at Apply time, so the launcher and the encoder can never drift.
    param([hashtable]$Plan)
    $cw = @@CELLW@@
    $ch = @@CELLH@@
    if ($Plan -and [int]$Plan.CellW -gt 0 -and [int]$Plan.CellH -gt 0) {
        $cw = [int]$Plan.CellW
        $ch = [int]$Plan.CellH
    }
    if ($cw -le 0) { $cw = 10 }
    if ($ch -le 0) { $ch = 20 }
    return @($cw, $ch)
}

function global:Get-FFSRasterSize {
    # The pixel size a sixel declares in its raster attributes ("1;1;W;H), which
    # is the size the terminal places it at. 0,0 when the file is missing or is
    # not a sixel, and the caller then draws no logo rather than guess one.
    param([string]$Path)
    if (-not $Path -or -not (Test-Path -LiteralPath $Path -PathType Leaf)) { return @(0, 0) }
    try {
        $fs = [IO.File]::OpenRead($Path)
        try {
            $head = New-Object byte[] 64
            $n = $fs.Read($head, 0, 64)
        } finally { $fs.Dispose() }
        $txt = -join ($head[0..([Math]::Max(0, $n - 1))] | ForEach-Object { [char]$_ })
        $m = [Text.RegularExpressions.Regex]::Match($txt, '"1;1;(\d+);(\d+)')
        if ($m.Success) { return @([int]$m.Groups[1].Value, [int]$m.Groups[2].Value) }
    } catch { }
    return @(0, 0)
}

# --- can this terminal draw an image, and how? ------------------------------
# Sending image bytes to a terminal that cannot render them prints garbage, so
# capability is detected rather than assumed. Getting this list wrong is what
# made a chosen logo silently turn into the built-in ASCII one.
#   sixel - Windows Terminal / OpenConsole (WT 1.22+), WezTerm, foot, contour,
#           mlterm, yaft
#   kitty - kitty, WezTerm, Ghostty
# Classic conhost draws neither. It is what you get from a bare powershell.exe
# outside Windows Terminal, a scheduled task, or a redirected run.
# VS Code and Zed are named only so the fallback can say where it happened.
# They ship image rendering off (VS Code needs terminal.integrated.gpuAcceleration),
# so claiming capability there would trade a visible logo for no logo at all.
if ($env:WT_SESSION) {
    $ffSixel = $true; $ffHost = 'Windows Terminal'
} elseif ($env:TERM_PROGRAM -eq 'Windows_Terminal') {
    $ffSixel = $true; $ffHost = 'Windows Terminal'
} elseif ($env:WEZTERM_PANE -or $env:TERM_PROGRAM -eq 'WezTerm') {
    $ffSixel = $true; $ffKitty = $true; $ffHost = 'WezTerm'
} elseif ($env:KITTY_WINDOW_ID -or $env:TERM -match 'kitty') {
    $ffKitty = $true; $ffHost = 'kitty'
} elseif ($env:TERM_PROGRAM -eq 'ghostty') {
    $ffKitty = $true; $ffHost = 'Ghostty'
} elseif ($env:TERM -match 'foot|contour|mlterm|yaft') {
    $ffSixel = $true; $ffHost = [string]$env:TERM
} elseif ($env:TERM_PROGRAM -eq 'vscode') {
    $ffHost = 'the VS Code terminal'
} elseif ($env:TERM_PROGRAM -eq 'zed') {
    $ffHost = 'the Zed terminal'
} elseif (Test-WindowsTerminalHost) {
    # Deliberately after the terminals that name themselves: an editor opened
    # *from* a Windows Terminal tab keeps WindowsTerminal.exe in its process
    # tree, and its own terminal still cannot draw sixel.
    $ffSixel = $true; $ffHost = 'Windows Terminal'
} elseif ($env:TERM_PROGRAM) {
    $ffHost = [string]$env:TERM_PROGRAM
}

# --- the user gets the last word -------------------------------------------
# Detection cannot cover every terminal, so this overrides it:
#   image   - draw the picture even when the terminal was not recognised
#   builtin - never draw it, always the tinted ASCII logo
# Comes from "Logo rendering" in the app; $env:FASTFETCH_STUDIO_LOGO wins over
# it so a single shell can be tested without touching the saved setting.
$ffLogoMode = $logoMode
if (-not $ffLogoMode) { $ffLogoMode = 'auto' }
if ($env:FASTFETCH_STUDIO_LOGO) { $ffLogoMode = [string]$env:FASTFETCH_STUDIO_LOGO }
if ($ffLogoMode -eq 'builtin') {
    $wantImage = $false
} elseif ($ffLogoMode -eq 'image') {
    # Keep whatever was detected; if nothing was, try the pre-encoded sixel
    # first (that is the format the app writes) and kitty-direct after it.
    if (-not $ffSixel -and -not $ffKitty) { $ffSixel = $true; $ffKitty = $true }
}

# --- the window, and one place that draws ----------------------------------
# Everything the draw needs is read from $global:FFSPlan rather than from this
# script's variables, because the launcher is a script: its scope is gone by the
# time the resize guard redraws at the next prompt.
function global:Get-FFSLogoFit {
    # The largest pre-encoded size of this picture the window can hold, as a
    # hashtable of W, H, Path and Px - or $null when even the smallest does not
    # fit.
    #
    # Every size in the ladder was encoded from the same image against the
    # terminal's measured cell size, so a variant's raster is exactly its cell
    # box. Picking a file keeps that true; rewriting --logo-width to whatever the
    # window allowed (what v1.1.15 did) broke it, and the fetch's text then
    # landed in the picture's own columns - which is what the terminal reacts to
    # by redrawing the picture in bands.
    #
    # Rows matter as much as columns: a 24-row picture in a 20-row window can
    # only be drawn by scrolling, and a scrolled sixel is exactly the torn one.
    param([string]$Path, [string]$Dir, [hashtable]$Plan, [int]$Room, [int]$Rows)
    if (-not $Path -or -not $Dir) { return $null }
    $cell = Get-FFSCellPx -Plan $Plan
    $base = [IO.Path]::GetFileNameWithoutExtension($Path)
    $files = @(Get-Item -LiteralPath $Path -ErrorAction SilentlyContinue)
    $files += @(Get-ChildItem -LiteralPath $Dir -Filter ($base + '-*x*.sixel') -File -ErrorAction SilentlyContinue)
    $best = $null
    foreach ($f in $files) {
        $px = @(Get-FFSRasterSize -Path $f.FullName)
        if ($px[0] -le 0 -or $px[1] -le 0) { continue }
        $w = [int][Math]::Ceiling($px[0] / [double]$cell[0])
        $h = [int][Math]::Ceiling($px[1] / [double]$cell[1])
        if ($w -gt $Room -or $h -gt $Rows) { continue }
        if ($null -eq $best -or ($w * $h) -gt ([int]$best.W * [int]$best.H)) {
            $best = @{ W = $w; H = $h; Path = $f.FullName; Px = $px }
        }
    }
    return $best
}

function global:Get-FFSArtFit {
    # The same choice for the block art: a text logo cannot tear, so it is what a
    # window too narrow or too short for the picture gets. The full-size file is
    # a candidate like any other - it is measured (that is what makes it drawable
    # at all) and the variants carry their size in the name, written by the
    # encoder from the very cells it rendered.
    param([string]$Path, [string]$Dir, [int]$Room, [int]$Rows)
    if (-not $Path -or -not $Dir) { return $null }
    $base = [IO.Path]::GetFileNameWithoutExtension($Path)
    $files = @(Get-Item -LiteralPath $Path -ErrorAction SilentlyContinue)
    $files += @(Get-ChildItem -LiteralPath $Dir -Filter ($base + '-*x*.art') -File -ErrorAction SilentlyContinue)
    $best = $null
    foreach ($f in $files) {
        $w = 0; $h = 0
        if ($f.BaseName -match '-(\d+)x(\d+)$') {
            $w = [int]$Matches[1]; $h = [int]$Matches[2]
        } else {
            $w = Get-TextLogoWidth -Path $f.FullName
            $h = @([IO.File]::ReadAllLines($f.FullName)).Count
        }
        if ($w -le 0 -or $h -le 0 -or $w -gt $Room -or $h -gt $Rows) { continue }
        if ($null -eq $best -or ($w * $h) -gt ([int]$best.W * [int]$best.H)) {
            $best = @{ W = $w; H = $h; Path = $f.FullName }
        }
    }
    return $best
}

function global:Get-FFSGrid {
    # The current grid as @(columns, rows). There is no resize event to
    # subscribe to on Windows - ReadConsoleInput's WINDOW_BUFFER_SIZE_EVENT is
    # the only signal, and PSReadLine owns the input buffer - so the size is
    # polled, and the answer changing is how a resize is noticed.
    $c = 0; $r = 0
    try { $c = [int][Console]::WindowWidth; $r = [int][Console]::WindowHeight } catch { }
    if ($c -le 0) {
        try { $c = [int]$Host.UI.RawUI.WindowSize.Width; $r = [int]$Host.UI.RawUI.WindowSize.Height } catch { }
    }
    return @($c, $r)
}

function global:Invoke-FFSRun {
    # Run fastfetch exactly ONCE, with at most one fallback - an image attempt
    # can fail on a terminal that cannot draw one, and the built-in logo is the
    # retry. The previous version called it twice on a successful run:
    # $LASTEXITCODE is $null when the call never reached a native command (the
    # exe vanished between the Test-Path and the call, say), and "$null -ne 0"
    # is TRUE.
    param([hashtable]$Plan, [string[]]$FFArgs, [string[]]$FFRetry)
    $attempts = @(,$FFArgs)
    if ($FFRetry) { $attempts += ,$FFRetry }
    foreach ($a in $attempts) {
        Invoke-FFSExe -Exe $Plan.Exe -FFArgs $a
        $ffRc = $LASTEXITCODE
        if ($null -eq $ffRc -or $ffRc -eq 0) { return }
    }
}

function global:Show-FFSFetch {
    # Draw the plan against the window that is there *now*. The fit is
    # recomputed on every draw instead of being captured, because a repair runs
    # after the window changed size - that is the whole point of it.
    #
    # The logo is drawn beside a frame that is a fixed number of columns wide,
    # so the two together are wider than most windows. fastfetch does not know
    # the frame is there: when the terminal is narrower than the pair it wraps
    # the line, the frame's border lands on the next row and the whole fetch
    # falls apart - which is what a small screen or a tiled window looked like.
    # Give the logo only what is left after the frame. That used to mean
    # rewriting --logo-width/--logo-height with whatever the window allowed,
    # which is a lie: those two flags only say how many cells the picture
    # occupies, so the picture stayed where it was and the text was laid out on
    # top of it. The size now comes from the file (Get-FFSRasterSize) and a
    # window too narrow for it gets the block art instead.
    #
    # @@LOGOGAP@@ of those columns are fastfetch's own logo padding, which sits
    # between the logo and the text and is easy to forget: without reserving it
    # the pair "fits" on paper while the frame's right corner is still pushed
    # three columns off the edge.
    param([hashtable]$Plan)
    $grid = Get-FFSGrid
    if ([int]$grid[0] -gt 0) { $Plan.Grid = @([int]$grid[0], [int]$grid[1]) }
    $cols = [int]$Plan.Grid[0]
    if ($cols -le 0) { $cols = 80 }
    $rows = [int]$Plan.Grid[1]
    if ($rows -le 0) { $rows = 24 }
    # How far the repair has to scroll to be rid of the picture - the whole
    # window is clearer than a few lines, and it is what takes a sixel off the
    # screen (clearing the text buffer does not).
    $Plan.Rows = $rows
    # One row is left for the prompt: a picture on the last row is only kept by
    # scrolling, and a scrolled sixel is the torn one.
    $rowRoom = $rows - 1
    $room = $cols - [int]$Plan.Band - 1 - [int]$Plan.Gap
    $logo = @()
    $retry = @()
    $w = 0
    $h = 0
    $pad = 0
    if ($Plan.Kind -eq 'sixel' -or $Plan.Kind -eq 'kitty') {
        # Which *file* to draw, and what to tell fastfetch about it.
        #
        # A sixel is placed by its raster size, so the picture's cell count is
        # read out of the file that is about to be drawn and passed on unchanged:
        # --logo-width does not scale an image, it only says how much room to
        # leave beside it, and a number that disagrees with the raster is what
        # tore the logo in the first place.
        #
        # Fitting therefore means picking a smaller file, not rewriting the size.
        # The ladder holds the same picture encoded at several cell sizes; the
        # largest one this window can hold wins.
        $fit = $null
        if ($Plan.Kind -eq 'sixel') {
            # One row less: the picture is drawn a row down (--logo-padding-top).
            $fit = Get-FFSLogoFit -Path $Plan.Logo -Dir $Plan.SixelsDir `
                                  -Plan $Plan -Room $room -Rows ($rowRoom - 1)
        } else {
            # kitty-direct hands the *PNG* to the terminal, which draws it into
            # the cell box fastfetch declares - so here the declaration is the
            # size, and the file's own raster is not consulted: a PNG declares
            # none (read as a sixel it measured 0x0, and the logo vanished from
            # every kitty and WezTerm window). The box is the configured one,
            # scaled down when the room the frame leaves cannot hold it, and
            # dropped when there is no room at all - a picture wider than the
            # window is what wrapped the fetch over itself.
            $w = [int]$Plan.W
            $h = [int]$Plan.H
            if ($w -gt 0 -and $h -gt 0) {
                if ($w -gt $room -or $h -gt $rowRoom) {
                    if ($room -lt 10 -or $rowRoom -lt 4) {
                        $w = 0; $h = 0
                    } else {
                        $scale = [Math]::Min($room / [double]$w, $rowRoom / [double]$h)
                        $w = [int][Math]::Max(4, [Math]::Round($w * $scale))
                        $h = [int][Math]::Max(4, [Math]::Round($h * $scale))
                    }
                }
                if ($w -gt 0 -and $h -gt 0) {
                    $fit = @{ W = $w; H = $h; Path = $Plan.Logo }
                }
            }
        }
        if (-not $fit) {
            # Nothing in the picture's ladder fits. The block art is the same
            # picture at block resolution - pure text, so it cannot tear - and
            # when even that is too wide the frame is drawn on its own.
            $w = 0
            $art = Get-FFSArtFit -Path $Plan.Art -Dir $Plan.ArtsDir -Room $room -Rows $rowRoom
            if ($art) { $logo = @('--logo', $art.Path) }
            else { $logo = @('--logo', 'none') }
        } elseif ($Plan.Kind -eq 'sixel') {
            $w = [int]$fit.W
            $h = [int]$fit.H
            # 'raw' passes the pre-encoded bytes straight through.
            # --logo-print-remaining false because fastfetch otherwise pads the
            # logo out to logo-height lines when the key list is shorter than
            # the picture, and those blank lines are written over the picture's
            # own rows - the other half of the banding.
            #
            # --logo-padding-top 1 lines the text up with the picture. After a
            # raw logo fastfetch prints a newline and then moves up logo-height
            # rows, which assumes the image left the cursor on its own last row;
            # Windows Terminal leaves it on the row *below* (measured: an N-row
            # sixel moves the cursor exactly N rows), so the text started one
            # row under the picture's top. The padding newline puts the picture
            # one row down and fastfetch counts it in the move up, which lands
            # the first line of text on the picture's first row.
            $logo  = @('--logo-type', 'raw', '--logo', $fit.Path,
                       '--logo-width', $w, '--logo-height', $h,
                       '--logo-print-remaining', 'false', '--logo-padding-top', '1')
            $pad = 1
            $retry = @('--logo', 'Windows11')
        } else {
            $w = [int]$fit.W
            $h = [int]$fit.H
            $logo  = @('--logo-type', 'kitty-direct', '--logo', $fit.Path,
                       '--logo-width', $w, '--logo-height', $h)
            $retry = @('--logo', 'Windows11')
        }
    } elseif ($Plan.Kind -eq 'art') {
        # fastfetch prints a *text* logo file verbatim - --logo-width has no
        # effect on it - so the block art is drawn only when it fits, and
        # nothing is drawn when it does not rather than fall back to a *wider*
        # logo: the built-in ASCII one is 40 columns and would be worse. The
        # ladder means a narrow window gets a smaller render instead of nothing.
        $art = Get-FFSArtFit -Path $Plan.Logo -Dir $Plan.ArtsDir -Room $room -Rows $rowRoom
        if ($art) { $logo = @('--logo', $art.Path) }
        else { $logo = @('--logo', 'none') }
    } else {
        $logo = @('--logo', 'Windows11')    # the built-in tinted ASCII logo
    }
    $ffArgs = @()
    if ($Plan.Theme) { $ffArgs += $Plan.Theme }
    $ffArgs += $logo
    # --logo-color only tints fastfetch's own logo, so it is passed on the
    # fallback path instead of beside an image.
    if ($Plan.Kind -ne 'sixel' -and $Plan.Kind -ne 'kitty') { $ffArgs += $Plan.Color }
    $ffArgs += $Plan.Fit
    $ffRetry = @()
    if ($retry.Count -gt 0) {
        if ($Plan.Theme) { $ffRetry += $Plan.Theme }
        $ffRetry += $retry
        $ffRetry += $Plan.Color
        $ffRetry += $Plan.Fit
    }
    $top = Get-FFSCursorRow
    Invoke-FFSRun -Plan $Plan -FFArgs $ffArgs -FFRetry $ffRetry
    if ($w -gt 0) { Complete-FFSLogoRows -Top $top -Rows ($h + $pad) }
    # What this draw actually did: the window it found, the room it had, the file
    # it chose and the size it told fastfetch. Written every time, so "my logo is
    # missing" (or smaller than it used to be) can be explained from a file
    # instead of from a screenshot - and so a test can check the choice without
    # reading pixels.
    try {
        $pick = 'none'
        if ($logo.Count -gt 1) { $pick = [IO.Path]::GetFileName([string]$logo[1]) }
        $how = 'none'
        if ($w -gt 0) { $how = 'picture' }
        elseif ($pick -like '*.art') { $how = 'block art' }
        Set-Content -LiteralPath (Join-Path $Plan.GuiDir 'last-draw.txt') -Encoding UTF8 -Value @(
            ('when  : ' + (Get-Date).ToString('s')),
            ('window: ' + $cols + ' x ' + $rows + ' cells'),
            ('room  : ' + $room + ' columns, ' + $rowRoom + ' rows'),
            ('drawn : ' + $how + ' (' + $pick + ')'),
            ('cells : ' + $(if ($w -gt 0) { '' + $w + ' x ' + $h } else { '-' }))
        )
    } catch { }
    # Say why the picture is missing - but only when one was clearly expected (a
    # pinned image with randomisation off), the same condition the fallback note
    # below uses. Otherwise every narrow window would print this, and that
    # silence is what once made "I set a logo and got a Windows logo" look like a
    # broken app.
    if ($Plan.WantImage -and -not $Plan.RandLogo -and $Plan.DefaultImg) {
        if ($w -le 0 -and ($Plan.Kind -eq 'sixel' -or $Plan.Kind -eq 'kitty')) {
            Write-Host ('FastFetch Studio: a ' + $cols + '-column window leaves no room for the logo beside the frame, so the frame was drawn on its own.') -ForegroundColor DarkGray
            Write-Host '  widen the window to bring the picture back.' -ForegroundColor DarkGray
        } elseif ($Plan.Kind -eq 'art' -and $logo[1] -eq 'none') {
            Write-Host ('FastFetch Studio: a ' + $cols + '-column window is too narrow for the logo beside the frame, so the frame was drawn on its own.') -ForegroundColor DarkGray
            Write-Host '  widen the window to bring the picture back.' -ForegroundColor DarkGray
        } elseif ($Plan.Kind -eq 'builtin') {
            Write-Host ('FastFetch Studio: no image support detected in ' + $Plan.Host + ', so the built-in logo was drawn.') -ForegroundColor DarkGray
            Write-Host '  force it: $env:FASTFETCH_STUDIO_LOGO = ''image''   (Windows Terminal 1.22+ draws it as-is)' -ForegroundColor DarkGray
        }
    }
}

# --- which logo, and which theme -------------------------------------------
# Chosen once per shell, then *recorded*: a repair after a resize redraws this
# same fetch, so neither the picture nor the palette may be re-rolled - a resize
# must never look like a new machine. Only the size is recomputed.
#
# No image protocol here means fastfetch prints a *text* logo file verbatim,
# ANSI escapes and all, so the block art FastFetch Studio pre-rendered gives the
# user their own picture at block resolution rather than a Windows logo.
# fastfetch's tinted ASCII logo is the last resort.
$ffKind = 'builtin'
$ffLogoPath = ''
# The block-art render of the same picture. It is what a window too narrow for
# the sixel gets: pure text, so it cannot be drawn wrong.
$ffArtPath = ''
if ($wantImage -and $ffSixel -and $sixels.Count -gt 0) {
    $six = $null
    if (-not $randLogo) {
        # Random logo OFF: always the chosen default image - never a random substitution.
        if ($defaultImg) {
            $stem = [IO.Path]::GetFileNameWithoutExtension($defaultImg)
            $key  = ($stem -replace '[^A-Za-z0-9]+', '-').Trim('-')
            if (-not $key) { $key = 'img' }
            $cand = Join-Path (Join-Path $ffRoot 'sixels') ($key + '.sixel')
            if (Test-Path -LiteralPath $cand) { $six = Get-Item -LiteralPath $cand }
        }
        if (-not $six) { $six = $sixels | Select-Object -First 1 }
    } else {
        $six = Get-Random -InputObject $sixels
    }
    $ffKind = 'sixel'; $ffLogoPath = $six.FullName
    $artCand = Join-Path (Join-Path $ffRoot 'arts') ($six.BaseName + '.art')
    if (Test-Path -LiteralPath $artCand) { $ffArtPath = $artCand }
} elseif ($wantImage -and $ffKitty -and $pngs.Count -gt 0) {
    $png = $null
    if (-not $randLogo -and $defaultImg -and (Test-Path -LiteralPath $defaultImg)) {
        $png = Get-Item -LiteralPath $defaultImg
    }
    if (-not $png) {
        if ($randLogo) { $png = Get-Random -InputObject $pngs }
        else { $png = $pngs | Select-Object -First 1 }
    }
    $ffKind = 'kitty'; $ffLogoPath = $png.FullName
} elseif ($wantImage -and $arts.Count -gt 0) {
    $art = $null
    if (-not $randLogo) {
        if ($defaultImg) {
            $stem = [IO.Path]::GetFileNameWithoutExtension($defaultImg)
            $key  = ($stem -replace '[^A-Za-z0-9]+', '-').Trim('-')
            if (-not $key) { $key = 'img' }
            $cand = Join-Path (Join-Path $ffRoot 'arts') ($key + '.art')
            if (Test-Path -LiteralPath $cand) { $art = Get-Item -LiteralPath $cand }
        }
        if (-not $art) { $art = $arts | Select-Object -First 1 }
    } else {
        $art = Get-Random -InputObject $arts
    }
    if ($art) { $ffKind = 'art'; $ffLogoPath = $art.FullName; $ffArtPath = $art.FullName }
}

$colorArgs = @()
if ($accent) { $colorArgs = @('--logo-color-1', $accent, '--logo-color-2', $accent) }

# fastfetch cannot know a frame is drawn beside the text, so a row longer than
# the window wraps and the frame's border lands on the next line. Refusing to
# wrap keeps every row on its own line: an over-long value is clipped at the edge
# instead of pulling the fetch apart. Without this, a window narrower than
# logo + frame wrapped every border and every long value.
$fitArgs = @('--disable-linewrap', 'true')

# --- the plan --------------------------------------------------------------
# One record of everything a draw needs, kept in a global on purpose: this file
# runs as a script, so its own scope is gone by the time the resize guard
# redraws at the next prompt.
$global:FFSPlan = @{
    Exe        = $ffExe
    Theme      = $themeArg
    Fit        = $fitArgs
    Color      = $colorArgs
    Kind       = $ffKind
    Logo       = $ffLogoPath
    Art        = $ffArtPath
    W          = $w
    H          = $h
    CellW      = @@CELLW@@
    CellH      = @@CELLH@@
    Band       = @@BOX@@
    Gap        = @@LOGOGAP@@
    WantImage  = $wantImage
    RandLogo   = $randLogo
    DefaultImg = $defaultImg
    Host       = $ffHost
    Redraw     = $redrawOnResize
    Grid       = @(0, 0)
    Rows       = 0
    # The last command in history when the fetch was drawn: a newer one means
    # the fetch has scrolled into the past and a resize must not redraw it.
    HistoryId  = [int](@(Get-History -Count 1) | ForEach-Object { $_.Id } | Select-Object -First 1)
    Pending    = $null
    Deferred   = $false
    # Carried in the plan rather than read from the script scope inside
    # Show-FFSFetch: the repair runs from the prompt, long after this script's
    # own scope is gone, and a script-scoped path would be empty by then.
    SixelsDir  = Join-Path $ffRoot 'sixels'
    ArtsDir    = Join-Path $ffRoot 'arts'
    GuiDir     = Join-Path $ffRoot 'gui'
}

@@RESIZEGUARD@@

# Draw it, then keep it straight: a resize that comes later is noticed by the
# prompt, which redraws this same fetch at the new size.
#
# A shell that has only just started does not have its real window size yet
# (see Wait-FFSGridSettled), so the first draw waits for it. Waiting here, in
# the profile, made Windows PowerShell print "Loading personal and system
# profiles took 687ms" under every fetch, so a fresh interactive shell hands the
# draw to its first prompt instead - which is the very next thing it does.
# Anything else (a script, -Command, a shell that has been up a while) draws
# right away.
function Test-FFSDeferFirstDraw {
    if ([int](Get-FFSGrid)[0] -le 0) { return $false }
    try {
        $age = ((Get-Date) - (Get-Process -Id $PID).StartTime).TotalMilliseconds
        if ($age -ge 1500) { return $false }
    } catch { return $false }
    $argv = @([Environment]::GetCommandLineArgs() | Select-Object -Skip 1)
    $noExit = @($argv | Where-Object { $_ -match '^[-/]noe' }).Count -gt 0
    $batch  = @($argv | Where-Object { $_ -match '^[-/](c|command|f|file|e|encodedcommand|noni|noninteractive)' }).Count -gt 0
    return ($noExit -or -not $batch)
}
if (Test-FFSDeferFirstDraw) {
    $global:FFSPlan.Deferred = $true
    Install-FFSResizeGuard
} else {
    Wait-FFSGridSettled
    Show-FFSFetch -Plan $global:FFSPlan
    if ($redrawOnResize) { Install-FFSResizeGuard }
}

# Remember the day. Without this the 'daily' check above could never match, so
# "Once per day (same look all day)" silently behaved like "every window".
if ($freq -eq 'daily') {
    try {
        $guiDir = Join-Path $ffRoot 'gui'
        if (-not (Test-Path -LiteralPath $guiDir)) {
            New-Item -ItemType Directory -Path $guiDir -Force | Out-Null
        }
        Set-Content -LiteralPath $flagPath -Value $stamp -Encoding ASCII -ErrorAction Stop
    } catch {}
}
# <<< FastFetch Studio :: launcher <<<
"""


# cmd.exe has no profile script. Its per-user AutoRun value
# (HKCU\Software\Microsoft\Command Processor) is the closest thing, and it runs
# at the start of *every* cmd.exe - including each "cmd /c ..." that scripts,
# git hooks and build tools start in the background. A fetch printed into those
# would land in their captured output, so only an interactive window draws.
#
# Every line carries its own "@" instead of one "@echo off": AutoRun runs in
# the interactive shell itself, so "echo off" would stay off and hide the
# prompt for the rest of the session.
CMD_HOOK_TEMPLATE = r"""@rem >>> FastFetch Studio :: cmd hook >>>
@rem Generated by FastFetch Studio; rewritten by "Apply & Generate".
@rem Registered as cmd.exe's AutoRun - see "Show it in Command Prompt too" in
@rem the README. Draws the same fetch as PowerShell, once, in an interactive
@rem window only.
@setlocal EnableDelayedExpansion
@if defined FASTFETCH_STUDIO_DISABLE goto :ffs_done
@rem "cmd /c <command>" runs one command for a script or a tool and exits.
@rem The quotes come out first: CMDCMDLINE starts with the quoted path to
@rem cmd.exe, and quotes inside the strings break the comparison below.
@set "FFS_CL=!CMDCMDLINE:"=!"
@if /i not "!FFS_CL:/c=!"=="!FFS_CL!" goto :ffs_done
@set "FFS_LAUNCHER=%USERPROFILE%\.config\fastfetch\fastfetch-random.ps1"
@if not exist "!FFS_LAUNCHER!" goto :ffs_done
@powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -File "!FFS_LAUNCHER!"
:ffs_done
@endlocal
@rem <<< FastFetch Studio :: cmd hook <<<
"""


def generate_launcher(st: dict) -> None:
    # Ensure the folder exists: the Random tab regenerates the launcher on every
    # toggle, which can be the first thing that ever writes into ~\.config\fastfetch.
    LAUNCHER_PATH.parent.mkdir(parents=True, exist_ok=True)
    text = LAUNCHER_TEMPLATE.replace("@@W@@", str(int(st.get("logWidth", 28)))) \
                            .replace("@@H@@", str(int(st.get("logHeight", 24)))) \
                            .replace("@@CELLW@@", str(logo_cell_px(st)[0])) \
                            .replace("@@CELLH@@", str(logo_cell_px(st)[1])) \
                            .replace("@@BOX@@", str(frame_width(st))) \
                            .replace("@@LOGOGAP@@", str(LOGO_GAP)) \
                            .replace("@@REDRAW@@", redraw_flag(st)) \
                            .replace("@@RESIZEGUARD@@", RESIZE_GUARD_TEMPLATE)
    LAUNCHER_PATH.write_text(text, "utf-8", newline="\n")
    # CRLF: cmd.exe misreads labels and goto targets in an LF-only batch file.
    CMD_HOOK_PATH.write_text(CMD_HOOK_TEMPLATE, "ascii", newline="\r\n")


def redraw_flag(st: dict) -> str:
    """'$true' / '$false' for the generated launcher's resize guard.

    A bare `True` in PowerShell is parsed as a command name, not a boolean, so
    the dollar sign is not optional here - without it the launcher's default
    would be $null and the guard would never install.
    """
    return "$true" if st.get("redrawOnResize", True) else "$false"


def profile_snippet() -> str:
    """The block users paste into $PROFILE.

    Deliberately guarded rather than a bare `& <path>`: the launcher is a
    generated file, so pasting a hard call into a profile on a machine where it
    has not been written yet produced
    "The term '...fastfetch-random.ps1' is not recognized as the name of a
    cmdlet, function, script file, or operable program." on every shell start.
    """
    return ('$ffLauncher = "$env:USERPROFILE\\.config\\fastfetch\\fastfetch-random.ps1"\r\n'
            'if (Test-Path -LiteralPath $ffLauncher) { & $ffLauncher } else { fastfetch.exe }')


def write_config(st: dict) -> Path:
    """Write the base palette to config.jsonc - fastfetch's own user config.

    The launcher passes --config <theme>, so this file is what a bare
    `fastfetch` picks up (and what any run falls back to if a theme file is
    missing). Without it fastfetch silently renders whatever else it finds
    earlier in its search path, which is how a config with a different module
    list and only some of the icons ended up on screen.
    """
    base = palettes_for(st["groups"])[0]
    CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    CONFIG_PATH.write_text(
        dump_jsonc(build_config_json({**st, "groups": base}, base["accent"])), "utf-8")
    return CONFIG_PATH


def apply_all(st: dict) -> str:
    """Backup config, encode sixels, write config + themes + launcher, persist state."""
    GUI_DIR.mkdir(parents=True, exist_ok=True)
    if CONFIG_PATH.exists() and not BACKUP_PATH.exists():
        shutil.copyfile(CONFIG_PATH, BACKUP_PATH)
    write_config(st)
    # The frame must be at least as wide as the widest row this machine renders,
    # and only fastfetch knows that. Measure against the config just written,
    # then rewrite it if the frame has to change - one extra write on the run
    # that changes it, and none on the runs after that.
    fitted = fitted_box_width(st)
    if fitted != box_width(st):
        st["boxWidth"] = fitted
        write_config(st)
    sixels = ensure_sixels(st)
    arts = ensure_arts(st)
    themes = generate_theme_files(st)
    generate_launcher(st)
    save_state(st)
    return (f"config + {len(themes)} themes + {len(sixels)} sixels + "
            f"{len(arts)} art + launcher written")


