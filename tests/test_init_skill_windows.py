"""Guard the Windows PowerShell 5.1 entrypoint's UTF-8 compatibility."""

from pathlib import Path
import shutil
import subprocess

import pytest


SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "init_skill.ps1"


def test_init_skill_has_utf8_bom_for_windows_powershell_51():
    assert SCRIPT.read_bytes().startswith(b"\xef\xbb\xbf")


@pytest.mark.skipif(shutil.which("powershell.exe") is None, reason="Windows PowerShell unavailable")
def test_init_skill_parses_in_windows_powershell_51():
    command = (
        "$tokens=$null; $errors=$null; "
        f"[System.Management.Automation.Language.Parser]::ParseFile('{SCRIPT}',"
        "[ref]$tokens,[ref]$errors) | Out-Null; "
        "if ($errors.Count) { $errors | ForEach-Object Message; exit 1 }"
    )
    result = subprocess.run(
        ["powershell.exe", "-NoProfile", "-Command", command],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
