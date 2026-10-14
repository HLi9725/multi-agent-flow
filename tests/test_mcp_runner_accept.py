# -*- coding: utf-8 -*-
"""MCP 批准走 Runner.accept。这里的宿主标记是测试双重，不是真实用户演练。"""

from __future__ import annotations

import json
import subprocess
import textwrap
import time
from pathlib import Path

from scripts._lib.core.agent_schema import HostCapabilities
from scripts._lib.core.evidence_schema import EvidenceMetadata, EvidenceRecord, EvidenceType
from scripts._lib.core.evidence_store import EvidenceStore
from scripts._lib.core.production_runner import ProductionRunner, _execution_spec_snapshot, _extract_capabilities_extra
from scripts._lib.core.runner_checkpoint_store import RunnerCheckpointStore
from scripts._lib.core.runner_schema import RunnerCheckpoint, RunnerState, TaskExecutionSpec
from scripts._lib.core.task_spec_loader import load_task_execution_spec
from scripts._lib.mcp.acceptance import decide_acceptance
from scripts._lib.mcp.audit_store import AuditStore
from scripts._lib.mcp.auth import AuthContext
from scripts._lib.mcp.registry import load_registry
from scripts._lib.mcp.service import task_version
from scripts._lib.mcp.acceptance import create_application


class _Ledger:
    def bind(self, **_kwargs):
        return None


def _git(repo: Path, *args: str) -> str:
    return subprocess.check_output(["git", *args], cwd=repo, text=True).strip()


def _prepare(tmp: Path):
    repo = tmp / "repo"
    repo.mkdir()
    subprocess.check_call(["git", "init"], cwd=repo)
    subprocess.check_call(["git", "config", "user.email", "test@example.com"], cwd=repo)
    subprocess.check_call(["git", "config", "user.name", "Test"], cwd=repo)
    (repo / "app.txt").write_text("base\n", encoding="utf-8")
    (repo / "user_data").mkdir()
    (repo / "config").mkdir()
    (repo / "config" / "workflow.config.yaml").write_text(
        "board:\n  provider: local\n  board_file: user_data/board.json\n",
        encoding="utf-8",
    )
    (repo / ".git" / "info").mkdir(parents=True, exist_ok=True)
    (repo / ".git" / "info" / "exclude").write_text(
        "user_data/\nmcp_registry.json\nmcp_auth/\nmcp_audit/\nrunner_checkpoints/\n",
        encoding="utf-8",
    )
    subprocess.check_call(["git", "add", "."], cwd=repo)
    subprocess.check_call(["git", "commit", "-m", "base"], cwd=repo)
    baseline = _git(repo, "rev-parse", "HEAD")
    (repo / "app.txt").write_text("candidate\n", encoding="utf-8")
    subprocess.check_call(["git", "add", "."], cwd=repo)
    subprocess.check_call(["git", "commit", "-m", "candidate"], cwd=repo)
    candidate = _git(repo, "rev-parse", "HEAD")
    card = {
        "id": "T0778",
        "name": "runner验收",
        "status": "已完成",
        "type": "A",
        "assignee": "李开发",
        "owner": "李开发",
        "handler": "李开发",
        "end_time": "2026-10-14 16:00",
        "process": "需求: 完成候选。验收标准: app.txt 含有 candidate",
    }
    (repo / "user_data" / "board.json").write_text(json.dumps([card], ensure_ascii=False), encoding="utf-8")
    spec = load_task_execution_spec(str(repo), "T0778", authority_root=str(repo), overrides={"test_commands": ["python -m pytest"]})
    project_id = spec.project_id
    baseline = spec.baseline_commit or baseline
    commands = ["python -m pytest", f"git diff --check {baseline}..{candidate} --"]
    evidence = EvidenceStore(str(repo / "user_data" / "runner_evidence"))
    created = time.time()
    evidence_ids = []
    for index, (role, source, target, session) in enumerate((
        ("BUILDER", "BUILDING", "REVIEWING", "session_builder"),
        ("REVIEWER", "REVIEWING", "TESTING", "session_reviewer"),
        ("QA", "TESTING", "PENDING_USER_ACCEPTANCE", "session_qa"),
    )):
        evidence_id = f"evi_{role.lower()}_bound"
        extra = _extract_capabilities_extra(HostCapabilities(is_real_host=True))
        extra["acceptance_criteria_hash"] = spec.acceptance_criteria_hash
        extra["contract_hash"] = spec.requirement_hash
        if role == "REVIEWER":
            extra["decision"] = "PASS"
        if role == "QA":
            report = {
                "task_id": "T0778",
                "qa_request_id": "qa-req-bound",
                "baseline_commit": baseline,
                "candidate_commit": candidate,
                "session_id": session,
                "decision": "PASS",
                "acceptance_criteria_hash": spec.acceptance_criteria_hash,
                "acceptance_coverage": [{"criterion_id": "AC-01", "status": "PASS", "evidence": "app.txt"}],
                "negative_scenarios": [{"name": "missing marker", "status": "PASS", "evidence": "absent"}],
                "test_commands": [{"command": command, "exit_code": 0, "summary": "exit_code=0"} for command in commands],
                "uncovered_risks": [],
                "defects": [],
            }
            digest = lambda value: __import__("hashlib").sha256(
                json.dumps(value, ensure_ascii=False, sort_keys=True).encode("utf-8")
            ).hexdigest()
            results = [{"command": command, "exit_code": 0, "output_hash": "d" * 64} for command in commands]
            extra.update({
                "qa_decision": "PASS",
                "qa_request_id": "qa-req-bound",
                "qa_report": report,
                "qa_report_hash": digest(report),
                "required_test_commands": commands,
                "required_test_command_count": len(commands),
                "runner_test_results": results,
                "test_command_hash": digest(commands),
                "test_output_hash": digest(results),
                "test_exit_codes": [0, 0],
                "covered_criterion_ids": ["AC-01"],
                "negative_scenario_count": 1,
                "uncovered_risk_count": 0,
                "defect_count": 0,
            })
        evidence.append(EvidenceRecord(
            evidence_id=evidence_id,
            evidence_type=EvidenceType.TASK_TRANSITION,
            baseline_commit=baseline,
            result_commit=candidate,
            artifacts=(),
            metadata=EvidenceMetadata(
                project_id=project_id,
                task_id="T0778",
                actor_role=role,
                host_id=f"host_{role.lower()}",
                adapter="codex_cli",
                host_session_id=session,
                host_invocation_id=f"inv_{role.lower()}",
                is_real_host=True,
                workspace_mode="workspace_write" if role == "BUILDER" else "workspace_read",
                transition_from=source,
                transition_to=target,
                created_at=created + index,
                extra=extra,
            ),
        ))
        evidence_ids.append(evidence_id)
    spec = TaskExecutionSpec(
        project_id=project_id,
        project_root=str(repo),
        authority_root=str(repo),
        task_id="T0778",
        task_name=spec.task_name,
        requirement_text=spec.requirement_text,
        acceptance_criteria=spec.acceptance_criteria,
        acceptance_criteria_hash=spec.acceptance_criteria_hash,
        task_version=spec.task_version,
        status_at_read="已完成",
        requirement_hash=spec.requirement_hash,
        baseline_commit=baseline,
        workspace_mode="inherit",
        test_commands=("python -m pytest",),
        acceptance_criteria_items=spec.acceptance_criteria_items,
    )
    store = RunnerCheckpointStore(data_root=str(repo), project_root=str(repo), project_id=project_id)
    store.save_checkpoint(RunnerCheckpoint(
        task_id="T0778",
        project_id=project_id,
        state=RunnerState.PENDING_USER_ACCEPTANCE.value,
        current_role="QA",
        candidate_commit=candidate,
        worktree_path=str(repo),
        evidence_ids=tuple(evidence_ids),
        confirmation_request_id="runner-conf-not-mcp",
        reviewer_session_id="session_reviewer",
        qa_session_id="session_qa",
        reviewer_invocation_id="inv_reviewer",
        qa_invocation_id="inv_qa",
        execution_spec_snapshot=_execution_spec_snapshot(spec),
        execution_options={"test_commands": ["python -m pytest"]},
    ))
    from scripts._lib.mcp.acceptance import _os_user
    registry_path = repo / "mcp_registry.json"
    registry_path.write_text(json.dumps({
        "server_revision": "drill",
        "projects": [{
            "project_id": project_id,
            "display_name": "Drill",
            "org_id": "local",
            "data_root": str(repo),
            "board_file": str(repo / "user_data" / "board.json"),
            "evidence_root": str(repo / "user_data" / "runner_evidence"),
            "board_config": str(repo / "config" / "workflow.config.yaml"),
            "board_kind": "offline",
        }],
        "local_principals": [{
            "os_user": _os_user(),
            "principal_id": "principal-local",
            "org_id": "local",
            "role": "PM",
            "grants": {project_id: ["read", "acceptance"]},
        }],
    }, ensure_ascii=False), encoding="utf-8")
    return repo, candidate, registry_path, evidence_ids, project_id


def test_mcp_approve_uses_runner_accept_without_equating_ids(tmp_path: Path) -> None:
    """测试双重：宿主标记由夹具写入，不是本次真实用户确认。"""
    repo, candidate, registry_path, _ids, project_id = _prepare(tmp_path)
    registry = load_registry(str(registry_path))
    project = registry.get_project(project_id)
    assert project is not None
    card = json.loads((repo / "user_data" / "board.json").read_text(encoding="utf-8"))[0]
    principal = AuthContext("principal-local", "local", {project_id: frozenset({"read", "acceptance"})}, False, "PM")
    body = create_application(project, principal, {
        "task_id": "T0778",
        "evidence_id": "evi_qa_bound",
        "expected_version": task_version(card),
        "idempotency_key": "runner-apply-01",
    }, _Ledger(), "digest-runner-apply")
    assert body["confirmation_id"] != "runner-conf-not-mcp"
    record_path = repo / "mcp_auth" / "acceptance" / f"{body['confirmation_id']}.json"
    saved = json.loads(record_path.read_text(encoding="utf-8"))
    assert saved["runner_confirmation_request_id"] == "runner-conf-not-mcp"
    audit = AuditStore(str(repo / "mcp_audit"))
    result = decide_acceptance(registry, body["confirmation_id"], "approve", audit=audit)
    assert result["ok"] is True, result
    assert result["status"] == "已验收"
    board = json.loads((repo / "user_data" / "board.json").read_text(encoding="utf-8"))
    assert board[0]["status"] == "已验收"
    store = RunnerCheckpointStore(data_root=str(repo), project_root=str(repo), project_id=project_id)
    checkpoint = store.load_checkpoint("T0778")
    assert checkpoint.state == RunnerState.ACCEPTED.value
    assert checkpoint.confirmation_request_id == "runner-conf-not-mcp"
    user = EvidenceStore(str(repo / "user_data" / "runner_evidence")).read(checkpoint.evidence_ids[-1])
    assert user.evidence_type == EvidenceType.USER_CONFIRMATION
    assert user.metadata.extra["confirmation_id"] == "runner-conf-not-mcp"
    assert user.metadata.extra["mcp_confirmation_id"] == body["confirmation_id"]
    consumed = json.loads(record_path.read_text(encoding="utf-8"))
    assert consumed["consumed"] is True
    assert consumed["audit_ok"] is True
    script = textwrap.dedent(f"""
        import json
        from scripts._lib.mcp.registry import load_registry
        from scripts._lib.mcp.acceptance import decide_acceptance
        from scripts._lib.mcp.audit_store import AuditStore
        registry = load_registry({str(registry_path)!r})
        result = decide_acceptance(registry, {body['confirmation_id']!r}, 'approve', audit=AuditStore({str(repo / 'mcp_audit')!r}))
        print(json.dumps({{'ok': result.get('ok'), 'replayed': result.get('replayed'), 'status': result.get('status')}}))
    """)
    completed = subprocess.run(["python", "-c", script], cwd=Path.cwd(), capture_output=True, text=True)
    assert completed.returncode == 0, completed.stderr
    replay = json.loads(completed.stdout.strip().splitlines()[-1])
    assert replay["replayed"] is True
    assert replay["status"] == "已验收"
    board_again = json.loads((repo / "user_data" / "board.json").read_text(encoding="utf-8"))
    assert board_again[0]["status"] == "已验收"
    opposite = decide_acceptance(registry, body["confirmation_id"], "reject", audit=audit)
    assert opposite["reason"] == "DECISION"
    assert board_again[0]["status"] == "已验收"


def test_runner_reject_syncs_checkpoint_for_rework(tmp_path: Path) -> None:
    """测试双重。拒绝必须把检查点送回 Builder，而不是只改看板。"""
    repo, _candidate, registry_path, _ids, project_id = _prepare(tmp_path)
    registry = load_registry(str(registry_path))
    project = registry.get_project(project_id)
    card = json.loads((repo / "user_data" / "board.json").read_text(encoding="utf-8"))[0]
    principal = AuthContext("principal-local", "local", {project_id: frozenset({"read", "acceptance"})}, False, "PM")
    body = create_application(project, principal, {
        "task_id": "T0778",
        "evidence_id": "evi_qa_bound",
        "expected_version": task_version(card),
        "idempotency_key": "runner-reject-01",
    }, _Ledger(), "digest-runner-reject")
    result = decide_acceptance(registry, body["confirmation_id"], "reject", audit=AuditStore(str(repo / "mcp_audit")))
    assert result["ok"] is True, result
    assert result["status"] == "已退回"
    store = RunnerCheckpointStore(data_root=str(repo), project_root=str(repo), project_id=project_id)
    checkpoint = store.load_checkpoint("T0778")
    assert checkpoint.state == RunnerState.NEEDS_USER_INPUT.value
    assert checkpoint.current_role == "BUILDER"
    assert checkpoint.confirmation_request_id in (None, "")
    assert any(
        isinstance(item, dict) and item.get("mcp_confirmation_id") == body["confirmation_id"]
        for item in checkpoint.defects_history
    )
    version = task_version(json.loads((repo / "user_data" / "board.json").read_text(encoding="utf-8"))[0])
    replay = decide_acceptance(registry, body["confirmation_id"], "reject", audit=AuditStore(str(repo / "mcp_audit")))
    assert replay["replayed"] is True
    assert task_version(json.loads((repo / "user_data" / "board.json").read_text(encoding="utf-8"))[0]) == version
    assert len(store.load_checkpoint("T0778").defects_history) == len(checkpoint.defects_history)
    from scripts._lib.core.runner_schema import RunnerResult

    seen = {}

    def stop_before_host(self, spec, **kwargs):
        checkpoint = kwargs["existing_checkpoint"]
        seen["state"] = checkpoint.state
        seen["role"] = checkpoint.current_role
        seen["confirmation"] = checkpoint.confirmation_request_id
        return RunnerResult(True, checkpoint.state, spec.task_id, message="测试双重在宿主执行前停下")

    monkeypatch = __import__("pytest").MonkeyPatch()
    monkeypatch.setattr(ProductionRunner, "_execute_with_checkpoint_guard", stop_before_host)
    try:
        resumed = ProductionRunner(checkpoint_store=store).resume(str(repo), "T0778", authority_root=str(repo))
    finally:
        monkeypatch.undo()
    assert resumed.success is True
    assert seen["state"] == RunnerState.NEEDS_USER_INPUT.value
    assert seen["role"] == "BUILDER"
    assert seen["confirmation"] in (None, "")
    assert "awaiting explicit user acceptance" not in resumed.message


def _open(tmp_path: Path):
    repo, candidate, registry_path, ids, project_id = _prepare(tmp_path)
    registry = load_registry(str(registry_path))
    project = registry.get_project(project_id)
    card = json.loads((repo / "user_data" / "board.json").read_text(encoding="utf-8"))[0]
    principal = AuthContext("principal-local", "local", {project_id: frozenset({"read", "acceptance"})}, False, "PM")
    return repo, candidate, registry_path, registry, project, card, principal, project_id


def _user_confirmations(repo: Path) -> int:
    store = EvidenceStore(str(repo / "user_data" / "runner_evidence"))
    count = 0
    root = repo / "user_data" / "runner_evidence"
    if not root.exists():
        return 0
    for path in root.glob("*.json"):
        record = store.read(path.stem)
        if record.evidence_type == EvidenceType.USER_CONFIRMATION:
            count += 1
    return count


def _ok_audits(repo: Path, confirmation_id: str) -> int:
    del confirmation_id
    path = repo / "mcp_audit" / "events.jsonl"
    if not path.exists():
        return 0
    rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
    return len([row for row in rows if row.get("tool") == "acceptance_decide" and row.get("code") == "OK"])


def _child_decide(registry_path: Path, confirmation_id: str, decision: str, audit_dir: Path) -> dict:
    source = (
        "import json,sys; from scripts._lib.mcp.registry import load_registry; "
        "from scripts._lib.mcp.acceptance import decide_acceptance; "
        "from scripts._lib.mcp.audit_store import AuditStore; "
        "print(json.dumps(decide_acceptance(load_registry(sys.argv[1]),sys.argv[2],sys.argv[3],audit=AuditStore(sys.argv[4]))))"
    )
    child = subprocess.run(
        ["python", "-c", source, str(registry_path), confirmation_id, decision, str(audit_dir)],
        cwd=Path.cwd(), capture_output=True, text=True, timeout=60,
    )
    assert child.returncode == 0, child.stderr
    return json.loads(child.stdout.strip().splitlines()[-1])


def test_runner_accept_ledger_failure_recovers_after_expiry(tmp_path: Path, monkeypatch) -> None:
    """测试双重。批准已落盘后消费记录失败，过期后第二个进程只补记。"""
    import scripts._lib.mcp.acceptance as acceptance_mod

    repo, _candidate, registry_path, registry, project, card, _principal, project_id = _open(tmp_path)
    body = create_application(project, _principal, {
        "task_id": "T0778", "evidence_id": "evi_qa_bound",
        "expected_version": task_version(card), "idempotency_key": "ledger-approve",
    }, _Ledger(), "digest-ledger-approve")
    real = acceptance_mod._persist
    failed = {"done": False}

    def persist(project_record, path, record):
        if record.get("consumed") and not failed["done"]:
            failed["done"] = True
            return False
        return real(project_record, path, record)

    monkeypatch.setattr(acceptance_mod, "_persist", persist)
    first = decide_acceptance(registry, body["confirmation_id"], "approve", audit=AuditStore(str(repo / "mcp_audit")))
    assert first["code"] == "APPLIED_LEDGER_PENDING", first
    version = task_version(json.loads((repo / "user_data" / "board.json").read_text(encoding="utf-8"))[0])
    confirmations = _user_confirmations(repo)
    intent_path = repo / "mcp_auth" / "acceptance" / f"{body['confirmation_id']}.json"
    intent = json.loads(intent_path.read_text(encoding="utf-8"))
    intent["expires_at"] = "2000-01-01T00:00:00Z"
    intent_path.write_text(json.dumps(intent, ensure_ascii=False), encoding="utf-8")
    monkeypatch.undo()
    recovered = _child_decide(registry_path, body["confirmation_id"], "approve", repo / "mcp_audit")
    assert recovered["ok"] is True and recovered["status"] == "已验收"
    assert task_version(json.loads((repo / "user_data" / "board.json").read_text(encoding="utf-8"))[0]) == version
    assert _user_confirmations(repo) == confirmations == 1
    assert _ok_audits(repo, body["confirmation_id"]) == 1
    store = RunnerCheckpointStore(data_root=str(repo), project_root=str(repo), project_id=project_id)
    assert store.load_checkpoint("T0778").state == RunnerState.ACCEPTED.value
    assert store.load_checkpoint("T0778").confirmation_request_id == "runner-conf-not-mcp"


def test_evidence_checkpoint_save_failure_reuses_user_confirmation(tmp_path: Path, monkeypatch) -> None:
    """测试双重。用户证据已写入但检查点编号未保存时，再次批准不新增证据。"""
    repo, _candidate, _registry_path, registry, project, card, principal, project_id = _open(tmp_path)
    body = create_application(project, principal, {
        "task_id": "T0778", "evidence_id": "evi_qa_bound",
        "expected_version": task_version(card), "idempotency_key": "evidence-save",
    }, _Ledger(), "digest-evidence-save")
    real = RunnerCheckpointStore.save_checkpoint
    failed = {"done": False}

    def save(self, checkpoint):
        if (
            not failed["done"]
            and checkpoint.state == RunnerState.PENDING_USER_ACCEPTANCE.value
            and any(str(item).startswith("evi_user_") for item in checkpoint.evidence_ids)
        ):
            failed["done"] = True
            raise OSError("checkpoint disk")
        return real(self, checkpoint)

    monkeypatch.setattr(RunnerCheckpointStore, "save_checkpoint", save)
    first = decide_acceptance(registry, body["confirmation_id"], "approve", audit=AuditStore(str(repo / "mcp_audit")))
    assert first["ok"] is False, first
    assert json.loads((repo / "user_data" / "board.json").read_text(encoding="utf-8"))[0]["status"] == "已完成"
    assert _user_confirmations(repo) == 1
    monkeypatch.undo()
    second = decide_acceptance(registry, body["confirmation_id"], "approve", audit=AuditStore(str(repo / "mcp_audit")))
    assert second["ok"] is True and second["status"] == "已验收", second
    assert _user_confirmations(repo) == 1
    store = RunnerCheckpointStore(data_root=str(repo), project_root=str(repo), project_id=project_id)
    assert store.load_checkpoint("T0778").state == RunnerState.ACCEPTED.value


def test_duplicate_user_confirmation_file_does_not_block_linked_recovery(tmp_path: Path) -> None:
    """测试双重。目录里多一份未挂接证据时，恢复仍认检查点里的那一条。"""
    repo, candidate, registry_path, registry, project, card, principal, project_id = _open(tmp_path)
    body = create_application(project, principal, {
        "task_id": "T0778", "evidence_id": "evi_qa_bound",
        "expected_version": task_version(card), "idempotency_key": "duplicate-evidence",
    }, _Ledger(), "digest-duplicate-evidence")
    assert decide_acceptance(registry, body["confirmation_id"], "approve", audit=AuditStore(str(repo / "mcp_audit")))["ok"] is True
    store = RunnerCheckpointStore(data_root=str(repo), project_root=str(repo), project_id=project_id)
    linked = store.load_checkpoint("T0778").evidence_ids[-1]
    source = repo / "user_data" / "runner_evidence" / f"{linked}.json"
    stray = repo / "user_data" / "runner_evidence" / "aaa_duplicate_user.json"
    payload = json.loads(source.read_text(encoding="utf-8"))
    payload["evidence_id"] = "aaa_duplicate_user"
    stray.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    record_path = repo / "mcp_auth" / "acceptance" / f"{body['confirmation_id']}.json"
    record = json.loads(record_path.read_text(encoding="utf-8"))
    record["consumed"] = False
    record["expires_at"] = "2000-01-01T00:00:00Z"
    record_path.write_text(json.dumps(record, ensure_ascii=False), encoding="utf-8")
    version = task_version(json.loads((repo / "user_data" / "board.json").read_text(encoding="utf-8"))[0])
    recovered = _child_decide(registry_path, body["confirmation_id"], "approve", repo / "mcp_audit")
    assert recovered["ok"] is True and recovered["status"] == "已验收"
    assert task_version(json.loads((repo / "user_data" / "board.json").read_text(encoding="utf-8"))[0]) == version
    assert store.load_checkpoint("T0778").evidence_ids[-1] == linked
    del candidate


def test_unapplied_expired_runner_approval_is_rejected(tmp_path: Path) -> None:
    """测试双重。尚未应用的过期批准不能执行。"""
    repo, _candidate, _registry_path, registry, project, card, principal, project_id = _open(tmp_path)
    body = create_application(project, principal, {
        "task_id": "T0778", "evidence_id": "evi_qa_bound",
        "expected_version": task_version(card), "idempotency_key": "expired-approve",
    }, _Ledger(), "digest-expired-approve")
    intent_path = repo / "mcp_auth" / "acceptance" / f"{body['confirmation_id']}.json"
    intent = json.loads(intent_path.read_text(encoding="utf-8"))
    intent["expires_at"] = "2000-01-01T00:00:00Z"
    intent_path.write_text(json.dumps(intent, ensure_ascii=False), encoding="utf-8")
    result = decide_acceptance(registry, body["confirmation_id"], "approve", audit=AuditStore(str(repo / "mcp_audit")))
    assert result["reason"] == "EXPIRED"
    assert json.loads((repo / "user_data" / "board.json").read_text(encoding="utf-8"))[0]["status"] == "已完成"
    store = RunnerCheckpointStore(data_root=str(repo), project_root=str(repo), project_id=project_id)
    assert store.load_checkpoint("T0778").state == RunnerState.PENDING_USER_ACCEPTANCE.value
    assert _user_confirmations(repo) == 0


def test_historical_reject_without_runner_binding_is_not_synced(tmp_path: Path) -> None:
    """测试双重。没有 Runner 确认绑定的旧拒绝记录不能靠看板状态猜测补检查点。"""
    from scripts._lib.mcp.acceptance import plan_rejection_checkpoint_sync

    repo, _candidate, registry_path, registry, project, card, principal, _project_id = _open(tmp_path)
    body = create_application(project, principal, {
        "task_id": "T0778", "evidence_id": "evi_qa_bound",
        "expected_version": task_version(card), "idempotency_key": "historical-reject",
    }, _Ledger(), "digest-historical-reject")
    path = repo / "mcp_auth" / "acceptance" / f"{body['confirmation_id']}.json"
    record = json.loads(path.read_text(encoding="utf-8"))
    record.pop("runner_confirmation_request_id", None)
    record.update(decision="reject", consumed=True, audit_ok=True)
    path.write_text(json.dumps(record, ensure_ascii=False), encoding="utf-8")
    card["status"] = "已退回"
    card["remarks"] = "用户拒绝验收 acceptance-decision:%s:reject" % body["confirmation_id"]
    (repo / "user_data" / "board.json").write_text(json.dumps([card], ensure_ascii=False), encoding="utf-8")
    plan = plan_rejection_checkpoint_sync(registry, body["confirmation_id"])
    assert plan["ok"] is False and plan["reason"] == "UNPROVEN"
    assert plan["writes"] == []
    assert load_registry(str(registry_path)).get_project(project.project_id) is not None


def test_accept_checkpoint_save_failure_recovers_in_new_process(tmp_path: Path, monkeypatch) -> None:
    """测试双重。看板已验收后检查点终态保存失败，恢复只补检查点。"""
    repo, _candidate, registry_path, registry, project, card, principal, project_id = _open(tmp_path)
    body = create_application(project, principal, {
        "task_id": "T0778", "evidence_id": "evi_qa_bound",
        "expected_version": task_version(card), "idempotency_key": "checkpoint-approve",
    }, _Ledger(), "digest-checkpoint-approve")
    real = RunnerCheckpointStore.save_checkpoint

    def save(self, checkpoint):
        if checkpoint.state == RunnerState.ACCEPTED.value:
            raise OSError("checkpoint disk")
        return real(self, checkpoint)

    monkeypatch.setattr(RunnerCheckpointStore, "save_checkpoint", save)
    first = decide_acceptance(registry, body["confirmation_id"], "approve", audit=AuditStore(str(repo / "mcp_audit")))
    assert first["ok"] is False, first
    assert json.loads((repo / "user_data" / "board.json").read_text(encoding="utf-8"))[0]["status"] == "已验收"
    assert _user_confirmations(repo) == 1
    version = task_version(json.loads((repo / "user_data" / "board.json").read_text(encoding="utf-8"))[0])
    monkeypatch.undo()
    recovered = _child_decide(registry_path, body["confirmation_id"], "approve", repo / "mcp_audit")
    assert recovered["ok"] is True and recovered["status"] == "已验收"
    assert task_version(json.loads((repo / "user_data" / "board.json").read_text(encoding="utf-8"))[0]) == version
    assert _user_confirmations(repo) == 1
    store = RunnerCheckpointStore(data_root=str(repo), project_root=str(repo), project_id=project_id)
    assert store.load_checkpoint("T0778").state == RunnerState.ACCEPTED.value


def test_reject_checkpoint_save_failure_recovers_without_duplicate_defect(tmp_path: Path, monkeypatch) -> None:
    """测试双重。看板已退回后检查点保存失败，恢复只补一条缺陷。"""
    repo, _candidate, registry_path, registry, project, card, principal, project_id = _open(tmp_path)
    body = create_application(project, principal, {
        "task_id": "T0778", "evidence_id": "evi_qa_bound",
        "expected_version": task_version(card), "idempotency_key": "checkpoint-reject",
    }, _Ledger(), "digest-checkpoint-reject")
    real = RunnerCheckpointStore.save_checkpoint

    def save(self, checkpoint):
        if checkpoint.state == RunnerState.NEEDS_USER_INPUT.value:
            raise OSError("checkpoint disk")
        return real(self, checkpoint)

    monkeypatch.setattr(RunnerCheckpointStore, "save_checkpoint", save)
    first = decide_acceptance(registry, body["confirmation_id"], "reject", audit=AuditStore(str(repo / "mcp_audit")))
    assert first["ok"] is False, first
    assert json.loads((repo / "user_data" / "board.json").read_text(encoding="utf-8"))[0]["status"] == "已退回"
    store = RunnerCheckpointStore(data_root=str(repo), project_root=str(repo), project_id=project_id)
    assert store.load_checkpoint("T0778").state == RunnerState.REJECTION_PENDING.value
    version = task_version(json.loads((repo / "user_data" / "board.json").read_text(encoding="utf-8"))[0])
    monkeypatch.undo()
    recovered = _child_decide(registry_path, body["confirmation_id"], "reject", repo / "mcp_audit")
    assert recovered["ok"] is True and recovered["status"] == "已退回"
    assert task_version(json.loads((repo / "user_data" / "board.json").read_text(encoding="utf-8"))[0]) == version
    checkpoint = store.load_checkpoint("T0778")
    assert checkpoint.state == RunnerState.NEEDS_USER_INPUT.value
    assert checkpoint.confirmation_request_id in (None, "")
    matched = [
        item for item in checkpoint.defects_history
        if isinstance(item, dict) and item.get("mcp_confirmation_id") == body["confirmation_id"]
    ]
    assert len(matched) == 1
    replay = _child_decide(registry_path, body["confirmation_id"], "reject", repo / "mcp_audit")
    assert replay.get("replayed") is True
    assert len(store.load_checkpoint("T0778").defects_history) == len(checkpoint.defects_history)


def test_wrong_binding_cannot_recover_applied_runner_decision(tmp_path: Path) -> None:
    """测试双重。错误主体、候选、契约或相反决定不能把已应用批准再执行一遍。"""
    repo, candidate, _registry_path, registry, project, card, principal, project_id = _open(tmp_path)
    body = create_application(project, principal, {
        "task_id": "T0778", "evidence_id": "evi_qa_bound",
        "expected_version": task_version(card), "idempotency_key": "binding-approve",
    }, _Ledger(), "digest-binding-approve")
    other = create_application(project, principal, {
        "task_id": "T0778", "evidence_id": "evi_qa_bound",
        "expected_version": task_version(card), "idempotency_key": "binding-other",
    }, _Ledger(), "digest-binding-other")
    audit = AuditStore(str(repo / "mcp_audit"))
    assert decide_acceptance(registry, body["confirmation_id"], "approve", audit=audit)["ok"] is True
    version = task_version(json.loads((repo / "user_data" / "board.json").read_text(encoding="utf-8"))[0])
    path = repo / "mcp_auth" / "acceptance" / f"{body['confirmation_id']}.json"
    original = json.loads(path.read_text(encoding="utf-8"))

    def rewrite(**changes):
        payload = json.loads(path.read_text(encoding="utf-8"))
        payload.update(changes)
        path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")

    rewrite(consumed=False, principal_id="someone-else")
    assert decide_acceptance(registry, body["confirmation_id"], "approve", audit=audit)["reason"] == "PRINCIPAL"
    rewrite(consumed=False, principal_id=original["principal_id"], candidate_sha="ab" * 20)
    assert decide_acceptance(registry, body["confirmation_id"], "approve", audit=audit)["reason"] == "CANDIDATE"
    rewrite(consumed=False, candidate_sha=candidate, recovery_contract_hash="c" * 64, contract_hash="d" * 64)
    assert decide_acceptance(registry, body["confirmation_id"], "approve", audit=audit)["reason"] == "CONTRACT"
    path.write_text(json.dumps(original, ensure_ascii=False), encoding="utf-8")
    assert decide_acceptance(registry, body["confirmation_id"], "reject", audit=audit)["reason"] == "DECISION"
    refused = decide_acceptance(registry, other["confirmation_id"], "approve", audit=audit)
    assert refused["ok"] is False
    assert task_version(json.loads((repo / "user_data" / "board.json").read_text(encoding="utf-8"))[0]) == version
    assert _user_confirmations(repo) == 1
    store = RunnerCheckpointStore(data_root=str(repo), project_root=str(repo), project_id=project_id)
    assert store.load_checkpoint("T0778").confirmation_request_id == "runner-conf-not-mcp"


def test_reject_ledger_and_audit_failures_leave_one_history(tmp_path: Path, monkeypatch) -> None:
    """测试双重。拒绝消费记录或审计失败后，缺陷历史和成功审计都不重复。"""
    import scripts._lib.mcp.acceptance as acceptance_mod
    from scripts._lib.mcp.audit_store import AuditWriteError

    repo, _candidate, registry_path, registry, project, card, principal, project_id = _open(tmp_path)
    body = create_application(project, principal, {
        "task_id": "T0778", "evidence_id": "evi_qa_bound",
        "expected_version": task_version(card), "idempotency_key": "reject-ledger",
    }, _Ledger(), "digest-reject-ledger")
    real = acceptance_mod._persist
    failed = {"done": False}

    def persist(project_record, path, record):
        if record.get("consumed") and not failed["done"]:
            failed["done"] = True
            return False
        return real(project_record, path, record)

    monkeypatch.setattr(acceptance_mod, "_persist", persist)
    first = decide_acceptance(registry, body["confirmation_id"], "reject", audit=AuditStore(str(repo / "mcp_audit")))
    assert first["code"] == "APPLIED_LEDGER_PENDING", first
    version = task_version(json.loads((repo / "user_data" / "board.json").read_text(encoding="utf-8"))[0])
    monkeypatch.undo()
    recovered = _child_decide(registry_path, body["confirmation_id"], "reject", repo / "mcp_audit")
    assert recovered["ok"] is True
    assert task_version(json.loads((repo / "user_data" / "board.json").read_text(encoding="utf-8"))[0]) == version
    store = RunnerCheckpointStore(data_root=str(repo), project_root=str(repo), project_id=project_id)
    assert len(store.load_checkpoint("T0778").defects_history) == 1
    assert _ok_audits(repo, body["confirmation_id"]) == 1

    (tmp_path / "audit").mkdir()
    repo2, _c2, registry_path2, registry2, project2, card2, principal2, project2_id = _open(tmp_path / "audit")
    body2 = create_application(project2, principal2, {
        "task_id": "T0778", "evidence_id": "evi_qa_bound",
        "expected_version": task_version(card2), "idempotency_key": "reject-audit",
    }, _Ledger(), "digest-reject-audit")
    real_append = AuditStore.append
    tripped = {"done": False}

    def append(self, **kwargs):
        if kwargs.get("code") == "OK" and not tripped["done"]:
            tripped["done"] = True
            raise AuditWriteError()
        return real_append(self, **kwargs)

    monkeypatch.setattr(AuditStore, "append", append)
    pending = decide_acceptance(registry2, body2["confirmation_id"], "reject", audit=AuditStore(str(repo2 / "mcp_audit")))
    assert pending["code"] == "APPLIED_AUDIT_PENDING", pending
    version2 = task_version(json.loads((repo2 / "user_data" / "board.json").read_text(encoding="utf-8"))[0])
    monkeypatch.undo()
    filled = _child_decide(registry_path2, body2["confirmation_id"], "reject", repo2 / "mcp_audit")
    assert filled["ok"] is True
    assert task_version(json.loads((repo2 / "user_data" / "board.json").read_text(encoding="utf-8"))[0]) == version2
    store2 = RunnerCheckpointStore(data_root=str(repo2), project_root=str(repo2), project_id=project2_id)
    assert len(store2.load_checkpoint("T0778").defects_history) == 1
    assert _ok_audits(repo2, body2["confirmation_id"]) == 1


def test_concurrent_approve_and_reject_do_not_cross_states(tmp_path: Path) -> None:
    """测试双重。并发批准和拒绝只有一个决定能落盘。"""
    import threading

    repo, _candidate, _registry_path, registry, project, card, principal, project_id = _open(tmp_path)
    approve = create_application(project, principal, {
        "task_id": "T0778", "evidence_id": "evi_qa_bound",
        "expected_version": task_version(card), "idempotency_key": "race-approve",
    }, _Ledger(), "digest-race-approve")
    reject = create_application(project, principal, {
        "task_id": "T0778", "evidence_id": "evi_qa_bound",
        "expected_version": task_version(card), "idempotency_key": "race-reject",
    }, _Ledger(), "digest-race-reject")
    results = []

    def run(confirmation_id: str, decision: str) -> None:
        results.append(decide_acceptance(
            registry, confirmation_id, decision, audit=AuditStore(str(repo / "mcp_audit")),
        ))

    threads = [
        threading.Thread(target=run, args=(approve["confirmation_id"], "approve")),
        threading.Thread(target=run, args=(reject["confirmation_id"], "reject")),
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    winners = [item for item in results if item.get("ok")]
    assert len(winners) == 1, results
    board = json.loads((repo / "user_data" / "board.json").read_text(encoding="utf-8"))[0]
    store = RunnerCheckpointStore(data_root=str(repo), project_root=str(repo), project_id=project_id)
    checkpoint = store.load_checkpoint("T0778")
    if winners[0]["decision"] == "approve":
        assert board["status"] == "已验收" and checkpoint.state == RunnerState.ACCEPTED.value
        assert _user_confirmations(repo) == 1
    else:
        assert board["status"] == "已退回" and checkpoint.state == RunnerState.NEEDS_USER_INPUT.value
        assert len(checkpoint.defects_history) == 1
    version = task_version(board)
    replay = decide_acceptance(registry, winners[0]["confirmation_id"], winners[0]["decision"], audit=AuditStore(str(repo / "mcp_audit")))
    assert replay.get("replayed") is True
    assert task_version(json.loads((repo / "user_data" / "board.json").read_text(encoding="utf-8"))[0]) == version


def test_lite_and_docs_short_runner_acceptance_stay_compatible(tmp_path: Path) -> None:
    """测试双重。Lite 与文档短链仍走正式 Runner，不退回成只改看板。"""
    for profile, role, evidence_id in (
        ("lite", "VERIFIER", "evi_verifier_bound"),
        ("docs-short", "DOCS_CHECK", "evi_docs_bound"),
    ):
        root = tmp_path / profile
        root.mkdir()
        repo, candidate, _registry_path, registry, project, card, principal, project_id = _open(root)
        store = RunnerCheckpointStore(data_root=str(repo), project_root=str(repo), project_id=project_id)
        checkpoint = store.load_checkpoint("T0778")
        spec = load_task_execution_spec(str(repo), "T0778", authority_root=str(repo), overrides={"test_commands": ["python -m pytest"]})
        evidence = EvidenceStore(str(repo / "user_data" / "runner_evidence"))
        extra = _extract_capabilities_extra(HostCapabilities(is_real_host=True))
        checks = {
            "diff_check": {"exit_code": 0, "passed": True, "summary": "clean"},
            "security_scan": {"exit_code": 0, "passed": True, "summary": "clean"},
            "tests": [{"command": "python -m pytest", "exit_code": 0, "summary": "passed"}],
            "all_passed": True,
        }
        report = {
            "decision": "PASS",
            "candidate_commit": candidate,
            "acceptance_criteria_hash": spec.acceptance_criteria_hash,
            "acceptance_coverage": [{"criterion_id": "AC-01", "status": "PASS"}],
            "runner_checks": checks,
        }
        extra.update({
            "acceptance_criteria_hash": spec.acceptance_criteria_hash,
            "contract_hash": spec.requirement_hash,
            "workflow_profile_id": profile,
            "verifier_decision": "PASS",
            "runner_checks": checks,
            "verifier_report": report,
            "checks": "secret_scan,doc_metadata,path_depth",
        })
        source, target = (("BUILDING", "PENDING_USER_ACCEPTANCE") if profile == "lite" else ("IN_PROGRESS", "COMPLETED"))
        evidence.append(EvidenceRecord(
            evidence_id=evidence_id,
            evidence_type=EvidenceType.TASK_TRANSITION,
            baseline_commit=spec.baseline_commit,
            result_commit=candidate,
            artifacts=(),
            metadata=EvidenceMetadata(
                project_id=project_id,
                task_id="T0778",
                actor_role=role,
                host_id="host_lightweight",
                adapter="codex_cli",
                host_session_id="sess-verifier-independent",
                host_invocation_id="inv_lightweight",
                is_real_host=True,
                workspace_mode="workspace_read",
                transition_from=source,
                transition_to=target,
                created_at=time.time(),
                extra=extra,
            ),
        ))
        options = dict(checkpoint.execution_options)
        options["workflow_policy"] = {"profile_id": profile}
        store.save_checkpoint(RunnerCheckpoint(
            task_id="T0778",
            project_id=project_id,
            state=checkpoint.state,
            current_role=checkpoint.current_role,
            candidate_commit=checkpoint.candidate_commit,
            worktree_path=checkpoint.worktree_path,
            evidence_ids=("evi_builder_bound", evidence_id),
            confirmation_request_id=checkpoint.confirmation_request_id,
            builder_session_id="session_builder",
            verifier_session_id="sess-verifier-independent",
            verifier_invocation_id="inv_lightweight",
            execution_spec_snapshot=checkpoint.execution_spec_snapshot,
            execution_options=options,
        ))
        body = create_application(project, principal, {
            "task_id": "T0778",
            "evidence_id": evidence_id,
            "expected_version": task_version(card),
            "idempotency_key": "light-" + profile,
        }, _Ledger(), "digest-" + profile)
        result = decide_acceptance(registry, body["confirmation_id"], "approve", audit=AuditStore(str(repo / "mcp_audit")))
        assert result["ok"] is True, (profile, result)
        saved = store.load_checkpoint("T0778")
        assert saved.state == RunnerState.ACCEPTED.value
        assert body["confirmation_id"] != saved.confirmation_request_id
