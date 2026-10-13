# -*- coding: utf-8 -*-
"""4B 受控写入。使用临时目录，不读取仓库看板。"""

from __future__ import annotations

import json
from pathlib import Path

import yaml

from scripts._lib.mcp.schema import TOOL_SCOPE, WRITE_TOOL_NAMES
from scripts._lib.mcp.service import _request_digest
from test_mcp_stdio import MARKER, StdioSession, _assert_clean, _registry

TOKEN_SAMPLE = "TOKEN_SAMPLE_9f3a"
PATH_SAMPLE = "PATH_SAMPLE_9f3a"


def _config(data_root: Path, board: Path) -> Path:
    path = data_root / "user_data" / "workflow.config.yaml"
    path.parent.mkdir(parents=True, exist_ok=True)
    document = {
        "project": {"name": "sample", "version": "1", "root_dir": "."},
        "board": {
            "provider": "local",
            "board_file": str(board.resolve()),
            "fields": {
                "task_id": "id",
                "task_name": "name",
                "status": "status",
                "assignee": "assignee",
                "owner": "owner",
                "remarks": "remarks",
                "end_time": "end_date",
                "handler": "handler",
            },
        },
        "roles": {
            "PM": {"name": "pm", "max_parallel_tasks": 3, "can_self_claim": True},
            "DEV": {"name": "dev", "max_parallel_tasks": 3, "can_self_claim": True},
        },
        "paths": {
            "docs_root": "docs",
            "dev_reports_dir": "docs",
            "review_reports_dir": "docs",
            "qa_reports_dir": "docs",
        },
        "duplicate_check": {"enabled": False},
    }
    path.write_text(yaml.safe_dump(document, allow_unicode=True), encoding="utf-8")
    return path


def _prepare(tmp: Path, grants: dict, role: str = "PM") -> tuple[Path, Path]:
    registry = _registry(tmp, grants)
    data_root = tmp / MARKER / "alpha"
    board = data_root / "user_data" / "board.json"
    board.write_text("[]", encoding="utf-8")
    policy = data_root / "workflow_policies" / "T0001.json"
    if policy.exists():
        policy.unlink()
    config = _config(data_root, board)
    document = json.loads(registry.read_text(encoding="utf-8"))
    document["projects"][0]["board_config"] = str(config.resolve())
    document["local_principals"][0]["role"] = role
    registry.write_text(json.dumps(document, ensure_ascii=False), encoding="utf-8")
    return registry, board


def _ids(session: StdioSession) -> list[str]:
    found = []
    cursor = ""
    while True:
        arguments = {"project_id": "alpha", "limit": 50}
        if cursor:
            arguments["cursor"] = cursor
        result = session.call("list_tasks", arguments)
        assert result["isError"] is False
        body = result["structuredContent"]
        found.extend(item["id"] for item in body["items"])
        if not body["has_more"]:
            return found
        cursor = body["next_cursor"]


def _create(session: StdioSession, key: str, name: str) -> dict:
    return session.call(
        "create_task",
        {
            "project_id": "alpha",
            "idempotency_key": key,
            "name": name,
            "type": "A",
            "assignee": "李开发",
        },
    )


def test_tool_list_follows_scopes(tmp_path: Path) -> None:
    read_registry, _board = _prepare(tmp_path / "read", {"alpha": ["read"]})
    reader = StdioSession(tmp_path / "read", registry=read_registry)
    try:
        reader.handshake()
        listed = reader.request("tools/list", {})
        names = [item["name"] for item in listed["result"]["tools"]]
        assert names == ["get_project", "get_task", "list_tasks", "get_status"]
        denied = reader.call(
            "create_task",
            {
                "project_id": "alpha",
                "idempotency_key": "denied-key-01",
                "name": "越权建卡",
                "type": "A",
                "assignee": "李开发",
            },
        )
        assert denied["isError"] is True
        assert denied["structuredContent"]["code"] == "FORBIDDEN"
    finally:
        blob = reader.close()
        _assert_clean(blob)

    narrow_registry, _board = _prepare(tmp_path / "narrow", {"alpha": ["task.transition"]})
    narrow = StdioSession(tmp_path / "narrow", registry=narrow_registry)
    try:
        narrow.handshake()
        names = [item["name"] for item in narrow.request("tools/list", {})["result"]["tools"]]
        assert names == ["create_task", "transition_task"]
        assert "get_project" not in names
        assert "submit_evidence" not in names
    finally:
        narrow.close()

    full_registry, _board = _prepare(
        tmp_path / "full",
        {"alpha": ["read", "task.transition", "evidence.submit", "acceptance", "admin"]},
    )
    full = StdioSession(tmp_path / "full", registry=full_registry)
    try:
        full.handshake()
        names = [item["name"] for item in full.request("tools/list", {})["result"]["tools"]]
        assert set(WRITE_TOOL_NAMES).issubset(names)
        assert all(TOOL_SCOPE[name] in {"read", "task.transition", "evidence.submit", "acceptance", "admin"} for name in names)
    finally:
        full.close()


def test_replay_conflict_version_and_fake_pass(tmp_path: Path) -> None:
    registry, board = _prepare(
        tmp_path,
        {"alpha": ["read", "task.transition", "evidence.submit"]},
        role="DEV",
    )
    session = StdioSession(tmp_path, registry=registry)
    try:
        session.handshake()
        created = _create(session, "create-key-01", "写入样例")
        assert created["isError"] is False
        task_id = created["structuredContent"]["task_id"]
        assert created["structuredContent"]["status"] == "待开始"
        replay = _create(session, "create-key-01", "写入样例")
        assert replay["isError"] is False
        assert replay["structuredContent"]["replayed"] is True
        assert replay["structuredContent"]["code"] == "IDEMPOTENCY_REPLAY"
        assert replay["structuredContent"]["task_id"] == task_id
        assert _ids(session) == [task_id]
        conflict = _create(session, "create-key-01", "另一份正文")
        assert conflict["structuredContent"]["code"] == "CONFLICT"
        assert _ids(session) == [task_id]
        current = session.call("get_task", {"project_id": "alpha", "task_id": task_id})
        version = current["structuredContent"]["task_version"]
        rejected = session.call(
            "transition_task",
            {
                "project_id": "alpha",
                "task_id": task_id,
                "idempotency_key": "move-key-0001",
                "expected_version": "0" * 64,
                "from_status": "待开始",
                "to_status": "进行中",
                "assignee": "李开发",
                "task_type": "A",
            },
        )
        assert rejected["structuredContent"]["code"] == "CONFLICT"
        assert rejected["structuredContent"]["details"]["task_version"] == version
        assert session.call("get_task", {"project_id": "alpha", "task_id": task_id})["structuredContent"]["status"] == "待开始"
        uploaded = session.call(
            "submit_evidence",
            {
                "project_id": "alpha",
                "task_id": task_id,
                "idempotency_key": "evidence-key-1",
                "expected_version": version,
                "evidence_type": "task_transition",
                "baseline_commit": "a" * 40,
                "result_commit": "b" * 40,
                "metadata": {"decision": "PASS"},
                "artifacts": [],
            },
        )
        assert uploaded["isError"] is False
        assert uploaded["structuredContent"]["gate_status"] == "NOT_EVALUATED"
        assert session.call("get_task", {"project_id": "alpha", "task_id": task_id})["structuredContent"]["status"] == "待开始"
        acceptance = session.call(
            "transition_task",
            {
                "project_id": "alpha",
                "task_id": task_id,
                "idempotency_key": "accept-key-01",
                "expected_version": version,
                "from_status": "待开始",
                "to_status": "已验收",
                "assignee": "李开发",
                "task_type": "A",
            },
        )
        assert acceptance["structuredContent"]["code"] == "ACCEPTANCE_REQUIRED"
        assert session.call("get_task", {"project_id": "alpha", "task_id": task_id})["structuredContent"]["status"] == "待开始"
        leaked = session.call(
            "create_task",
            {
                "project_id": "alpha",
                "idempotency_key": "token-key-001",
                "name": "令牌探针",
                "type": "A",
                "assignee": "李开发",
                "invocation_token": TOKEN_SAMPLE,
                "path": PATH_SAMPLE,
            },
        )
        assert leaked["isError"] is True
        encoded = json.dumps(leaked, ensure_ascii=False)
        assert TOKEN_SAMPLE not in encoded
        assert PATH_SAMPLE not in encoded
    finally:
        blob = session.close()
        text = blob.decode("utf-8", errors="replace")
        assert TOKEN_SAMPLE not in text
        assert PATH_SAMPLE not in text
        _assert_clean(blob)
    cards = json.loads(board.read_text(encoding="utf-8"))
    assert [card.get("id") for card in cards if card.get("id")] == [task_id]


def test_restart_reconciles_same_key_without_second_card(tmp_path: Path) -> None:
    registry, board = _prepare(tmp_path, {"alpha": ["read", "task.transition"]}, role="DEV")
    first = StdioSession(tmp_path, registry=registry)
    try:
        first.handshake()
        created = _create(first, "restart-key-1", "重启样例")
        assert created["isError"] is False
        task_id = created["structuredContent"]["task_id"]
    finally:
        first.close()
    second = StdioSession(tmp_path, registry=registry)
    try:
        second.handshake()
        replay = _create(second, "restart-key-1", "重启样例")
        assert replay["structuredContent"]["replayed"] is True
        assert replay["structuredContent"]["task_id"] == task_id
        assert _ids(second) == [task_id]
    finally:
        second.close()
    cards = json.loads(board.read_text(encoding="utf-8"))
    assert [card.get("id") for card in cards if card.get("name") == "重启样例"] == [task_id]


def _open(tmp: Path, registry: Path, confirmed: bool = False) -> StdioSession:
    extra = {"YY_FLOW_MCP_ACCEPTANCE_CONFIRMED": "1"} if confirmed else None
    session = StdioSession(tmp, registry=registry, env_extra=extra)
    session.handshake()
    return session


def _version(session: StdioSession, task_id: str, include_deleted: bool = False) -> tuple[str, str]:
    arguments = {"project_id": "alpha", "task_id": task_id}
    if include_deleted:
        arguments["include_deleted"] = True
    result = session.call("get_task", arguments)
    assert result["isError"] is False, result["structuredContent"]
    body = result["structuredContent"]
    return body["task_version"], body["status"]


def _finish_type_b(session: StdioSession, name: str, key: str) -> str:
    created = session.call(
        "create_task",
        {
            "project_id": "alpha",
            "idempotency_key": key,
            "name": name,
            "type": "B",
            "assignee": "李开发",
        },
    )
    assert created["isError"] is False, created["structuredContent"]
    task_id = created["structuredContent"]["task_id"]
    version, status = _version(session, task_id)
    assert status == "待开始"
    moved = session.call(
        "transition_task",
        {
            "project_id": "alpha",
            "task_id": task_id,
            "idempotency_key": key + "-go",
            "expected_version": version,
            "from_status": "待开始",
            "to_status": "进行中",
            "assignee": "李开发",
            "task_type": "B",
        },
    )
    assert moved["isError"] is False, moved["structuredContent"]
    version, status = _version(session, task_id)
    assert status == "进行中"
    done = session.call(
        "transition_task",
        {
            "project_id": "alpha",
            "task_id": task_id,
            "idempotency_key": key + "-done",
            "expected_version": version,
            "from_status": "进行中",
            "to_status": "已完成",
            "assignee": "李开发",
            "task_type": "B",
            "end_time": "2026-10-13 10:00:00",
        },
    )
    assert done["isError"] is False, done["structuredContent"].get("details")
    version, status = _version(session, task_id)
    assert status == "已完成"
    return task_id


def _submit(session: StdioSession, task_id: str, key: str, evidence_type: str, metadata: dict) -> dict:
    version, _status = _version(session, task_id)
    return session.call(
        "submit_evidence",
        {
            "project_id": "alpha",
            "task_id": task_id,
            "idempotency_key": key,
            "expected_version": version,
            "evidence_type": evidence_type,
            "baseline_commit": "a" * 40,
            "result_commit": "b" * 40,
            "metadata": metadata,
            "artifacts": [],
        },
    )


def test_request_acceptance_requires_gate_and_transport_confirmation(tmp_path: Path) -> None:
    """模型参数不能代替传输确认；成功验收必须经过 USER_CONFIRMATION 门禁。"""
    grants = {"alpha": ["read", "task.transition", "evidence.submit", "acceptance"]}
    registry, _board = _prepare(tmp_path, grants, role="DEV")
    plain = _open(tmp_path, registry, confirmed=False)
    try:
        task_id = _finish_type_b(plain, "待验收任务", "accept-create-1")
        user_evidence = _submit(
            plain,
            task_id,
            "user-evidence-1",
            "user_confirmation",
            {"user_source": "explicit_user", "confirmed_at": "2026-10-13T02:00:00Z"},
        )
        assert user_evidence["isError"] is False
        assert user_evidence["structuredContent"]["gate_status"] == "NOT_EVALUATED"
        user_id = user_evidence["structuredContent"]["evidence_id"]
        fake = _submit(
            plain,
            task_id,
            "fake-pass-evid1",
            "task_transition",
            {"decision": "PASS"},
        )
        assert fake["isError"] is False
        fake_id = fake["structuredContent"]["evidence_id"]
        version, status = _version(plain, task_id)
        assert status == "已完成"
        rejected = plain.call(
            "request_acceptance",
            {
                "project_id": "alpha",
                "task_id": task_id,
                "idempotency_key": "accept-try-0001",
                "expected_version": version,
                "evidence_id": user_id,
                "acceptance_confirmed": True,
            },
        )
        assert rejected["isError"] is True
        assert rejected["structuredContent"]["code"] == "ACCEPTANCE_REQUIRED"
        assert _version(plain, task_id)[1] == "已完成"
    finally:
        plain.close()

    confirmed = _open(tmp_path, registry, confirmed=True)
    try:
        version, status = _version(confirmed, task_id)
        assert status == "已完成"
        forged = confirmed.call(
            "request_acceptance",
            {
                "project_id": "alpha",
                "task_id": task_id,
                "idempotency_key": "accept-fake-001",
                "expected_version": version,
                "evidence_id": fake_id,
            },
        )
        assert forged["isError"] is True
        assert forged["structuredContent"]["code"] == "GATE_REJECTED"
        assert forged["structuredContent"]["details"]["reason"] == "EVIDENCE_TYPE"
        assert _version(confirmed, task_id)[1] == "已完成"
        model_only = confirmed.call(
            "request_acceptance",
            {
                "project_id": "alpha",
                "task_id": task_id,
                "idempotency_key": "accept-model-01",
                "expected_version": version,
                "evidence_id": user_id,
                "acceptance_confirmed": True,
            },
        )
        assert model_only["structuredContent"]["code"] == "ACCEPTANCE_REQUIRED"
        assert model_only["structuredContent"]["details"]["reason"] == "CONFIRMATION"
        assert _version(confirmed, task_id)[1] == "已完成"
        server_id = "srv-confirm-9f3a"
        confirm_path = tmp_path / MARKER / "alpha" / "mcp_auth" / "confirmation" / f"{task_id}.json"
        confirm_path.parent.mkdir(parents=True, exist_ok=True)
        confirm_path.write_text(
            json.dumps({"confirmation_request_id": server_id}, ensure_ascii=False),
            encoding="utf-8",
        )
        stale_model = confirmed.call(
            "request_acceptance",
            {
                "project_id": "alpha",
                "task_id": task_id,
                "idempotency_key": "accept-stale-01",
                "expected_version": version,
                "evidence_id": user_id,
            },
        )
        assert stale_model["structuredContent"]["code"] == "GATE_REJECTED"
        assert _version(confirmed, task_id)[1] == "已完成"
        witnessed = _submit(
            confirmed,
            task_id,
            "user-evidence-2",
            "user_confirmation",
            {
                "user_source": "explicit_user",
                "confirmed_at": "2026-10-13T02:00:00Z",
                "confirmation_id": "model-confirm-xyz",
            },
        )
        assert witnessed["isError"] is False, witnessed["structuredContent"]
        witnessed_id = witnessed["structuredContent"]["evidence_id"]
        stored = json.loads((tmp_path / MARKER / "alpha" / "evidence" / f"{witnessed_id}.json").read_text(encoding="utf-8"))
        assert stored["metadata"]["extra"]["confirmation_id"] == server_id
        assert stored["metadata"]["extra"]["confirmation_id"] != "model-confirm-xyz"
        version, status = _version(confirmed, task_id)
        assert status == "已完成"
        accepted = confirmed.call(
            "request_acceptance",
            {
                "project_id": "alpha",
                "task_id": task_id,
                "idempotency_key": "accept-real-001",
                "expected_version": version,
                "evidence_id": witnessed_id,
            },
        )
        assert accepted["isError"] is False, accepted["structuredContent"]
        assert accepted["structuredContent"]["status"] == "已验收"
        assert _version(confirmed, task_id)[1] == "已验收"
        fresh = confirmed.call(
            "create_task",
            {
                "project_id": "alpha",
                "idempotency_key": "still-todo-0001",
                "name": "尚未完成",
                "type": "B",
                "assignee": "李开发",
            },
        )
        fresh_id = fresh["structuredContent"]["task_id"]
        fresh_version, fresh_status = _version(confirmed, fresh_id)
        assert fresh_status == "待开始"
        too_early = confirmed.call(
            "request_acceptance",
            {
                "project_id": "alpha",
                "task_id": fresh_id,
                "idempotency_key": "accept-early-01",
                "expected_version": fresh_version,
                "evidence_id": user_id,
            },
        )
        assert too_early["structuredContent"]["code"] == "ACCEPTANCE_REQUIRED"
        assert _version(confirmed, fresh_id)[1] == "待开始"
    finally:
        confirmed.close()

    denied_registry, _board = _prepare(
        tmp_path / "no-accept",
        {"alpha": ["read", "task.transition", "evidence.submit"]},
        role="DEV",
    )
    denied = _open(tmp_path / "no-accept", denied_registry, confirmed=True)
    try:
        other = denied.call(
            "create_task",
            {
                "project_id": "alpha",
                "idempotency_key": "no-scope-00001",
                "name": "无验收权限",
                "type": "B",
                "assignee": "李开发",
            },
        )
        other_id = other["structuredContent"]["task_id"]
        other_version, _status = _version(denied, other_id)
        forbidden = denied.call(
            "request_acceptance",
            {
                "project_id": "alpha",
                "task_id": other_id,
                "idempotency_key": "no-scope-acc01",
                "expected_version": other_version,
                "evidence_id": "not-used",
            },
        )
        assert forbidden["structuredContent"]["code"] == "FORBIDDEN"
        assert _version(denied, other_id)[1] == "待开始"
    finally:
        denied.close()


def _ledger(tmp: Path) -> Path:
    return tmp / MARKER / "alpha" / "mcp_idempotency" / "alpha.jsonl"


def _expire_pending(
    path: Path,
    principal_id: str,
    key: str,
    digest: str,
    result: dict | None,
    target: dict | None = None,
    tool: str = "create_task",
) -> None:
    record = {
        "principal_id": principal_id,
        "tool": tool,
        "idempotency_key": key,
        "request_sha256": digest,
        "state": "pending",
        "created_at": "2000-01-01T00:00:00Z",
    }
    if result is not None:
        record["result"] = result
    if target is not None:
        record["target"] = target
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n", encoding="utf-8")


def test_expired_pending_reconciles_without_sleep(tmp_path: Path) -> None:
    """过期 pending 走 STDIO 重试：事实已达成则重放，未达成则原键补做一次。"""
    done_root = tmp_path / "done"
    registry, board = _prepare(done_root, {"alpha": ["read", "task.transition"]}, role="DEV")
    created_args = {
        "project_id": "alpha",
        "idempotency_key": "pending-done-01",
        "name": "已经建好",
        "type": "A",
        "assignee": "李开发",
    }
    first = _open(done_root, registry)
    try:
        created = first.call("create_task", created_args)
        assert created["isError"] is False
        task_id = created["structuredContent"]["task_id"]
    finally:
        first.close()
    _expire_pending(
        _ledger(done_root),
        "principal-dev",
        created_args["idempotency_key"],
        _request_digest(created_args),
        {"task_id": task_id, "status": "待开始"},
        target={"task_id": task_id},
    )
    again = _open(done_root, registry)
    try:
        replay = again.call("create_task", created_args)
        assert replay["isError"] is False
        assert replay["structuredContent"]["replayed"] is True
        assert replay["structuredContent"]["task_id"] == task_id
        assert _ids(again) == [task_id]
    finally:
        again.close()
    cards = json.loads(board.read_text(encoding="utf-8"))
    assert [card.get("id") for card in cards if card.get("name") == "已经建好"] == [task_id]

    fresh_root = tmp_path / "fresh"
    fresh_registry, fresh_board = _prepare(fresh_root, {"alpha": ["read", "task.transition"]}, role="DEV")
    missing_args = {
        "project_id": "alpha",
        "idempotency_key": "pending-miss-01",
        "name": "超时后补建",
        "type": "A",
        "assignee": "李开发",
    }
    seeded = _open(fresh_root, fresh_registry)
    try:
        seed = seeded.call(
            "create_task",
            {
                "project_id": "alpha",
                "idempotency_key": "seed-todo-0001",
                "name": "碰巧待开始",
                "type": "A",
                "assignee": "李开发",
            },
        )
        assert seed["isError"] is False
        seed_id = seed["structuredContent"]["task_id"]
    finally:
        seeded.close()
    _expire_pending(
        _ledger(fresh_root),
        "principal-dev",
        missing_args["idempotency_key"],
        _request_digest(missing_args),
        {"status": "待开始"},
    )
    retry = _open(fresh_root, fresh_registry)
    try:
        made = retry.call("create_task", missing_args)
        assert made["structuredContent"]["code"] == "CONFLICT"
        assert made["structuredContent"]["details"]["reason"] == "UNBOUND"
        assert made["structuredContent"].get("replayed") is not True
        assert _ids(retry) == [seed_id]
    finally:
        retry.close()
    cards = json.loads(fresh_board.read_text(encoding="utf-8"))
    assert [card.get("name") for card in cards if card.get("name") == "超时后补建"] == []
    assert [card.get("id") for card in cards if card.get("id") == seed_id] == [seed_id]

    evidence_root = tmp_path / "evidence"
    evidence_registry, _evidence_board = _prepare(
        evidence_root,
        {"alpha": ["read", "task.transition", "evidence.submit"]},
        role="DEV",
    )
    evidence_args_task = {
        "project_id": "alpha",
        "idempotency_key": "evidence-task-01",
        "name": "证据宿主",
        "type": "A",
        "assignee": "李开发",
    }
    host = _open(evidence_root, evidence_registry)
    try:
        made_task = host.call("create_task", evidence_args_task)
        evidence_task = made_task["structuredContent"]["task_id"]
        version, _status = _version(host, evidence_task)
        submit_args = {
            "project_id": "alpha",
            "task_id": evidence_task,
            "idempotency_key": "evidence-key-99",
            "expected_version": version,
            "evidence_type": "task_transition",
            "baseline_commit": "c" * 40,
            "result_commit": "d" * 40,
            "metadata": {"decision": "PASS"},
            "artifacts": [],
        }
        uploaded = host.call("submit_evidence", submit_args)
        assert uploaded["isError"] is False
        evidence_id = uploaded["structuredContent"]["evidence_id"]
    finally:
        host.close()
    _expire_pending(
        _ledger(evidence_root),
        "principal-dev",
        submit_args["idempotency_key"],
        _request_digest(submit_args),
        None,
        target={"evidence_id": evidence_id, "content_hash": "f" * 64},
        tool="submit_evidence",
    )
    retry_evidence = _open(evidence_root, evidence_registry)
    try:
        mismatched = retry_evidence.call("submit_evidence", submit_args)
        assert mismatched["structuredContent"]["code"] == "CONFLICT"
        assert mismatched["structuredContent"]["details"]["reason"] == "EVIDENCE"
        assert mismatched["structuredContent"].get("replayed") is not True
    finally:
        retry_evidence.close()
    ledger_text = _ledger(evidence_root).read_text(encoding="utf-8")
    assert '"state": "completed"' not in ledger_text and '"state":"completed"' not in ledger_text


def test_artifact_bind_retries_same_request_without_conflict(tmp_path: Path) -> None:
    """带非空附件时，绑定后中断再重试仍按证据编号和内容摘要重放。"""
    root = tmp_path / "artifact"
    registry, _board = _prepare(root, {"alpha": ["read", "task.transition", "evidence.submit"]}, role="DEV")
    artifact_hash = "ab" * 32
    session = _open(root, registry)
    try:
        created = session.call(
            "create_task",
            {
                "project_id": "alpha",
                "idempotency_key": "artifact-task-1",
                "name": "附件宿主",
                "type": "A",
                "assignee": "李开发",
            },
        )
        assert created["isError"] is False
        task_id = created["structuredContent"]["task_id"]
        version, _status = _version(session, task_id)
        submit_args = {
            "project_id": "alpha",
            "task_id": task_id,
            "idempotency_key": "artifact-evid-01",
            "expected_version": version,
            "evidence_type": "task_transition",
            "baseline_commit": "e" * 40,
            "result_commit": "f" * 40,
            "metadata": {"note": "with-artifact"},
            "artifacts": [{"relative_path": "notes/readme.txt", "sha256": artifact_hash}],
        }
        uploaded = session.call("submit_evidence", submit_args)
        assert uploaded["isError"] is False, uploaded["structuredContent"]
        evidence_id = uploaded["structuredContent"]["evidence_id"]
        content_hash = uploaded["structuredContent"]["content_hash"]
        request_digest = _request_digest(submit_args)
        ledger_lines = [
            json.loads(line)
            for line in _ledger(root).read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
        bound = [item for item in ledger_lines if item.get("idempotency_key") == submit_args["idempotency_key"]]
        assert bound
        assert all(item.get("request_sha256") == request_digest for item in bound)
        assert all(item.get("request_sha256") != artifact_hash for item in bound)
    finally:
        session.close()
    _expire_pending(
        _ledger(root),
        "principal-dev",
        submit_args["idempotency_key"],
        request_digest,
        None,
        target={"evidence_id": evidence_id, "content_hash": content_hash},
        tool="submit_evidence",
    )
    retry = _open(root, registry)
    try:
        replay = retry.call("submit_evidence", submit_args)
        assert replay["isError"] is False, replay["structuredContent"]
        assert replay["structuredContent"]["code"] != "CONFLICT"
        assert replay["structuredContent"]["replayed"] is True
        assert replay["structuredContent"]["evidence_id"] == evidence_id
        assert replay["structuredContent"]["content_hash"] == content_hash
    finally:
        retry.close()


def test_soft_delete_hides_without_changing_status_and_restore_clears_flag(tmp_path: Path) -> None:
    registry, board = _prepare(
        tmp_path,
        {"alpha": ["read", "task.transition", "admin"]},
        role="DEV",
    )
    session = _open(tmp_path, registry)
    try:
        created = _create(session, "delete-key-0001", "可隐藏任务")
        task_id = created["structuredContent"]["task_id"]
        version, status = _version(session, task_id)
        assert status == "待开始"
        hidden = session.call(
            "soft_delete",
            {
                "project_id": "alpha",
                "task_id": task_id,
                "idempotency_key": "soft-delete-001",
                "expected_version": version,
            },
        )
        assert hidden["isError"] is False, hidden["structuredContent"]
        assert hidden["structuredContent"]["status"] == "待开始"
        assert hidden["structuredContent"]["is_deleted"] is True
        assert task_id not in _ids(session)
        missing = session.call("get_task", {"project_id": "alpha", "task_id": task_id})
        assert missing["structuredContent"]["code"] == "NOT_FOUND"
        listed = session.call(
            "list_tasks",
            {"project_id": "alpha", "include_deleted": True, "limit": 20},
        )
        matched = [item for item in listed["structuredContent"]["items"] if item["id"] == task_id]
        assert len(matched) == 1
        assert matched[0]["status"] == "待开始"
        assert matched[0]["is_deleted"] is True
        deleted_version, deleted_status = _version(session, task_id, include_deleted=True)
        assert deleted_status == "待开始"
        restored = session.call(
            "restore",
            {
                "project_id": "alpha",
                "task_id": task_id,
                "idempotency_key": "restore-key-001",
                "expected_version": deleted_version,
            },
        )
        assert restored["isError"] is False, restored["structuredContent"]
        assert restored["structuredContent"]["status"] == "待开始"
        assert restored["structuredContent"]["is_deleted"] is False
        visible_version, visible_status = _version(session, task_id)
        assert visible_status == "待开始"
        assert task_id in _ids(session)
        assert visible_version
    finally:
        session.close()
    cards = json.loads(board.read_text(encoding="utf-8"))
    card = next(item for item in cards if item.get("id") == task_id)
    assert card["status"] == "待开始"
    assert card["is_deleted"] is False
    assert card.get("restored_at")


def test_concurrent_project_roots_stay_isolated() -> None:
    import os
    import threading
    import time

    from scripts import paths
    from scripts._lib.mcp.registry import ProjectRecord
    from scripts._lib.mcp.service import _call_in_project

    def project(name: str) -> ProjectRecord:
        return ProjectRecord(
            project_id=name,
            display_name=name,
            org_id="org-1",
            data_root=name + "-root",
            board_file=name + "-board",
            evidence_root=name + "-evidence",
            board_kind="offline",
        )

    before = os.environ.get("YY_FLOW_PROJECT_ROOT")
    barrier = threading.Barrier(2)
    seen: dict[str, str] = {}

    def run(name: str, record: ProjectRecord) -> None:
        def action() -> None:
            barrier.wait(timeout=5)
            time.sleep(0.05)
            seen[name] = paths.resolve_data_root()

        _call_in_project(record, action)

    alpha = project("alpha")
    beta = project("beta")
    threads = [
        threading.Thread(target=run, args=("alpha", alpha)),
        threading.Thread(target=run, args=("beta", beta)),
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(5)
        assert not thread.is_alive()
    assert seen["alpha"].endswith("alpha-root")
    assert seen["beta"].endswith("beta-root")
    assert seen["alpha"] != seen["beta"]
    assert os.environ.get("YY_FLOW_PROJECT_ROOT") == before


def test_model_task_type_cannot_skip_review(tmp_path: Path) -> None:
    registry, board = _prepare(tmp_path, {"alpha": ["read", "task.transition"]}, role="DEV")
    session = StdioSession(tmp_path, registry=registry)
    try:
        session.handshake()
        created = _create(session, "type-lock-001", "类型锁定")
        assert created["isError"] is False, created["structuredContent"]
        task_id = created["structuredContent"]["task_id"]
        version, status = _version(session, task_id)
        assert status == "待开始"
        moved = session.call(
            "transition_task",
            {
                "project_id": "alpha",
                "task_id": task_id,
                "idempotency_key": "type-lock-go",
                "expected_version": version,
                "from_status": "待开始",
                "to_status": "进行中",
                "assignee": "李开发",
                "task_type": "A",
            },
        )
        assert moved["isError"] is False, moved["structuredContent"]
        version, status = _version(session, task_id)
        assert status == "进行中"
        skipped = session.call(
            "transition_task",
            {
                "project_id": "alpha",
                "task_id": task_id,
                "idempotency_key": "type-lock-skip",
                "expected_version": version,
                "from_status": "进行中",
                "to_status": "已完成",
                "assignee": "李开发",
                "task_type": "B",
                "end_time": "2026-10-13 16:40:00",
            },
        )
        assert skipped["isError"] is True
        assert skipped["structuredContent"]["details"]["reason"] == "TASK_TYPE"
        _version_after, status_after = _version(session, task_id)
        assert status_after == "进行中"
    finally:
        session.close()
    cards = json.loads(board.read_text(encoding="utf-8"))
    card = next(item for item in cards if item.get("id") == task_id)
    assert card["type"] == "A"
    assert card["status"] == "进行中"


def test_audit_write_failure_does_not_report_success(tmp_path: Path, monkeypatch) -> None:
    import builtins
    import getpass
    import os

    from scripts._lib.mcp.audit_store import AuditStore
    from scripts._lib.mcp.registry import load_registry
    from scripts._lib.mcp.service import handle_tool

    registry_path, board = _prepare(tmp_path, {"alpha": ["read", "task.transition"]}, role="DEV")
    registry = load_registry(str(registry_path))
    audit = AuditStore(str(tmp_path / "mcp_audit"))
    real_open = builtins.open

    def broken_open(file, mode="r", *args, **kwargs):
        text = os.fspath(file)
        if text.endswith("events.jsonl") and "a" in str(mode):
            raise OSError("injected audit failure")
        return real_open(file, mode, *args, **kwargs)

    monkeypatch.setattr(builtins, "open", broken_open)
    result = handle_tool(
        "create_task",
        {
            "project_id": "alpha",
            "idempotency_key": "audit-fail-001",
            "name": "审计失败不应建卡",
            "type": "A",
            "assignee": "李开发",
        },
        {"registry": registry, "audit": audit, "os_user": getpass.getuser()},
    )
    assert result["isError"] is True
    assert result["structured"]["details"]["reason"] == "AUDIT"
    cards = json.loads(board.read_text(encoding="utf-8"))
    assert cards == []
    assert not (tmp_path / "mcp_audit" / "events.jsonl").exists()
