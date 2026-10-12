# -*- coding: utf-8 -*-
"""DEF-T0034-1..4 复现与闭环。"""
import json
import os
import subprocess
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPTS = os.path.join(ROOT, "scripts")
if SCRIPTS not in sys.path:
    sys.path.insert(0, SCRIPTS)

from _lib.core.agent_schema import AgentNotSupportedError, AgentRequest, CapabilitySupport, HostCapabilities
from _lib.core.evidence_schema import EvidenceMetadata, EvidenceRecord, EvidenceType
from _lib.core.evidence_store import EvidenceStore
from _lib.core.runner_schema import RunnerCheckpoint
from _lib.core.runner_transition_gate import validate_managed_transition
from _lib.core.validate_transition import validate
from _lib.core.workflow_policy import (
    PolicyError,
    freeze_policy,
    resume_policy,
    select_profile,
)
from _lib.hosts.antigravity_adapter import RUNNER_MANAGED_AGENT_MAP as AG_MAP
from _lib.hosts.codex_cli_adapter import CodexCliAdapter
from _lib.hosts.cursor_sdk_adapter import RUNNER_MANAGED_AGENT_MAP as CURSOR_MAP
from _lib.core.orchestrator import Orchestrator
from _lib.core.orchestrator_schema import BuilderToReviewerHandover, OrchestrationGateError, OrchestrationState
from _lib.core.adapter_registry import AdapterRegistry
from _lib.core.production_runner import independent_review_allowed
from _lib.hosts.antigravity_adapter import AntigravityAdapter
from verify_and_export_agents import serialize_runner_verifier


DOC = """---
title: 说明
type: guide
---

只补充使用说明。
"""
REQ = "req-hash-1"
ACC = "ac-hash-1"


def _decision(**kwargs):
    payload = dict(task_type="A", requested="lite", requirement_hash=REQ, acceptance_criteria_hash=ACC)
    payload.update(kwargs)
    return select_profile(**payload)


def test_def1_remarks_cannot_complete_lite_or_docs():
    cmd = [
        sys.executable,
        os.path.join(SCRIPTS, "_lib", "core", "validate_transition.py"),
        "--role", "VERIFIER",
        "--from-status", "进行中",
        "--to-status", "已完成",
        "--assignee", "严经理",
        "--type", "A",
        "--end-time", "2026-10-12 12:00:00",
        "--remarks", "WF_POLICY profile=lite;version=3.0.0;risk=low;checks=1;verifier=not-an-evidence",
    ]
    env = os.environ.copy()
    env["PYTHONIOENCODING"] = "utf-8"
    result = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace", env=env)
    assert result.returncode != 0

    docs = subprocess.run(
        [
            sys.executable,
            os.path.join(SCRIPTS, "_lib", "core", "validate_transition.py"),
            "--role", "DEV",
            "--from-status", "进行中",
            "--to-status", "已完成",
            "--assignee", "严经理",
            "--type", "A",
            "--end-time", "2026-10-12 12:00:00",
            "--remarks", "WF_POLICY profile=docs-short;version=3.0.0;risk=low;checks=1;verifier=",
        ],
        capture_output=True, text=True, encoding="utf-8", errors="replace", env=env,
    )
    assert docs.returncode != 0
    assert validate(
        "DEV", "进行中", "已完成", "严经理", "2026-10-12 12:00:00", 0,
        task_type="A",
        workflow_policy={"profile_id": "docs-short", "risk_level": "low", "checks_passed": True, "requirement_hash": REQ, "acceptance_criteria_hash": ACC},
        changed_files=[{"path": "docs/guide.md", "content": DOC + "\n-----BEGIN PRIVATE KEY-----\n"}],
    ) is False


def test_def1_completion_reads_verifier_evidence(tmp_path):
    candidate = "a" * 40
    store = EvidenceStore(str(tmp_path / "evidence"))
    store.append(EvidenceRecord(
        evidence_id="evi_verifier_real",
        evidence_type=EvidenceType.TASK_TRANSITION,
        baseline_commit="b" * 40,
        result_commit=candidate,
        artifacts=(),
        metadata=EvidenceMetadata(
            project_id="p", task_id="T1", actor_role="VERIFIER",
            host_id="cursor-sdk", adapter="cursor-sdk",
            host_session_id="sess_verifier_t1", host_invocation_id="inv_verifier_1",
            is_real_host=False, workspace_mode="workspace_read",
            transition_from="BUILDING", transition_to="PENDING_USER_ACCEPTANCE",
            created_at=1.0,
            extra={"workflow_profile_id": "lite", "verifier_decision": "PASS"},
        ),
    ))
    store.append(EvidenceRecord(
        evidence_id="evi_reviewer_forged",
        evidence_type=EvidenceType.TASK_TRANSITION,
        baseline_commit="b" * 40,
        result_commit=candidate,
        artifacts=(),
        metadata=EvidenceMetadata(
            project_id="p", task_id="T1", actor_role="REVIEWER",
            host_id="cursor-sdk", adapter="cursor-sdk",
            host_session_id="sess_reviewer_t1", host_invocation_id="inv_reviewer_1",
            is_real_host=False, workspace_mode="workspace_read",
            transition_from="REVIEWING", transition_to="TESTING",
            created_at=1.0,
            extra={"verifier_decision": "PASS"},
        ),
    ))
    policy = {
        "profile_id": "lite", "risk_level": "low", "requirement_hash": REQ, "acceptance_criteria_hash": ACC,
        "verifier_evidence_id": "evi_verifier_real",
    }
    files = [{"path": "app.py", "content": "def ready():\n    return True\n"}]
    assert validate(
        "VERIFIER", "进行中", "已完成", "严经理", "2026-10-12 12:00:00", 0,
        task_type="A", workflow_policy=policy, changed_files=files,
        evidence_root=str(tmp_path / "evidence"), candidate_commit=candidate,
        builder_session_id="sess_builder_t1",
    )
    forged = dict(policy)
    forged["verifier_evidence_id"] = "evi_reviewer_forged"
    assert validate(
        "VERIFIER", "进行中", "已完成", "严经理", "2026-10-12 12:00:00", 0,
        task_type="A", workflow_policy=forged, changed_files=files,
        evidence_root=str(tmp_path / "evidence"), candidate_commit=candidate,
        builder_session_id="sess_builder_t1",
    ) is False
    assert validate(
        "DOCS", "进行中", "已完成", "严经理", "2026-10-12 12:00:00", 0,
        task_type="C",
        workflow_policy={"profile_id": "docs-short", "checks_passed": True, "risk_level": "low", "requirement_hash": REQ, "acceptance_criteria_hash": ACC},
        changed_files=[{"path": "docs/guide.md", "content": DOC}],
    )


def test_def2_verifier_is_distinct_readonly_managed_role(tmp_path):
    assert CURSOR_MAP["VERIFIER"] == "flow-runner-verifier"
    assert AG_MAP["VERIFIER"] == "flow-runner-verifier"
    assert CURSOR_MAP["VERIFIER"] != CURSOR_MAP["BUILDER"]
    content = serialize_runner_verifier("antigravity")
    frontmatter = content.split("---", 2)[1]
    parsed = __import__("yaml").safe_load(frontmatter)
    assert parsed["name"] == "flow-runner-verifier"
    assert parsed["enable_write_tools"] is False
    assert "run_command" not in parsed["tools"]
    adapter = CodexCliAdapter(is_real_host=False)
    command = adapter.build_codex_exec_command(AgentRequest(
        session_id="sess_verifier_codex",
        prompt="verify",
        role="VERIFIER",
        workspace_dir=os.path.abspath(ROOT),
    ))
    assert command[command.index("-s") + 1] == "read-only"
    with pytest.raises(AgentNotSupportedError):
        adapter.build_codex_exec_command(AgentRequest(
            session_id="sess_unknown",
            prompt="write",
            role="INTRUDER",
            workspace_dir=os.path.abspath(ROOT),
        ))
    orchestrator = Orchestrator(AdapterRegistry(context_id="def2"))
    session = orchestrator.start_builder(
        task_id="T0034", project_id="p", branch="feature", baseline_commit="b" * 40,
        assignee="李开发", workspace_dir=str(tmp_path), worktree_dir=str(tmp_path),
        auth_context="local", billing_context="local",
        builder_session_id="sess_builder_t0034", builder_invocation_id="inv_builder",
        builder_adapter_id="codex_cli",
        workflow_policy={"profile_id": "lite"},
    )
    assert session.workflow_policy["profile_id"] == "lite"


def test_def3_five_counterexamples_are_high_risk_and_rescanned():
    samples = [
        ("perm.py", "user.is_admin = True\n", "permission"),
        ("pay.py", "balance -= amount\n", "funds"),
        ("pii.py", "email = user.phone\n", "privacy"),
        ("mig.py", "op.drop_table(...)\n", "migration"),
        ("rel.py", "helm upgrade api\n", "deploy"),
    ]
    for path, content, category in samples:
        decision = _decision(files=[{"path": path, "content": content}], title="小改动", tags=["lite"], user_claims_low_risk=True)
        assert decision.risk_level == "high", path
        assert decision.blocked is True, path
        assert any(item["category"] == category for item in decision.risk_signals), path
    frozen = freeze_policy(_decision(files=[{"path": "app.py", "content": "x = 1\n"}]), "T9")
    upgraded = resume_policy(
        frozen, requirement_hash=REQ, acceptance_criteria_hash=ACC,
        candidate_commit="c" * 40, files=[{"path": "pay.py", "content": "balance -= amount\n"}],
        budget_consumed=1,
    )
    assert upgraded["ok"] is False


def test_def4_empty_hash_upgrade_and_candidate_binding(tmp_path):
    bare = _decision(files=[{"path": "app.py", "content": "x = 1\n"}], requirement_hash="", acceptance_criteria_hash="")
    with pytest.raises(PolicyError):
        freeze_policy(bare, "T9")
    frozen = freeze_policy(_decision(files=[{"path": "app.py", "content": "x = 1\n"}]), "T9")
    missing = resume_policy(
        frozen, requirement_hash="", acceptance_criteria_hash="",
        candidate_commit="c" * 40, files=[{"path": "app.py", "content": "x = 1\n"}],
        budget_consumed=1,
    )
    assert missing["ok"] is False
    changed = resume_policy(
        frozen, requirement_hash="other", acceptance_criteria_hash=ACC,
        candidate_commit="c" * 40, files=[{"path": "app.py", "content": "x = 1\n"}],
        budget_consumed=1,
    )
    assert changed["ok"] is False
    upgraded = resume_policy(
        frozen, requirement_hash=REQ, acceptance_criteria_hash=ACC,
        candidate_commit="c" * 40, files=[{"path": "app.py", "content": "x = 1\n"}],
        budget_consumed=2, requested_profile="standard",
    )
    assert upgraded["ok"] is True
    assert upgraded["profile_id"] == "standard"
    assert "independent_review" in upgraded["added_gates"]
    snapshot = {"completed_roles": ["BUILDER"], "budget": {"budget_consumed": 3}}
    from _lib.core.workflow_policy import backfill_standard
    before = json.dumps(snapshot)
    note = backfill_standard(snapshot)
    assert note["equivalent"] is False
    assert json.dumps(snapshot) == before

    candidate = "d" * 40
    other = "e" * 40
    caps = HostCapabilities(is_real_host=True, supports_real_subagents=CapabilitySupport.SUPPORTED)
    extra = {f"capability_{key}": value for key, value in caps.__dict__.items() if key != "extra"}
    extra.update({"workflow_profile_id": "lite", "verifier_decision": "PASS"})
    project = tmp_path / "repo"
    project.mkdir()
    store = EvidenceStore(str(project / "user_data" / "runner_evidence"))
    store.append(EvidenceRecord(
        evidence_id="evi_bound",
        evidence_type=EvidenceType.TASK_TRANSITION,
        baseline_commit="b" * 40,
        result_commit=candidate,
        artifacts=(),
        metadata=EvidenceMetadata(
            project_id="repo", task_id="T9", actor_role="VERIFIER",
            host_id="cursor-sdk", adapter="cursor-sdk",
            host_session_id="sess_verifier_t9", host_invocation_id="inv_verifier_9",
            is_real_host=True, workspace_mode="workspace_read",
            transition_from="BUILDING", transition_to="PENDING_USER_ACCEPTANCE",
            created_at=1.0, extra=extra,
        ),
    ))
    data = tmp_path / "data" / "runner_checkpoints" / "repo"
    data.mkdir(parents=True)
    checkpoint = RunnerCheckpoint(
        task_id="T9", project_id="repo", state="COMPLETION_PENDING", current_role="VERIFIER",
        candidate_commit=candidate, builder_session_id="sess_builder_t9",
        evidence_ids=("evi_bound",),
        execution_options={"workflow_policy": {"profile_id": "lite"}},
        execution_spec_snapshot={"project_root": str(project), "baseline_commit": "b" * 40},
    )
    (data / "T9.json").write_text(json.dumps(checkpoint.to_dict()), encoding="utf-8")
    validate_managed_transition(str(tmp_path / "data"), "T9", "进行中", "已完成", "evi_bound")
    checkpoint = RunnerCheckpoint(
        task_id="T9", project_id="repo", state="COMPLETION_PENDING", current_role="VERIFIER",
        candidate_commit="f" * 40, builder_session_id="sess_builder_t9",
        evidence_ids=("evi_bound",),
        execution_options={"workflow_policy": {"profile_id": "lite"}},
        execution_spec_snapshot={"project_root": str(project), "baseline_commit": "b" * 40},
    )
    (data / "T9.json").write_text(json.dumps(checkpoint.to_dict()), encoding="utf-8")
    with pytest.raises(Exception, match="current candidate"):
        validate_managed_transition(str(tmp_path / "data"), "T9", "进行中", "已完成", "evi_bound")


def test_def2_stranger_cannot_get_writable_antigravity_mode():
    adapter = AntigravityAdapter(is_real_host=False)
    with pytest.raises(AgentNotSupportedError, match="unknown"):
        adapter.build_antigravity_exec_command(AgentRequest(
            session_id="sess_stranger",
            prompt="mutate",
            role="STRANGER",
            workspace_dir=os.path.abspath(ROOT),
        ))


def test_def2_exported_cursor_verifier_is_readonly():
    path = os.path.join(ROOT, ".cursor", "agents", "flow-runner-verifier.md")
    assert os.path.isfile(path)
    frontmatter = open(path, encoding="utf-8").read().split("---", 2)[1]
    parsed = __import__("yaml").safe_load(frontmatter)
    assert parsed["name"] == "flow-runner-verifier"
    assert parsed["enable_write_tools"] is False
    tools = {str(tool).lower() for tool in parsed.get("tools") or []}
    assert tools.isdisjoint({"edit", "write", "write_to_file", "replace_file_content", "bash", "run_command"})


def test_def2_lite_review_is_rejected_and_standard_is_not(tmp_path):
    assert independent_review_allowed(None) is True
    assert independent_review_allowed({"profile_id": "standard"}) is True
    assert independent_review_allowed({"profile_id": "lite"}) is False
    registry = AdapterRegistry(context_id="def2-review")

    def _handover(task_id):
        return BuilderToReviewerHandover(
            task_id=task_id, project_id="p", branch="feature",
            baseline_commit="b" * 40, candidate_commit="c" * 40,
            modified_files=("app.py",), diff_stat={"files": 1}, test_summary={"ran": False},
            workspace_dir=str(tmp_path), worktree_dir=str(tmp_path),
            builder_session_id="sess_builder_std", builder_invocation_id="inv_builder",
            auth_context="local", billing_context="local",
        )

    lite = Orchestrator(registry, workflow_policy={"profile_id": "lite"})
    lite.start_builder(
        task_id="Tlite", project_id="p", branch="feature", baseline_commit="b" * 40,
        assignee="李开发", workspace_dir=str(tmp_path), worktree_dir=str(tmp_path),
        auth_context="local", billing_context="local",
        builder_session_id="sess_builder_std", builder_invocation_id="inv_builder",
        builder_adapter_id="codex_cli",
    )
    with pytest.raises(OrchestrationGateError, match="Lite"):
        lite.submit_to_reviewer(_handover("Tlite"))

    standard = Orchestrator(registry)
    standard.start_builder(
        task_id="Tstd", project_id="p", branch="feature", baseline_commit="b" * 40,
        assignee="李开发", workspace_dir=str(tmp_path), worktree_dir=str(tmp_path),
        auth_context="local", billing_context="local",
        builder_session_id="sess_builder_std", builder_invocation_id="inv_builder",
        builder_adapter_id="codex_cli",
    )
    session = standard.submit_to_reviewer(_handover("Tstd"))
    assert session.state == OrchestrationState.REVIEWING
