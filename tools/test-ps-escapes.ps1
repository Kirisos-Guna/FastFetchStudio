[CmdletBinding()]
param(
    [string]$FastFetchRoot = (Join-Path $env:USERPROFILE '.config\fastfetch'),
    [switch]$RequirePowerShell7
)

$ErrorActionPreference = 'Stop'

$launcher = Join-Path $FastFetchRoot 'fastfetch-random.ps1'
$preview = Join-Path $FastFetchRoot 'gui\preview.ps1'
$measure = Join-Path $FastFetchRoot 'gui\measure-cells.ps1'
foreach ($file in $launcher, $preview, $measure) {
    if (-not (Test-Path -LiteralPath $file -PathType Leaf)) {
        throw "generated script is missing: $file"
    }
}

$engines = @(
    [pscustomobject]@{
        Name = 'Windows PowerShell 5.1'
        Path = Join-Path $env:SystemRoot 'System32\WindowsPowerShell\v1.0\powershell.exe'
    }
)
if (-not (Test-Path -LiteralPath $engines[0].Path -PathType Leaf)) {
    throw "Windows PowerShell 5.1 is missing: $($engines[0].Path)"
}

$pwsh = Get-Command pwsh.exe -ErrorAction SilentlyContinue | Select-Object -First 1
if ($pwsh) {
    $engines += [pscustomobject]@{ Name = 'PowerShell 7'; Path = $pwsh.Source }
} elseif ($RequirePowerShell7) {
    throw 'PowerShell 7 is required for this regression test but pwsh.exe was not found.'
} else {
    Write-Host 'PowerShell 7 not found locally; its matching CI run is required.'
}

$probeDir = Join-Path $env:TEMP 'ffs-escape-compat'
New-Item -ItemType Directory -Path $probeDir -Force | Out-Null
$repairProbe = Join-Path $probeDir 'repair.ps1'
$measureProbe = Join-Path $probeDir 'measure.ps1'
$parseProbe = Join-Path $probeDir 'parse.ps1'
$output = Join-Path $probeDir 'escapes.bin'
$expectedRepair = [Convert]::ToBase64String(
    [Text.Encoding]::UTF8.GetBytes(([string][char]27) + '[3J' + ([string][char]27) + '[1;1H'))
$expectedMeasure = [Convert]::ToBase64String(
    [Text.Encoding]::UTF8.GetBytes(([string][char]27) + '[?1049h|' + ([string][char]27) + '[?1049l'))

$parseLines = @(
    '$parseErrors = $null'
    '$null = [System.Management.Automation.Language.Parser]::ParseFile($env:FFS_PARSE_FILE, [ref]$null, [ref]$parseErrors)'
    'if (@($parseErrors).Count) { [Console]::Error.WriteLine(($parseErrors | ForEach-Object { $_.Message }) -join "; "); exit 1 }'
)
Set-Content -LiteralPath $parseProbe -Value ($parseLines -join [Environment]::NewLine) -Encoding UTF8
foreach ($engine in $engines) {
    foreach ($source in $launcher, $preview, $measure) {
        $env:FFS_PARSE_FILE = $source
        $parseOutput = @(& $engine.Path -NoLogo -NoProfile -ExecutionPolicy Bypass -File $parseProbe 2>&1)
        $parseExit = $LASTEXITCODE
        if ($parseExit -ne 0) {
            throw "$($engine.Name) could not parse $(Split-Path -Leaf $source): $($parseOutput -join ' ')"
        }
    }
    Write-Host "  generated scripts parse: $($engine.Name)"
}
Remove-Item Env:\FFS_PARSE_FILE -ErrorAction SilentlyContinue

function Invoke-EscapeProbe {
    param(
        [System.Management.Automation.PSObject]$Engine,
        [string]$Probe,
        [string]$Expected,
        [string]$Description
    )
    $env:FFS_ESC_FILE = $output
    Remove-Item -LiteralPath $output -Force -ErrorAction SilentlyContinue
    $run = @(& $Engine.Path -NoLogo -NoProfile -ExecutionPolicy Bypass -File $Probe 2>&1)
    $exitCode = $LASTEXITCODE
    if ($exitCode -ne 0) {
        throw "$($Engine.Name) $Description probe failed (exit $exitCode): $($run -join ' ')"
    }
    if (-not (Test-Path -LiteralPath $output -PathType Leaf)) {
        throw "$($Engine.Name) $Description probe wrote no bytes."
    }
    $actual = [Convert]::ToBase64String([IO.File]::ReadAllBytes($output))
    if ($actual -ne $Expected) {
        throw "$($Engine.Name) emitted the wrong $Description bytes: $actual"
    }
}

# Exercise the actual generated resize repair from both scripts. Only the
# scroll/host-clear and terminal-write sinks are stubbed so the emitted ESC
# sequence can be compared byte-for-byte without touching a user's display.
foreach ($source in $launcher, $preview) {
    $text = Get-Content -LiteralPath $source -Raw
    $begin = $text.IndexOf('function global:Repair-FFSFetch {')
    if ($begin -lt 0) { throw "could not find Repair-FFSFetch in $source" }
    $end = $text.IndexOf('function global:Redraw-Fetch {', $begin)
    if ($end -le $begin) {
        throw "could not find the end of Repair-FFSFetch in $source"
    }
    $repair = $text.Substring($begin, $end - $begin)
    $writeCall = '[Console]::Write($clearScreen)'
    if (-not $repair.Contains($writeCall)) {
        throw "repair does not write the PowerShell-compatible clear sequence: $source"
    }
    $repair = $repair.Replace('[Console]::Write(("`n" * ($rows + 2)))', '# scroll suppressed by byte-capture test')
    $repair = $repair.Replace('try { [Console]::Clear() } catch { }', '# host clear suppressed by byte-capture test')
    $capture = '[IO.File]::WriteAllBytes($env:FFS_ESC_FILE, [Text.Encoding]::UTF8.GetBytes($clearScreen))'
    $repair = $repair.Replace($writeCall, $capture)
    $probeLines = @(
        $repair
        'function global:Show-FFSFetch { }'
        'Repair-FFSFetch -Plan @{ Rows = 1 }'
    )
    Set-Content -LiteralPath $repairProbe -Value ($probeLines -join [Environment]::NewLine) -Encoding UTF8

    foreach ($engine in $engines) {
        Invoke-EscapeProbe -Engine $engine -Probe $repairProbe -Expected $expectedRepair `
            -Description "resize repair from $(Split-Path -Leaf $source)"
    }
    Write-Host "  repair escape ok: $(Split-Path -Leaf $source)"
}

# Execute the exact assignment lines in the generated measurement script. The
# measurement routine uses these for entering and leaving the alternate screen.
$measureText = Get-Content -LiteralPath $measure -Raw
$enter = [regex]::Match($measureText, '(?m)^[ \t]*\$enterAltScreen[ \t]*=[^\r\n]*').Value.Trim()
$leave = [regex]::Match($measureText, '(?m)^[ \t]*\$leaveAltScreen[ \t]*=[^\r\n]*').Value.Trim()
if (-not $enter -or -not $leave) {
    throw 'measurement script is missing alternate-screen escape assignments.'
}
$measureLines = @(
    $enter
    $leave
    '[IO.File]::WriteAllBytes($env:FFS_ESC_FILE, [Text.Encoding]::UTF8.GetBytes($enterAltScreen + ''|'' + $leaveAltScreen))'
)
Set-Content -LiteralPath $measureProbe -Value ($measureLines -join [Environment]::NewLine) -Encoding UTF8
foreach ($engine in $engines) {
    Invoke-EscapeProbe -Engine $engine -Probe $measureProbe -Expected $expectedMeasure `
        -Description 'measurement alternate-screen'
    Write-Host "  measurement escapes ok: $($engine.Name)"
}

Remove-Item Env:\FFS_ESC_FILE -ErrorAction SilentlyContinue
Write-Host 'terminal ESC sequences are real control bytes in every tested PowerShell.'
