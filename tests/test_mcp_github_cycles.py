"""Regression cases from real sandbox cycles; no network or credentials."""
import json
from pathlib import Path

import pytest

from scripts._lib.connectors import github_projects_sync as sync
from scripts._lib.core.file_lock import acquire_lock
from test_mcp_github_sync import FakeGithubTransport, _ledger, _node, _page, _project, _task


class StatefulTransport(FakeGithubTransport):
    def __init__(self, status="待开始"):
        super().__init__({})
        self.status = status
        self.archived = False
        self.title = "名称"
        self.writes = []
        self.lose_response = False

    def query(self, query, variables):
        if "items(first:" in query:
            self.calls.append((query, dict(variables)))
            return _page([_node("PVTI_1", "T0001", self.title, status=self.status, archived=self.archived)])
        if not query.startswith("mutation"):
            return super().query(query, variables)
        self.calls.append((query, dict(variables)))
        self.writes.append(query)
        if "unarchiveProjectV2Item" in query:
            self.archived = False
        elif "archiveProjectV2Item" in query:
            self.archived = True
        else:
            self.status = {"OPT_TODO": "待开始", "OPT_DOING": "进行中", "OPT_DONE": "已完成"}[variables["optionId"]]
        if self.lose_response:
            self.lose_response = False
            return sync.GithubResult(data=None, http_status=500)
        return sync.GithubResult(data={"ack": {"id": "PVTI_1"}}, http_status=200)


def write_task(board, status="进行中", deleted=False):
    board.write_text(json.dumps([_task(status=status, is_deleted=deleted)], ensure_ascii=False), encoding="utf-8")


def test_successful_projection_advances_only_status_baseline(tmp_path):
    project, board = _project(tmp_path, [_task(status="进行中")], write_enabled=True)
    remote = StatefulTransport()
    assert sync.run_sync(project, mode="write", transport=remote).ok
    assert remote.status == "进行中"
    baseline = _ledger(project)["tasks"]["T0001"]["baseline"]
    assert baseline["gh"]["status"] == baseline["yy"]["status"] == "进行中"
    write_task(board, "已完成")
    report = sync.run_sync(project, mode="write", transport=remote)
    assert report.ok and remote.status == "已完成"
    assert len(remote.writes) == 2


def test_status_can_cycle_and_identical_replay_is_idle(tmp_path):
    project, board = _project(tmp_path, [_task(status="待开始")], write_enabled=True)
    remote = StatefulTransport()
    for index, status in enumerate(["进行中", "已完成", "进行中", "待开始", "已完成"], 1):
        write_task(board, status)
        assert sync.run_sync(project, mode="write", transport=remote).ok
        assert remote.status == status
        assert len(remote.writes) == index
        assert sync.run_sync(project, mode="write", transport=remote).ok
        assert len(remote.writes) == index
    keys = [action["key"] for action in _ledger(project)["actions"]]
    assert len(keys) == len(set(keys)) == 5


def test_archive_restore_three_cycles_not_swallowed_by_applied(tmp_path):
    project, board = _project(tmp_path, [_task(status="待开始")], write_enabled=True)
    remote = StatefulTransport()
    for index, deleted in enumerate([True, False] * 3, 1):
        write_task(board, "待开始", deleted)
        assert sync.run_sync(project, mode="write", transport=remote).ok
        assert remote.archived is deleted
        assert sync.run_sync(project, mode="write", transport=remote).ok
        assert len(remote.writes) == index


@pytest.mark.parametrize("operation", ["project", "archive", "unarchive"])
def test_remote_applied_but_response_lost_is_reconciled(tmp_path, operation):
    project, board = _project(tmp_path, [_task(status="进行中")], write_enabled=True)
    remote = StatefulTransport("进行中" if operation != "project" else "待开始")
    if operation == "archive":
        write_task(board, deleted=True)
    if operation == "unarchive":
        remote.archived = True
    remote.lose_response = True
    assert not sync.run_sync(project, mode="write", transport=remote).ok
    assert sync.run_sync(project, mode="write", transport=remote).ok
    assert len(remote.writes) == 1
    assert _ledger(project)["actions"][0]["status"] == "applied"


def test_write_ahead_survives_post_mutation_ledger_failure(tmp_path, monkeypatch):
    project, _ = _project(tmp_path, [_task()], write_enabled=True)
    remote = StatefulTransport()
    save = sync._save_ledger

    def fail_after_remote(project, ledger):
        if remote.writes:
            raise OSError("injected disk failure")
        return save(project, ledger)

    monkeypatch.setattr(sync, "_save_ledger", fail_after_remote)
    assert sync.run_sync(project, mode="write", transport=remote).reason == "LOCAL_STATE_INVALID"
    assert _ledger(project)["actions"][0]["status"] == "pending"
    monkeypatch.setattr(sync, "_save_ledger", save)
    assert sync.run_sync(project, mode="write", transport=remote).ok
    assert len(remote.writes) == 1


def test_pending_action_cannot_bypass_new_contract_conflict(tmp_path):
    project, board = _project(tmp_path, [_task()], write_enabled=True)
    remote = StatefulTransport()
    remote.lose_response = True
    assert not sync.run_sync(project, mode="write", transport=remote).ok
    write_task(board, "已完成")
    remote.title = "外部修改的需求"
    report = sync.run_sync(project, mode="write", transport=remote)
    assert not report.ok and report.reason == "CONTRACT_CHANGED"
    assert len(remote.writes) == 1


def test_legacy_applied_status_receipt_is_corroborated_without_reset(tmp_path):
    project, board = _project(tmp_path, [_task(status="待开始")], write_enabled=True)
    remote = StatefulTransport("Todo")
    sync.run_sync(project, mode="read-only", transport=remote)
    stored = _ledger(project)
    stored["actions"] = [{"key":"project:T0001", "task_id":"T0001", "op":"project", "status":"applied", "item_id":"PVTI_1", "delivery_id":"project:T0001"}]
    path = Path(project.data_root) / "mcp_github_sync/ledger.json"
    path.write_text(json.dumps(stored), encoding="utf-8")
    remote.status = "待开始"
    write_task(board, "进行中")
    assert sync.run_sync(project, mode="write", transport=remote).ok
    assert remote.status == "进行中"
    assert len(_ledger(project)["actions"]) == 2


@pytest.mark.parametrize("raw", ["broken", "[]", '{"tasks": [], "actions": [], "deliveries": {}}', '{"actions":[null]}', '{"tasks":{"T0001":null}}'])
def test_corrupt_ledger_fails_closed_without_network(tmp_path, raw):
    project, _ = _project(tmp_path, [_task()], write_enabled=True)
    path = Path(project.data_root) / "mcp_github_sync/ledger.json"
    path.parent.mkdir(parents=True)
    path.write_text(raw, encoding="utf-8")
    remote = StatefulTransport()
    assert sync.run_sync(project, mode="write", transport=remote).reason == "LOCAL_STATE_INVALID"
    assert remote.calls == []
    assert path.read_text(encoding="utf-8") == raw


def test_same_project_lock_prevents_concurrent_mutations(tmp_path):
    project, _ = _project(tmp_path, [_task()], write_enabled=True)
    remote = StatefulTransport()
    with acquire_lock(str(Path(project.data_root) / "mcp_github_sync/sync.lock")):
        assert sync.run_sync(project, mode="write", transport=remote).reason == "LOCK_BUSY"
    assert remote.calls == []


def test_page_resume_preserves_first_page_content_for_full_diff(tmp_path):
    project, _ = _project(tmp_path, [_task(name="甲"), _task("T0002", name="乙")])
    remote = FakeGithubTransport({"":_page([_node("PVTI_1","T0001","甲")], True, "next"), "next":_page([_node("PVTI_2","T0002","乙")])})
    assert sync.run_sync(project, transport=remote, page_budget=1).reason == "PARTIAL"
    assert sync.run_sync(project, transport=remote).ok
    stored = _ledger(project)
    assert all(stored["tasks"][task]["baseline"]["gh"]["title"] == title for task, title in [("T0001","甲"),("T0002","乙")])
    assert "pending_items" not in stored


def test_reset_header_does_not_turn_forbidden_into_rate_limit():
    result = sync.GithubResult(data=None, http_status=403, rate_limit_reset_at=9999999999)
    assert not sync._is_rate_limited(result)


def test_lost_create_not_yet_visible_never_creates_a_duplicate(tmp_path):
    project, _ = _project(tmp_path, [_task()], write_enabled=True)
    remote = FakeGithubTransport({"": _page([])}, mutations=[sync.GithubResult(data=None, http_status=500)])
    assert not sync.run_sync(project, mode="write", transport=remote).ok
    assert not sync.run_sync(project, mode="write", transport=remote).ok
    assert len([q for q, _ in remote.calls if "addProjectV2DraftIssue" in q]) == 1
    assert _ledger(project)["actions"][0]["attempted"] is True


@pytest.mark.parametrize("status,code", [(401,"UNAUTHORIZED"),(403,"FORBIDDEN"),(200,"GRAPHQL_VALIDATION_FAILED")])
def test_definitive_create_rejection_can_retry_after_authorization(tmp_path, status, code):
    project, _ = _project(tmp_path, [_task()], write_enabled=True)
    remote = FakeGithubTransport({"":_page([])}, mutations=[sync.GithubResult(data=None, http_status=status, graphql_errors=({"code":code},))])
    assert not sync.run_sync(project, mode="write", transport=remote).ok
    assert _ledger(project)["actions"][0]["attempted"] is False
    assert sync.run_sync(project, mode="write", transport=remote).ok


def test_local_edit_cannot_renew_ambiguous_create_identity(tmp_path):
    project, board = _project(tmp_path, [_task(status="待开始")], write_enabled=True)
    remote = FakeGithubTransport({"":_page([])}, mutations=[sync.GithubResult(data=None,http_status=500)])
    assert not sync.run_sync(project, mode="write", transport=remote).ok
    write_task(board, "进行中")
    report = sync.run_sync(project, mode="write", transport=remote)
    assert not report.ok and "REMOTE_RECONCILIATION_REQUIRED" in report.reasons
    assert len([q for q,_ in remote.calls if "addProjectV2DraftIssue" in q]) == 1
    assert len(_ledger(project)["actions"]) == 1
    assert _ledger(project)["actions"][0]["attempted"] is True
