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
