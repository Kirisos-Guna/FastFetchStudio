"""Launcher template invariants behind the torn-logo-on-a-new-window bug."""
from studio.launcher import LAUNCHER_TEMPLATE, RESIZE_GUARD_TEMPLATE
from studio.preview import PREVIEW_TEMPLATE


def test_fastfetch_never_called_through_the_pipeline():
    # "& $exe" inside the prompt function turns fastfetch's output into the
    # prompt string (OEM-decoded mojibake, the sixel lost).
    for src in (LAUNCHER_TEMPLATE, RESIZE_GUARD_TEMPLATE, PREVIEW_TEMPLATE):
        assert "& $Plan.Exe" not in src
    # The exact statement the CI probes in build.yml swap for a log write: if
    # it changes, their swap silently matches nothing and the probes test the
    # real (missing) fastfetch instead.
    assert LAUNCHER_TEMPLATE.count("Invoke-FFSExe -Exe $Plan.Exe -FFArgs $a") == 1
    assert PREVIEW_TEMPLATE.count("Invoke-FFSExe -Exe $Plan.Exe -FFArgs $a") == 1


def test_first_draw_waits_for_the_real_window_size():
    # A new Windows Terminal shell reports 120x30 before its real size.
    assert "function global:Wait-FFSGridSettled" in RESIZE_GUARD_TEMPLATE
    tail = LAUNCHER_TEMPLATE[LAUNCHER_TEMPLATE.index("@@RESIZEGUARD@@"):]
    assert tail.index("Wait-FFSGridSettled") < tail.index("Show-FFSFetch -Plan $global:FFSPlan")
    ptail = PREVIEW_TEMPLATE[PREVIEW_TEMPLATE.index("@@RESIZEGUARD@@"):]
    assert ptail.index("Wait-FFSGridSettled") < ptail.index("Show-FFSFetch")


def test_repair_runs_when_idle_but_spares_command_output():
    assert "PowerShell.OnIdle" in RESIZE_GUARD_TEMPLATE
    assert "Get-History -Count 1" in RESIZE_GUARD_TEMPLATE
    assert "GetBufferState" in RESIZE_GUARD_TEMPLATE


def test_cmd_hook_is_quiet_outside_an_interactive_window():
    from studio.launcher import CMD_HOOK_TEMPLATE

    lines = [ln for ln in CMD_HOOK_TEMPLATE.splitlines() if ln.strip()]
    # "echo off" in an AutoRun script would hide the prompt for the session.
    assert not any("echo off" in ln.lower() for ln in lines)
    assert all(ln.startswith(("@", ":")) for ln in lines)
    # "cmd /c" from scripts and tools must not get a fetch in its output; the
    # quotes are stripped first or the comparison silently never matches.
    assert '@set "FFS_CL=!CMDCMDLINE:"=!"' in CMD_HOOK_TEMPLATE
    assert '"!FFS_CL:/c=!"=="!FFS_CL!" goto :ffs_done' in CMD_HOOK_TEMPLATE
    assert "fastfetch-random.ps1" in CMD_HOOK_TEMPLATE
