# -*- coding: utf-8 -*-
"""人类验收确认通道。环境变量和模型参数都不是真实用户演练。"""

from __future__ import annotations

import getpass
import hashlib
import json
import threading
from pathlib import Path

import pytest

from scripts._lib.core.evidence_schema import ArtifactRecord, EvidenceMetadata, EvidenceRecord, EvidenceType
from scripts._lib.mcp import acceptance as acceptance_mod
from scripts._lib.core.evidence_store import EvidenceStore
from scripts._lib.mcp.acceptance import decide_acceptance, load_acceptance_record
from scripts._lib.mcp.audit_store import AuditStore, AuditWriteError
from scripts._lib.mcp.idempotency import IdempotencyStore
from scripts._lib.mcp.registry import load_registry
from scripts._lib.mcp.service import handle_tool, task_version
from scripts.mcp_server import main
from test_mcp_stdio import MARKER
from test_mcp_write import _finish_type_b, _open, _prepare, _submit, _version


def test_legacy_runner_cli_still_imports() -> None:
    import sys

    from scripts._lib.core.production_runner import ProductionRunner
    from scripts.run_task import cmd_accept, cmd_reject

    previous = list(sys.path)
    try:
        assert callable(cmd_accept)
        assert callable(cmd_reject)
        assert callable(ProductionRunner.accept)
        assert callable(ProductionRunner.reject)
    finally:
        sys.path[:] = previous


def _board(tmp: Path) -> list:
    path = tmp / MARKER / "alpha" / "user_data" / "board.json"
    return json.loads(path.read_text(encoding="utf-8"))


def _status(tmp: Path, task_id: str) -> str:
    for card in _board(tmp):
        if card.get("id") == task_id:
            return str(card.get("status") or "")
    return ""


def _acceptance_path(registry_path: Path, confirmation_id: str) -> Path:
    project = load_registry(str(registry_path)).get_project("alpha")
    assert project is not None
    return Path(project.data_root) / "mcp_auth" / "acceptance" / f"{confirmation_id}.json"


def _bind_card(tmp: Path, task_id: str, candidate: str = "b" * 40, items: list | None = None) -> None:
    path = tmp / MARKER / "alpha" / "user_data" / "board.json"
    cards = json.loads(path.read_text(encoding="utf-8"))
    criteria = items or [{"id": "AC-01", "text": "隔离任务保持已完成直到本机确认"}]
    for card in cards:
        if card.get("id") != task_id:
            continue
        card["candidate_commit"] = candidate
        card["acceptance_criteria_items"] = criteria
        card["acceptance_criteria_hash"] = hashlib.sha256(
            json.dumps(criteria, ensure_ascii=False).encode("utf-8")
        ).hexdigest()
        card["remarks"] = "验收标准: " + criteria[0]["text"]
    path.write_text(json.dumps(cards, ensure_ascii=False), encoding="utf-8")


def _plant_pair(tmp: Path, registry: Path, task_id: str, suffix: str, *, real_host: bool = True, artifacts: bool = True) -> str:
    project = load_registry(str(registry)).get_project("alpha")
    assert project is not None
    store = EvidenceStore(project.evidence_root)
    reviewer_id = "reviewer-" + suffix
    for role, session, evidence_id in (
        ("REVIEWER", "reviewer-session-" + suffix, reviewer_id),
        ("QA", "qa-session-" + suffix, "qa-" + suffix),
    ):
        store.append(
            EvidenceRecord(
                evidence_id=evidence_id,
                evidence_type=EvidenceType.TASK_COMPLETE,
                baseline_commit="a" * 40,
                result_commit="b" * 40,
                artifacts=(ArtifactRecord(relative_path="reports/note.txt", sha256_hash="ab" * 32),) if artifacts else (),
                metadata=EvidenceMetadata(
                    project_id="alpha",
                    task_id=task_id,
                    actor_role=role,
                    host_id="yy-flow-host",
                    adapter="yy-flow",
                    host_session_id=session,
                    host_invocation_id="invoke-" + evidence_id,
                    is_real_host=real_host,
                    workspace_mode="runner",
                    transition_from="测试中",
                    transition_to="已完成",
                    created_at=1,
                    extra={"decision": "PASS"},
                ),
            )
        )
    return reviewer_id


def _apply(tmp: Path, suffix: str, *, confirmed_env: bool = True) -> tuple[Path, str, str, str]:
    registry, _board_path = _prepare(
        tmp,
        {"alpha": ["read", "task.transition", "evidence.submit", "acceptance"]},
        role="DEV",
    )
    session = _open(tmp, registry, confirmed=confirmed_env)
    try:
        task_id = _finish_type_b(session, "验收样例" + suffix, "task-" + suffix + "-0001")
        _bind_card(tmp, task_id)
        evidence_id = _plant_pair(tmp, registry, task_id, suffix)
        version, status = _version(session, task_id)
        assert status == "已完成"
        applied = session.call(
            "request_acceptance",
            {
                "project_id": "alpha",
                "task_id": task_id,
                "idempotency_key": "apply-" + suffix + "-001",
                "expected_version": version,
                "evidence_id": evidence_id,
                "acceptance_confirmed": True,
                "user_id": "forged-principal",
            },
        )
        assert applied["isError"] is False, applied["structuredContent"]
        body = applied["structuredContent"]
        assert body["status"] == "已完成"
        assert body["action"] == "accept"
        assert body["project_id"] == "alpha"
        assert body["task_id"] == task_id
        assert body["candidate_sha"] == "b" * 40
        assert body["contract_hash"]
        assert body["expires_at"]
        assert body["quality_evidence_summary"]["roles"] == ["QA", "REVIEWER"]
        confirmation_id = body["confirmation_id"]
        replay = session.call(
            "request_acceptance",
            {
                "project_id": "alpha",
                "task_id": task_id,
                "idempotency_key": "apply-" + suffix + "-001",
                "expected_version": version,
                "evidence_id": evidence_id,
            },
        )
        assert replay["structuredContent"]["replayed"] is True
        assert replay["structuredContent"]["confirmation_id"] == confirmation_id
        files = list(_acceptance_path(registry, confirmation_id).parent.glob("*.json"))
        assert len(files) == 1
        assert _status(tmp, task_id) == "已完成"
    finally:
        session.close()
    return registry, task_id, evidence_id, confirmation_id


def test_request_without_human_confirmation_stays_completed(tmp_path: Path) -> None:
    registry, task_id, _evidence_id, confirmation_id = _apply(tmp_path, "nocfm", confirmed_env=True)
    assert confirmation_id
    assert _status(tmp_path, task_id) == "已完成"
    loaded = load_acceptance_record(load_registry(str(registry)), confirmation_id)
    assert loaded is not None
    assert loaded["confirmation_id"] == confirmation_id
    assert loaded["consumed"] is False


def test_approve_is_one_time_and_ignores_user_id(tmp_path: Path) -> None:
    registry, task_id, _evidence_id, confirmation_id = _apply(tmp_path, "approve1", confirmed_env=True)
    before = task_version(next(card for card in _board(tmp_path) if card.get("id") == task_id))
    payload = decide_acceptance(
        load_registry(str(registry)),
        confirmation_id,
        "approve",
        user_id="intruder",
        audit=AuditStore(str(tmp_path / "mcp_audit")),
    )
    assert payload["ok"] is True, payload
    assert payload["status"] == "已验收"
    assert payload["consumed"] is True
    assert payload["decision"] == "approve"
    assert _status(tmp_path, task_id) == "已验收"
    after = task_version(next(card for card in _board(tmp_path) if card.get("id") == task_id))
    assert after != before
    replayed = decide_acceptance(
        load_registry(str(registry)),
        confirmation_id,
        "approve",
        user_id="someone-else",
        audit=AuditStore(str(tmp_path / "mcp_audit")),
    )
    assert replayed["ok"] is True
    assert replayed["replayed"] is True
    assert replayed["status"] == "已验收"
    assert task_version(next(card for card in _board(tmp_path) if card.get("id") == task_id)) == after
    assert len([card for card in _board(tmp_path) if card.get("id")]) == 1


def test_cli_without_console_does_not_approve(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    registry, task_id, _evidence_id, confirmation_id = _apply(tmp_path, "console")
    code = main(
        [
            "acceptance",
            "--registry",
            str(registry),
            "--id",
            confirmation_id,
            "--decision",
            "approve",
            "--user-id",
            "intruder",
        ]
    )
    payload = json.loads(capsys.readouterr().out)
    assert code == 2
    assert payload["reason"] == "CONSOLE"
    assert payload["consumed"] is False
    assert _status(tmp_path, task_id) == "已完成"


def test_reject_returns_same_task(tmp_path: Path) -> None:
    registry, task_id, _evidence_id, confirmation_id = _apply(tmp_path, "reject1")
    before_ids = [card.get("id") for card in _board(tmp_path) if card.get("id")]
    result = decide_acceptance(
        load_registry(str(registry)),
        confirmation_id,
        "reject",
        user_id="not-used",
        audit=AuditStore(str(tmp_path / "mcp_audit")),
    )
    assert result["ok"] is True, result
    assert result["status"] == "已退回"
    assert _status(tmp_path, task_id) == "已退回"
    assert [card.get("id") for card in _board(tmp_path) if card.get("id")] == before_ids


def test_binding_and_identity_rejections(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    registry, task_id, _evidence_id, confirmation_id = _apply(tmp_path, "bind01")
    reg = load_registry(str(registry))
    missing = decide_acceptance(reg, "missing-confirmation-id", "approve", user_id="principal-dev")
    assert missing["code"] == "NOT_FOUND"
    assert _status(tmp_path, task_id) == "已完成"

    path = _acceptance_path(registry, confirmation_id)
    saved = json.loads(path.read_text(encoding="utf-8"))

    def put(changes: dict) -> dict:
        current = json.loads(json.dumps(saved))
        current.update(changes)
        path.write_text(json.dumps(current, ensure_ascii=False), encoding="utf-8")
        return decide_acceptance(load_registry(str(registry)), confirmation_id, "approve", user_id="root")

    assert put({"project_id": "beta"})["reason"] == "PROJECT"
    assert put({"principal_id": "other-principal"})["reason"] == "PRINCIPAL"
    assert put({"candidate_sha": "c" * 40})["reason"] == "CANDIDATE"
    assert put({"expires_at": "2000-01-01T00:00:00Z"})["reason"] == "EXPIRED"
    other = _second_task(tmp_path, registry)
    assert put({"task_id": other})["reason"] == "TASK"
    cards = _board(tmp_path)
    for card in cards:
        if card.get("id") == task_id:
            card["name"] = "契约已变"
    (tmp_path / MARKER / "alpha" / "user_data" / "board.json").write_text(
        json.dumps(cards, ensure_ascii=False),
        encoding="utf-8",
    )
    path.write_text(json.dumps(saved, ensure_ascii=False), encoding="utf-8")
    changed = decide_acceptance(load_registry(str(registry)), confirmation_id, "approve", user_id="root")
    assert changed["reason"] == "CONTRACT"
    assert _status(tmp_path, task_id) == "已完成"
    record = json.loads(path.read_text(encoding="utf-8"))
    assert record.get("consumed") is not True

    monkeypatch.setattr("scripts._lib.mcp.acceptance._os_user", lambda: "nobody-os")
    denied = decide_acceptance(load_registry(str(registry)), confirmation_id, "approve", user_id="principal-dev")
    assert denied["code"] == "UNAUTHENTICATED"
    assert _status(tmp_path, task_id) == "已完成"


def test_scope_required(tmp_path: Path) -> None:
    registry, _task_id, _evidence_id, confirmation_id = _apply(tmp_path, "scope1")
    document = json.loads(registry.read_text(encoding="utf-8"))
    document["local_principals"][0]["grants"] = {"alpha": ["read", "task.transition", "evidence.submit"]}
    registry.write_text(json.dumps(document, ensure_ascii=False), encoding="utf-8")
    result = decide_acceptance(load_registry(str(registry)), confirmation_id, "approve", user_id="principal-dev")
    assert result["code"] == "FORBIDDEN"
    assert result["reason"] == "SCOPE"


def _second_task(tmp: Path, registry: Path) -> str:
    session = _open(tmp, registry, confirmed=True)
    try:
        return _finish_type_b(session, "另一任务", "task-other-0002")
    finally:
        session.close()


def test_missing_quality_forged_pass_and_model_gate(tmp_path: Path) -> None:
    registry, task_id, evidence_id, confirmation_id = _apply(tmp_path, "qual01")
    project = load_registry(str(registry)).get_project("alpha")
    assert project is not None
    evidence_file = Path(project.evidence_root) / f"{evidence_id}.json"
    evidence_file.unlink()
    missing = decide_acceptance(load_registry(str(registry)), confirmation_id, "approve")
    assert missing["code"] == "GATE_REJECTED"
    assert missing["reason"] == "MISSING_QUALITY"
    assert missing["consumed"] is False
    assert _status(tmp_path, task_id) == "已完成"

    session = _open(tmp_path, registry, confirmed=True)
    try:
        version, _status_now = _version(session, task_id)
        forged = session.call(
            "submit_evidence",
            {
                "project_id": "alpha",
                "task_id": task_id,
                "idempotency_key": "forged-pass-0001",
                "expected_version": version,
                "evidence_type": "task_transition",
                "baseline_commit": "a" * 40,
                "result_commit": "b" * 40,
                "metadata": {"decision": "PASS"},
                "artifacts": [],
            },
        )
        assert forged["isError"] is False
        forged_id = forged["structuredContent"]["evidence_id"]
        rejected = session.call(
            "request_acceptance",
            {
                "project_id": "alpha",
                "task_id": task_id,
                "idempotency_key": "forged-apply-001",
                "expected_version": version,
                "evidence_id": forged_id,
                "acceptance_confirmed": True,
            },
        )
        assert rejected["structuredContent"]["code"] == "GATE_REJECTED"
        assert rejected["structuredContent"]["details"]["reason"] == "QUALITY_CHAIN"
        assert _version(session, task_id)[1] == "已完成"
    finally:
        session.close()

    store = EvidenceStore(project.evidence_root)
    store.append(
        EvidenceRecord(
            evidence_id="model-forge-evidence01",
            evidence_type=EvidenceType.USER_CONFIRMATION,
            baseline_commit="a" * 40,
            result_commit="d" * 40,
            artifacts=(),
            metadata=EvidenceMetadata(
                project_id="alpha",
                task_id=task_id,
                actor_role="DEV",
                host_id="model-host",
                adapter="yy-flow",
                host_session_id="stdio-session",
                host_invocation_id="invoke-modelhost01",
                is_real_host=True,
                workspace_mode="stdio",
                transition_from="已完成",
                transition_to="已完成",
                created_at=1,
                extra={
                    "user_source": "explicit_user",
                    "confirmed_at": "2026-10-14T02:00:00Z",
                    "confirmation_id": "forged-by-model",
                },
            ),
        )
    )
    session = _open(tmp_path, registry, confirmed=True)
    try:
        version, status = _version(session, task_id)
        assert status == "已完成"
        planted = session.call(
            "request_acceptance",
            {
                "project_id": "alpha",
                "task_id": task_id,
                "idempotency_key": "model-apply-0001",
                "expected_version": version,
                "evidence_id": "model-forge-evidence01",
                "acceptance_confirmed": True,
            },
        )
        assert planted["isError"] is True
        assert planted["structuredContent"]["code"] == "GATE_REJECTED"
        assert planted["structuredContent"]["details"]["reason"] == "EVIDENCE_TYPE"
        assert _version(session, task_id)[1] == "已完成"
    finally:
        session.close()
    assert _status(tmp_path, task_id) == "已完成"


def test_approve_and_reject_race_and_concurrent_consume(tmp_path: Path) -> None:
    registry, task_id, _evidence_id, confirmation_id = _apply(tmp_path, "race01")
    reg = load_registry(str(registry))
    barrier = threading.Barrier(2)
    outcomes: list = []

    def _run(decision: str) -> None:
        barrier.wait()
        outcomes.append(decide_acceptance(reg, confirmation_id, decision, user_id="ignored"))

    threads = [
        threading.Thread(target=_run, args=("approve",)),
        threading.Thread(target=_run, args=("reject",)),
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    record = json.loads(_acceptance_path(registry, confirmation_id).read_text(encoding="utf-8"))
    assert record["decision"] in {"approve", "reject"}
    assert record["consumed"] is True
    if record["decision"] == "approve":
        assert _status(tmp_path, task_id) == "已验收"
    else:
        assert _status(tmp_path, task_id) == "已退回"
    opposite = "reject" if record["decision"] == "approve" else "approve"
    blocked = decide_acceptance(reg, confirmation_id, opposite, user_id="late")
    assert blocked["ok"] is False
    assert blocked["reason"] == "DECISION"
    assert _status(tmp_path, task_id) == ("已验收" if record["decision"] == "approve" else "已退回")

    registry2, task_id2, _evidence2, confirmation2 = _apply(tmp_path / "second", "race02")
    reg2 = load_registry(str(registry2))
    barrier2 = threading.Barrier(2)
    results: list = []

    def _approve() -> None:
        barrier2.wait()
        results.append(decide_acceptance(reg2, confirmation2, "approve"))

    pair = [threading.Thread(target=_approve), threading.Thread(target=_approve)]
    for thread in pair:
        thread.start()
    for thread in pair:
        thread.join()
    assert _status(tmp_path / "second", task_id2) == "已验收"
    assert len(results) == 2
    assert all(item["ok"] for item in results)
    assert json.loads(_acceptance_path(registry2, confirmation2).read_text(encoding="utf-8"))["consumed"] is True


def test_interrupt_recovery_audit_and_ledger(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    registry, task_id, _evidence_id, confirmation_id = _apply(tmp_path, "disk01")
    loaded = load_acceptance_record(load_registry(str(registry)), confirmation_id)
    assert loaded is not None
    assert loaded["candidate_sha"] == "b" * 40
    assert loaded["consumed"] is False
    path = _acceptance_path(registry, confirmation_id)
    loaded["decision"] = "approve"
    loaded["decided_by"] = "recovered"
    path.write_text(json.dumps(loaded, ensure_ascii=False), encoding="utf-8")
    resumed = decide_acceptance(
        load_registry(str(registry)),
        confirmation_id,
        "approve",
        audit=AuditStore(str(tmp_path / "mcp_audit")),
    )
    assert resumed["ok"] is True, resumed
    assert _status(tmp_path, task_id) == "已验收"
    again = load_acceptance_record(load_registry(str(registry)), confirmation_id)
    assert again is not None and again["consumed"] is True

    registry2, task_id2, evidence2, confirmation2 = _apply(tmp_path / "audit", "audit1")
    version = task_version(next(card for card in _board(tmp_path / "audit") if card.get("id") == task_id2))

    class _Broken(AuditStore):
        def append(self, **kwargs):
            raise AuditWriteError()

    failed = decide_acceptance(
        load_registry(str(registry2)),
        confirmation2,
        "approve",
        audit=_Broken(str(tmp_path / "audit" / "unused")),
    )
    assert failed["ok"] is False
    assert failed["reason"] == "AUDIT"
    assert _status(tmp_path / "audit", task_id2) == "已完成"
    disk = load_acceptance_record(load_registry(str(registry2)), confirmation2)
    assert disk is not None and disk.get("consumed") is not True

    def _boom(self, **kwargs):
        raise OSError("ledger")

    monkeypatch.setattr(IdempotencyStore, "bind", _boom)
    reg = load_registry(str(registry2))
    context = {
        "registry": reg,
        "audit": AuditStore(str(tmp_path / "audit" / "mcp_audit")),
        "os_user": __import__("getpass").getuser(),
        "acceptance_confirmed": True,
    }
    outcome = handle_tool(
        "request_acceptance",
        {
            "project_id": "alpha",
            "task_id": task_id2,
            "idempotency_key": "ledger-fail-0001",
            "expected_version": version,
            "evidence_id": evidence2,
            "acceptance_confirmed": True,
        },
        context,
    )
    assert outcome["isError"] is True
    assert outcome["structured"]["code"] == "GATE_REJECTED"
    assert outcome["structured"]["details"]["reason"] == "LEDGER"
    assert _status(tmp_path / "audit", task_id2) == "已完成"
    directory = Path(reg.get_project("alpha").data_root) / "mcp_auth" / "acceptance"
    found = False
    for item in directory.glob("*.json"):
        body = json.loads(item.read_text(encoding="utf-8"))
        if body.get("idempotency_key") == "ledger-fail-0001":
            found = True
            restored = load_acceptance_record(reg, body["confirmation_id"])
            assert restored is not None
            assert restored["task_id"] == task_id2
    assert found


def test_checkpoint_criteria_hash_changes_contract(tmp_path: Path) -> None:
    registry, task_id, evidence_id, _confirmation_id = _apply(tmp_path, "snap01")
    project = load_registry(str(registry)).get_project("alpha")
    assert project is not None
    path = Path(project.data_root) / "runner_checkpoints" / "alpha" / f"{task_id}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    snapshot = {
        "acceptance_criteria_hash": "a" * 64,
        "task_id": task_id,
    }
    path.write_text(
        json.dumps(
            {
                "task_id": task_id,
                "project_id": "alpha",
                "state": "PENDING_USER_ACCEPTANCE",
                "current_role": "PM",
                "candidate_commit": "b" * 40,
                "execution_spec_snapshot": snapshot,
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    card = next(item for item in _board(tmp_path) if item.get("id") == task_id)
    authority = acceptance_mod._authority(project, card)
    assert authority is not None
    assert authority["acceptance_criteria_hash"] == "a" * 64
    assert authority["candidate_commit"] == "b" * 40
    evidence = acceptance_mod._read_evidence(project, evidence_id)
    first = acceptance_mod._contract_hash(project, card, evidence, authority)
    snapshot["acceptance_criteria_hash"] = "b" * 64
    path.write_text(
        json.dumps(
            {
                "task_id": task_id,
                "project_id": "alpha",
                "state": "PENDING_USER_ACCEPTANCE",
                "current_role": "PM",
                "candidate_commit": "b" * 40,
                "execution_spec_snapshot": snapshot,
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    moved = acceptance_mod._authority(project, card)
    assert moved is not None
    assert acceptance_mod._contract_hash(project, card, evidence, moved) != first
    path.unlink()


def test_runner_shaped_quality_chain_can_apply(tmp_path: Path) -> None:
    registry, _board_path = _prepare(
        tmp_path,
        {"alpha": ["read", "task.transition", "evidence.submit", "acceptance"]},
        role="DEV",
    )
    session = _open(tmp_path, registry, confirmed=False)
    try:
        task_id = _finish_type_b(session, "真实证据形状", "task-shape-0001")
        _bind_card(tmp_path, task_id)
        project = load_registry(str(registry)).get_project("alpha")
        assert project is not None
        store = EvidenceStore(project.evidence_root)
        store.append(
            EvidenceRecord(
                evidence_id="reviewer-shape",
                evidence_type=EvidenceType.TASK_TRANSITION,
                baseline_commit="a" * 40,
                result_commit="b" * 40,
                artifacts=(),
                metadata=EvidenceMetadata(
                    project_id="alpha",
                    task_id=task_id,
                    actor_role="REVIEWER",
                    host_id="review-host",
                    adapter="yy-flow",
                    host_session_id="sess_reviewer_shape",
                    host_invocation_id="invoke-reviewer-shape",
                    is_real_host=True,
                    workspace_mode="workspace_read",
                    transition_from="REVIEWING",
                    transition_to="TESTING",
                    created_at=1,
                    extra={"acceptance_criteria_hash": "c" * 64},
                ),
            )
        )
        store.append(
            EvidenceRecord(
                evidence_id="qa-shape",
                evidence_type=EvidenceType.TASK_TRANSITION,
                baseline_commit="a" * 40,
                result_commit="b" * 40,
                artifacts=(),
                metadata=EvidenceMetadata(
                    project_id="alpha",
                    task_id=task_id,
                    actor_role="QA",
                    host_id="qa-host",
                    adapter="yy-flow",
                    host_session_id="sess_qa_shape",
                    host_invocation_id="invoke-qa-shape",
                    is_real_host=True,
                    workspace_mode="workspace_read",
                    transition_from="TESTING",
                    transition_to="PENDING_USER_ACCEPTANCE",
                    created_at=1,
                    extra={"qa_decision": "PASS"},
                ),
            )
        )
        version, status = _version(session, task_id)
        assert status == "已完成"
        applied = session.call(
            "request_acceptance",
            {
                "project_id": "alpha",
                "task_id": task_id,
                "idempotency_key": "shape-apply-0001",
                "expected_version": version,
                "evidence_id": "reviewer-shape",
            },
        )
        assert applied["isError"] is False, applied["structuredContent"]
        assert applied["structuredContent"]["quality_evidence_summary"]["roles"] == ["QA", "REVIEWER"]
        assert _status(tmp_path, task_id) == "已完成"
    finally:
        session.close()


def test_os_user_ignores_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("USERNAME", "forged-user")
    monkeypatch.setenv("USER", "forged-user")
    monkeypatch.setenv("LOGNAME", "forged-user")
    assert getpass.getuser() == "forged-user"
    assert acceptance_mod._os_user() != "forged-user"
    assert acceptance_mod._os_user()


def test_contract_tracks_criteria_and_latest_candidate(tmp_path: Path) -> None:
    registry, task_id, _evidence_id, confirmation_id = _apply(tmp_path, "criteria")
    reg = load_registry(str(registry))
    _bind_card(tmp_path, task_id, items=[{"id": "AC-02", "text": "改过的验收条目"}])
    criteria = decide_acceptance(reg, confirmation_id, "approve")
    assert criteria["reason"] == "CONTRACT"
    assert criteria["consumed"] is False
    assert _status(tmp_path, task_id) == "已完成"
    _bind_card(tmp_path, task_id, candidate="c" * 40)
    candidate = decide_acceptance(reg, confirmation_id, "approve")
    assert candidate["reason"] == "CANDIDATE"
    assert _status(tmp_path, task_id) == "已完成"


def test_user_confirmation_cannot_replace_quality_chain(tmp_path: Path) -> None:
    registry, task_id, evidence_id, _confirmation_id = _apply(tmp_path, "chain1")
    session = _open(tmp_path, registry, confirmed=True)
    try:
        version, status = _version(session, task_id)
        assert status == "已完成"
        uploaded = _submit(
            session,
            task_id,
            "solo-confirm-0001",
            "user_confirmation",
            {"user_source": "explicit_user", "confirmed_at": "2026-10-14T02:00:00Z"},
        )
        assert uploaded["isError"] is False
        rejected = session.call(
            "request_acceptance",
            {
                "project_id": "alpha",
                "task_id": task_id,
                "idempotency_key": "solo-apply-0001",
                "expected_version": version,
                "evidence_id": uploaded["structuredContent"]["evidence_id"],
                "acceptance_confirmed": True,
            },
        )
        assert rejected["isError"] is True
        assert rejected["structuredContent"]["details"]["reason"] == "EVIDENCE_TYPE"
        assert _version(session, task_id)[1] == "已完成"
    finally:
        session.close()
    assert evidence_id.startswith("reviewer-")


def test_audit_backfill_does_not_transition_again(tmp_path: Path) -> None:
    registry, task_id, _evidence_id, confirmation_id = _apply(tmp_path, "backfill")
    audit_dir = tmp_path / "backfill-audit"

    class _OkFails(AuditStore):
        def append(self, **kwargs):
            if kwargs.get("code") == "OK":
                raise AuditWriteError()
            return super().append(**kwargs)

    pending = decide_acceptance(
        load_registry(str(registry)),
        confirmation_id,
        "approve",
        audit=_OkFails(str(audit_dir)),
    )
    assert pending["ok"] is True, pending
    assert pending["code"] == "APPLIED_AUDIT_PENDING"
    assert pending["message"] == "业务已应用、审计待补记"
    assert pending["status"] == "已验收"
    assert _status(tmp_path, task_id) == "已验收"
    version = task_version(next(card for card in _board(tmp_path) if card.get("id") == task_id))
    lines = (audit_dir / "events.jsonl").read_text(encoding="utf-8").splitlines()
    assert any('"code": "PENDING"' in line or '"code":"PENDING"' in line for line in lines)
    assert not any('"code": "OK"' in line or '"code":"OK"' in line for line in lines)
    retried = decide_acceptance(
        load_registry(str(registry)),
        confirmation_id,
        "approve",
        audit=AuditStore(str(audit_dir)),
    )
    assert retried["ok"] is True, retried
    assert retried["code"] == "OK"
    assert retried["replayed"] is True
    assert task_version(next(card for card in _board(tmp_path) if card.get("id") == task_id)) == version
    assert _status(tmp_path, task_id) == "已验收"
    retried_lines = (audit_dir / "events.jsonl").read_text(encoding="utf-8")
    assert '"OK"' in retried_lines or '"code": "OK"' in retried_lines
