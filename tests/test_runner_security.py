# -*- coding: utf-8 -*-
"""
tests/test_runner_security.py
Runner 缺陷回环、最大循环预算与 QA 源码不可变性安全边界测试。
"""
import json
import os
import subprocess
import pytest
from unittest.mock import MagicMock

from scripts._lib.core.adapter_registry import AdapterRegistry
from scripts._lib.core.agent_schema import (
    AgentHandle,
    AgentResult,
    AgentStatus,
    CapabilitySupport,
    HostCapabilities,
)
from scripts._lib.core.evidence_store import EvidenceStore
from scripts._lib.core.production_runner import ProductionRunner
from scripts._lib.core.runner_checkpoint_store import RunnerCheckpointStore
from scripts._lib.core.runner_schema import RunnerState, TaskExecutionSpec
from scripts._lib.hosts.antigravity_adapter import AntigravityAdapter, create_antigravity_manifest
from scripts._lib.hosts.codex_cli_adapter import CodexCliAdapter, create_codex_cli_manifest


@pytest.fixture
def mock_git_repo(tmp_path):
    repo_dir = tmp_path / "repo"
    repo_dir.mkdir()
    subprocess.run(["git", "init"], cwd=repo_dir, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.name", "TestDev"], cwd=repo_dir, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "test@test.com"], cwd=repo_dir, check=True, capture_output=True)
    
    readme = repo_dir / "README.md"
    readme.write_text("# Test Repo\n", encoding="utf-8")
    subprocess.run(["git", "add", "README.md"], cwd=repo_dir, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "initial commit"], cwd=repo_dir, check=True, capture_output=True)
    
    head_sha = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=repo_dir, text=True).strip()
    return repo_dir, head_sha


def test_reviewer_rejection_auto_loop_to_builder_and_exceed_max_cycles(mock_git_repo, tmp_path, monkeypatch):
    repo_dir, baseline_sha = mock_git_repo
    data_root = tmp_path / "data_root"
    data_root.mkdir()

    builder_call_count = 0
    reviewer_call_count = 0

    def mock_detect_caps(self):
        return HostCapabilities(
            is_real_host=True,
            supports_real_subagents=CapabilitySupport.SUPPORTED,
            supports_parallelism=CapabilitySupport.SUPPORTED,
            supports_isolated_context=CapabilitySupport.SUPPORTED,
            supports_worktree=CapabilitySupport.SUPPORTED,
            supports_permission_approval=CapabilitySupport.SUPPORTED,
            supports_mcp=CapabilitySupport.SUPPORTED,
            supports_interactive_confirmation=CapabilitySupport.UNSUPPORTED,
            supports_usage_telemetry=CapabilitySupport.SUPPORTED,
            max_concurrent_agents=4,
        )

    def mock_builder_dispatch(self, req):
        nonlocal builder_call_count
        builder_call_count += 1
        return AgentHandle(
            session_id=req.session_id,
            host_id="codex_cli",
            status="completed",
            is_real_host=True,
            adapter_instance_id="inst_codex",
            invocation_token="tok_builder",
        )

    def mock_builder_wait(self, handle, timeout_seconds=None):
        return AgentResult(
            session_id=handle.session_id,
            status=AgentStatus.SUCCESS,
            output="Builder fix attempted",
            is_real_host=True,
        )

    def mock_reviewer_dispatch(self, req):
        nonlocal reviewer_call_count
        reviewer_call_count += 1
        return AgentHandle(
            session_id=req.session_id,
            host_id="antigravity",
            status="completed",
            is_real_host=True,
            adapter_instance_id="inst_ag",
            invocation_token="tok_reviewer",
        )

    def mock_reviewer_wait(self, handle, timeout_seconds=None):
        out_json = {
            "task_id": "T0077",
            "baseline_commit": baseline_sha,
            "candidate_commit": baseline_sha,
            "session_id": handle.session_id,
            "host_invocation_id": f"inv_reviewer_{reviewer_call_count}",
            "decision": "REJECT",
            "defects": [
                {
                    "defect_id": f"DEF-T0077-{reviewer_call_count}",
                    "severity": "P1",
                    "description": "Code does not satisfy security criteria",
                }
            ],
            "summary": "Review rejected",
        }
        return AgentResult(
            session_id=handle.session_id,
            status=AgentStatus.SUCCESS,
            output=json.dumps(out_json),
            is_real_host=True,
        )

    monkeypatch.setattr(CodexCliAdapter, "detect_capabilities", mock_detect_caps)
    monkeypatch.setattr(AntigravityAdapter, "detect_capabilities", mock_detect_caps)
    monkeypatch.setattr(CodexCliAdapter, "dispatch_agent", mock_builder_dispatch)
    monkeypatch.setattr(CodexCliAdapter, "wait_for_result", mock_builder_wait)
    monkeypatch.setattr(AntigravityAdapter, "dispatch_agent", mock_reviewer_dispatch)
    monkeypatch.setattr(AntigravityAdapter, "wait_for_result", mock_reviewer_wait)

    registry = AdapterRegistry(context_id="test_repo")
    codex_manifest = create_codex_cli_manifest(adapter_id="codex_cli", verified_version="0.149.0")
    codex_adapter = CodexCliAdapter(is_real_host=True)
    registry.register(codex_adapter, codex_manifest)

    ag_manifest = create_antigravity_manifest(adapter_id="antigravity", verified_version="1.1.22")
    ag_adapter = AntigravityAdapter(is_real_host=True)
    registry.register(ag_adapter, ag_manifest)

    evidence_store = EvidenceStore(root_dir=str(data_root / "evidence"))
    checkpoint_store = RunnerCheckpointStore(data_root=str(data_root))

    runner = ProductionRunner(
        registry=registry,
        evidence_store=evidence_store,
        checkpoint_store=checkpoint_store,
    )

    spec = TaskExecutionSpec(
        project_id="test_repo",
        project_root=str(repo_dir),
        authority_root=str(repo_dir),
        task_id="T0077",
        task_name="安全加固模块",
        requirement_text="实现严格安全加固",
        acceptance_criteria="验收标准: 零漏洞",
        acceptance_criteria_hash="hash_777",
        task_version="1.0",
        status_at_read="进行中",
        baseline_commit=baseline_sha,
        workspace_mode="inherit",
        max_review_cycles=2,
    )

    result = runner.start(spec)
    assert result.success is False
    assert result.state == RunnerState.NEEDS_USER_INPUT.value
    assert reviewer_call_count == 2
    assert builder_call_count == 2
    assert "exceeded max review cycles" in result.message.lower()


def test_qa_source_immutability_violation_fails_closed(mock_git_repo, tmp_path, monkeypatch):
    repo_dir, baseline_sha = mock_git_repo
    data_root = tmp_path / "data_root"
    data_root.mkdir()

    def mock_detect_caps(self):
        return HostCapabilities(
            is_real_host=True,
            supports_real_subagents=CapabilitySupport.SUPPORTED,
            supports_parallelism=CapabilitySupport.SUPPORTED,
            supports_isolated_context=CapabilitySupport.SUPPORTED,
            supports_worktree=CapabilitySupport.SUPPORTED,
            supports_permission_approval=CapabilitySupport.SUPPORTED,
            supports_mcp=CapabilitySupport.SUPPORTED,
            supports_interactive_confirmation=CapabilitySupport.UNSUPPORTED,
            supports_usage_telemetry=CapabilitySupport.SUPPORTED,
            max_concurrent_agents=4,
        )

    def mock_builder_dispatch(self, req):
        return AgentHandle(session_id=req.session_id, host_id="codex_cli", status="completed", is_real_host=True, adapter_instance_id="inst_c", invocation_token="tok")

    def mock_builder_wait(self, handle, timeout_seconds=None):
        return AgentResult(session_id=handle.session_id, status=AgentStatus.SUCCESS, output="ok", is_real_host=True)

    def mock_reviewer_dispatch(self, req):
        return AgentHandle(session_id=req.session_id, host_id="antigravity", status="completed", is_real_host=True, adapter_instance_id="inst_a", invocation_token="tok")

    def mock_reviewer_wait(self, handle, timeout_seconds=None):
        out_json = {
            "task_id": "T0066",
            "baseline_commit": baseline_sha,
            "candidate_commit": baseline_sha,
            "session_id": handle.session_id,
            "host_invocation_id": "inv_r",
            "decision": "PASS",
            "defects": [],
            "summary": "pass",
        }
        return AgentResult(session_id=handle.session_id, status=AgentStatus.SUCCESS, output=json.dumps(out_json), is_real_host=True)

    def mock_qa_dispatch(self, req):
        return AgentHandle(session_id=req.session_id, host_id="codex_cli", status="completed", is_real_host=True, adapter_instance_id="inst_c", invocation_token="tok")

    def mock_qa_wait(self, handle, timeout_seconds=None):
        target_dir = str(repo_dir)
        readme_path = os.path.join(target_dir, "README.md")
        with open(readme_path, "a", encoding="utf-8") as f:
            f.write("\n# QA modified source code illegally!\n")
        return AgentResult(session_id=handle.session_id, status=AgentStatus.SUCCESS, output="QA done", is_real_host=True)

    monkeypatch.setattr(CodexCliAdapter, "detect_capabilities", mock_detect_caps)
    monkeypatch.setattr(AntigravityAdapter, "detect_capabilities", mock_detect_caps)
    monkeypatch.setattr(CodexCliAdapter, "dispatch_agent", mock_builder_dispatch)
    monkeypatch.setattr(CodexCliAdapter, "wait_for_result", mock_builder_wait)
    monkeypatch.setattr(AntigravityAdapter, "dispatch_agent", mock_reviewer_dispatch)
    monkeypatch.setattr(AntigravityAdapter, "wait_for_result", mock_reviewer_wait)
    monkeypatch.setattr(CodexCliAdapter, "dispatch_agent", mock_qa_dispatch)
    monkeypatch.setattr(CodexCliAdapter, "wait_for_result", mock_qa_wait)

    registry = AdapterRegistry(context_id="test_repo")
    codex_manifest = create_codex_cli_manifest(adapter_id="codex_cli", verified_version="0.149.0")
    codex_adapter = CodexCliAdapter(is_real_host=True)
    registry.register(codex_adapter, codex_manifest)

    ag_manifest = create_antigravity_manifest(adapter_id="antigravity", verified_version="1.1.22")
    ag_adapter = AntigravityAdapter(is_real_host=True)
    registry.register(ag_adapter, ag_manifest)

    evidence_store = EvidenceStore(root_dir=str(data_root / "evidence"))
    checkpoint_store = RunnerCheckpointStore(data_root=str(data_root))

    runner = ProductionRunner(
        registry=registry,
        evidence_store=evidence_store,
        checkpoint_store=checkpoint_store,
    )

    spec = TaskExecutionSpec(
        project_id="test_repo",
        project_root=str(repo_dir),
        authority_root=str(repo_dir),
        task_id="T0066",
        task_name="只读测试验证",
        requirement_text="只读测试",
        acceptance_criteria="验收标准: 测试通过",
        acceptance_criteria_hash="hash_666",
        task_version="1.0",
        status_at_read="进行中",
        baseline_commit=baseline_sha,
        workspace_mode="inherit",
        test_command="python -c \"print('ok')\"",
    )

    result = runner.start(spec)
    assert result.success is False
    assert result.state == RunnerState.FAILED.value
    assert "Code immutability boundary violated" in result.message
