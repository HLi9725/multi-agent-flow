# -*- coding: utf-8 -*-
"""项目接入反例。演练只用临时目录，不改用户主目录，不访问 GitHub。"""

from __future__ import annotations

import getpass
import json
import os
import stat
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

from scripts._lib.core.file_lock import acquire_lock, release_lock
from scripts._lib.onboarding.clients import merge_toml
from scripts._lib.onboarding.engine import probe_python

REPO = Path(__file__).resolve().parents[1]
SETUP = REPO / "scripts" / "setup_project.py"
TOKEN = "SENTINEL_TOKEN_SHOULD_NOT_LEAK_9f3a"
CLIENTS = "cursor,codex,antigravity"


def _run(args: list[str], env: dict | None = None, timeout: int = 180) -> subprocess.CompletedProcess:
    merged = os.environ.copy()
    merged.pop("YY_FLOW_PROJECT_ROOT", None)
    merged.pop("YY_FLOW_ONBOARDING_FAIL_CONFIG", None)
    merged.pop("YY_FLOW_ONBOARDING_INTERRUPT_AFTER_CONFIG", None)
    merged["GITHUB_TOKEN"] = TOKEN
    if env:
        merged.update(env)
    return subprocess.run(
        [sys.executable, str(SETUP), *args],
        cwd=str(REPO),
        env=merged,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
        check=False,
    )


def _payload(proc: subprocess.CompletedProcess) -> dict:
    raw = proc.stdout.strip() if proc.returncode == 0 else (proc.stderr.strip() or proc.stdout.strip())
    return json.loads(raw)


def _base(root: Path, *extra: str) -> list[str]:
    return ["--project-root", str(root), "--skill-root", str(REPO), "--clients", CLIENTS, "--json", *extra]


def _assert_readonly_registry(root: Path) -> dict:
    path = next(root.rglob("mcp_registry.json"))
    data = json.loads(path.read_text(encoding="utf-8"))
    text = path.read_text(encoding="utf-8")
    assert "github_credential" not in text
    assert "cursor_secret" not in text
    assert "token_secret" not in text
    assert TOKEN not in text
    assert data["projects"][0]["write_enabled"] is False
    assert data["projects"][0]["board_kind"] == "offline"
    grants = data["local_principals"][0]["grants"]
    assert list(grants.values()) == [["read"]]
    assert data["local_principals"][0]["os_user"] == getpass.getuser()
    return data


def _scan_tree(root: Path) -> None:
    for path in root.rglob("*"):
        if not path.is_file() or path.is_symlink():
            continue
        text = path.read_text(encoding="utf-8", errors="ignore")
        assert TOKEN not in text
        assert "github_credential" not in text


def test_apply_and_dry_run_rejected(tmp_path: Path) -> None:
    proc = _run(["--project-root", str(tmp_path), "--apply", "--dry-run", "--json"])
    assert proc.returncode != 0
    assert _payload(proc)["code"] == "USAGE"


def test_ac01_preview_then_apply_three_clients(tmp_path: Path) -> None:
    project = tmp_path / "blank"
    project.mkdir()
    preview = _run(_base(project))
    assert preview.returncode == 0, preview.stderr
    assert not (project / ".cursor" / "mcp.json").exists()
    body = _payload(preview)
    assert body["action"] == "preview"
    assert body["client_connection_verified"] is False
    applied = _run(_base(project, "--apply"))
    assert applied.returncode == 0, applied.stderr
    result = _payload(applied)
    assert result["verification"] == "protocol_self_check"
    assert result["ac14_live_client_verified"] is False
    cursor = json.loads((project / ".cursor" / "mcp.json").read_text(encoding="utf-8"))
    codex = (project / ".codex" / "config.toml").read_text(encoding="utf-8")
    anti = json.loads((project / ".agents" / "mcp_config.json").read_text(encoding="utf-8"))
    assert list(cursor["mcpServers"]) == ["yy-flow"]
    command = cursor["mcpServers"]["yy-flow"]
    assert command["args"][1:4] == ["stdio", "--registry", str(next(project.rglob("mcp_registry.json")))]
    assert command["command"] == sys.executable
    assert "[mcp_servers.yy-flow]" in codex
    assert "# BEGIN YY-FLOW MANAGED yy-flow" in codex
    assert list(anti["mcpServers"]) == ["yy-flow"]
    assert (project / ".cursor" / "agents" / "flow-dev.md").is_file()
    assert (project / ".cursor" / "rules" / "yy-flow.mdc").is_file()
    assert (project / ".codex" / "agents" / "flow-dev.toml").is_file()
    assert (project / ".agents" / "agents" / "flow-dev.md").is_file()
    arch = yaml.safe_load((project / ".yy-flow" / "user_data" / "project_architecture.config.yaml").read_text(encoding="utf-8"))
    assert arch["meta"]["initialized"] is False
    board = json.loads((project / ".yy-flow" / "user_data" / "board.json").read_text(encoding="utf-8"))
    assert board == []
    _assert_readonly_registry(project)
    _scan_tree(project)
    before = {
        path.relative_to(project).as_posix(): path.read_bytes()
        for path in project.rglob("*")
        if path.is_file()
        and not path.is_symlink()
        and "onboarding" not in path.parts
        and "mcp_audit" not in path.parts
        and "locks" not in path.parts
        and path.name != "state.json"
    }
    again = _run(_base(project, "--apply"))
    assert again.returncode == 0, again.stderr
    after_board = json.loads((project / ".yy-flow" / "user_data" / "board.json").read_text(encoding="utf-8"))
    assert after_board == []
    _assert_readonly_registry(project)
    for relative, blob in before.items():
        assert (project / relative).read_bytes() == blob
    assert codex.count("[mcp_servers.yy-flow]") == 1


def test_ac03_preserves_foreign_config_and_rejects_conflict(tmp_path: Path) -> None:
    project = tmp_path / "mix"
    project.mkdir()
    (project / ".cursor").mkdir()
    (project / ".codex").mkdir()
    (project / ".agents").mkdir()
    (project / ".cursor" / "mcp.json").write_text(
        json.dumps({"note": "keep", "mcpServers": {"other": {"command": "node", "args": ["a"]}}}, indent=2) + "\n",
        encoding="utf-8",
    )
    original_toml = "# keep comment\n[other]\nvalue = 1\n\n[mcp_servers.other]\ncommand = \"node\"\n"
    (project / ".codex" / "config.toml").write_text(original_toml, encoding="utf-8")
    (project / ".agents" / "mcp_config.json").write_text('{"mcpServers": {"remote": {"command": "node"}}}\n', encoding="utf-8")
    proc = _run(_base(project, "--apply"))
    assert proc.returncode == 0, proc.stderr
    toml_text = (project / ".codex" / "config.toml").read_text(encoding="utf-8")
    assert "# keep comment" in toml_text
    assert '[mcp_servers.other]' in toml_text
    assert toml_text.count("[mcp_servers.yy-flow]") == 1
    cursor = json.loads((project / ".cursor" / "mcp.json").read_text(encoding="utf-8"))
    assert cursor["note"] == "keep"
    assert cursor["mcpServers"]["other"]["command"] == "node"
    conflict = tmp_path / "conflict"
    conflict.mkdir()
    (conflict / ".cursor").mkdir()
    original = '{"mcpServers": {"yy-flow": {"command": "not-managed"}}}\n'
    (conflict / ".cursor" / "mcp.json").write_text(original, encoding="utf-8")
    denied = _run(_base(conflict, "--apply"))
    assert denied.returncode != 0
    assert (conflict / ".cursor" / "mcp.json").read_text(encoding="utf-8") == original
    assert _payload(denied)["code"] == "CONFLICT"
    broken = tmp_path / "broken"
    broken.mkdir()
    (broken / ".codex").mkdir()
    (broken / ".codex" / "config.toml").write_text("[\n", encoding="utf-8")
    bad = _run(_base(broken, "--apply"))
    assert bad.returncode != 0
    assert (broken / ".codex" / "config.toml").read_text(encoding="utf-8") == "[\n"
    edited = (project / ".cursor" / "mcp.json").read_text(encoding="utf-8").replace("mcp_server.py", "mcp_server_user.py", 1)
    (project / ".cursor" / "mcp.json").write_text(edited, encoding="utf-8")
    changed = _run(_base(project, "--apply"))
    assert changed.returncode != 0
    assert (project / ".cursor" / "mcp.json").read_text(encoding="utf-8") == edited


def test_def_t0048_1_user_edits_to_registry_and_pointer_are_not_overwritten(tmp_path: Path) -> None:
    project = tmp_path / "owned"
    project.mkdir()
    args = ["--project-root", str(project), "--skill-root", str(REPO), "--clients", "cursor", "--json", "--apply"]
    first = _run(args)
    assert first.returncode == 0, first.stderr
    second = _run(args)
    assert second.returncode == 0, second.stderr
    registry = project / ".yy-flow" / "user_data" / "mcp_registry.json"
    pointer = project / ".yy-flow" / "skill-pointer.json"
    original_registry = registry.read_text(encoding="utf-8")
    original_pointer = pointer.read_text(encoding="utf-8")
    _assert_readonly_registry(project)
    document = json.loads(original_registry)
    project_id = document["projects"][0]["project_id"]
    document["local_principals"][0]["grants"][project_id] = ["read", "admin"]
    mutated_registry = json.dumps(document, ensure_ascii=False, indent=2) + "\n"
    assert mutated_registry != original_registry
    registry.write_text(mutated_registry, encoding="utf-8")
    denied = _run(args)
    assert denied.returncode != 0
    body = _payload(denied)
    assert body["code"] == "CONFLICT"
    assert body.get("diff")
    assert len(body["diff"].splitlines()) <= 45
    assert registry.read_text(encoding="utf-8") == mutated_registry
    assert "admin" in registry.read_text(encoding="utf-8")
    registry.write_bytes(original_registry.encode("utf-8"))
    pointer_doc = json.loads(original_pointer)
    pointer_doc["note"] = "user-edit"
    mutated_pointer = json.dumps(pointer_doc, ensure_ascii=False, indent=2) + "\n"
    pointer.write_bytes(mutated_pointer.encode("utf-8"))
    denied_pointer = _run(args)
    assert denied_pointer.returncode != 0
    pointer_body = _payload(denied_pointer)
    assert pointer_body["code"] == "CONFLICT"
    assert pointer_body.get("diff")
    assert len(pointer_body["diff"].splitlines()) <= 45
    assert pointer.read_text(encoding="utf-8") == mutated_pointer
    assert registry.read_text(encoding="utf-8") == original_registry


def test_toml_merge_keeps_comments() -> None:
    original = "# keep\n[other]\nvalue = 1\n"
    block = "\n".join(
        [
            "# BEGIN YY-FLOW MANAGED yy-flow",
            "[mcp_servers.yy-flow]",
            'command = "python"',
            'args = ["server.py", "stdio", "--registry", "reg.json"]',
            "# END YY-FLOW MANAGED yy-flow",
            "",
        ]
    )
    merged = merge_toml(original, block, None)
    assert merged.startswith("# keep")
    assert merge_toml(merged, block, None) == merged


def test_ac04_unicode_space_and_alternate_python(tmp_path: Path) -> None:
    project = tmp_path / "项目 目录" / "my app"
    project.mkdir(parents=True)
    runtime_home = tmp_path / "解释器 目录"
    runtime_home.mkdir()
    runtime = runtime_home / "runtime"
    source = os.path.dirname(os.path.realpath(sys.executable))
    try:
        os.symlink(source, runtime, target_is_directory=True)
    except OSError:
        import _winapi

        _winapi.CreateJunction(source, str(runtime))
    python = str(runtime / os.path.basename(sys.executable))
    proc = _run(_base(project, "--apply", "--python", python), timeout=180)
    assert proc.returncode == 0, proc.stderr
    cursor = json.loads((project / ".cursor" / "mcp.json").read_text(encoding="utf-8"))
    assert cursor["mcpServers"]["yy-flow"]["command"] == os.path.abspath(python)
    text = "\n".join(path.read_text(encoding="utf-8") for path in (REPO / "scripts" / "_lib" / "onboarding").glob("*.py"))
    text += (REPO / "scripts" / "setup_project.py").read_text(encoding="utf-8")
    assert "shell=True" not in text
    assert "C:\\Users" not in text
    assert "/Users/" not in text


def test_probe_rejects_missing_dependency() -> None:
    def runner(command: list[str]) -> int:
        if "tomllib" in command[-1]:
            return 1
        return 0

    with pytest.raises(Exception) as caught:
        probe_python(sys.executable, runner=runner)
    assert caught.value.exit_code == 5


def test_ac05_two_projects_share_code_and_isolate_data(tmp_path: Path) -> None:
    skill_board = REPO / "user_data" / "board.json"
    before = skill_board.read_bytes() if skill_board.is_file() else b""
    first = tmp_path / "alpha"
    second = tmp_path / "beta"
    first.mkdir()
    second.mkdir()
    env = os.environ.copy()
    env.pop("YY_FLOW_PROJECT_ROOT", None)
    env["GITHUB_TOKEN"] = TOKEN
    commands = []
    for root in (first, second):
        commands.append(
            subprocess.Popen(
                [sys.executable, str(SETUP), *_base(root, "--apply")],
                cwd=str(REPO),
                env=env,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                errors="replace",
            )
        )
    results = []
    for proc in commands:
        out, err = proc.communicate(timeout=180)
        results.append((proc.returncode, out, err))
    assert results[0][0] == 0, results[0][2]
    assert results[1][0] == 0, results[1][2]
    reg_a = json.loads(next((first / ".yy-flow").rglob("mcp_registry.json")).read_text(encoding="utf-8"))
    reg_b = json.loads(next((second / ".yy-flow").rglob("mcp_registry.json")).read_text(encoding="utf-8"))
    assert reg_a["projects"][0]["project_id"] != reg_b["projects"][0]["project_id"]
    assert reg_a["projects"][0]["data_root"] != reg_b["projects"][0]["data_root"]
    assert skill_board.read_bytes() == before if skill_board.is_file() else before == b""
    assert not (REPO / ".yy-flow" / "user_data" / "mcp_registry.json").exists()


def test_ac06_legacy_kept_and_ambiguous_rejected(tmp_path: Path) -> None:
    project = tmp_path / "legacy"
    board = project / "user_data" / "board.json"
    board.parent.mkdir(parents=True)
    cards = [{"id": "T0001", "name": "keep", "status": "待开始", "type": "G"}]
    board.write_text(json.dumps(cards, ensure_ascii=False), encoding="utf-8")
    evidence = project / "evidence" / "note.txt"
    evidence.parent.mkdir()
    evidence.write_text("evidence", encoding="utf-8")
    checkpoint = project / "user_data" / "checkpoints" / "keep.txt"
    checkpoint.parent.mkdir()
    checkpoint.write_text("checkpoint", encoding="utf-8")
    proc = _run(_base(project, "--apply"))
    assert proc.returncode == 0, proc.stderr
    assert json.loads(board.read_text(encoding="utf-8")) == cards
    assert evidence.read_text(encoding="utf-8") == "evidence"
    assert checkpoint.read_text(encoding="utf-8") == "checkpoint"
    assert not (project / ".yy-flow" / "user_data" / "board.json").exists()
    ambiguous = tmp_path / "both"
    (ambiguous / "user_data").mkdir(parents=True)
    (ambiguous / ".yy-flow" / "user_data").mkdir(parents=True)
    (ambiguous / "user_data" / "board.json").write_text("[]\n", encoding="utf-8")
    (ambiguous / ".yy-flow" / "user_data" / "board.json").write_text("[{\"id\":\"T0008\"}]\n", encoding="utf-8")
    denied = _run(_base(ambiguous, "--apply"))
    assert denied.returncode != 0
    assert _payload(denied)["code"] == "AMBIGUOUS_LAYOUT"
    assert (ambiguous / "user_data" / "board.json").read_text(encoding="utf-8") == "[]\n"


def test_reject_other_project_skill_as_shared(tmp_path: Path) -> None:
    other = tmp_path / "biz" / ".yy-flow" / "skill" / "scripts"
    other.mkdir(parents=True)
    (other / "mcp_server.py").write_text("print('no')\n", encoding="utf-8")
    project = tmp_path / "target"
    project.mkdir()
    proc = _run(["--project-root", str(project), "--skill-root", str(other.parent), "--json"])
    assert proc.returncode != 0
    assert _payload(proc)["code"] == "SKILL_ROOT"
    assert not (project / ".cursor").exists()


def test_ac07_write_failure_interrupt_and_user_edit(tmp_path: Path) -> None:
    project = tmp_path / "fault"
    project.mkdir()
    failed = _run(_base(project, "--apply"), env={"YY_FLOW_ONBOARDING_FAIL_CONFIG": "2"})
    assert failed.returncode != 0
    assert (project / ".cursor" / "mcp.json").is_file()
    assert not (project / ".codex" / "config.toml").exists()
    diagnosed = _run(["--project-root", str(project), "--diagnose", "--json"])
    assert diagnosed.returncode == 0
    assert _payload(diagnosed)["complete"] is False
    recovered = _run(_base(project, "--apply"))
    assert recovered.returncode == 0, recovered.stderr
    assert (project / ".codex" / "config.toml").read_text(encoding="utf-8").count("[mcp_servers.yy-flow]") == 1
    _assert_readonly_registry(project)

    third = tmp_path / "fault3"
    third.mkdir()
    third_fail = _run(_base(third, "--apply"), env={"YY_FLOW_ONBOARDING_FAIL_CONFIG": "3"})
    assert third_fail.returncode != 0
    assert (third / ".codex" / "config.toml").is_file()
    assert not (third / ".agents" / "mcp_config.json").exists()
    third_ok = _run(_base(third, "--apply"))
    assert third_ok.returncode == 0, third_ok.stderr
    assert (third / ".agents" / "mcp_config.json").is_file()

    stopped = tmp_path / "stop"
    stopped.mkdir()
    interrupted = _run(_base(stopped, "--apply"), env={"YY_FLOW_ONBOARDING_INTERRUPT_AFTER_CONFIG": "2"})
    assert interrupted.returncode == 86
    resumed = _run(_base(stopped, "--apply"))
    assert resumed.returncode == 0, resumed.stderr
    assert (stopped / ".codex" / "config.toml").read_text(encoding="utf-8").count("[mcp_servers.yy-flow]") == 1

    edited = tmp_path / "edited"
    edited.mkdir()
    crash = _run(_base(edited, "--apply"), env={"YY_FLOW_ONBOARDING_INTERRUPT_AFTER_CONFIG": "1"})
    assert crash.returncode == 86
    cursor = edited / ".cursor" / "mcp.json"
    original = cursor.read_text(encoding="utf-8")
    cursor.write_text(original.replace("yy-flow", "yy-flow-user", 1), encoding="utf-8")
    user_text = cursor.read_text(encoding="utf-8")
    blocked = _run(_base(edited, "--apply"))
    assert blocked.returncode != 0
    assert cursor.read_text(encoding="utf-8") == user_text
    assert _payload(blocked)["code"] == "USER_CHANGE"


def test_ac08_missing_python_symlink_and_lock(tmp_path: Path) -> None:
    project = tmp_path / "guard"
    project.mkdir()
    missing = _run(_base(project, "--apply", "--python", str(tmp_path / "no-python.exe")))
    assert missing.returncode != 0
    assert not (project / ".cursor").exists()
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "mcp.json").write_text('{"keep":1}\n', encoding="utf-8")
    linked = tmp_path / "linked"
    linked.mkdir()
    try:
        os.symlink(outside, linked / ".cursor", target_is_directory=True)
    except OSError:
        import _winapi

        _winapi.CreateJunction(str(outside), str(linked / ".cursor"))
    escaped = _run(_base(linked, "--apply"))
    assert escaped.returncode != 0
    assert (outside / "mcp.json").read_text(encoding="utf-8") == '{"keep":1}\n'
    locked = tmp_path / "locked"
    locked.mkdir()
    lock_file = locked / ".yy-flow" / "user_data" / "locks" / "onboarding.lock"
    lock_file.parent.mkdir(parents=True)
    handle = acquire_lock(str(lock_file), blocking=False)
    try:
        busy = _run(_base(locked, "--apply"))
    finally:
        release_lock(handle)
    assert busy.returncode == 3
    assert not (locked / ".cursor" / "mcp.json").exists()
    parent = tmp_path / "file-parent"
    parent.mkdir()
    (parent / ".cursor").write_text("not-a-directory", encoding="utf-8")
    denied = _run(_base(parent, "--apply"))
    assert denied.returncode != 0
    assert (parent / ".cursor").read_text(encoding="utf-8") == "not-a-directory"


def test_ac10_unselected_client_and_readonly_boundary(tmp_path: Path) -> None:
    project = tmp_path / "one"
    project.mkdir()
    (project / ".codex").mkdir()
    sentinel = "# do not touch\nvalue = 1\n"
    (project / ".codex" / "config.toml").write_text(sentinel, encoding="utf-8")
    proc = _run(["--project-root", str(project), "--skill-root", str(REPO), "--clients", "cursor", "--json", "--apply"])
    assert proc.returncode == 0, proc.stderr
    assert (project / ".codex" / "config.toml").read_text(encoding="utf-8") == sentinel
    assert not (project / ".agents" / "mcp_config.json").exists()
    assert (project / ".cursor" / "mcp.json").is_file()
    _assert_readonly_registry(project)
    _scan_tree(project)
    home_cursor = Path.home() / ".cursor" / "mcp.json"
    before = home_cursor.read_bytes() if home_cursor.is_file() else None
    assert before == (home_cursor.read_bytes() if home_cursor.is_file() else None)


def test_ac12_remove_only_unmodified_entry(tmp_path: Path) -> None:
    project = tmp_path / "remove"
    project.mkdir()
    (project / ".cursor").mkdir()
    (project / ".cursor" / "mcp.json").write_text(
        json.dumps({"mcpServers": {"other": {"command": "node"}}}) + "\n",
        encoding="utf-8",
    )
    proc = _run(["--project-root", str(project), "--skill-root", str(REPO), "--clients", "cursor", "--json", "--apply"])
    assert proc.returncode == 0, proc.stderr
    preview = _run(["--project-root", str(project), "--remove", "--clients", "cursor", "--json"])
    assert preview.returncode == 0
    assert (project / ".cursor" / "mcp.json").read_text(encoding="utf-8").count("yy-flow") >= 1
    removed = _run(["--project-root", str(project), "--remove", "--clients", "cursor", "--json", "--apply"])
    assert removed.returncode == 0, removed.stderr
    cursor = json.loads((project / ".cursor" / "mcp.json").read_text(encoding="utf-8"))
    assert "yy-flow" not in cursor["mcpServers"]
    assert cursor["mcpServers"]["other"]["command"] == "node"
    assert (project / ".yy-flow" / "user_data" / "board.json").is_file()
    again = _run(["--project-root", str(project), "--skill-root", str(REPO), "--clients", "cursor", "--json", "--apply"])
    assert again.returncode == 0, again.stderr
    path = project / ".cursor" / "mcp.json"
    changed = path.read_text(encoding="utf-8").replace("mcp_server.py", "mcp_server_user.py", 1)
    path.write_text(changed, encoding="utf-8")
    refused = _run(["--project-root", str(project), "--remove", "--clients", "cursor", "--json", "--apply"])
    assert refused.returncode != 0
    assert path.read_text(encoding="utf-8") == changed


def test_skip_self_check_is_not_live_verification(tmp_path: Path) -> None:
    project = tmp_path / "skip"
    project.mkdir()
    proc = _run(["--project-root", str(project), "--skill-root", str(REPO), "--clients", "cursor", "--json", "--apply", "--skip-self-check"])
    assert proc.returncode == 0, proc.stderr
    body = _payload(proc)
    assert body["verification"] == "skipped"
    assert body["ac14_live_client_verified"] is False
    assert "AC-14" in body["message"] or "现场" in body["message"] or "不是真实客户端" in body["message"]


def test_def_t0048_2_unwritable_directory_is_permission_failure(tmp_path: Path) -> None:
    if sys.platform != "win32":
        pytest.fail("权限拒绝必须在 Windows 上观察到真实写入失败，不能跳过")
    project = tmp_path / "denied"
    project.mkdir()
    cursor_dir = project / ".cursor"
    cursor_dir.mkdir()
    config = cursor_dir / "mcp.json"
    sentinel = '{"mcpServers": {"other": {"command": "keep"}}}\n'
    config.write_bytes(sentinel.encode("utf-8"))
    os.chmod(config, stat.S_IREAD)
    handle = _deny_write_share(config)
    if handle is None:
        pytest.fail("当前进程无法独占只读文件。提升权限会使只读属性失效，此用例不能跳过或记为通过")
    try:
        probe = os.open(config, os.O_RDWR)
        os.close(probe)
    except OSError:
        blocked = True
    else:
        blocked = False
    if not blocked:
        _release_write_share(handle)
        pytest.fail("只读文件仍可被当前进程改写。提升权限会使只读属性失效，此用例不能跳过或记为通过")
    try:
        proc = _run(
            ["--project-root", str(project), "--skill-root", str(REPO), "--clients", "cursor", "--json", "--apply"],
            timeout=90,
        )
    finally:
        _release_write_share(handle)
        os.chmod(config, stat.S_IWRITE | stat.S_IREAD)
    if proc.returncode == 0:
        pytest.fail("只读文件仍被覆盖写入。当前进程若以提升权限运行，只读属性无效，此用例不能跳过或记为通过")
    assert _payload(proc)["code"] == "WRITE_FAILED"
    assert config.read_bytes() == sentinel.encode("utf-8")


def _deny_write_share(path: Path):
    """以不共享方式打开只读文件，使替换写入得到拒绝访问。"""
    import ctypes

    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.CreateFileW.argtypes = [
        ctypes.c_wchar_p, ctypes.c_uint32, ctypes.c_uint32, ctypes.c_void_p,
        ctypes.c_uint32, ctypes.c_uint32, ctypes.c_void_p,
    ]
    kernel.CreateFileW.restype = ctypes.c_void_p
    generic_read = 0x80000000
    open_existing = 3
    file_attribute_readonly = 0x1
    handle = kernel.CreateFileW(str(path), generic_read, 0, None, open_existing, file_attribute_readonly, None)
    invalid = ctypes.c_void_p(-1).value
    if not handle or handle == invalid:
        return None
    return handle


def _release_write_share(handle) -> None:
    import ctypes

    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.CloseHandle.argtypes = [ctypes.c_void_p]
    kernel.CloseHandle.restype = ctypes.c_int
    kernel.CloseHandle(handle)
