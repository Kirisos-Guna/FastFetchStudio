<#
.SYNOPSIS
    One-shot setup for fastfetch on Windows: download, install, and hook it into
    every PowerShell profile so it runs automatically in each new terminal.

.DESCRIPTION
    What this script does, in order:

      1. Detects the machine (CPU architecture, Windows PowerShell 5.1,
         PowerShell 7+, Windows Terminal) and reports what it found.
      2. Downloads the latest official fastfetch release for your architecture
         and extracts it to %USERPROFILE%\.local\bin. If fastfetch is already
         installed and working, the download is skipped.
      3. Adds that folder to your *user* PATH (idempotent - never duplicated),
         so `fastfetch` resolves in any new terminal.
      4. Writes a small, marker-delimited block into your PowerShell profile(s).
         The block calls FastFetch Studio's fastfetch-random.ps1 when it exists
         (random logo + theme per window), and falls back to a plain `fastfetch`
         run otherwise.

    Safety properties:

      * Never needs Administrator. Everything is user-scoped.
      * Idempotent: running it twice changes nothing the second time.
      * Never duplicates an existing fastfetch call. If your profile already
        invokes fastfetch (from any tool, with or without our markers), the
        script reports it and leaves it alone.
      * Your profile is backed up before it is modified, and only when it is
        actually about to change.
      * Nothing is deleted except the files this script itself installed, and
        only when you explicitly pass -Uninstall -RemoveBinary.

.PARAMETER InstallDir
    Where fastfetch.exe is installed. Default: %USERPROFILE%\.local\bin

.PARAMETER FastfetchVersion
    Release tag to install, e.g. '2.68.1'. Default 'latest'.

.PARAMETER ProfilePath
    Advanced: write the hook into this specific file instead of the
    auto-detected profile(s). Useful for custom profile locations.

.PARAMETER Force
    Reinstall the binary even if a working copy exists, and add the managed
    block even when an unrelated fastfetch invocation was found in the profile.

.PARAMETER SkipDownload
    Do not touch the binary. Only update PATH and the profile hook.

.PARAMETER SkipPath
    Do not modify your user PATH. Use this if you manage PATH yourself.

.PARAMETER SkipProfile
    Do not touch any PowerShell profile. Only install the binary and PATH.

.PARAMETER DryRun
    Print every action without changing anything on disk.

.PARAMETER Uninstall
    Remove the managed profile block. Add -RemoveBinary to also delete the
    fastfetch files this script installed.

.EXAMPLE
    .\setup.ps1

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File .\setup.ps1

.EXAMPLE
    .\setup.ps1 -DryRun

.EXAMPLE
    .\setup.ps1 -Uninstall
#>

param(
    [string]   $InstallDir,
    [string]   $FastfetchVersion = 'latest',
    [string[]] $ProfilePath,
    [switch]   $Force,
    [switch]   $SkipDownload,
    [switch]   $SkipPath,
    [switch]   $SkipProfile,
    [switch]   $DryRun,
    [switch]   $Uninstall,
    [switch]   $RemoveBinary
)

# Everything is user-scoped, so a failure in one step must be reported clearly
# rather than swallowed. Individual risky operations opt back into SilentlyContinue.
$ErrorActionPreference = 'Stop'
$ProgressPreference    = 'SilentlyContinue'   # the 5.1 progress bar makes downloads crawl

$MarkerBegin = '# >>> FastFetch Studio :: fastfetch on shell start >>>'
$MarkerEnd   = '# <<< FastFetch Studio :: fastfetch on shell start <<<'
$RepoSlug    = 'fastfetch-cli/fastfetch'

if (-not $InstallDir) { $InstallDir = Join-Path $env:USERPROFILE '.local\bin' }
$InstallDir = $InstallDir.TrimEnd('\')

# Guard the path-shaped parameters. A mis-typed invocation would otherwise
# surface as a confusing 404 from GitHub instead of a clear message.
if (-not [IO.Path]::IsPathRooted($InstallDir)) {
    throw "-InstallDir must be an absolute path (got '$InstallDir')."
}
if ($FastfetchVersion -ne 'latest' -and $FastfetchVersion -notmatch '^\d+(\.\d+)*$') {
    throw "-FastfetchVersion must be 'latest' or a release tag such as '2.68.1' (got '$FastfetchVersion')."
}
foreach ($candidate in $ProfilePath) {
    if (-not [IO.Path]::IsPathRooted($candidate)) {
        throw "-ProfilePath must be an absolute path (got '$candidate')."
    }
}

# The exact block written into a profile. Single-quoted here-string: nothing
# expands here, the variables belong to the profile at run time.
$ProfileBlock = @'
# >>> FastFetch Studio :: fastfetch on shell start >>>
if (-not $env:FASTFETCH_STUDIO_DISABLE) {
    & {
        $ffLauncher = Join-Path $env:USERPROFILE '.config\fastfetch\fastfetch-random.ps1'
        if (Test-Path -LiteralPath $ffLauncher) {
            & $ffLauncher
        } elseif (Get-Command fastfetch.exe -ErrorAction SilentlyContinue) {
            fastfetch.exe
        }
    }
}
# <<< FastFetch Studio :: fastfetch on shell start <<<
'@

# The files that make up a fastfetch release. Used only by -Uninstall -RemoveBinary
# so we never delete anything we did not put there (e.g. uv.exe in the same folder).
$InstalledFiles = @('fastfetch.exe', 'flashfetch.exe', 'libqjs-0.dll', 'lua55.dll', 'LICENSE', 'presets')


# --------------------------------------------------------------------------- output

function Say      { param([string]$m, [ConsoleColor]$Color = [ConsoleColor]::Gray) Write-Host $m -ForegroundColor $Color }
function Say-Head { param([string]$m) Write-Host ''; Write-Host "== $m" -ForegroundColor Cyan }
function Say-Ok   { param([string]$m) Write-Host "   [ok]   $m" -ForegroundColor Green }
function Say-Skip { param([string]$m) Write-Host "   [skip] $m" -ForegroundColor DarkGray }
function Say-Warn { param([string]$m) Write-Host "   [warn] $m" -ForegroundColor Yellow }
function Say-Info { param([string]$m) Write-Host "          $m" -ForegroundColor DarkGray }

$script:Warnings = New-Object System.Collections.ArrayList
function Add-Warning { param([string]$m) [void]$script:Warnings.Add($m); Say-Warn $m }


# --------------------------------------------------------------------------- detection

function Get-Architecture {
    # A 32-bit PowerShell host on a 64-bit OS reports x86 in PROCESSOR_ARCHITECTURE
    # and the real value in PROCESSOR_ARCHITEW6432.
    $arch = $env:PROCESSOR_ARCHITECTURE
    if ($env:PROCESSOR_ARCHITEW6432) { $arch = $env:PROCESSOR_ARCHITEW6432 }
    switch ($arch) {
        'AMD64' { return 'amd64' }
        'ARM64' { return 'aarch64' }
        'x86'   { return 'x86' }
        default { return 'amd64' }
    }
}

function Get-ShellInfo {
    $docs = [Environment]::GetFolderPath('MyDocuments')
    $result = New-Object System.Collections.ArrayList

    $ps51Exe = Join-Path $env:SystemRoot 'System32\WindowsPowerShell\v1.0\powershell.exe'
    [void]$result.Add([pscustomobject]@{
        Name      = 'Windows PowerShell 5.1'
        Exe       = $ps51Exe
        Profile   = (Join-Path $docs 'WindowsPowerShell\Microsoft.PowerShell_profile.ps1')
        Installed = (Test-Path -LiteralPath $ps51Exe)
    })

    $pwshExe = $null
    $cmd = Get-Command pwsh.exe -ErrorAction SilentlyContinue
    if ($cmd) { $pwshExe = $cmd.Source }
    if (-not $pwshExe) {
        foreach ($candidate in @("$env:ProgramFiles\PowerShell\7\pwsh.exe",
                                 "$env:LOCALAPPDATA\Microsoft\WindowsApps\pwsh.exe")) {
            if (Test-Path -LiteralPath $candidate) { $pwshExe = $candidate; break }
        }
    }
    [void]$result.Add([pscustomobject]@{
        Name      = 'PowerShell 7+'
        Exe       = $pwshExe
        Profile   = (Join-Path $docs 'PowerShell\Microsoft.PowerShell_profile.ps1')
        Installed = [bool]$pwshExe
    })

    return $result
}

function Get-WindowsTerminalInfo {
    $exe = $null
    $cmd = Get-Command wt.exe -ErrorAction SilentlyContinue
    if ($cmd) { $exe = $cmd.Source }

    $settings = $null
    foreach ($candidate in @(
            (Join-Path $env:LOCALAPPDATA 'Packages\Microsoft.WindowsTerminal_8wekyb3d8bbwe\LocalState\settings.json'),
            (Join-Path $env:LOCALAPPDATA 'Packages\Microsoft.WindowsTerminalPreview_8wekyb3d8bbwe\LocalState\settings.json'),
            (Join-Path $env:LOCALAPPDATA 'Microsoft\Windows Terminal\settings.json'))) {
        if (Test-Path -LiteralPath $candidate) { $settings = $candidate; break }
    }

    $suppresses = $false
    if ($settings) {
        $raw = Get-Content -LiteralPath $settings -Raw -ErrorAction SilentlyContinue
        if ($raw -and $raw -match '-NoProfile') { $suppresses = $true }
    }

    return [pscustomobject]@{
        Installed         = [bool]$exe
        Exe               = $exe
        Settings          = $settings
        SuppressesProfile = $suppresses
    }
}


# --------------------------------------------------------------------------- profile

function Backup-File {
    param([string]$Path)
    $stamp = Get-Date -Format 'yyyyMMdd-HHmmss'
    $backup = "$Path.bak-$stamp"
    Copy-Item -LiteralPath $Path -Destination $backup -Force
    return $backup
}

function Update-ProfileHook {
    <#
        Adds / refreshes the managed block in one profile file.
        Returns a status string:
          created            - profile did not exist, wrote a new one
          appended           - block added to an existing profile
          replaced           - block was there but stale (or duplicated), rewrote it
          unchanged          - block already there and identical, nothing written
          blocked-existing   - an unmanaged fastfetch call was found, left alone

        -Predict answers "what would happen" without writing anything, which is
        how both -DryRun and the backup-only-when-needed logic work.
    #>
    param(
        [string]$Path,
        [switch]$ForceWrite,
        [switch]$Predict
    )

    $noWrite = ($DryRun -or $Predict)
    $desired = @($ProfileBlock -split "\r?\n")

    # --- read
    $exists  = Test-Path -LiteralPath $Path
    $hasBom  = $false
    $lines   = @()
    $eol     = "`r`n"

    if ($exists) {
        $bytes = [IO.File]::ReadAllBytes($Path)
        if ($bytes.Length -ge 2 -and $bytes[0] -eq 0xFF -and $bytes[1] -eq 0xFE) {
            throw "Profile is UTF-16 encoded, which this script will not rewrite: $Path  (convert it to UTF-8 and re-run)"
        }
        if ($bytes.Length -ge 3 -and $bytes[0] -eq 0xEF -and $bytes[1] -eq 0xBB -and $bytes[2] -eq 0xBF) { $hasBom = $true }

        $text = [IO.File]::ReadAllText($Path)
        if ($text -match "`r`n") { $eol = "`r`n" } else { $eol = "`n" }
        $lines = @($text -split "\r?\n")
    }

    # --- locate every existing managed block
    $spans = New-Object System.Collections.ArrayList
    $i = 0
    while ($i -lt $lines.Count) {
        if ($lines[$i].Trim() -eq $MarkerBegin) {
            $end = -1
            for ($j = $i; $j -lt $lines.Count; $j++) {
                if ($lines[$j].Trim() -eq $MarkerEnd) { $end = $j; break }
            }
            if ($end -ge 0) {
                [void]$spans.Add([pscustomobject]@{ Start = $i; End = $end })
                $i = $end + 1
                continue
            }
        }
        $i++
    }

    if ($spans.Count -gt 0) {
        $firstStart   = $spans[0].Start
        $currentBlock = @($lines[$spans[0].Start..$spans[0].End])

        $same = ($currentBlock.Count -eq $desired.Count)
        if ($same) {
            for ($k = 0; $k -lt $desired.Count; $k++) {
                if ($currentBlock[$k].TrimEnd() -ne $desired[$k].TrimEnd()) { $same = $false; break }
            }
        }
        if ($same -and $spans.Count -eq 1) { return 'unchanged' }

        # Self-healing: drop every managed block, then put exactly one back where
        # the first one was. Handles stale content and accidental duplicates.
        $keep = New-Object System.Collections.ArrayList
        for ($k = 0; $k -lt $lines.Count; $k++) {
            $inside = $false
            foreach ($s in $spans) { if ($k -ge $s.Start -and $k -le $s.End) { $inside = $true; break } }
            if (-not $inside) { [void]$keep.Add($lines[$k]) }
        }

        $out = New-Object System.Collections.ArrayList
        $limit = [Math]::Min($firstStart, $keep.Count)
        for ($k = 0; $k -lt $limit; $k++) { [void]$out.Add($keep[$k]) }
        foreach ($l in $desired) { [void]$out.Add($l) }
        for ($k = $firstStart; $k -lt $keep.Count; $k++) { [void]$out.Add($keep[$k]) }

        if (-not $noWrite) {
            $enc = New-Object System.Text.UTF8Encoding($hasBom)
            [IO.File]::WriteAllText($Path, (($out -join $eol).TrimEnd("`r", "`n") + $eol), $enc)
        }
        return 'replaced'
    }

    # --- no managed block: is fastfetch invoked by hand or by another tool?
    $foreign = New-Object System.Collections.ArrayList
    for ($k = 0; $k -lt $lines.Count; $k++) {
        $t = $lines[$k].Trim()
        if ($t -eq '' -or $t.StartsWith('#')) { continue }
        if ($t -match '(?i)\bfastfetch\b') { [void]$foreign.Add(($k + 1)) }
    }

    if ($foreign.Count -gt 0 -and -not $ForceWrite) {
        $script:ForeignLines = @($foreign)
        return 'blocked-existing'
    }

    # --- append
    $out = New-Object System.Collections.ArrayList
    foreach ($l in $lines) { [void]$out.Add($l) }
    while ($out.Count -gt 0 -and $out[$out.Count - 1].Trim() -eq '') { $out.RemoveAt($out.Count - 1) }
    if ($out.Count -gt 0) { [void]$out.Add('') }
    foreach ($l in $desired) { [void]$out.Add($l) }

    if (-not $noWrite) {
        if (-not $exists) {
            $dir = Split-Path -Path $Path -Parent
            if ($dir -and -not (Test-Path -LiteralPath $dir)) { New-Item -ItemType Directory -Path $dir -Force | Out-Null }
        }
        $enc = New-Object System.Text.UTF8Encoding($true)   # BOM keeps PS 5.1 happy
        [IO.File]::WriteAllText($Path, (($out -join $eol).TrimEnd("`r", "`n") + $eol), $enc)
    }
    if ($exists) { return 'appended' }
    return 'created'
}

function Remove-ProfileHook {
    param([string]$Path)

    if (-not (Test-Path -LiteralPath $Path)) { return 'absent' }
    $text  = [IO.File]::ReadAllText($Path)
    $eol   = "`n"
    if ($text -match "`r`n") { $eol = "`r`n" }
    $lines = @($text -split "\r?\n")

    $keep    = New-Object System.Collections.ArrayList
    $inside  = $false
    $removed = 0
    foreach ($l in $lines) {
        if ($l.Trim() -eq $MarkerBegin) { $inside = $true; $removed++; continue }
        if ($l.Trim() -eq $MarkerEnd)   { $inside = $false; continue }
        if (-not $inside) { [void]$keep.Add($l) }
    }
    if ($removed -eq 0) { return 'absent' }

    while ($keep.Count -gt 0 -and $keep[$keep.Count - 1].Trim() -eq '') { $keep.RemoveAt($keep.Count - 1) }

    if (-not $DryRun) {
        $bytes  = [IO.File]::ReadAllBytes($Path)
        $hasBom = ($bytes.Length -ge 3 -and $bytes[0] -eq 0xEF -and $bytes[1] -eq 0xBB -and $bytes[2] -eq 0xBF)
        $enc    = New-Object System.Text.UTF8Encoding($hasBom)
        $body   = ''
        if ($keep.Count -gt 0) { $body = ($keep -join $eol).TrimEnd("`r", "`n") + $eol }
        [IO.File]::WriteAllText($Path, $body, $enc)
    }
    return 'removed'
}


# --------------------------------------------------------------------------- PATH

function Update-UserPath {
    param([string]$Dir)

    $raw = [Environment]::GetEnvironmentVariable('Path', 'User')
    if ($null -eq $raw) { $raw = '' }
    $entries = @($raw -split ';' | Where-Object { $_ -ne '' })

    foreach ($e in $entries) {
        $expanded = ([Environment]::ExpandEnvironmentVariables($e)).TrimEnd('\')
        if ($expanded -eq $Dir) { return 'present' }
    }

    $new = (@($entries) + $Dir) -join ';'
    if (-not $DryRun) {
        [Environment]::SetEnvironmentVariable('Path', $new, 'User')
        # Make it usable in this session too, so the verification step below works.
        $inSession = @($env:Path -split ';' | Where-Object { $_ -ne '' } |
                       Where-Object { ([Environment]::ExpandEnvironmentVariables($_)).TrimEnd('\') -eq $Dir })
        if ($inSession.Count -eq 0) { $env:Path = $env:Path.TrimEnd(';') + ';' + $Dir }
    }
    if ($new.Length -gt 2047) {
        Add-Warning "Your user PATH is now $($new.Length) characters. Very long PATHs can be truncated by some tools."
    }
    return 'added'
}


# --------------------------------------------------------------------------- install

function Get-InstalledFastfetchVersion {
    <#
        Runs `fastfetch --version` and returns the first output line, or $null.

        This deliberately uses System.Diagnostics.Process instead of `& $exe`:
        PowerShell only treats a file as runnable when its extension is listed in
        PATHEXT, and a machine with a trimmed PATHEXT would otherwise make an
        already-installed fastfetch look missing - which would re-download it on
        every run and break idempotency. Going through .NET also lets us read
        stdout and the exit code without depending on stderr redirection.
    #>
    param([string]$Dir)

    $exe = Join-Path $Dir 'fastfetch.exe'
    if (-not (Test-Path -LiteralPath $exe)) { return $null }

    $proc = $null
    try {
        $psi = New-Object System.Diagnostics.ProcessStartInfo
        $psi.FileName               = $exe
        $psi.Arguments              = '--version'
        $psi.UseShellExecute        = $false
        $psi.RedirectStandardOutput = $true
        $psi.RedirectStandardError  = $true
        $psi.CreateNoWindow         = $true
        $psi.WorkingDirectory       = $Dir

        $proc = [System.Diagnostics.Process]::Start($psi)
        # Read both pipes before waiting: a full stderr buffer would deadlock.
        $stdout = $proc.StandardOutput.ReadToEnd()
        $stderr = $proc.StandardError.ReadToEnd()

        if (-not $proc.WaitForExit(15000)) { return $null }
        if ($proc.ExitCode -ne 0) { return $null }

        $first = $stdout -split "\r?\n" | Where-Object { $_.Trim() -ne '' } | Select-Object -First 1
        if (-not $first) { return $null }
        return "$first".Trim()
    } catch {
        return $null
    } finally {
        if ($proc -and -not $proc.HasExited) { try { $proc.Kill() } catch { } }
        if ($proc) { $proc.Dispose() }
    }
}

function Install-Fastfetch {
    param([string]$Dir, [string]$Version, [string]$Arch)

    $zipName = "fastfetch-windows-$Arch.zip"
    if ($Version -eq 'latest') {
        $url   = "https://github.com/$RepoSlug/releases/latest/download/$zipName"
        $label = 'latest release'
    } else {
        $url   = "https://github.com/$RepoSlug/releases/download/$Version/$zipName"
        $label = $Version
    }

    $tmpZip = Join-Path $env:TEMP ("fastfetch-setup-" + [Guid]::NewGuid().ToString('N') + ".zip")
    try {
        Say-Info "GET $url"
        # -UseBasicParsing avoids the IE engine on Windows PowerShell 5.1; it is
        # meaningless (and may be removed) on 7+, so only pass it when it applies.
        $iwr = @{
            Uri     = $url
            OutFile = $tmpZip
            Headers = @{ 'User-Agent' = 'FastFetchStudio-Setup' }
        }
        if ($PSVersionTable.PSVersion.Major -lt 6) { $iwr['UseBasicParsing'] = $true }
        Invoke-WebRequest @iwr

        if (-not (Test-Path -LiteralPath $tmpZip)) { throw 'download produced no file' }
        $size = (Get-Item -LiteralPath $tmpZip).Length
        if ($size -lt 1MB) {
            throw "downloaded file is only $size bytes - the release asset name probably changed (expected $zipName)"
        }
        Say-Ok "downloaded $label ($([math]::Round($size / 1MB, 1)) MB)"

        if (-not (Test-Path -LiteralPath $Dir)) { New-Item -ItemType Directory -Path $Dir -Force | Out-Null }
        Expand-Archive -LiteralPath $tmpZip -DestinationPath $Dir -Force
        Say-Ok "extracted to $Dir"
    } finally {
        Remove-Item -LiteralPath $tmpZip -Force -ErrorAction SilentlyContinue
    }

    if (-not (Test-Path -LiteralPath (Join-Path $Dir 'fastfetch.exe'))) {
        throw "fastfetch.exe was not found in $Dir after extraction"
    }
}

function Remove-InstalledFastfetch {
    param([string]$Dir)

    $removed = 0
    foreach ($name in $InstalledFiles) {
        $target = Join-Path $Dir $name
        if (Test-Path -LiteralPath $target) {
            if (-not $DryRun) { Remove-Item -LiteralPath $target -Recurse -Force -ErrorAction SilentlyContinue }
            Say-Skip "removed $target"
            $removed++
        }
    }
    # Only remove the folder when it is empty - it often holds unrelated tools.
    if ((Test-Path -LiteralPath $Dir) -and -not (Get-ChildItem -LiteralPath $Dir -Force -ErrorAction SilentlyContinue)) {
        if (-not $DryRun) { Remove-Item -LiteralPath $Dir -Force -ErrorAction SilentlyContinue }
        Say-Skip "removed empty folder $Dir"
    }
    return $removed
}


# --------------------------------------------------------------------------- main

Say ''
Say '  FastFetch Studio - fastfetch setup for Windows' -Color White
Say '  ------------------------------------------------------------'
if ($DryRun) { Say '  DRY RUN - nothing will be written' -Color Yellow }
Say ''

$arch    = Get-Architecture
$shells  = Get-ShellInfo
$wt      = Get-WindowsTerminalInfo

$isAdmin = $false
try {
    $identity = [Security.Principal.WindowsIdentity]::GetCurrent()
    $isAdmin  = (New-Object Security.Principal.WindowsPrincipal($identity)).IsInRole(
        [Security.Principal.WindowsBuiltInRole]::Administrator)
} catch { }

$processBits = '32-bit'
if ([Environment]::Is64BitProcess) { $processBits = '64-bit' }

Say-Head 'Environment'
Say-Info "PowerShell host : $($PSVersionTable.PSVersion)  ($processBits process)"
Say-Info "Architecture    : $arch"
Say-Info "Install folder  : $InstallDir"
Say-Info "Elevated        : $isAdmin  (not required - everything is user-scoped)"
foreach ($s in $shells) {
    if ($s.Installed) { Say-Ok "$($s.Name) detected" } else { Say-Skip "$($s.Name) not installed" }
}
if ($wt.Installed) { Say-Ok 'Windows Terminal detected - sixel logos will render' }
else { Say-Skip 'Windows Terminal not detected - fastfetch will use its built-in logo' }

if ($arch -eq 'x86') {
    Add-Warning 'A 32-bit Windows install was detected. fastfetch ships no 32-bit Windows build, so the amd64 package will not run here.'
}


# ---------------------------------------------------------------- uninstall branch
if ($Uninstall) {
    Say-Head 'Uninstall'

    $targets = $ProfilePath
    if (-not $targets) {
        $targets = @($shells | Where-Object { $_.Installed } | ForEach-Object { $_.Profile })
    }
    foreach ($p in $targets) {
        $status = Remove-ProfileHook -Path $p
        if ($status -eq 'removed') { Say-Ok "hook removed from $p" } else { Say-Skip "no hook in $p" }
    }

    if ($RemoveBinary) {
        $n = Remove-InstalledFastfetch -Dir $InstallDir
        if ($n -eq 0) { Say-Skip 'no fastfetch files found to remove' }
    } else {
        Say-Info "Binary left in place. Add -RemoveBinary to delete it, or remove $InstallDir by hand."
    }

    Say ''
    Say '  Uninstall complete. Open a new terminal for it to take effect.' -Color White
    Say ''
    return
}


# ---------------------------------------------------------------- 1. binary
Say-Head 'Step 1/3 - fastfetch binary'

$installed = Get-InstalledFastfetchVersion -Dir $InstallDir

if ($SkipDownload) {
    if ($installed) { Say-Skip "download skipped; found $installed" }
    else { Say-Warn "download skipped and no working fastfetch.exe in $InstallDir" }
} elseif ($installed -and -not $Force) {
    Say-Ok "already installed: $installed"
    Say-Info 'Use -Force to reinstall or upgrade.'
} else {
    if ($installed) { Say-Info "replacing existing install: $installed" }
    if ($DryRun) {
        Say-Skip "would download fastfetch-windows-$arch.zip ($FastfetchVersion) into $InstallDir"
    } else {
        Install-Fastfetch -Dir $InstallDir -Version $FastfetchVersion -Arch $arch
        $installed = Get-InstalledFastfetchVersion -Dir $InstallDir
        if ($installed) { Say-Ok "verified: $installed" }
        else { throw "fastfetch.exe was installed to $InstallDir but does not run. Check the architecture ($arch)." }
    }
}


# ---------------------------------------------------------------- 2. PATH
Say-Head 'Step 2/3 - PATH'

if ($SkipPath) {
    Say-Skip 'PATH update skipped (-SkipPath)'
} elseif ($DryRun) {
    Say-Skip "would ensure $InstallDir is on the user PATH"
} else {
    $pathStatus = Update-UserPath -Dir $InstallDir
    if ($pathStatus -eq 'present') { Say-Ok "$InstallDir is already on the user PATH" }
    else { Say-Ok "added $InstallDir to the user PATH" }
}


# ---------------------------------------------------------------- 3. profile
Say-Head 'Step 3/3 - PowerShell profile hook'

if ($SkipProfile) {
    Say-Skip 'profile update skipped (-SkipProfile)'
} else {
    if (-not $ProfilePath) {
        foreach ($s in $shells) {
            if (-not $s.Installed) { Say-Skip "$($s.Name) not installed - re-run this script after installing it" }
        }
        $targets = @($shells | Where-Object { $_.Installed } | ForEach-Object { $_.Profile })
    } else {
        $targets = $ProfilePath
    }

    foreach ($p in $targets) {
        $script:ForeignLines = @()

        # Ask first, write second: this is what keeps repeat runs free of
        # pointless backups and pointless rewrites.
        $predicted = Update-ProfileHook -Path $p -ForceWrite:$Force -Predict

        if ($predicted -eq 'unchanged') {
            Say-Ok "already configured, nothing to do: $p"
            continue
        }
        if ($predicted -eq 'blocked-existing') {
            Say-Warn "fastfetch is already invoked in $p (line $($script:ForeignLines -join ', ')) - left untouched"
            Say-Info 'That existing call already runs fastfetch, so adding ours would run it twice.'
            Say-Info 'Remove that line and re-run, or pass -Force to add the managed block anyway.'
            continue
        }
        if ($DryRun) {
            Say-Skip "would write the hook into $p ($predicted)"
            continue
        }

        if (Test-Path -LiteralPath $p) {
            $backup = Backup-File -Path $p
            Say-Info "backed up to $(Split-Path $backup -Leaf)"
        }

        $status = Update-ProfileHook -Path $p -ForceWrite:$Force
        switch ($status) {
            'created'  { Say-Ok "created profile with the hook: $p" }
            'appended' { Say-Ok "hook added: $p" }
            'replaced' { Say-Ok "hook refreshed: $p" }
            default    { Say-Ok "$status : $p" }
        }
    }
}


# ---------------------------------------------------------------- guardrails
$policy = Get-ExecutionPolicy -Scope CurrentUser
if ($policy -eq 'Restricted' -or $policy -eq 'AllSigned') {
    Add-Warning "PowerShell execution policy for your user is '$policy' - profiles may not run."
    Say-Info 'Fix with:  Set-ExecutionPolicy -Scope CurrentUser RemoteSigned'
}
if ($wt.SuppressesProfile) {
    Add-Warning 'Your Windows Terminal settings.json contains -NoProfile, which skips the profile and this hook.'
}


# ---------------------------------------------------------------- summary
Say-Head 'Done'

if ($DryRun) {
    Say '  Dry run finished - nothing was changed.' -Color White
} else {
    Say '  Open a NEW terminal window and fastfetch should appear.' -Color White
}
Say ''
Say-Info "Binary   : $InstallDir\fastfetch.exe"
if ($installed) { Say-Info "Version  : $installed" }
Say-Info 'Undo     : .\setup.ps1 -Uninstall'
Say-Info 'Skip once: set FASTFETCH_STUDIO_DISABLE=1 in the environment before the shell starts'
Say-Info 'Random logos/themes: run FastFetch Studio (FastFetchStudio.exe)'

if ($script:Warnings.Count -gt 0) {
    Say ''
    Say "  $($script:Warnings.Count) warning(s):" -Color Yellow
    foreach ($w in $script:Warnings) { Say "   - $w" -Color Yellow }
}
Say ''
