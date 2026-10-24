"""Launcher template invariants behind the torn-logo-on-a-new-window bug."""
from studio.launcher import LAUNCHER_TEMPLATE, RESIZE_GUARD_TEMPLATE
from studio.preview import PREVIEW_TEMPLATE


def test_fastfetch_never_called_through_the_pipeline():
    # "& $exe" inside the prompt function turns fastfetch's output into the
    # prompt string (OEM-decoded mojibake, the sixel lost).
    for src in (LAUNCHER_TEMPLATE, RESIZE_GUARD_TEMPLATE, PREVIEW_TEMPLATE):
        assert "& $Plan.Exe" not in src
    assert "Invoke-FFSExe -Exe $Plan.Exe" in LAUNCHER_TEMPLATE
    assert "Invoke-FFSExe -Exe $Plan.Exe" in PREVIEW_TEMPLATE


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
