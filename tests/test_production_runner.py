# -*- coding: utf-8 -*-
"""
tests/test_production_runner.py
ProductionRunner 完整编排测试。
"""
import json
import hashlib
import os
import subprocess
import threading
import time
import pytest
import yaml

from scripts._lib.core.adapter_registry import AdapterRegistry
from scripts._lib.core.agent_schema import (
    AgentHandle,
    AgentResult,
    AgentStatus,
    AgentTimeoutError,
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
    test_file = repo_dir / "test_app.py"
    test_file.write_text("def test_app(): assert True\n", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=repo_dir, check=True, capture_output=True)
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
        "id": "T0088", "name": "实现测试功能", "status": "待开始", "type": "A",
        "owner": "李开发", "handler": "李开发", "updated_at": "1.0",
        "process": "需求: 需要新增 dummy 函数。验收标准: dummy 函数正确返回 True",
    }], ensure_ascii=False), encoding="utf-8")
    return repo_dir, head_sha


def test_finalize_builder_candidate_creates_controlled_commit(tmp_path):
    repo_dir = tmp_path / "builder-repo"
    repo_dir.mkdir()
    subprocess.run(["git", "init"], cwd=repo_dir, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.name", "TestDev"], cwd=repo_dir, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "test@test.com"], cwd=repo_dir, check=True, capture_output=True)
    (repo_dir / "README.md").write_text("# Test\n", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=repo_dir, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "initial commit"], cwd=repo_dir, check=True, capture_output=True)
    baseline = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=repo_dir, text=True).strip()
    (repo_dir / "generated.py").write_text("VALUE = 1\n", encoding="utf-8")

    runner = object.__new__(ProductionRunner)
    candidate = runner._finalize_builder_candidate(str(repo_dir), baseline)

    assert candidate != baseline
    assert len(candidate) == 40
    assert subprocess.check_output(
        ["git", "status", "--porcelain=v1", "--untracked-files=all"],
        cwd=repo_dir,
        text=True,
    ).strip() == ""
    assert "固化自动开发产物" in subprocess.check_output(
        ["git", "log", "-1", "--pretty=%s"], cwd=repo_dir, text=True, encoding="utf-8"
    )


def test_finalize_builder_candidate_rejects_empty_output(tmp_path):
    repo_dir = tmp_path / "empty-builder-repo"
    repo_dir.mkdir()
    subprocess.run(["git", "init"], cwd=repo_dir, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.name", "TestDev"], cwd=repo_dir, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "test@test.com"], cwd=repo_dir, check=True, capture_output=True)
    (repo_dir / "README.md").write_text("# Test\n", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=repo_dir, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "initial commit"], cwd=repo_dir, check=True, capture_output=True)
    baseline = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=repo_dir, text=True).strip()

    runner = object.__new__(ProductionRunner)
    with pytest.raises(RuntimeError, match="no new commits or working-tree changes"):
        runner._finalize_builder_candidate(str(repo_dir), baseline)


def test_reviewer_diff_bundle_is_inline_bounded_and_nonempty(tmp_path):
    repo_dir = tmp_path / "review-repo"
    repo_dir.mkdir()
    subprocess.run(["git", "init"], cwd=repo_dir, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.name", "TestDev"], cwd=repo_dir, check=True)
    subprocess.run(["git", "config", "user.email", "test@test.com"], cwd=repo_dir, check=True)
    (repo_dir / "value.py").write_text("VALUE = 1\n", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=repo_dir, check=True)
    subprocess.run(["git", "commit", "-m", "initial"], cwd=repo_dir, check=True, capture_output=True)
    baseline = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=repo_dir, text=True).strip()
    (repo_dir / "value.py").write_text("VALUE = 2\n", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=repo_dir, check=True)
    subprocess.run(["git", "commit", "-m", "change"], cwd=repo_dir, check=True, capture_output=True)
    candidate = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=repo_dir, text=True).strip()

    runner = object.__new__(ProductionRunner)
    bundle = runner._build_reviewer_diff_bundle(str(repo_dir), baseline, candidate)

    assert "DIFF STAT:" in bundle
    assert "-VALUE = 1" in bundle
    assert "+VALUE = 2" in bundle
    with pytest.raises(Exception, match="exceeds safe inline limit"):
        runner._build_reviewer_diff_bundle(str(repo_dir), baseline, candidate, max_chars=10)


def test_runner_wait_enforces_outer_deadline():
    release = threading.Event()

    class CheckpointStore:
        @staticmethod
        def query_status(task_id):
            return {}

    class SlowAdapter:
        cancelled = False

        def wait_for_result(self, handle, timeout_seconds=None):
            release.wait(5)
            return AgentResult(
                session_id=handle.session_id,
                status=AgentStatus.CANCELLED,
                output="cancelled",
                is_real_host=True,
            )

        def cancel_agent(self, handle):
            self.cancelled = True
            release.set()
            return True

    runner = object.__new__(ProductionRunner)
    runner.checkpoint_store = CheckpointStore()
    handle = AgentHandle(
        session_id="sess_deadline",
        host_id="slow",
        status="running",
        is_real_host=True,
        adapter_instance_id="slow-instance",
        invocation_token="deadline-token",
    )
    adapter = SlowAdapter()

    started = time.monotonic()
    with pytest.raises(AgentTimeoutError, match="exceeded Runner deadline"):
        runner._wait_for_result_cancellable(adapter, handle, 0.05, "T0088")
    assert time.monotonic() - started < 1.0
    assert adapter.cancelled is True


def test_production_runner_full_pass_pipeline(mock_git_repo, tmp_path, monkeypatch):
    repo_dir, baseline_sha = mock_git_repo
    data_root = tmp_path / "data_root"
    data_root.mkdir()

    session_workspaces = {}
    review_requests = {}
    qa_requests = {}

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
        session_workspaces[req.session_id] = req.workspace_dir
        if req.role == "QA":
            qa_requests[req.session_id] = req
        return AgentHandle(
            session_id=req.session_id,
            host_id="codex_cli",
            status="completed",
            is_real_host=True,
            adapter_instance_id="inst_codex",
            invocation_token="tok_builder_1234567890",
        )

    def mock_codex_wait(self, handle, timeout_seconds=None):
        target_dir = session_workspaces.get(handle.session_id, str(repo_dir))
        if "builder" in handle.session_id:
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
            qa_req = qa_requests[handle.session_id]
            cand_sha = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=target_dir, text=True).strip()
            qa_json = {
                "task_id": "T0088",
                "baseline_commit": baseline_sha,
                "candidate_commit": cand_sha,
                "session_id": handle.session_id,
                "qa_request_id": qa_req.extra_context["qa_request_id"],
                "acceptance_criteria_hash": qa_req.extra_context["acceptance_criteria_hash"],
                "decision": "PASS",
                "acceptance_coverage": [{
                    "criterion_id": "AC-01",
                    "status": "PASS",
                    "evidence": "test_app.py::test_app",
                }],
                "test_commands": [{
                    "command": "python -m pytest -q",
                    "exit_code": 0,
                    "summary": "1 passed",
                }],
                "negative_scenarios": [{
                    "name": "unexpected false result",
                    "status": "PASS",
                    "evidence": "test_app.py::test_app",
                }],
                "uncovered_risks": [],
                "defects": [],
                "summary": "All acceptance criteria and negative scenarios passed.",
            }
            return AgentResult(
                session_id=handle.session_id,
                status=AgentStatus.SUCCESS,
                output=json.dumps(qa_json),
                partial_results=({"invocation_id": "inv_qa_codex_real"},),
                is_real_host=True,
            )

    def mock_reviewer_dispatch(self, req):
        session_workspaces[req.session_id] = req.workspace_dir
        review_requests[req.session_id] = req.extra_context["review_request_id"]
        return AgentHandle(
            session_id=req.session_id,
            host_id="antigravity",
            status="completed",
            is_real_host=True,
            adapter_instance_id="inst_ag",
            invocation_token="tok_reviewer_1234567890",
        )

    def mock_reviewer_wait(self, handle, timeout_seconds=None):
        target_dir = session_workspaces.get(handle.session_id, str(repo_dir))
        cand_sha = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=target_dir, text=True).strip()
        out_json = {
            "task_id": "T0088",
            "baseline_commit": baseline_sha,
            "candidate_commit": cand_sha,
            "session_id": handle.session_id,
            "review_request_id": review_requests[handle.session_id],
            "decision": "PASS",
            "defects": [],
            "summary": "Code review passed. Implementation meets all requirements.",
        }
        return AgentResult(
            session_id=handle.session_id,
            status=AgentStatus.SUCCESS,
            output=json.dumps(out_json),
            partial_results=({"invocation_id": "inv_reviewer_ag_real"},),
            is_real_host=True,
        )

    monkeypatch.setattr(CodexCliAdapter, "detect_capabilities", mock_detect_caps)
    monkeypatch.setattr(AntigravityAdapter, "detect_capabilities", mock_detect_caps)
    monkeypatch.setattr(CodexCliAdapter, "dispatch_agent", mock_codex_dispatch)
    monkeypatch.setattr(CodexCliAdapter, "wait_for_result", mock_codex_wait)
    monkeypatch.setattr(AntigravityAdapter, "dispatch_agent", mock_reviewer_dispatch)
    monkeypatch.setattr(AntigravityAdapter, "wait_for_result", mock_reviewer_wait)

    registry = AdapterRegistry(context_id="test_proj")
    codex_manifest = create_codex_cli_manifest(adapter_id="codex_cli", verified_version="0.149.0")
    codex_adapter = CodexCliAdapter(is_real_host=True)
    registry.register(codex_adapter, codex_manifest)

    ag_manifest = create_antigravity_manifest(adapter_id="antigravity", verified_version="1.1.22")
    ag_adapter = AntigravityAdapter(is_real_host=True)
    registry.register(ag_adapter, ag_manifest)

    evidence_store = EvidenceStore(root_dir=str(data_root / "evidence"))
    evidence_gate = EvidenceGate(store=evidence_store, project_root=str(repo_dir))
    checkpoint_store = RunnerCheckpointStore(data_root=str(data_root), project_root=str(repo_dir), project_id="test_proj")
    progress_events = []

    runner = ProductionRunner(
        registry=registry,
        evidence_store=evidence_store,
        evidence_gate=evidence_gate,
        checkpoint_store=checkpoint_store,
        progress_callback=progress_events.append,
    )

    spec = TaskExecutionSpec(
        project_id="test_proj",
        project_root=str(repo_dir),
        authority_root=str(repo_dir),
        task_id="T0088",
        task_name="实现测试功能",
        requirement_text="需求: 需要新增 dummy 函数",
        acceptance_criteria="验收标准: dummy 函数正确返回 True",
        acceptance_criteria_hash=hashlib.sha256("验收标准: dummy 函数正确返回 True".encode("utf-8")).hexdigest(),
        task_version="1.0",
        status_at_read="待开始",
        baseline_commit=baseline_sha,
        workspace_mode="inherit",
        test_command="python -m pytest -q",
    )

    result = runner.start(spec)

    assert result.success is True
    assert result.state == RunnerState.PENDING_USER_ACCEPTANCE.value
    assert result.candidate_commit is not None
    assert len(result.evidence_ids) >= 3
    stage_roles = [
        event["role"]
        for event in progress_events
        if event["event"] == "stage_started"
    ]
    assert stage_roles == ["BUILDER", "REVIEWER", "QA"]
    assert progress_events[-1]["event"] == "pending_user_acceptance"
    board = json.loads((repo_dir / "user_data" / "board.json").read_text(encoding="utf-8"))
    assert board[0]["status"] == "已完成"
    process = board[0]["process"]
    for transition in (
        "待开始】更新至【进行中",
        "进行中】更新至【审查中",
        "审查中】更新至【测试中",
        "测试中】更新至【已完成",
    ):
        assert transition in process


def test_waiting_task_is_not_claimed_when_adapters_are_missing(mock_git_repo, tmp_path):
    repo_dir, baseline_sha = mock_git_repo
    data_root = tmp_path / "missing-adapter-data"
    data_root.mkdir()
    runner = ProductionRunner(
        registry=AdapterRegistry(context_id="missing_adapters"),
        evidence_store=EvidenceStore(root_dir=str(data_root / "evidence")),
        evidence_gate=EvidenceGate(
            store=EvidenceStore(root_dir=str(data_root / "gate-evidence")),
            project_root=str(repo_dir),
        ),
        checkpoint_store=RunnerCheckpointStore(
            data_root=str(data_root),
            project_root=str(repo_dir),
            project_id="test_proj",
        ),
    )
    spec = TaskExecutionSpec(
        project_id="test_proj",
        project_root=str(repo_dir),
        authority_root=str(repo_dir),
        task_id="T0088",
        task_name="实现测试功能",
        requirement_text="需求: 需要新增 dummy 函数",
        acceptance_criteria="验收标准: dummy 函数正确返回 True",
        acceptance_criteria_hash=hashlib.sha256(
            "验收标准: dummy 函数正确返回 True".encode("utf-8")
        ).hexdigest(),
        task_version="1.0",
        status_at_read="待开始",
        baseline_commit=baseline_sha,
        workspace_mode="inherit",
        test_command="python -m pytest -q",
    )

    result = runner.start(spec)

    assert result.success is False
    assert "Missing adapter" in result.message
    board = json.loads((repo_dir / "user_data" / "board.json").read_text(encoding="utf-8"))
    assert board[0]["status"] == "待开始"
