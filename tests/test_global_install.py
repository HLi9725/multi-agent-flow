# -*- coding: utf-8 -*-
"""共享安装器反例。使用假 git，不访问 GitHub，不写用户主目录。"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

from scripts._lib.core.file_lock import acquire_lock, release_lock
from scripts._lib.onboarding.global_install import DEFAULT_REF, DEFAULT_SOURCE, install

REPO = Path(__file__).resolve().parents[1]
FAKE = REPO / "tests" / "support" / "fake_git.py"
PS1 = REPO / "scripts" / "install_global.ps1"
SH = REPO / "scripts" / "install_global.sh"


def _env(tmp: Path, **extra: str) -> dict:
    env = os.environ.copy()
    env["YY_FLOW_GIT_ARGV"] = json.dumps([sys.executable, str(FAKE)])
    env["FAKE_GIT_LOG"] = str(tmp / "git.log")
    env["FAKE_GIT_SHA"] = extra.pop("FAKE_GIT_SHA", "a" * 40)
    env.pop("FAKE_GIT_FAIL_CMD", None)
    env.pop("FAKE_GIT_DIRTY", None)
    env.update(extra)
    return env


def test_defaults_are_stable_source_and_main() -> None:
    assert DEFAULT_SOURCE == "HLi9725/multi-agent-flow"
    assert DEFAULT_REF == "main"
    text = PS1.read_text(encoding="utf-8") + SH.read_text(encoding="utf-8")
    assert "HLi9725/multi-agent-flow" in text
    assert "feature/S4-mcp-integration" not in text
    assert "git pull" not in text
    assert "degit" not in text
    assert "LASTEXITCODE" in PS1.read_text(encoding="utf-8")
    assert "status=$?" in SH.read_text(encoding="utf-8")


def test_install_records_sha_and_keeps_previous(tmp_path: Path) -> None:
    dest = tmp_path / "canon"
    os.environ.update(_env(tmp_path, FAKE_GIT_SHA="a" * 40))
    try:
        result = install(DEFAULT_SOURCE, "main", str(dest))
        assert result["sha"] == "a" * 40
        record = json.loads((dest / "install-record.json").read_text(encoding="utf-8"))
        assert record["source"] == DEFAULT_SOURCE
        assert record["ref"] == "main"
        assert (dest / "scripts" / "mcp_server.py").is_file()
        assert (dest / ".yy-flow-shared").is_file()
        log = (tmp_path / "git.log").read_text(encoding="utf-8")
        assert "pull" not in log
        assert "fetch" in log and "checkout" in log
        os.environ["FAKE_GIT_SHA"] = "b" * 40
        updated = install(DEFAULT_SOURCE, "v1", str(dest))
    finally:
        for key in ("YY_FLOW_GIT_ARGV", "FAKE_GIT_SHA", "FAKE_GIT_LOG"):
            os.environ.pop(key, None)
    assert updated["sha"] == "b" * 40
    previous = Path(str(dest) + ".previous")
    assert previous.is_dir()
    assert "sha=" + ("a" * 40) in (previous / "README.md").read_text(encoding="utf-8")
    assert "sha=" + ("b" * 40) in (dest / "README.md").read_text(encoding="utf-8")


def test_dirty_running_and_fetch_failure(tmp_path: Path) -> None:
    dest = tmp_path / "canon"
    dest.mkdir()
    (dest / ".git").mkdir()
    (dest / "README.md").write_text("old\n", encoding="utf-8")
    (dest / "install-record.json").write_text("{}\n", encoding="utf-8")
    os.environ.update(_env(tmp_path, FAKE_GIT_DIRTY="1"))
    try:
        try:
            install(DEFAULT_SOURCE, "main", str(dest))
            raised = False
        except Exception as exc:
            raised = True
            assert exc.code == "DIRTY"
        assert raised
        assert (dest / "README.md").read_text(encoding="utf-8") == "old\n"
        log = (tmp_path / "git.log").read_text(encoding="utf-8")
        assert '"fetch"' not in log
        (dest / ".yy-flow-running").write_text("pid\n", encoding="utf-8")
        os.environ["FAKE_GIT_DIRTY"] = "0"
        try:
            install(DEFAULT_SOURCE, "main", str(dest))
            running = False
        except Exception as exc:
            running = exc.code == "RUNNING"
        assert running
        os.environ["FAKE_GIT_FAIL_CMD"] = "fetch"
        os.environ["FAKE_GIT_DIRTY"] = ""
        (dest / ".yy-flow-running").unlink()
        (dest / ".git").rmdir()
        try:
            install(DEFAULT_SOURCE, "main", str(dest))
            failed = False
        except Exception as exc:
            failed = True
            assert "3" in exc.message
        assert failed
        assert (dest / "README.md").read_text(encoding="utf-8") == "old\n"
        assert not (dest / "install-record.json").read_text(encoding="utf-8").startswith('{"source"')
    finally:
        for key in ("YY_FLOW_GIT_ARGV", "FAKE_GIT_SHA", "FAKE_GIT_LOG", "FAKE_GIT_DIRTY", "FAKE_GIT_FAIL_CMD"):
            os.environ.pop(key, None)


def test_lock_blocks_update(tmp_path: Path) -> None:
    dest = tmp_path / "canon"
    dest.mkdir()
    (dest / "install-record.json").write_text("{}\n", encoding="utf-8")
    (dest / ".yy-flow-shared").write_text("", encoding="utf-8")
    handle = acquire_lock(str(dest) + ".install.lock", blocking=False)
    os.environ.update(_env(tmp_path))
    try:
        try:
            install(DEFAULT_SOURCE, "main", str(dest))
            blocked = False
        except Exception as exc:
            blocked = exc.code == "RUNNING"
        assert blocked
        assert (dest / "install-record.json").read_text(encoding="utf-8") == "{}\n"
        assert not (dest / "scripts" / "mcp_server.py").exists()
    finally:
        release_lock(handle)
        for key in ("YY_FLOW_GIT_ARGV", "FAKE_GIT_SHA", "FAKE_GIT_LOG"):
            os.environ.pop(key, None)


def test_powershell_parses_and_propagates_failure(tmp_path: Path) -> None:
    parse = (
        "$tokens=$null; $errors=$null; "
        f"[void][System.Management.Automation.Language.Parser]::ParseFile('{PS1}', [ref]$tokens, [ref]$errors); "
        "if ($errors -and $errors.Count -gt 0) { exit 1 } else { exit 0 }"
    )
    parsed = subprocess.run(
        ["powershell.exe", "-NoProfile", "-Command", parse],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=60,
        check=False,
    )
    assert parsed.returncode == 0, parsed.stderr
    env = _env(tmp_path, FAKE_GIT_FAIL_CMD="fetch")
    failed = subprocess.run(
        ["powershell.exe", "-NoProfile", "-File", str(PS1), "-Dest", str(tmp_path / "canon"), "-Ref", "main"],
        cwd=str(REPO),
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=60,
        check=False,
    )
    assert failed.returncode != 0
    assert not (tmp_path / "canon" / "install-record.json").exists()
    bash = shutil_which("bash")
    if bash:
        syntax = subprocess.run([bash, "-n", str(SH)], capture_output=True, text=True, timeout=30, check=False)
        assert syntax.returncode == 0


def shutil_which(name: str) -> str | None:
    from shutil import which

    return which(name)
