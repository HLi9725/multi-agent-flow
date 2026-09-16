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
from pathlib import Path
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
from scripts._lib.core.production_runner import (
    ProductionRunner,
    _compact_test_diagnostics,
    _derive_command_failure_defects,
    _is_empty_host_completion,
)
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


def test_finalize_builder_repair_requires_a_new_candidate(tmp_path):
    repo_dir = tmp_path / "repair-builder-repo"
    repo_dir.mkdir()
    subprocess.run(["git", "init"], cwd=repo_dir, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.name", "TestDev"], cwd=repo_dir, check=True)
    subprocess.run(["git", "config", "user.email", "test@test.com"], cwd=repo_dir, check=True)
    (repo_dir / "value.py").write_text("VALUE = 1\n", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=repo_dir, check=True)
    subprocess.run(["git", "commit", "-m", "baseline"], cwd=repo_dir, check=True, capture_output=True)
    baseline = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=repo_dir, text=True).strip()
    (repo_dir / "value.py").write_text("VALUE = 2\n", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=repo_dir, check=True)
    subprocess.run(["git", "commit", "-m", "candidate"], cwd=repo_dir, check=True, capture_output=True)
    candidate = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=repo_dir, text=True).strip()

    runner = object.__new__(ProductionRunner)
    with pytest.raises(RuntimeError, match="repair cycle produced no new candidate"):
        runner._finalize_builder_candidate(
            str(repo_dir),
            baseline,
            previous_candidate_commit=candidate,
        )

    (repo_dir / "value.py").write_text("VALUE = 3\n", encoding="utf-8")
    repaired = runner._finalize_builder_candidate(
        str(repo_dir), baseline, previous_candidate_commit=candidate
    )
    assert repaired != candidate
    assert subprocess.check_output(
        ["git", "status", "--porcelain"], cwd=repo_dir, text=True
    ).strip() == ""


def test_empty_host_completion_and_repair_diagnostics_are_deterministic():
    result = AgentResult(
        session_id="sess_empty",
        status=AgentStatus.SUCCESS,
        output="No output returned",
        partial_results=({"output_empty": True, "invocation_id": "inv-empty"},),
        is_real_host=True,
    )
    assert _is_empty_host_completion(result) is True

    diagnostics = [{
        "command": "git diff --check base..candidate --",
        "exit_code": 2,
        "output_excerpt": (
            "app/main.py:12: trailing whitespace.\n"
            "app/main.py:12: trailing whitespace.\n"
            "tests/test_db.py:30: new blank line at EOF.\n"
        ),
        "output_truncated": False,
    }]
    compact = _compact_test_diagnostics(diagnostics)
    assert compact[0]["findings"].count("app/main.py:12") == 1
    defects = _derive_command_failure_defects("T0014", diagnostics)
    assert [item["file_path"] for item in defects] == ["app/main.py", "tests/test_db.py"]
    assert all(item["severity"] == "P2" for item in defects)


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
    large = runner._build_reviewer_diff_bundle(str(repo_dir), baseline, candidate, max_chars=10)
    assert "not silently truncated" in large
    artifact = large.split("complete_artifact=", 1)[1].splitlines()[0]
    assert Path(artifact).read_text(encoding="utf-8") == bundle


def test_runner_security_scan_is_candidate_bound_and_fail_closed(tmp_path):
    repo_dir = tmp_path / "security-repo"
    repo_dir.mkdir()
    subprocess.run(["git", "init"], cwd=repo_dir, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.name", "TestDev"], cwd=repo_dir, check=True)
    subprocess.run(["git", "config", "user.email", "test@test.com"], cwd=repo_dir, check=True)
    (repo_dir / "app.py").write_text("TOKEN = None\n", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=repo_dir, check=True)
    subprocess.run(["git", "commit", "-m", "base"], cwd=repo_dir, check=True, capture_output=True)
    baseline = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=repo_dir, text=True).strip()
    (repo_dir / "app.py").write_text("TOKEN = 'ghp_" + "a" * 36 + "'\n", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=repo_dir, check=True)
    subprocess.run(["git", "commit", "-m", "candidate"], cwd=repo_dir, check=True, capture_output=True)
    candidate = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=repo_dir, text=True).strip()

    runner = object.__new__(ProductionRunner)
    runner.evidence_store = None
    result = runner._run_reviewer_security_scan(str(repo_dir), baseline, candidate)
    assert result["candidate_commit"] == candidate
    assert result["exit_code"] == 1
    assert result["passed"] is False


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


@pytest.mark.parametrize(
    "recover_budget,empty_builder_completions",
    [(False, 0), (True, 0), (False, 1), (False, 2)],
)
def test_production_runner_full_pass_pipeline(
    mock_git_repo, tmp_path, monkeypatch, recover_budget, empty_builder_completions
):
    repo_dir, baseline_sha = mock_git_repo
    if empty_builder_completions:
        # Production installations keep Runner control data outside candidate
        # Git changes. Mirror that boundary so an empty Builder cannot create a
        # candidate merely by committing board transitions from this fixture.
        (repo_dir / ".gitignore").write_text("user_data/\n", encoding="utf-8")
        subprocess.run(["git", "add", ".gitignore", "config/workflow.config.yaml"], cwd=repo_dir, check=True)
        subprocess.run(["git", "commit", "-m", "test: isolate control data"], cwd=repo_dir, check=True, capture_output=True)
        baseline_sha = subprocess.check_output(
            ["git", "rev-parse", "HEAD"], cwd=repo_dir, text=True
        ).strip()
    data_root = tmp_path / "data_root"
    data_root.mkdir()

    session_workspaces = {}
    review_requests = {}
    qa_requests = {}
    builder_calls = 0

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
        nonlocal builder_calls
        target_dir = session_workspaces.get(handle.session_id, str(repo_dir))
        if "builder" in handle.session_id:
            builder_calls += 1
            if builder_calls <= empty_builder_completions:
                return AgentResult(
                    session_id=handle.session_id,
                    status=AgentStatus.SUCCESS,
                    output="No output returned",
                    partial_results=({
                        "output_empty": True,
                        "invocation_id": "inv_builder_empty_first",
                    },),
                    is_real_host=True,
                )
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
                    "evidence": "test_app.py::test_access_token verified persistence",
                }],
                "test_commands": [
                    {"command": command, "exit_code": 0, "summary": "passed"}
                    for command in qa_req.extra_context["required_test_commands"]
                ],
                "negative_scenarios": [{
                    "name": "unexpected false result",
                    "status": "PASS",
                    "evidence": "test_app.py::test_refresh_token concurrent single winner",
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
    checkpoint_store = RunnerCheckpointStore(data_root=str(data_root), project_root=str(repo_dir), project_id="repo")
    progress_events = []

    runner = ProductionRunner(
        registry=registry,
        evidence_store=evidence_store,
        evidence_gate=evidence_gate,
        checkpoint_store=checkpoint_store,
        progress_callback=progress_events.append,
    )

    spec = TaskExecutionSpec(
        project_id="repo",
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
        max_total_attempts=0 if recover_budget else 6,
    )

    result = runner.start(spec)

    if empty_builder_completions == 2:
        assert result.success is False
        assert result.state == RunnerState.NEEDS_USER_INPUT.value
        assert result.diagnostics["failure_kind"] == "HOST_EMPTY_COMPLETION"
        assert result.diagnostics["empty_completion_retries"] == 1
        assert builder_calls == 2
        assert checkpoint_store.load_checkpoint("T0088").total_attempts == 1
        return

    if recover_budget:
        assert result.state == RunnerState.NEEDS_USER_INPUT.value
        assert result.diagnostics["total_attempts"] == 0
        assert not session_workspaces
        for _ in range(2):
            result = runner.resume(str(repo_dir), "T0088", authority_root=str(repo_dir))
            assert result.state == RunnerState.NEEDS_USER_INPUT.value, result.message
            assert checkpoint_store.load_checkpoint("T0088").total_attempts == 0
            assert not session_workspaces
        from dataclasses import replace
        checkpoint = checkpoint_store.load_checkpoint("T0088")
        checkpoint_store.save_checkpoint(replace(checkpoint, total_attempts=21,
            execution_options={**checkpoint.execution_options, "max_total_attempts": 20}))
        for _ in range(2):
            result = runner.resume(str(repo_dir), "T0088", authority_root=str(repo_dir))
            assert result.state == RunnerState.NEEDS_USER_INPUT.value, result.message
            assert checkpoint_store.load_checkpoint("T0088").total_attempts == 21
            assert not session_workspaces
        checkpoint = checkpoint_store.load_checkpoint("T0088")
        checkpoint_store.save_checkpoint(replace(
            checkpoint,
            total_attempts=21,
            active_elapsed_seconds=2600.0,
            execution_options={
                **checkpoint.execution_options,
                "max_total_attempts": 25,
                "total_wall_clock_timeout_seconds": 1800,
            },
        ))
        for _ in range(2):
            result = runner.resume(str(repo_dir), "T0088", authority_root=str(repo_dir))
            assert result.state == RunnerState.NEEDS_USER_INPUT.value, result.message
            paused = checkpoint_store.load_checkpoint("T0088")
            assert paused.total_attempts == 21
            assert paused.active_elapsed_seconds >= 2600.0
            assert result.diagnostics["total_wall_clock_timeout_seconds"] == 1800
            assert result.diagnostics["remaining_wall_clock_seconds"] == 0.0
            assert not session_workspaces
        result = runner.resume(
            str(repo_dir), "T0088", authority_root=str(repo_dir),
            overrides={"max_total_attempts": 25, "total_wall_clock_timeout_seconds": 4500},
        )
        assert result.success, result.message
        assert checkpoint_store.load_checkpoint("T0088").execution_options["max_total_attempts"] == 25
        assert checkpoint_store.load_checkpoint("T0088").execution_options["total_wall_clock_timeout_seconds"] == 4500
        assert checkpoint_store.load_checkpoint("T0088").total_attempts == 22

    assert result.success is True, result.message
    assert result.state == RunnerState.PENDING_USER_ACCEPTANCE.value
    assert result.candidate_commit is not None
    assert len(result.evidence_ids) >= 3
    qa_evidence = evidence_store.read(next(item for item in reversed(result.evidence_ids) if item.startswith("evi_qa_")))
    diagnostics = qa_evidence.metadata.extra["test_diagnostics"]
    assert diagnostics[0]["exit_code"] == 0
    assert "passed" in diagnostics[0]["output_excerpt"]
    assert qa_evidence.metadata.extra["qa_report"]["acceptance_coverage"][0]["evidence"] == "***MASKED***"
    assert qa_evidence.metadata.extra["qa_report"]["negative_scenarios"][0]["evidence"] == "***MASKED***"
    assert "test from observed execution" in next(iter(qa_requests.values())).prompt
    stage_roles = [
        event["role"]
        for event in progress_events
        if event["event"] == "stage_started"
    ]
    assert stage_roles == ["BUILDER", "REVIEWER", "QA"]
    assert any(event["event"] == "stage_retrying" for event in progress_events) is bool(empty_builder_completions)
    assert builder_calls == (2 if empty_builder_completions else 1)
    checkpoint_after_run = checkpoint_store.load_checkpoint("T0088")
    if empty_builder_completions:
        assert checkpoint_after_run.host_attempt_history[-1]["outcome"] == "EMPTY_COMPLETION_RETRIED"
        assert checkpoint_after_run.host_attempt_history[-1]["host_invocation_id"] == "inv_builder_empty_first"
    else:
        assert checkpoint_after_run.host_attempt_history == ()
    assert progress_events[-1]["event"] == "pending_user_acceptance"
    qa_request = next(iter(qa_requests.values()))
    assert "Do not invoke commands" in qa_request.prompt
    assert "Runner-produced test evidence" in qa_request.prompt
    assert '"exit_code": 0' in qa_request.prompt
    assert any(
        command.startswith("git diff --check ")
        for command in qa_request.extra_context["required_test_commands"]
    )
    assert "SQLite StaticPool" in qa_request.prompt
    assert "Execute the required" not in qa_request.prompt
    assert qa_request.extra_context["operation_intent"] == (
        "read-only semantic assessment of inline Runner test evidence"
    )
    assert any(event["event"] == "qa_command_started" for event in progress_events)
    assert any(
        event["event"] == "qa_command_completed" and "exited 0" in event["message"]
        for event in progress_events
    )
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
