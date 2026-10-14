# -*- coding: utf-8 -*-
"""原生回执不能证明宿主。Runner 快照必须带上验收条目。"""

from __future__ import annotations

from pathlib import Path

import pytest

from scripts._lib.core.native_receipt import (
    NativeReceiptRejected,
    bind_native_handover,
    binding_problem,
    execute_controlled,
)
from scripts._lib.core.production_runner import _execution_spec_snapshot, _upgrade_legacy_contract_snapshot
from scripts._lib.core.runner_checkpoint_store import RunnerCheckpointStore
from scripts._lib.core.runner_schema import RunnerCheckpoint, TaskExecutionSpec
from scripts._lib.mcp.acceptance import _authority
from scripts._lib.mcp.registry import load_registry
from test_mcp_write import _finish_type_b, _open, _prepare, _submit, _version


def _spec(root: Path, task_id: str, *, criteria_hash: str = "ab" * 32) -> TaskExecutionSpec:
    return TaskExecutionSpec(
        project_id="alpha",
        project_root=str(root),
        authority_root=str(root),
        task_id=task_id,
        task_name="隔离验收",
        requirement_text="做一个可观察的临时结果",
        acceptance_criteria="验收标准: 临时任务保持已完成直到本机确认",
        acceptance_criteria_hash=criteria_hash,
        task_version="1",
        status_at_read="已完成",
        requirement_hash="cd" * 32,
        test_commands=('python -c "import sys; raise SystemExit(0)"',),
    )


def test_receipt_claims_never_become_a_host(tmp_path: Path) -> None:
    source = Path("scripts/_lib/core/native_receipt.py").read_text(encoding="utf-8")
    assert "is_real_host=True" not in source
    claims = [
        {"session_id": "forged", "invocation_id": "forged-1", "role": "REVIEWER"},
        {"session_id": "c3efc272-b9d3-4af3-b3ad-3b261f9929ac", "role": "QA", "invocation_id": "once"},
        {"session_id": "c3efc272-b9d3-4af3-b3ad-3b261f9929ac", "role": "REVIEWER", "invocation_id": "once"},
        {"role": "REVIEWER", "candidate": "c" * 40, "invocation_id": "other"},
    ]
    for claimed in claims:
        with pytest.raises(NativeReceiptRejected) as raised:
            bind_native_handover(claimed, evidence_root=str(tmp_path))
        assert raised.value.reason == "RECEIPT_UNVERIFIED"
    assert list(tmp_path.iterdir()) == []


def test_binding_rules_do_not_trust_claimed_results(tmp_path: Path) -> None:
    expected = {
        "expected_role": "QA",
        "expected_task_id": "T0001",
        "expected_candidate": "b" * 40,
        "expected_contract_hash": "ab" * 32,
        "expected_criterion_ids": ["AC-01"],
        "seen_invocations": {"used-once"},
    }
    base = {
        "role": "QA",
        "task_id": "T0001",
        "candidate": "b" * 40,
        "contract_hash": "ab" * 32,
        "criterion_ids": ["AC-01"],
        "invocation_id": "fresh",
        "exit_code": 0,
    }
    failed = execute_controlled(['python -c "import sys; raise SystemExit(2)"'], str(tmp_path))
    assert binding_problem(base, executor_results=failed, **expected) == "FAILED_TEST"
    assert binding_problem({**base, "role": "REVIEWER"}, executor_results=(), **expected) == "ROLE_MISMATCH"
    assert binding_problem({**base, "invocation_id": "used-once"}, executor_results=(), **expected) == "REPLAY"
    assert binding_problem({**base, "candidate": "c" * 40}, executor_results=(), **expected) == "CANDIDATE"
    assert binding_problem({**base, "criterion_ids": []}, executor_results=(), **expected) == "CRITERIA"
    passed = execute_controlled(['python -c "import sys; raise SystemExit(0)"'], str(tmp_path))
    assert binding_problem({**base, "session_id": "forged"}, executor_results=passed, **expected) is None
    with pytest.raises(NativeReceiptRejected) as raised:
        bind_native_handover({**base, "session_id": "forged"})
    assert raised.value.reason == "RECEIPT_UNVERIFIED"


def test_legacy_snapshot_backfill_keeps_candidate_and_refuses_drift(tmp_path: Path) -> None:
    spec = _spec(tmp_path, "T0001")
    current = _execution_spec_snapshot(spec)
    legacy = {
        key: value
        for key, value in current.items()
        if key not in {"acceptance_criteria_items", "test_commands", "contract_hash_version"}
    }
    legacy["contract_hash_version"] = 2
    legacy["baseline_commit"] = "b" * 40
    migrated = _upgrade_legacy_contract_snapshot(legacy, spec)
    assert migrated["contract_hash_version"] == 3
    assert migrated["acceptance_criteria_items"] == current["acceptance_criteria_items"]
    assert migrated["test_commands"] == current["test_commands"]
    assert migrated["baseline_commit"] == "b" * 40
    drifted = dict(legacy, acceptance_criteria_hash="ee" * 32)
    assert _upgrade_legacy_contract_snapshot(drifted, spec) == drifted
    conflicted = dict(legacy, acceptance_criteria_items=[{"criterion_id": "AC-09", "text": "另一条目"}])
    assert _upgrade_legacy_contract_snapshot(conflicted, spec)["acceptance_criteria_items"][0]["criterion_id"] == "AC-09"


def test_runner_snapshot_reaches_acceptance_and_still_needs_a_receipt(tmp_path: Path) -> None:
    registry, _board = _prepare(
        tmp_path,
        {"alpha": ["read", "task.transition", "evidence.submit", "acceptance"]},
        role="DEV",
    )
    session = _open(tmp_path, registry, confirmed=False)
    try:
        task_id = _finish_type_b(session, "快照链路", "snap-task-0001")
        project = load_registry(str(registry)).get_project("alpha")
        assert project is not None
        spec = _spec(Path(project.data_root), task_id)
        snapshot = _execution_spec_snapshot(spec)
        assert snapshot["contract_hash_version"] == 3
        assert snapshot["acceptance_criteria_items"]
        assert snapshot["test_commands"]
        store = RunnerCheckpointStore(
            data_root=project.data_root,
            project_root=project.data_root,
            project_id="alpha",
        )
        store.save_checkpoint(
            RunnerCheckpoint(
                task_id=task_id,
                project_id="alpha",
                state="PENDING_USER_ACCEPTANCE",
                current_role="PM",
                candidate_commit="b" * 40,
                execution_spec_snapshot=snapshot,
            )
        )
        card = next(item for item in __import__("json").loads(Path(project.board_file).read_text(encoding="utf-8")) if item["id"] == task_id)
        authority = _authority(project, card)
        assert authority is not None
        assert authority["candidate_commit"] == "b" * 40
        assert authority["acceptance_criteria_items"]
        uploaded = _submit(session, task_id, "snap-evid-0001", "task_transition", {"decision": "PASS"})
        assert uploaded["isError"] is False
        version, status = _version(session, task_id)
        assert status == "已完成"
        rejected = session.call(
            "request_acceptance",
            {
                "project_id": "alpha",
                "task_id": task_id,
                "idempotency_key": "snap-apply-0001",
                "expected_version": version,
                "evidence_id": uploaded["structuredContent"]["evidence_id"],
                "acceptance_confirmed": True,
            },
        )
        assert rejected["structuredContent"]["code"] == "GATE_REJECTED"
        assert rejected["structuredContent"]["details"]["reason"] == "QUALITY_CHAIN"
        assert _version(session, task_id)[1] == "已完成"
        bare = dict(snapshot)
        bare["acceptance_criteria_items"] = []
        store.save_checkpoint(
            RunnerCheckpoint(
                task_id=task_id,
                project_id="alpha",
                state="PENDING_USER_ACCEPTANCE",
                current_role="PM",
                candidate_commit="b" * 40,
                execution_spec_snapshot=bare,
            )
        )
        missing = session.call(
            "request_acceptance",
            {
                "project_id": "alpha",
                "task_id": task_id,
                "idempotency_key": "snap-apply-0002",
                "expected_version": version,
                "evidence_id": uploaded["structuredContent"]["evidence_id"],
            },
        )
        assert missing["structuredContent"]["details"]["reason"] == "CONTRACT"
        store.save_checkpoint(
            RunnerCheckpoint(
                task_id=task_id,
                project_id="alpha",
                state="PENDING_USER_ACCEPTANCE",
                current_role="PM",
                candidate_commit="c" * 40,
                execution_spec_snapshot=snapshot,
            )
        )
        moved = session.call(
            "request_acceptance",
            {
                "project_id": "alpha",
                "task_id": task_id,
                "idempotency_key": "snap-apply-0003",
                "expected_version": version,
                "evidence_id": uploaded["structuredContent"]["evidence_id"],
            },
        )
        assert moved["structuredContent"]["code"] == "CONFLICT"
        assert moved["structuredContent"]["details"]["reason"] == "CANDIDATE"
    finally:
        session.close()
