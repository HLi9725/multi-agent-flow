"""Antigravity app plugin discovery layout; all files belong to temporary projects."""
from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from scripts._lib.onboarding import engine, transaction as txn
from scripts._lib.onboarding.common import OnboardError, sha256_bytes

REPO = Path(__file__).resolve().parents[1]
PLUGIN = ".agents/plugins/yy-flow"
CONFIG = PLUGIN + "/mcp_config.json"
MANIFEST = PLUGIN + "/plugin.json"
LEGACY = ".agents/mcp_config.json"


def args(project, *extra, clients="antigravity"):
    return engine._parser().parse_args([
        "--project-root", str(project), "--skill-root", str(REPO),
        "--clients", clients, *extra,
    ])


@pytest.fixture
def project(tmp_path, monkeypatch):
    # Role export contents are covered by the full onboarding tests. Keep these
    # regressions focused on plugin ownership and real protocol self-checks.
    monkeypatch.setattr(engine, "_export_bytes", lambda *a: {})
    root = tmp_path / "项目 root"
    root.mkdir()
    return root


def read(path):
    return json.loads(path.read_text(encoding="utf-8"))


def snapshot(root):
    return {str(p.relative_to(root)): p.read_bytes() for p in root.rglob("*")
            if p.is_file() and not p.is_symlink() and ".yy-flow" not in p.relative_to(root).parts}


def test_plugin_preview_apply_and_idempotency(project):
    assert engine.execute(args(project))["ok"]
    assert not (project / MANIFEST).exists()
    first = engine.execute(args(project, "--apply"))
    assert first["verification"] == "protocol_self_check"
    assert first["client_connection_verified"] is False
    assert read(project / MANIFEST) == {"name": "yy-flow"}
    config = read(project / CONFIG)
    assert list(config["mcpServers"]) == ["yy-flow"]
    entry = config["mcpServers"]["yy-flow"]
    assert Path(entry["args"][0]) == REPO / "scripts/mcp_server.py"
    registry = read(Path(entry["args"][-1]))
    assert registry["projects"][0]["write_enabled"] is False
    assert list(registry["local_principals"][0]["grants"].values()) == [["read"]]
    assert not (project / LEGACY).exists()
    before = snapshot(project / ".agents/plugins")
    assert engine.execute(args(project, "--apply"))["ok"]
    assert snapshot(project / ".agents/plugins") == before


def test_unmanaged_plugin_is_not_adopted(project):
    folder = project / PLUGIN
    folder.mkdir(parents=True)
    (folder / "plugin.json").write_text('{"name":"yy-flow"}', encoding="utf-8")
    before = snapshot(folder)
    with pytest.raises(OnboardError) as failure:
        engine.execute(args(project, "--apply"))
    assert failure.value.code == "CONFLICT"
    assert snapshot(folder) == before
    assert not (project / ".yy-flow").exists()


@pytest.mark.parametrize("target", [MANIFEST, CONFIG])
@pytest.mark.parametrize("action", ["apply", "remove"])
def test_user_modified_plugin_is_preserved(project, target, action):
    assert engine.execute(args(project, "--apply"))["ok"]
    path = project / target
    value = read(path)
    if target == MANIFEST:
        value["disabled"] = True
    else:
        value["mcpServers"]["yy-flow"]["args"].append("--user-change")
    path.write_text(json.dumps(value), encoding="utf-8")
    before = snapshot(project / ".agents/plugins")
    extra = ["--apply"] + (["--remove"] if action == "remove" else [])
    with pytest.raises(OnboardError) as failure:
        engine.execute(args(project, *extra))
    assert failure.value.code == "CONFLICT"
    assert snapshot(project / ".agents/plugins") == before


def legacy_install(project):
    assert engine.execute(args(project, "--apply"))["ok"]
    old = project / LEGACY
    config = read(project / CONFIG)
    config["mcpServers"]["third-party"] = {"command": "node"}
    old.write_text(json.dumps(config), encoding="utf-8")
    state_path = Path(txn.state_path(str(project / ".yy-flow")))
    state = read(state_path)
    state["files"].pop(CONFIG)
    state["files"].pop(MANIFEST)
    state["files"][LEGACY] = {"sha256": sha256_bytes(old.read_bytes()), "role": "config"}
    (project / CONFIG).unlink()
    (project / MANIFEST).unlink()
    (project / PLUGIN).rmdir()
    state_path.write_text(json.dumps(state), encoding="utf-8")


def test_managed_legacy_migrates_without_duplicate_server(project):
    legacy_install(project)
    assert engine.execute(args(project, "--apply"))["ok"]
    assert list(read(project / LEGACY)["mcpServers"]) == ["third-party"]
    assert list(read(project / CONFIG)["mcpServers"]) == ["yy-flow"]
    assert engine.execute(args(project, "--apply"))["ok"]


def test_changed_legacy_entry_blocks_migration(project):
    legacy_install(project)
    old = read(project / LEGACY)
    old["mcpServers"]["yy-flow"]["command"] = "user-python"
    (project / LEGACY).write_text(json.dumps(old), encoding="utf-8")
    before = (project / LEGACY).read_bytes()
    with pytest.raises(OnboardError) as failure:
        engine.execute(args(project, "--apply"))
    assert failure.value.code == "CONFLICT"
    assert (project / LEGACY).read_bytes() == before
    assert not (project / MANIFEST).exists()


def test_remove_legacy_install_without_creating_plugin(project):
    legacy_install(project)
    assert engine.execute(args(project, "--remove", "--apply"))["ok"]
    assert read(project / LEGACY)["mcpServers"] == {"third-party": {"command": "node"}}
    assert not (project / MANIFEST).exists()


def test_unmanaged_legacy_name_is_not_silently_duplicated(project):
    (project / ".agents").mkdir()
    old = project / LEGACY
    old.write_text('{"mcpServers":{"yy-flow":{"command":"user-server"}}}', encoding="utf-8")
    before = old.read_bytes()
    with pytest.raises(OnboardError) as failure:
        engine.execute(args(project, "--apply"))
    assert failure.value.code == "CONFLICT"
    assert old.read_bytes() == before
    assert not (project / MANIFEST).exists()


@pytest.mark.parametrize("redirect", [".agents/plugins", PLUGIN])
@pytest.mark.parametrize("action", ["apply", "remove"])
def test_plugin_directory_cannot_escape_project(project, tmp_path, redirect, action):
    if action == "remove":
        assert engine.execute(args(project, "--apply"))["ok"]
    outside = tmp_path / "outside"
    outside.mkdir()
    destination = project / redirect
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.exists():
        destination.rename(destination.with_name(destination.name + "-saved"))
    if os.name == "nt":
        import _winapi
        _winapi.CreateJunction(str(outside), str(destination))
    else:
        destination.symlink_to(outside, target_is_directory=True)
    with pytest.raises(OnboardError) as failure:
        engine.execute(args(project, "--apply", *(["--remove"] if action == "remove" else [])))
    assert failure.value.code == "PATH"
    assert list(outside.iterdir()) == []


def test_remove_keeps_third_party_and_inert_manifest(project):
    assert engine.execute(args(project, "--apply"))["ok"]
    config = read(project / CONFIG)
    config["mcpServers"]["third-party"] = {"command": "node"}
    (project / CONFIG).write_text(json.dumps(config), encoding="utf-8")
    assert engine.execute(args(project, "--remove", "--apply"))["ok"]
    assert read(project / CONFIG)["mcpServers"] == {"third-party": {"command": "node"}}
    assert read(project / MANIFEST) == {"name": "yy-flow"}
    assert read(project / ".yy-flow/user_data/board.json") == []


def test_unselected_client_does_not_change_plugin(project):
    assert engine.execute(args(project, "--apply"))["ok"]
    before = snapshot(project / ".agents/plugins")
    assert engine.execute(args(project, "--apply", clients="cursor"))["ok"]
    assert snapshot(project / ".agents/plugins") == before


def test_partial_plugin_write_can_be_recovered(project, monkeypatch):
    legacy_install(project)
    before = (project / LEGACY).read_bytes()
    write = txn.atomic_write

    def fail_manifest(path, data, **kwargs):
        if Path(path) == project / MANIFEST:
            raise OSError("injected manifest failure")
        return write(path, data, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(txn, "atomic_write", fail_manifest)
        with pytest.raises(OnboardError):
            engine.execute(args(project, "--apply"))
    assert engine.execute(args(project, "--recover", "--apply"))["ok"]
    assert (project / LEGACY).read_bytes() == before
    assert not (project / CONFIG).exists()
    assert engine.execute(args(project, "--apply"))["ok"]


def test_active_plugin_self_check_failure_rolls_back_before_legacy_restore(project, monkeypatch):
    legacy_install(project)
    before = (project / LEGACY).read_bytes()

    def failed_check(*unused):
        assert (project / MANIFEST).is_file()
        assert "yy-flow" not in read(project / LEGACY)["mcpServers"]
        raise OnboardError("SELF_CHECK", "injected after manifest activation")

    with monkeypatch.context() as patch:
        patch.setattr(engine, "run_self_check", failed_check)
        with pytest.raises(OnboardError, match="injected"):
            engine.execute(args(project, "--apply"))
    write = txn.atomic_write
    observed = []

    def restore(path, data, **kwargs):
        if Path(path) == project / LEGACY:
            assert not (project / MANIFEST).exists()
            observed.append(True)
        return write(path, data, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(txn, "atomic_write", restore)
        assert engine.execute(args(project, "--recover", "--apply"))["ok"]
    assert observed
    assert (project / LEGACY).read_bytes() == before
    assert not (project / MANIFEST).exists()
    assert not (project / CONFIG).exists()
    assert engine.execute(args(project, "--apply"))["ok"]
