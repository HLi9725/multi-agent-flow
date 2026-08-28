# -*- coding: utf-8 -*-
"""
tests/test_runner_security.py
Runner 缺陷回环、JSON Schema 对抗校验、候选提交校验、权限暂停/恢复与源码不可变性测试。
"""
import json
import os
import subprocess
import time
import pytest
import yaml

from scripts._lib.core.adapter_registry import AdapterRegistry
from scripts._lib.core.agent_schema import (
    AgentHandle,
    AgentNotSupportedError,
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

    config_dir = repo_dir / "config"
    config_dir.mkdir()
    user_data_dir = repo_dir / "user_data"
    user_data_dir.mkdir()

    workflow_cfg = {
        "board": {
            "provider": "local",
            "board_file": str(user_data_dir / "board.json"),
            "fields": {
                "task_id": "id",
                "task_name": "name",
                "status": "status",
                "assignee": "assignee",
                "owner": "owner",
                "handler": "handler",
                "remarks": "remarks",
                "process": "process",
            },
        }
    }
    with open(config_dir / "workflow.config.yaml", "w", encoding="utf-8") as f:
        yaml.dump(workflow_cfg, f)

    tasks_data = [
        {
            "id": "T0033",
            "name": "权限测试任务",
            "status": "进行中",
            "assignee": "李开发",
            "owner": "李开发",
            "handler": "李开发",
            "process": "需求: 需要网络权限。验收标准: 权限通过。",
            "updated_at": "1787890000",
        }
    ]
    with open(user_data_dir / "board.json", "w", encoding="utf-8") as f:
        json.dump(tasks_data, f)

    return repo_dir, head_sha


def test_reviewer_schema_adversarial_rejections(mock_git_repo, tmp_path):
    """
    DEF-T0061-1 对抗测试：
    1. 普通文本 PASS: looks good 必须被拒绝；
    2. 残缺 JSON（缺失必填身份字段）必须被拒绝；
    3. 身份/提交哈希不匹配必须被拒绝；
    4. PASS 携带缺陷列表必须被拒绝。
    """
    repo_dir, baseline_sha = mock_git_repo
    data_root = tmp_path / "data_root"
    data_root.mkdir()

    runner = ProductionRunner(
        checkpoint_store=RunnerCheckpointStore(data_root=str(data_root), project_root=str(repo_dir)),
    )

    # 1. 普通文本 "PASS: looks good"
    res1 = runner._parse_reviewer_structured_json(
        raw_output="PASS: looks good",
        task_id="T0077",
        baseline_commit=baseline_sha,
        candidate_commit="b" * 40,
        session_id="sess_r_1",
        invocation_id="inv_r_1",
    )
    assert res1.decision == "REJECT"
    assert "DEF-T0077-SCHEMA-VIOLATION" in res1.defects[0]["defect_id"]

    # 2. 残缺 JSON（仅含 decision/defects/summary，缺失必填身份字段）
    incomplete_json = '{"decision": "PASS", "defects": [], "summary": "looks good"}'
    res2 = runner._parse_reviewer_structured_json(
        raw_output=incomplete_json,
        task_id="T0077",
        baseline_commit=baseline_sha,
        candidate_commit="b" * 40,
        session_id="sess_r_1",
        invocation_id="inv_r_1",
    )
    assert res2.decision == "REJECT"
    assert "DEF-T0077-SCHEMA-VIOLATION" in res2.defects[0]["defect_id"]

    # 3. 身份/哈希不匹配（task_id 或 commit 伪造）
    mismatched_json = json.dumps({
        "task_id": "T9999_FAKE",
        "baseline_commit": baseline_sha,
        "candidate_commit": "b" * 40,
        "session_id": "sess_r_1",
        "host_invocation_id": "inv_r_1",
        "decision": "PASS",
        "defects": [],
        "summary": "pass",
    })
    res3 = runner._parse_reviewer_structured_json(
        raw_output=mismatched_json,
        task_id="T0077",
        baseline_commit=baseline_sha,
        candidate_commit="b" * 40,
        session_id="sess_r_1",
        invocation_id="inv_r_1",
    )
    assert res3.decision == "REJECT"
    assert "IDENTITY-MISMATCH" in res3.defects[0]["defect_id"]

    # 4. PASS 携带缺陷列表
    contradictory_json = json.dumps({
        "task_id": "T0077",
        "baseline_commit": baseline_sha,
        "candidate_commit": "b" * 40,
        "session_id": "sess_r_1",
        "host_invocation_id": "inv_r_1",
        "decision": "PASS",
        "defects": [{"defect_id": "DEF-1", "severity": "P1", "description": "some error"}],
        "summary": "pass with defect",
    })
    res4 = runner._parse_reviewer_structured_json(
        raw_output=contradictory_json,
        task_id="T0077",
        baseline_commit=baseline_sha,
        candidate_commit="b" * 40,
        session_id="sess_r_1",
        invocation_id="inv_r_1",
    )
    assert res4.decision == "REJECT"
    assert "INVALID-PASS" in res4.defects[0]["defect_id"]


def test_builder_no_commit_or_missing_invocation_fails_closed(mock_git_repo, tmp_path, monkeypatch):
    """
    DEF-T0061-2 对抗测试：
    1. Builder 没有产出新提交 (Candidate == Baseline) 必须 Fail-Closed 终止；
    2. Host 没有返回真实 Invocation 必须 Fail-Closed 终止。
    """
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

    # Builder 没有提交任何代码
    def mock_builder_no_commit_wait(self, handle, timeout_seconds=None):
        return AgentResult(
            session_id=handle.session_id,
            status=AgentStatus.SUCCESS,
            output="Builder executed but made no commit",
            partial_results=({"invocation_id": "inv_b_real"},),
            is_real_host=True,
        )

    def mock_builder_dispatch(self, req):
        return AgentHandle(session_id=req.session_id, host_id="codex_cli", status="completed", is_real_host=True, adapter_instance_id="inst_c", invocation_token="tok_b")

    monkeypatch.setattr(CodexCliAdapter, "detect_capabilities", mock_detect_caps)
    monkeypatch.setattr(CodexCliAdapter, "dispatch_agent", mock_builder_dispatch)
    monkeypatch.setattr(CodexCliAdapter, "wait_for_result", mock_builder_no_commit_wait)
    monkeypatch.setattr(AntigravityAdapter, "detect_capabilities", mock_detect_caps)

    registry = AdapterRegistry(context_id="test_repo")
    codex_manifest = create_codex_cli_manifest(adapter_id="codex_cli", verified_version="0.149.0")
    codex_adapter = CodexCliAdapter(is_real_host=True)
    registry.register(codex_adapter, codex_manifest)

    ag_manifest = create_antigravity_manifest(adapter_id="antigravity", verified_version="1.1.22")
    ag_adapter = AntigravityAdapter(is_real_host=True)
    registry.register(ag_adapter, ag_manifest)

    runner = ProductionRunner(
        registry=registry,
        evidence_store=EvidenceStore(root_dir=str(data_root / "evidence")),
        checkpoint_store=RunnerCheckpointStore(data_root=str(data_root), project_root=str(repo_dir)),
    )

    spec = TaskExecutionSpec(
        project_id="test_repo",
        project_root=str(repo_dir),
        authority_root=str(repo_dir),
        task_id="T0055",
        task_name="无提交测试",
        requirement_text="无提交测试",
        acceptance_criteria="验收标准: 测试",
        acceptance_criteria_hash="hash_55",
        task_version="1.0",
        status_at_read="进行中",
        baseline_commit=baseline_sha,
        workspace_mode="inherit",
    )

    result = runner.start(spec)
    assert result.success is False
    assert result.state == RunnerState.FAILED.value
    assert "Builder produced no new commits" in result.message


def test_permission_approval_required_pause_and_resume(mock_git_repo, tmp_path, monkeypatch):
    """
    DEF-T0061-7 测试：
    权限被拒绝时暂停在 APPROVAL_REQUIRED，并支持通过 resume(--approve) 授权恢复。
    """
    repo_dir, baseline_sha = mock_git_repo
    data_root = tmp_path / "data_root"
    data_root.mkdir()

    call_count = 0

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

    def mock_dispatch(self, req):
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            raise AgentNotSupportedError("Operation requires explicit user permission approval for network access.")
        return AgentHandle(session_id=req.session_id, host_id="codex_cli", status="completed", is_real_host=True, adapter_instance_id="inst_c", invocation_token="tok_b")

    def mock_wait(self, handle, timeout_seconds=None):
        target_dir = str(repo_dir)
        dummy_file = os.path.join(target_dir, f"perm_feature_{int(time.time()*1000)}.py")
        with open(dummy_file, "w", encoding="utf-8") as f:
            f.write("def perm_ok(): return True\n")
        subprocess.run(["git", "add", "."], cwd=target_dir, check=True, capture_output=True)
        subprocess.run(["git", "commit", "-m", "feat: perm ok"], cwd=target_dir, check=True, capture_output=True)

        return AgentResult(session_id=handle.session_id, status=AgentStatus.SUCCESS, output="ok", partial_results=({"invocation_id": "inv_b"},), is_real_host=True)

    monkeypatch.setattr(CodexCliAdapter, "detect_capabilities", mock_detect_caps)
    monkeypatch.setattr(CodexCliAdapter, "dispatch_agent", mock_dispatch)
    monkeypatch.setattr(CodexCliAdapter, "wait_for_result", mock_wait)
    monkeypatch.setattr(AntigravityAdapter, "detect_capabilities", mock_detect_caps)

    registry = AdapterRegistry(context_id="test_repo")
    codex_manifest = create_codex_cli_manifest(adapter_id="codex_cli", verified_version="0.149.0")
    codex_adapter = CodexCliAdapter(is_real_host=True)
    registry.register(codex_adapter, codex_manifest)

    ag_manifest = create_antigravity_manifest(adapter_id="antigravity", verified_version="1.1.22")
    ag_adapter = AntigravityAdapter(is_real_host=True)
    registry.register(ag_adapter, ag_manifest)

    checkpoint_store = RunnerCheckpointStore(data_root=str(data_root), project_root=str(repo_dir), project_id="test_repo")
    runner = ProductionRunner(
        registry=registry,
        evidence_store=EvidenceStore(root_dir=str(data_root / "evidence")),
        checkpoint_store=checkpoint_store,
    )

    spec = TaskExecutionSpec(
        project_id="test_repo",
        project_root=str(repo_dir),
        authority_root=str(repo_dir),
        task_id="T0033",
        task_name="权限测试任务",
        requirement_text="需要网络权限",
        acceptance_criteria="验收标准: 权限通过",
        acceptance_criteria_hash="hash_33",
        task_version="1.0",
        status_at_read="进行中",
        baseline_commit=baseline_sha,
        workspace_mode="inherit",
    )

    # 1. 首次启动应触发 APPROVAL_REQUIRED 暂停
    res1 = runner.start(spec)
    assert res1.success is False
    assert res1.state == RunnerState.APPROVAL_REQUIRED.value
    assert "Operation requires explicit user permission approval" in res1.message

    # 2. 通过 resume 并授权后继续执行
    res2 = runner.resume(project_root=str(repo_dir), task_id="T0033", authority_root=str(repo_dir), pre_granted_approval=True)
    assert res2.state != RunnerState.APPROVAL_REQUIRED.value


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
            invocation_token="tok_builder_1234567890",
        )

    def mock_builder_wait(self, handle, timeout_seconds=None):
        target_dir = str(repo_dir)
        dummy_file = os.path.join(target_dir, f"fix_{builder_call_count}_{int(time.time()*1000)}.py")
        with open(dummy_file, "w", encoding="utf-8") as f:
            f.write(f"def fix_{builder_call_count}(): return True\n")
        subprocess.run(["git", "add", "."], cwd=target_dir, check=True, capture_output=True)
        subprocess.run(["git", "commit", "-m", f"feat: attempt {builder_call_count}"], cwd=target_dir, check=True, capture_output=True)

        return AgentResult(
            session_id=handle.session_id,
            status=AgentStatus.SUCCESS,
            output="Builder fix attempted",
            partial_results=({"invocation_id": f"inv_builder_{builder_call_count}"},),
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
            invocation_token="tok_reviewer_1234567890",
        )

    def mock_reviewer_wait(self, handle, timeout_seconds=None):
        cand_sha = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=str(repo_dir), text=True).strip()
        out_json = {
            "task_id": "T0077",
            "baseline_commit": baseline_sha,
            "candidate_commit": cand_sha,
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
            partial_results=({"invocation_id": f"inv_reviewer_{reviewer_call_count}"},),
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

    def mock_codex_dispatch(self, req):
        return AgentHandle(
            session_id=req.session_id,
            host_id="codex_cli",
            status="completed",
            is_real_host=True,
            adapter_instance_id="inst_c",
            invocation_token="tok_codex_1234567890",
        )

    def mock_codex_wait(self, handle, timeout_seconds=None):
        if "builder" in handle.session_id:
            target_dir = str(repo_dir)
            dummy_file = os.path.join(target_dir, f"feature_{int(time.time()*1000)}.py")
            with open(dummy_file, "w", encoding="utf-8") as f:
                f.write("def feat(): return True\n")
            subprocess.run(["git", "add", "."], cwd=target_dir, check=True, capture_output=True)
            subprocess.run(["git", "commit", "-m", "feat: new feature"], cwd=target_dir, check=True, capture_output=True)
            return AgentResult(session_id=handle.session_id, status=AgentStatus.SUCCESS, output="ok", partial_results=({"invocation_id": "inv_builder_real"},), is_real_host=True)
        else:
            target_dir = str(repo_dir)
            readme_path = os.path.join(target_dir, "README.md")
            with open(readme_path, "a", encoding="utf-8") as f:
                f.write("\n# QA modified source code illegally!\n")
            return AgentResult(session_id=handle.session_id, status=AgentStatus.SUCCESS, output="QA done", partial_results=({"invocation_id": "inv_qa_real"},), is_real_host=True)

    def mock_reviewer_dispatch(self, req):
        return AgentHandle(session_id=req.session_id, host_id="antigravity", status="completed", is_real_host=True, adapter_instance_id="inst_a", invocation_token="tok_reviewer_1234567890")

    def mock_reviewer_wait(self, handle, timeout_seconds=None):
        cand_sha = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=str(repo_dir), text=True).strip()
        out_json = {
            "task_id": "T0066",
            "baseline_commit": baseline_sha,
            "candidate_commit": cand_sha,
            "session_id": handle.session_id,
            "host_invocation_id": "inv_reviewer_real",
            "decision": "PASS",
            "defects": [],
            "summary": "pass",
        }
        return AgentResult(session_id=handle.session_id, status=AgentStatus.SUCCESS, output=json.dumps(out_json), partial_results=({"invocation_id": "inv_reviewer_real"},), is_real_host=True)

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
