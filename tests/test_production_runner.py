# -*- coding: utf-8 -*-
"""
tests/test_production_runner.py
ProductionRunner 完整编排测试。
"""
import json
import hashlib
import os
import subprocess
import time
import pytest
import yaml

from scripts._lib.core.adapter_registry import AdapterRegistry
from scripts._lib.core.agent_schema import (
    AgentHandle,
    AgentResult,
    AgentStatus,
    CapabilitySupport,
    HostCapabilities,
)
from scripts._lib.core.evidence_gate import EvidenceGate
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
    config_dir = repo_dir / "config"
    config_dir.mkdir()
    user_data_dir = repo_dir / "user_data"
    user_data_dir.mkdir()
    board_file = user_data_dir / "board.json"
    with open(config_dir / "workflow.config.yaml", "w", encoding="utf-8") as stream:
        yaml.safe_dump({"board": {"provider": "local", "board_file": str(board_file)}}, stream)
    board_file.write_text(json.dumps([{
        "id": "T0088", "name": "实现测试功能", "status": "进行中", "type": "A",
        "owner": "李开发", "handler": "李开发", "updated_at": "1.0",
        "process": "需求: 需要新增 dummy 函数。验收标准: dummy 函数正确返回 True",
    }], ensure_ascii=False), encoding="utf-8")
    return repo_dir, head_sha


def test_production_runner_full_pass_pipeline(mock_git_repo, tmp_path, monkeypatch):
    repo_dir, baseline_sha = mock_git_repo
    data_root = tmp_path / "data_root"
    data_root.mkdir()

    # Fast mock capabilities
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

    # Mock builder & QA dispatch & wait
    def mock_codex_dispatch(self, req):
        return AgentHandle(
            session_id=req.session_id,
            host_id="codex_cli",
            status="completed",
            is_real_host=True,
            adapter_instance_id="inst_codex",
            invocation_token="tok_builder_1234567890",
        )

    def mock_codex_wait(self, handle, timeout_seconds=None):
        if "builder" in handle.session_id:
            target_dir = str(repo_dir)
            dummy_file = os.path.join(target_dir, f"new_feature_{int(time.time()*1000)}.py")
            with open(dummy_file, "w", encoding="utf-8") as f:
                f.write("def dummy(): return True\n")
            subprocess.run(["git", "add", "."], cwd=target_dir, check=True, capture_output=True)
            subprocess.run(["git", "commit", "-m", "feat: add dummy function"], cwd=target_dir, check=True, capture_output=True)

            return AgentResult(
                session_id=handle.session_id,
                status=AgentStatus.SUCCESS,
                output="Builder successfully implemented dummy function and committed.",
                partial_results=({"invocation_id": "inv_builder_codex_real"},),
                is_real_host=True,
            )
        else:
            return AgentResult(
                session_id=handle.session_id,
                status=AgentStatus.SUCCESS,
                output="QA test suite passed: 1 passed in 0.01s",
                partial_results=({"invocation_id": "inv_qa_codex_real"},),
                is_real_host=True,
            )

    # Mock reviewer dispatch & wait (strict structured JSON PASS)
    def mock_reviewer_dispatch(self, req):
        return AgentHandle(
            session_id=req.session_id,
            host_id="antigravity",
            status="completed",
            is_real_host=True,
            adapter_instance_id="inst_ag",
            invocation_token="tok_reviewer_1234567890",
        )

    def mock_reviewer_wait(self, handle, timeout_seconds=None):
        cand_sha = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=str(repo_dir), text=True).strip()
        out_json = {
            "task_id": "T0088",
            "baseline_commit": baseline_sha,
            "candidate_commit": cand_sha,
            "session_id": handle.session_id,
            "review_request_id": "review_req_" + hashlib.sha256(handle.session_id.encode("utf-8")).hexdigest()[:24],
            "decision": "PASS",
            "defects": [],
            "summary": "Code review passed. Implementation meets all requirements.",
        }
        return AgentResult(
            session_id=handle.session_id,
            status=AgentStatus.SUCCESS,
            output=json.dumps(out_json),
            partial_results=({"invocation_id": "inv_reviewer_real_456"},),
            is_real_host=True,
        )

    monkeypatch.setattr(CodexCliAdapter, "detect_capabilities", mock_detect_caps)
    monkeypatch.setattr(AntigravityAdapter, "detect_capabilities", mock_detect_caps)
    monkeypatch.setattr(CodexCliAdapter, "dispatch_agent", mock_codex_dispatch)
    monkeypatch.setattr(CodexCliAdapter, "wait_for_result", mock_codex_wait)
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
    checkpoint_store = RunnerCheckpointStore(data_root=str(data_root), project_root=str(repo_dir), project_id="test_repo")

    runner = ProductionRunner(
        registry=registry,
        evidence_store=evidence_store,
        checkpoint_store=checkpoint_store,
    )

    spec = TaskExecutionSpec(
        project_id="test_repo",
        project_root=str(repo_dir),
        authority_root=str(repo_dir),
        task_id="T0088",
        task_name="实现测试功能",
        requirement_text="需要新增 dummy 函数",
        acceptance_criteria="验收标准: dummy 函数正确返回 True",
        acceptance_criteria_hash="",
        task_version="1.0",
        status_at_read="进行中",
        baseline_commit=baseline_sha,
        workspace_mode="inherit",
        test_command="python -c \"import sys; sys.exit(0)\"",
    )

    result = runner.start(spec)
    assert result.success is True
    assert result.state == RunnerState.PENDING_USER_ACCEPTANCE.value
    assert result.confirmation_request_id is not None
    assert len(result.evidence_ids) == 3

    # Test status query
    status_info = runner.status(project_root=str(repo_dir), task_id="T0088")
    assert status_info["has_checkpoint"] is True
    assert status_info["state"] == RunnerState.PENDING_USER_ACCEPTANCE.value
