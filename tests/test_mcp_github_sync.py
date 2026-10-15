# -*- coding: utf-8 -*-
"""4E 同步回归。模拟传输默认运行；真实沙箱仅显式选择后只读运行。"""

from __future__ import annotations

import json
import os
import re
import urllib.request
from pathlib import Path

import pytest

from scripts._lib.connectors.github_projects_sync import (
    GithubResult,
    HttpGithubTransport,
    _ARCHIVE_QUERY,
    _CREATE_QUERY,
    _ITEMS_QUERY,
    _STATUS_FIELDS_QUERY,
    _UNARCHIVE_QUERY,
    _UPDATE_QUERY,
    _error_code,
    run_sync,
)
from scripts._lib.mcp.registry import ProjectRecord, load_registry
from scripts.mcp_server import main

ROOT = Path(__file__).resolve().parents[1]
SYNC_SOURCE = ROOT / "scripts" / "_lib" / "connectors" / "github_projects_sync.py"


class FakeGithubTransport:
    def __init__(self, pages: dict, mutations: list | None = None, status_options: list | None = None) -> None:
        self.pages = pages
        self.mutations = list(mutations or [])
        self.calls = []
        self._mutation_index = 0
        if status_options is None:
            status_options = [
                ("OPT_TODO", "待开始"),
                ("OPT_DOING", "进行中"),
                ("OPT_DONE", "已完成"),
                ("OPT_ACCEPTED", "已验收"),
            ]
        self.status_options = status_options

    def query(self, query: str, variables: dict) -> GithubResult:
        self.calls.append((query, dict(variables)))
        if "ProjectV2SingleSelectField" in query:
            return GithubResult(
                data={
                    "node": {
                        "fields": {
                            "nodes": [
                                {
                                    "id": "FIELD_STATUS",
                                    "name": "Status",
                                    "options": [
                                        {"id": option_id, "name": name}
                                        for option_id, name in self.status_options
                                    ],
                                }
                            ]
                        }
                    }
                },
                http_status=200,
            )
        if query.lstrip().startswith("mutation"):
            if self._mutation_index < len(self.mutations):
                result = self.mutations[self._mutation_index]
                self._mutation_index += 1
                return result
            return GithubResult(
                data={"addProjectV2DraftIssue": {"projectItem": {"id": "PVTI"}}}
                if "addProjectV2DraftIssue" in query
                else {"updateProjectV2ItemFieldValue": {"projectV2Item": {"id": variables.get("itemId", "PVTI")}}},
                http_status=200,
            )
        after = variables.get("after") or ""
        page = self.pages.get(after)
        if page is None:
            return GithubResult(data=None, http_status=500)
        return page


class BoomTransport:
    def query(self, query: str, variables: dict) -> GithubResult:
        raise AssertionError("不应访问传输")


def _page(nodes: list, has_next: bool = False, cursor: str = "", events: list | None = None) -> GithubResult:
    data = {
        "node": {
            "items": {
                "pageInfo": {"hasNextPage": has_next, "endCursor": cursor},
                "nodes": nodes,
            }
        }
    }
    if events is not None:
        data["events"] = events
    return GithubResult(
        data=data,
        page_info={"hasNextPage": has_next, "endCursor": cursor},
        http_status=200,
    )


def _node(item_id: str, task_id: str, title: str, body: str = "正文", status: str = "进行中", login: str = "", archived: bool = False) -> dict:
    node = {
        "id": item_id,
        "taskId": task_id,
        "updatedAt": "2026-10-13T00:00:00Z",
        "isArchived": archived,
        "content": {"title": title, "body": body},
        "fieldValues": {
            "nodes": [
                {"name": status, "field": {"name": "Status"}},
                {"text": task_id, "field": {"name": "task_id"}},
            ]
        },
    }
    if login:
        node["actors"] = [{"login": login}]
    return node


def _task(task_id: str = "T0001", name: str = "名称", status: str = "进行中", assignee: str = "李开发", body: str = "正文", **extra) -> dict:
    item = {
        "id": task_id,
        "name": name,
        "status": status,
        "assignee": assignee,
        "remarks": body,
        "type": "A",
        "profile_id": "standard",
        "acceptance_criteria": "原验收标准",
    }
    item.update(extra)
    return item


def _project(tmp: Path, tasks: list, write_enabled: bool = False) -> tuple[ProjectRecord, Path]:
    root = tmp / "data"
    board = root / "user_data" / "board.json"
    evidence = root / "evidence"
    board.parent.mkdir(parents=True, exist_ok=True)
    evidence.mkdir(parents=True, exist_ok=True)
    board.write_text(json.dumps(tasks, ensure_ascii=False), encoding="utf-8")
    project = ProjectRecord(
        project_id="alpha",
        display_name="Alpha",
        org_id="org-1",
        data_root=str(root),
        board_file=str(board),
        evidence_root=str(evidence),
        board_kind="github",
        github_project_id="PVT_test",
        write_enabled=write_enabled,
        identity_map={"octocat": "李开发"},
    )
    return project, board


def _mutations(transport: FakeGithubTransport) -> list:
    return [query for query, _variables in transport.calls if query.lstrip().startswith("mutation")]


def _ledger(project: ProjectRecord) -> dict:
    path = Path(project.data_root) / "mcp_github_sync" / "ledger.json"
    return json.loads(path.read_text(encoding="utf-8"))


def _outcomes(report) -> list:
    return [row.get("outcome") for row in report.task_results]


def test_failure_result_is_not_an_empty_object() -> None:
    failed = GithubResult(data=None, http_status=403, graphql_errors=({"code": "FORBIDDEN"},))
    assert failed.ok is False
    assert failed.data is None
    assert GithubResult(data={}).ok is False


def test_sync_source_does_not_shell_out_or_delete_items() -> None:
    text = SYNC_SOURCE.read_text(encoding="utf-8")
    assert "subprocess" not in text
    assert "os.system" not in text
    for query in (
        _ITEMS_QUERY,
        _CREATE_QUERY,
        _UPDATE_QUERY,
        _ARCHIVE_QUERY,
        _UNARCHIVE_QUERY,
        _STATUS_FIELDS_QUERY,
    ):
        assert "deleteProjectV2Item" not in query
        counts: dict[str, int] = {}
        for name in re.findall(r"\$([A-Za-z_][A-Za-z0-9_]*)", query):
            counts[name] = counts.get(name, 0) + 1
        assert counts
        assert all(count >= 2 for count in counts.values())
        assert "$taskId" not in query
        assert "fieldId: $status" not in query
        assert "singleSelectOptionId: $status" not in query


def test_more_than_one_page_is_not_treated_as_complete(tmp_path: Path) -> None:
    project, _board = _project(tmp_path, [_task("T0001", name="甲"), _task("T0002", name="乙", status="待开始")])
    transport = FakeGithubTransport(
        {
            "": _page([_node("PVTI_1", "T0001", "甲", status="待开始")], has_next=True, cursor="cursor-2"),
            "cursor-2": _page([_node("PVTI_2", "T0002", "乙")]),
        }
    )
    first = run_sync(project, mode="read-only", transport=transport, page_budget=1, environ={})
    assert first.ok is False
    assert first.partial is True
    assert first.reason == "PARTIAL"
    assert first.cursor == "cursor-2"
    assert first.network_writes == 0
    assert _mutations(transport) == []
    assert "first: 50" in transport.calls[0][0]
    assert transport.calls[0][1].get("first", 50) == 50
    stored = _ledger(project)
    assert stored["cursor"] == "cursor-2"
    assert "T0001" in stored["tasks"]
    assert "T0002" not in stored["tasks"]
    second = run_sync(project, mode="read-only", transport=transport, page_budget=40, environ={})
    assert "T0002" in _ledger(project)["tasks"]
    assert second.network_writes == 0


def test_missing_fields_are_not_a_full_success(tmp_path: Path) -> None:
    project, _board = _project(tmp_path, [_task()])
    broken = {"id": "PVTI_1", "taskId": "T0001"}
    transport = FakeGithubTransport({"": _page([broken])})
    report = run_sync(project, mode="read-only", transport=transport, environ={})
    assert report.ok is False
    assert report.reason == "FIELD_MISSING"
    assert report.network_writes == 0


def test_permission_error_is_not_an_empty_project(tmp_path: Path) -> None:
    project, _board = _project(tmp_path, [_task()])
    transport = FakeGithubTransport({"": GithubResult(data=None, http_status=403)})
    report = run_sync(project, mode="read-only", transport=transport, environ={})
    assert report.ok is False
    assert report.reason == "FORBIDDEN"
    assert report.public_dict()["ok"] is False


def test_rate_limit_keeps_checkpoint_and_caps_backoff(tmp_path: Path) -> None:
    project, _board = _project(tmp_path, [_task()])
    directory = Path(project.data_root) / "mcp_github_sync"
    directory.mkdir(parents=True)
    directory.joinpath("ledger.json").write_text(
        json.dumps(
            {
                "cursor": "keep-cursor",
                "marker": "keep",
                "tasks": {"T0001": {"item_id": "PVTI_1"}},
                "deliveries": {},
                "actions": [
                    {
                        "key": "create:T0001",
                        "task_id": "T0001",
                        "op": "create",
                        "status": "applied",
                        "item_id": "PVTI_1",
                        "delivery_id": "create:T0001",
                    }
                ],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    class Limited:
        def __init__(self) -> None:
            self.calls = []

        def query(self, query: str, variables: dict) -> GithubResult:
            self.calls.append((query, dict(variables)))
            return GithubResult(data=None, http_status=429, rate_limit_reset_at=1_500.0)

    transport = Limited()
    waits = []
    report = run_sync(
        project,
        mode="read-only",
        transport=transport,
        environ={},
        now=lambda: 1_000.0,
        sleep=waits.append,
    )
    assert report.reason == "RATE_LIMITED"
    assert report.ok is False
    assert report.network_writes == 0
    assert waits == [60.0, 60.0, 60.0]
    stored = _ledger(project)
    assert stored["cursor"] == "keep-cursor"
    assert stored["marker"] == "keep"
    assert stored["actions"][0]["status"] == "applied"


def test_duplicate_delivery_is_not_applied_twice(tmp_path: Path) -> None:
    project, _board = _project(tmp_path, [_task(name="名称", status="进行中")], write_enabled=True)
    event = {
        "delivery_id": "delivery-1",
        "external_updated_at": "2026-10-13T03:00:00Z",
        "task_id": "T0001",
        "item_id": "PVTI_1",
        "title": "名称",
        "body": "正文",
        "status": "待开始",
    }
    transport = FakeGithubTransport({"": _page([], events=[event])})
    first = run_sync(project, mode="write", transport=transport, environ={})
    assert any(row.get("outcome") == "applied" for row in first.event_outcomes)
    created = len(_mutations(transport))
    assert created == 1
    second = run_sync(project, mode="write", transport=transport, environ={})
    assert any(row.get("outcome") == "duplicate" for row in second.event_outcomes)
    assert second.projection_writes == 0
    assert len(_mutations(transport)) == created


def test_out_of_order_newer_event_wins_and_older_is_stale(tmp_path: Path) -> None:
    project, _board = _project(tmp_path, [_task(name="新标题")])
    older = {
        "delivery_id": "old",
        "external_updated_at": "2026-10-13T01:00:00Z",
        "task_id": "T0001",
        "item_id": "PVTI_1",
        "title": "旧标题",
        "body": "正文",
        "status": "进行中",
    }
    newer = {
        "delivery_id": "new",
        "external_updated_at": "2026-10-13T02:00:00Z",
        "task_id": "T0001",
        "item_id": "PVTI_1",
        "title": "新标题",
        "body": "正文",
        "status": "进行中",
    }
    transport = FakeGithubTransport({"": _page([], events=[older, newer])})
    report = run_sync(project, mode="read-only", transport=transport, environ={})
    outcomes = [row.get("outcome") for row in report.event_outcomes]
    assert "applied" in outcomes
    assert "stale" in outcomes
    assert _ledger(project)["tasks"]["T0001"]["external"]["title"] == "新标题"
    oldest = dict(older)
    oldest["delivery_id"] = "oldest"
    oldest["external_updated_at"] = "2026-10-12T00:00:00Z"
    oldest["title"] = "更旧"
    again = FakeGithubTransport({"": _page([], events=[oldest])})
    follow = run_sync(project, mode="read-only", transport=again, environ={})
    assert any(row.get("outcome") == "stale" for row in follow.event_outcomes)
    assert _ledger(project)["tasks"]["T0001"]["external"]["title"] == "新标题"


def test_both_sides_changed_is_conflict_without_merge(tmp_path: Path) -> None:
    project, board = _project(tmp_path, [_task(name="甲")], write_enabled=True)
    transport = FakeGithubTransport({"": _page([_node("PVTI_1", "T0001", "甲")])})
    first = run_sync(project, mode="read-only", transport=transport, environ={})
    assert first.ok is True
    board.write_text(json.dumps([_task(name="乙")], ensure_ascii=False), encoding="utf-8")
    changed = FakeGithubTransport({"": _page([_node("PVTI_1", "T0001", "丙")])})
    second = run_sync(project, mode="write", transport=changed, environ={})
    assert second.ok is False
    assert "CONFLICT" in _outcomes(second)
    assert _mutations(changed) == []
    saved = json.loads(board.read_text(encoding="utf-8"))
    assert saved[0]["name"] == "乙"
    assert saved[0]["status"] == "进行中"
    assert saved[0]["profile_id"] == "standard"
    assert saved[0]["acceptance_criteria"] == "原验收标准"


def test_external_title_change_stops_later_writes(tmp_path: Path) -> None:
    project, board = _project(tmp_path, [_task(name="甲")], write_enabled=True)
    run_sync(
        project,
        mode="read-only",
        transport=FakeGithubTransport({"": _page([_node("PVTI_1", "T0001", "甲")])}),
        environ={},
    )
    original = board.read_bytes()
    changed = FakeGithubTransport({"": _page([_node("PVTI_1", "T0001", "外部新标题")])})
    report = run_sync(project, mode="write", transport=changed, environ={})
    assert "CONTRACT_CHANGED" in _outcomes(report)
    assert _mutations(changed) == []
    assert board.read_bytes() == original
    again = FakeGithubTransport({"": _page([_node("PVTI_1", "T0001", "外部新标题")])})
    follow = run_sync(project, mode="write", transport=again, environ={})
    assert "CONTRACT_CHANGED" in _outcomes(follow)
    assert _mutations(again) == []
    assert board.read_bytes() == original


def test_partial_write_resumes_without_recreating_applied(tmp_path: Path) -> None:
    tasks = [
        _task("T0001", name="甲", status="进行中"),
        _task("T0002", name="乙", status="待开始"),
    ]
    project, board = _project(tmp_path, tasks, write_enabled=True)
    original = board.read_bytes()
    pages = {"": _page([_node("PVTI_1", "T0001", "甲", status="待开始")])}
    fail = GithubResult(data=None, http_status=500)
    created = GithubResult(
        data={"addProjectV2DraftIssue": {"projectItem": {"id": "PVTI_2"}}},
        http_status=200,
    )
    succeed = GithubResult(
        data={"updateProjectV2ItemFieldValue": {"projectV2Item": {"id": "PVTI_1"}}},
        http_status=200,
    )
    transport = FakeGithubTransport(pages, mutations=[fail, created, succeed])
    first = run_sync(project, mode="write", transport=transport, environ={})
    assert first.ok is False
    assert first.reason == "FAILED"
    assert sum("addProjectV2DraftIssue" in query for query in _mutations(transport)) == 1
    second = run_sync(project, mode="write", transport=transport, environ={})
    assert second.ok is True
    assert sum("addProjectV2DraftIssue" in query for query in _mutations(transport)) == 1
    assert _ledger(project)["actions"][0]["status"] == "applied"
    assert all(action["status"] == "applied" for action in _ledger(project)["actions"])
    assert board.read_bytes() == original


def test_external_acceptance_is_ignored(tmp_path: Path) -> None:
    tasks = [
        _task("T0001", status="进行中"),
        _task("T0002", name="完成项", status="进行中"),
    ]
    project, board = _project(tmp_path, tasks, write_enabled=True)
    transport = FakeGithubTransport(
        {
            "": _page(
                [
                    _node("PVTI_1", "T0001", "名称", status="已验收"),
                    _node("PVTI_2", "T0002", "完成项", status="已完成"),
                ]
            )
        }
    )
    report = run_sync(project, mode="write", transport=transport, environ={})
    assert "EXTERNAL_ACCEPTANCE_IGNORED" in _outcomes(report)
    saved = json.loads(board.read_text(encoding="utf-8"))
    assert saved[0]["status"] == "进行中"
    assert saved[1]["status"] == "进行中"
    assert all("deleteProjectV2Item" not in query for query in _mutations(transport))
    assert any("updateProjectV2ItemFieldValue" in query for query in _mutations(transport))


def test_unmapped_actor_does_not_change_assignee(tmp_path: Path) -> None:
    project, board = _project(tmp_path, [_task(assignee="李开发")], write_enabled=True)
    transport = FakeGithubTransport({"": _page([_node("PVTI_1", "T0001", "名称", login="ghost")])})
    report = run_sync(project, mode="write", transport=transport, environ={})
    assert any(row.get("outcome") == "unmapped_actor" and row.get("login") == "ghost" for row in report.task_results)
    saved = json.loads(board.read_text(encoding="utf-8"))
    assert saved[0]["assignee"] == "李开发"


def test_soft_delete_archives_only_when_item_exists(tmp_path: Path) -> None:
    tasks = [
        _task("T0001", status="进行中", is_deleted=True),
        _task("T0002", status="进行中", is_deleted=True),
        _task("T0003", status="已取消", is_deleted=False),
    ]
    project, board = _project(tmp_path, tasks, write_enabled=True)
    transport = FakeGithubTransport(
        {
            "": _page(
                [
                    _node("PVTI_1", "T0001", "名称"),
                    _node("PVTI_3", "T0003", "取消项", archived=True),
                ]
            )
        }
    )
    report = run_sync(project, mode="write", transport=transport, environ={})
    queries = _mutations(transport)
    assert any("archiveProjectV2Item" in query for query in queries)
    assert any("unarchiveProjectV2Item" in query for query in queries)
    assert all("deleteProjectV2Item" not in query for query in queries)
    for query, variables in transport.calls:
        if "archiveProjectV2Item" in query or "unarchiveProjectV2Item" in query:
            assert variables["itemId"] in {"PVTI_1", "PVTI_3"}
            assert variables["itemId"] not in {"进行中", "已验收", "已完成", "已取消"}
    assert "NO_EXTERNAL_ITEM" in _outcomes(report)
    saved = json.loads(board.read_text(encoding="utf-8"))
    assert [item["status"] for item in saved] == ["进行中", "进行中", "已取消"]
    assert [item["is_deleted"] for item in saved] == [True, True, False]


def test_credentials_absent_does_not_open_network(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(*_args, **_kwargs):
        raise AssertionError("不应访问 api.github.com")

    monkeypatch.setattr(urllib.request, "urlopen", boom)
    result = HttpGithubTransport("", opener=boom).query("query { viewer { login } }", {})
    assert result.ok is False
    assert _error_code(result) == "CREDENTIALS_ABSENT"
    assert result.data is None
    project, _board = _project(tmp_path, [_task()])
    report = run_sync(project, mode="read-only", environ={}, opener=boom)
    assert report.reason == "CREDENTIALS_ABSENT"
    assert report.attempted_network is False
    assert report.network_writes == 0


def test_write_mode_rejected_when_disabled(tmp_path: Path) -> None:
    project, _board = _project(tmp_path, [_task()], write_enabled=False)
    report = run_sync(project, mode="write", transport=BoomTransport(), environ={"GITHUB_TOKEN": "ghp_should_not_leak"})
    assert report.reason == "WRITE_DISABLED"
    assert report.network_writes == 0
    assert "ghp_should_not_leak" not in json.dumps(report.public_dict(), ensure_ascii=False)


def _write_registry(tmp: Path, write_enabled: bool) -> Path:
    root = tmp / "reg-root"
    board = root / "user_data" / "board.json"
    evidence = root / "evidence"
    board.parent.mkdir(parents=True)
    evidence.mkdir()
    board.write_text(json.dumps([_task()], ensure_ascii=False), encoding="utf-8")
    path = tmp / "mcp_registry.json"
    path.write_text(
        json.dumps(
            {
                "server_revision": "4e-test",
                "projects": [
                    {
                        "project_id": "alpha",
                        "display_name": "Alpha",
                        "org_id": "org-1",
                        "data_root": str(root),
                        "board_file": str(board),
                        "evidence_root": str(evidence),
                        "board_kind": "github",
                        "github_project_id": "PVT_test",
                        "write_enabled": write_enabled,
                        "identity_map": {"octocat": "李开发"},
                    }
                ],
                "local_principals": [],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    loaded = load_registry(str(path))
    assert loaded.get_project("alpha") is not None
    return path


def test_cli_rejects_write_without_flag_and_missing_credentials(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    def boom(*_args, **_kwargs):
        raise AssertionError("不应访问 api.github.com")

    monkeypatch.setattr(urllib.request, "urlopen", boom)
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    monkeypatch.setenv("YY_FLOW_MCP_REGISTRY", str(_write_registry(tmp_path / "disabled", write_enabled=False)))
    monkeypatch.setenv("GITHUB_TOKEN", "ghp_cli_should_not_leak")
    code = main(["sync-github", "--project-id", "alpha", "--mode", "write"])
    assert code == 2
    written = json.loads(capsys.readouterr().out)
    assert written["reason"] == "WRITE_DISABLED"
    assert written["network_writes"] == 0
    assert "ghp_cli_should_not_leak" not in json.dumps(written)

    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    monkeypatch.setenv("YY_FLOW_MCP_REGISTRY", str(_write_registry(tmp_path / "absent", write_enabled=False)))
    code = main(["sync-github", "--project-id", "alpha", "--mode", "read-only"])
    assert code == 2
    absent = json.loads(capsys.readouterr().out)
    assert absent["reason"] == "CREDENTIALS_ABSENT"
    assert absent["network_writes"] == 0
    assert absent["attempted_network"] is False
    with pytest.raises(SystemExit):
        main(["sync-github", "--project-id", "alpha", "--mode", "read-only", "--registry", str(tmp_path)])
    assert main(["sync-github", "--project-id", "../alpha", "--mode", "read-only"]) == 2


def test_retry_claims_draft_by_task_marker_without_creating_again(tmp_path: Path) -> None:
    project, board = _project(tmp_path, [_task("T0001", name="甲", body="正文")], write_enabled=True)
    original = board.read_bytes()
    failed = GithubResult(data=None, http_status=500)
    first_transport = FakeGithubTransport({"": _page([])}, mutations=[failed])
    first = run_sync(project, mode="write", transport=first_transport, environ={})
    assert first.ok is False
    creates = [(query, variables) for query, variables in first_transport.calls if "addProjectV2DraftIssue" in query]
    assert len(creates) == 1
    assert "$taskId" not in creates[0][0]
    assert "yy-flow-task-id:T0001" in creates[0][1]["body"]
    assert "yy-flow-delivery-id:create:T0001" in creates[0][1]["body"]
    recovered = {
        "id": "PVTI_LOST",
        "updatedAt": "2026-10-13T00:00:00Z",
        "isArchived": False,
        "content": {"title": "甲", "body": creates[0][1]["body"]},
        "fieldValues": {"nodes": []},
    }
    second_transport = FakeGithubTransport({"": _page([recovered])})
    second = run_sync(project, mode="write", transport=second_transport, environ={})
    assert [query for query, _variables in second_transport.calls if "addProjectV2DraftIssue" in query] == []
    assert _ledger(project)["tasks"]["T0001"]["item_id"] == "PVTI_LOST"
    assert _ledger(project)["actions"][0]["status"] == "applied"
    assert second.ok is True
    assert board.read_bytes() == original


def test_status_rewrite_uses_same_name_option(tmp_path: Path) -> None:
    project, board = _project(tmp_path, [_task(status="进行中")], write_enabled=True)
    original = board.read_bytes()
    transport = FakeGithubTransport(
        {"": _page([_node("PVTI_1", "T0001", "名称", status="待开始")])},
        status_options=[("OPT_DOING", "进行中"), ("OPT_TODO", "待开始")],
    )
    report = run_sync(project, mode="write", transport=transport, environ={})
    updates = [
        (query, variables)
        for query, variables in transport.calls
        if "updateProjectV2ItemFieldValue" in query
    ]
    assert len(updates) == 1
    assert updates[0][1]["fieldId"] == "FIELD_STATUS"
    assert updates[0][1]["optionId"] == "OPT_DOING"
    assert updates[0][1]["fieldId"] != "进行中"
    assert updates[0][1]["optionId"] != "进行中"
    assert report.ok is True
    assert _ledger(project)["actions"][0]["status"] == "applied"
    saved = json.loads(board.read_text(encoding="utf-8"))
    assert saved[0]["status"] == "进行中"
    assert board.read_bytes() == original


def test_missing_status_option_fails_the_round(tmp_path: Path) -> None:
    project, board = _project(tmp_path, [_task(status="进行中")], write_enabled=True)
    original = board.read_bytes()
    transport = FakeGithubTransport(
        {"": _page([_node("PVTI_1", "T0001", "名称", status="待开始")])},
        status_options=[("OPT_TODO", "待开始")],
    )
    report = run_sync(project, mode="write", transport=transport, environ={})
    assert [query for query, _variables in transport.calls if "updateProjectV2ItemFieldValue" in query] == []
    assert report.ok is False
    assert report.network_writes == 0
    assert _ledger(project)["actions"][0]["status"] == "failed"
    saved = json.loads(board.read_text(encoding="utf-8"))
    assert saved[0]["status"] == "进行中"
    assert saved[0]["status"] != "已验收"
    assert saved[0]["status"] != "已完成"
    assert board.read_bytes() == original


@pytest.mark.skipif(
    os.environ.get("YY_FLOW_GITHUB_REAL_SANDBOX") != "https://github.com/users/HLi9725/projects/2",
    reason="默认离线运行；真实沙箱须显式选择并配置本进程凭据，历史实测见连接器说明",
)
def test_real_github_projects_sandbox_read_only(tmp_path: Path) -> None:
    token = os.environ.get("GITHUB_TOKEN", "")
    if not token:
        pytest.fail("Real sandbox credential absent", pytrace=False)
    transport = HttpGithubTransport(token)
    identity = transport.query(
        'query($id:ID!){node(id:$id){... on ProjectV2{id title url}}}',
        {"id":"PVT_kwHOEv0NRs4BnI4K"},
    )
    if not identity.ok:
        pytest.fail("Real sandbox identity query failed", pytrace=False)
    node = (identity.data or {}).get("node") or {}
    assert node.get("url") == "https://github.com/users/HLi9725/projects/2"
    assert node.get("title") == "yy-flow-s4-sandbox"
    project = ProjectRecord(
        project_id="sandbox-probe", display_name="Sandbox", org_id="sandbox",
        data_root=str(tmp_path), board_file=str(tmp_path/"absent-board.json"),
        evidence_root=str(tmp_path/"evidence"), board_kind="github",
        github_project_id="PVT_kwHOEv0NRs4BnI4K", write_enabled=False,
    )
    report = run_sync(project, mode="read-only", transport=transport)
    assert report.ok and report.network_reads >= 1
    assert report.network_writes == 0 and not report.partial
