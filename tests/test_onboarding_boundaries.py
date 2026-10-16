"""Boundary regressions use temporary projects, never real client configuration."""
from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

from scripts._lib.onboarding import engine, transaction as txn
from scripts._lib.onboarding.common import CLIENT_CONFIGS, OnboardError

REPO = Path(__file__).resolve().parents[1]


def arguments(project: Path, clients: str, *extra: str):
    return engine._parser().parse_args([
        "--project-root", str(project), "--skill-root", str(REPO),
        "--clients", clients, *extra,
    ])


def directory_link(source: Path, destination: Path) -> None:
    if os.name == "nt":
        import _winapi

        _winapi.CreateJunction(str(source), str(destination))
    else:
        destination.symlink_to(source, target_is_directory=True)


@pytest.mark.parametrize("client", ["cursor", "codex", "antigravity"])
def test_apply_rejects_redirected_client_parent_before_any_write(tmp_path, client):
    project = tmp_path / "project"
    outside = tmp_path / "outside"
    project.mkdir()
    outside.mkdir()
    relative = Path(CLIENT_CONFIGS[client])
    directory_link(outside, project / relative.parts[0])
    before = set(project.iterdir())
    with pytest.raises(OnboardError, match="跳出项目根"):
        engine.execute(arguments(project, client, "--apply"))
    assert list(outside.iterdir()) == []
    assert set(project.iterdir()) == before


@pytest.mark.parametrize("client", ["cursor", "codex", "antigravity"])
def test_remove_never_follows_redirected_client_directory(tmp_path, client):
    project = tmp_path / "project"
    outside = tmp_path / "outside"
    project.mkdir()
    outside.mkdir()
    assert engine.execute(arguments(project, client, "--apply"))["ok"]
    relative = Path(CLIENT_CONFIGS[client])
    original = (project / relative).read_bytes()
    external = outside / relative.name
    external.write_bytes(original)
    directory = project / relative.parts[0]
    saved = project / "saved-client"
    assert directory.parent.resolve() == project.resolve() == saved.parent.resolve()
    directory.rename(saved)
    directory_link(outside, directory)
    with pytest.raises(OnboardError) as failure:
        engine.execute(arguments(project, client, "--remove", "--apply"))
    assert failure.value.code == "PATH"
    assert external.read_bytes() == original


def test_lock_parent_is_validated_before_lock_creation(tmp_path):
    project = tmp_path / "project"
    data = project / ".yy-flow" / "user_data"
    outside = tmp_path / "outside"
    data.mkdir(parents=True)
    outside.mkdir()
    directory_link(outside, data / "locks")
    with pytest.raises(OnboardError) as failure:
        engine.execute(arguments(project, "cursor", "--apply"))
    assert failure.value.code == "PATH"
    assert list(outside.iterdir()) == []


@pytest.mark.parametrize("redirect", ["target", "backup"])
def test_recover_checks_journal_and_backup_paths_before_writing(tmp_path, redirect):
    project = tmp_path / "project"
    project.mkdir()
    assert engine.execute(arguments(project, "cursor", "--apply"))["ok"]
    data = project / ".yy-flow"
    target = project / ".cursor" / "mcp.json"
    backup_dir = data / "user_data/onboarding/backups/current"
    record = txn.file_record(str(target), b"new\n", "config", 1, "", str(backup_dir))
    progress = txn.new_progress(str(data), [record])
    txn.atomic_write(str(target), b"new\n")
    txn.mark_written(progress, str(target))
    txn.save_json(txn.progress_path(str(data)), progress)
    outside = tmp_path / "outside"
    outside.mkdir()
    if redirect == "target":
        moved = project / "saved-cursor"
        (project / ".cursor").rename(moved)
        (outside / "mcp.json").write_bytes(b"new\n")
        directory_link(outside, project / ".cursor")
    else:
        moved = backup_dir.parent / "saved-backup"
        backup_dir.rename(moved)
        for path in moved.iterdir():
            (outside / path.name).write_bytes(path.read_bytes())
        directory_link(outside, backup_dir)
    before = {p.name: p.read_bytes() for p in outside.iterdir()}
    with pytest.raises(OnboardError) as failure:
        engine.execute(arguments(project, "cursor", "--recover", "--apply"))
    assert failure.value.code == "PATH"
    assert {p.name: p.read_bytes() for p in outside.iterdir()} == before
    assert txn.load_json(txn.progress_path(str(data)))["phase"] == "writing"


def test_recovery_primitive_rejects_outside_backup(tmp_path):
    project = tmp_path / "project"
    project.mkdir()
    outside = tmp_path / "outside-secret"
    outside.write_bytes(b"old\n")
    target = project / "config"
    target.write_bytes(b"new\n")
    from scripts._lib.onboarding.common import sha256_bytes

    progress = {"phase": "writing", "files": [{
        "path": str(target), "status": "written", "had_backup": True,
        "backup": str(outside), "backup_sha256": sha256_bytes(b"old\n"),
        "sha256": sha256_bytes(b"new\n"),
    }]}
    with pytest.raises(OnboardError) as failure:
        txn.rollback(progress, str(project))
    assert failure.value.code == "PATH"
    assert target.read_bytes() == b"new\n"
    assert outside.read_bytes() == b"old\n"


def run_command(command: str) -> subprocess.CompletedProcess:
    if os.name == "nt":
        command = "[Console]::OutputEncoding = [System.Text.Encoding]::UTF8; " + command
        return subprocess.run(["powershell.exe", "-NoProfile", "-Command", command],
                              capture_output=True, timeout=30)
    return subprocess.run(["/bin/sh", "-c", command], capture_output=True, timeout=30)


def test_cursor_without_links_has_executable_absolute_role_entry(tmp_path, monkeypatch):
    project = tmp_path / "项目 目录"
    project.mkdir()
    monkeypatch.setenv("YY_FLOW_ONBOARDING_DENY_LINKS", "1")
    assert engine.execute(arguments(project, "cursor", "--apply"))["ok"]
    assert not (project / ".yy-flow/skill").exists()
    text = (project / ".cursor/agents/flow-dev.md").read_text(encoding="utf-8")
    assert ".yy-flow/skill/scripts/transition_task.py" not in text
    assert "python3 scripts/" not in text
    assert "python scripts/" not in text
    match = re.search(chr(96) + r"([^" + chr(96) + r"\n]+transition_task\.py['\"]?)\s+--role", text)
    assert match, "role must carry a usable, quoted absolute invocation"
    proc = run_command(match.group(1) + " --help")
    assert proc.returncode == 0, proc.stderr


def test_absolute_role_invocation_quotes_spaces_and_apostrophes(tmp_path):
    skill = tmp_path / "技能 root's"
    (skill / "scripts").mkdir(parents=True)
    (skill / "scripts/probe.py").write_text("raise SystemExit(0)\n", encoding="utf-8")
    source = {"role.md": b"python .yy-flow/skill/scripts/probe.py --help"}
    updated = engine._rewrite_exports_for_absolute(source, [".yy-flow/skill"], str(skill), sys.executable)
    assert run_command(updated["role.md"].decode()).returncode == 0


def test_embedded_install_absolute_rewrite_is_not_recursive(tmp_path):
    skill = tmp_path / "项目 空格" / ".yy-flow" / "skill"
    (skill / "scripts").mkdir(parents=True)
    (skill / "scripts/probe.py").write_text("raise SystemExit(0)\n", encoding="utf-8")
    source = {"role.md": b"python .agents/skills/yy-flow/scripts/probe.py --help"}
    updated = engine._rewrite_exports_for_absolute(
        source, [".agents/skills/yy-flow", ".yy-flow/skill"], str(skill), sys.executable,
    )
    command = updated["role.md"].decode()
    assert command.count(str(skill).replace("\\", "/")) == 1
    assert run_command(command).returncode == 0


def test_shared_role_command_binds_project_data_root(tmp_path):
    data_root = tmp_path / "项目 root's" / ".yy-flow"
    source = {"role.md": b"python scripts/paths.py"}
    rewritten = engine._rewrite_exports_for_absolute(source, [], str(REPO), sys.executable, str(data_root))
    proc = run_command(rewritten["role.md"].decode())
    assert proc.returncode == 0, proc.stderr
    decoded = proc.stdout.decode("utf-8").strip()
    assert os.path.normcase(os.path.normpath(decoded)) == os.path.normcase(str(data_root))
