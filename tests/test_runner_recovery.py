# -*- coding: utf-8 -*-
"""
tests/test_runner_recovery.py
Runner 状态恢复、原子 Checkpoint、防路径逃逸、并发锁与 Host 取消/断点测试。
"""
import json
import hashlib
import os
import subprocess
import tempfile
import threading
import time
from dataclasses import replace
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
from scripts._lib.core.evidence_schema import (
    EvidenceMetadata,
    EvidenceRecord,
    EvidenceType,
)
from scripts._lib.core.evidence_store import EvidenceStore
from scripts._lib.core.production_runner import ProductionRunner, RunnerCancelledError
import scripts._lib.core.production_runner as production_runner_module
from scripts._lib.core.runner_checkpoint_store import CheckpointStoreError, RunnerCheckpointStore
from scripts._lib.core.runner_schema import RunnerCheckpoint, RunnerState, TaskExecutionSpec
from scripts._lib.hosts.antigravity_adapter import AntigravityAdapter, create_antigravity_manifest
from scripts._lib.hosts.codex_cli_adapter import CodexCliAdapter, create_codex_cli_manifest


def test_acceptance_commands_bind_confirmation_and_return_original_task(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init"], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=repo, check=True)
    (repo / "app.txt").write_text("candidate\n", encoding="utf-8")
    subprocess.run(["git", "add", "app.txt"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-m", "candidate"], cwd=repo, check=True, capture_output=True)
    candidate = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=repo, text=True).strip()
    store = RunnerCheckpointStore(data_root=str(tmp_path / "data"), project_root=str(repo), project_id="demo")
    checkpoint = RunnerCheckpoint(
        task_id="T0012", project_id="demo", state=RunnerState.PENDING_USER_ACCEPTANCE.value,
        current_role="QA", candidate_commit=candidate, worktree_path=str(repo),
        confirmation_request_id="conf-secret", evidence_ids=("evi-qa",),
    )
    store.save_checkpoint(checkpoint)
    runner = ProductionRunner(checkpoint_store=store)
    transitions = []
    monkeypatch.setattr(runner, "_do_state_transition", lambda *args, **kwargs: (transitions.append(args) or (True, None)))

    mismatch = runner.reject(str(repo), "T0012", "wrong", "defect")
    assert mismatch.success is False
    assert not transitions

    rejected = runner.reject(str(repo), "T0012", "conf-secret", "acceptance defect")
    assert rejected.success is False
    assert "revalidate" in rejected.message.lower()
    assert not transitions

    store.save_checkpoint(checkpoint)
    accepted = runner.accept(str(repo), "T0012", "conf-secret")
    assert accepted.success is False
    assert "validation failed closed" in accepted.message.lower()
    assert store.load_checkpoint("T0012").state == RunnerState.PENDING_USER_ACCEPTANCE.value


def test_checkpoint_store_atomic_save_and_load(tmp_path):
    store = RunnerCheckpointStore(data_root=str(tmp_path), project_id="proj_alpha")

    ckpt = RunnerCheckpoint(
        task_id="T0099",
        project_id="proj_alpha",
        state=RunnerState.REVIEWING.value,
        current_role="REVIEWER",
        candidate_commit="a" * 40,
        candidate_generation=1,
        review_cycle=1,
        qa_cycle=0,
        total_attempts=1,
        worktree_path=str(tmp_path / "wt"),
        worktree_branch="feature/t0099-runner",
        builder_session_id="sess_b_1",
        reviewer_session_id="sess_r_1",
        evidence_ids=("evi_b_1",),
        execution_options={
            "reviewer_adapter_id": "antigravity",
            "qa_adapter_id": "codex_cli",
            "test_commands": ["python -m pytest tests -q"],
            "reviewer_timeout_seconds": 17,
        },
    )

    store.save_checkpoint(ckpt)
    loaded = store.load_checkpoint("T0099")

    assert loaded is not None
    assert loaded.task_id == "T0099"
    assert loaded.project_id == "proj_alpha"
    assert loaded.state == RunnerState.REVIEWING.value
    assert loaded.evidence_ids == ("evi_b_1",)
    assert loaded.execution_options["reviewer_timeout_seconds"] == 17
    assert loaded.execution_options["test_commands"] == ("python -m pytest tests -q",)


def test_checkpoint_store_path_traversal_and_multi_project_isolation(tmp_path):
    store_a = RunnerCheckpointStore(data_root=str(tmp_path), project_id="proj_a")
    store_b = RunnerCheckpointStore(data_root=str(tmp_path), project_id="proj_b")

    # 1. 严格拒绝非法 task_id 路径逃逸 (DEF-T0061-6)
    with pytest.raises(CheckpointStoreError, match="Invalid task_id format"):
        store_a.load_checkpoint("../escape")

    with pytest.raises(CheckpointStoreError, match="Invalid task_id format"):
        store_a.load_checkpoint("T001/../../secret")

    # 2. 多项目同名任务隔离 (DEF-T0061-6)
    ckpt_a = RunnerCheckpoint(
        task_id="T0001",
        project_id="proj_a",
        state=RunnerState.BUILDING.value,
        current_role="BUILDER",
    )
    ckpt_b = RunnerCheckpoint(
        task_id="T0001",
        project_id="proj_b",
        state=RunnerState.QA_TESTING.value,
        current_role="QA",
    )

    store_a.save_checkpoint(ckpt_a)
    store_b.save_checkpoint(ckpt_b)

    loaded_a = store_a.load_checkpoint("T0001")
    loaded_b = store_b.load_checkpoint("T0001")

    assert loaded_a.state == RunnerState.BUILDING.value
    assert loaded_b.state == RunnerState.QA_TESTING.value
    assert loaded_a.project_id == "proj_a"
    assert loaded_b.project_id == "proj_b"

    # Sanitization collisions must not share a namespace.
    collision_a = RunnerCheckpointStore(data_root=str(tmp_path), project_root=str(tmp_path / "a"), project_id="a/b")
    collision_b = RunnerCheckpointStore(data_root=str(tmp_path), project_root=str(tmp_path / "b"), project_id="a?b")
    assert collision_a.checkpoint_dir != collision_b.checkpoint_dir


def test_runner_concurrency_lock(tmp_path):
    store = RunnerCheckpointStore(data_root=str(tmp_path), project_id="proj_alpha")

    h1, f1 = store.acquire_runner_lock("T0099")
    assert h1 is not None
    assert f1 is not None

    # 同一任务不能被并发重复锁定
    h2, f2 = store.acquire_runner_lock("T0099")
    assert h2 is None
    assert f2 is None

    store.release_runner_lock((h1, f1))


def test_runner_lock_uses_checkpoint_data_root_not_process_context(tmp_path):
    explicit_data_root = tmp_path / "project-data"
    store = RunnerCheckpointStore(
        data_root=str(explicit_data_root),
        project_root=str(tmp_path / "project"),
        project_id="isolated",
    )

    lock_tuple = store.acquire_runner_lock("T0061")
    try:
        assert lock_tuple[0] is not None
        assert lock_tuple[1] is not None
        assert os.path.commonpath([str(explicit_data_root), lock_tuple[1]]) == str(explicit_data_root)
        assert lock_tuple[1].endswith(".lock")
    finally:
        store.release_runner_lock(lock_tuple)


def test_runner_cancel_safely_cancels_hosts_and_preserves_worktree(tmp_path):
    """
    P1 测试：
    cancel 取消正在运行的 Host 会话，更新 Checkpoint 为 CANCELLED，同时保留 Worktree 和 Evidence。
    """
    data_root = tmp_path / "data_root"
    data_root.mkdir()
    worktree_path = tmp_path / "wt"
    worktree_path.mkdir()

    cancelled_handles = []

    class MockCancellableAdapter(CodexCliAdapter):
        def cancel_agent(self, handle):
            cancelled_handles.append(handle)
            return True

    registry = AdapterRegistry(context_id="test_proj")
    codex_manifest = create_codex_cli_manifest(adapter_id="codex_cli", verified_version="0.149.0")
    codex_adapter = MockCancellableAdapter(is_real_host=True)
    registry.register(codex_adapter, codex_manifest)

    store = RunnerCheckpointStore(data_root=str(data_root), project_id="test_proj")
    ckpt = RunnerCheckpoint(
        task_id="T0088",
        project_id="test_proj",
        state=RunnerState.BUILDING.value,
        current_role="BUILDER",
        worktree_path=str(worktree_path),
        worktree_branch="feature/t0088-runner",
        evidence_ids=("evi_88",),
    )
    store.save_checkpoint(ckpt)

    runner = ProductionRunner(registry=registry, checkpoint_store=store)
    active_handle = AgentHandle(
        session_id="sess_builder_t0088_123",
        host_id="codex_cli",
        status="running",
        is_real_host=True,
        adapter_instance_id="inst_1",
        invocation_token="tok_12345678901234567890",
    )
    runner._active_handles["T0088"] = active_handle

    result = runner.cancel(project_root=str(tmp_path), task_id="T0088")
    assert result.success is True
    assert result.state == RunnerState.CANCELLED.value
    assert len(cancelled_handles) == 1
    assert cancelled_handles[0].session_id == "sess_builder_t0088_123"

    loaded = store.load_checkpoint("T0088")
    assert loaded.state == RunnerState.CANCELLED.value
    assert loaded.evidence_ids == ("evi_88",)
    assert os.path.exists(str(worktree_path))


def test_cross_process_cancel_marker_reaches_original_host_handle(tmp_path):
    """独立 cancel CLI 进程只写 Checkpoint；原 Runner 必须据此取消自己持有的真实句柄。"""
    data_root = tmp_path / "data_root"
    data_root.mkdir()
    store = RunnerCheckpointStore(data_root=str(data_root), project_root=str(tmp_path), project_id="proj")
    store.save_checkpoint(RunnerCheckpoint(
        task_id="T0089",
        project_id="proj",
        state=RunnerState.BUILDING.value,
        current_role="BUILDER",
        worktree_path=str(tmp_path),
    ))

    started = threading.Event()
    released = threading.Event()
    cancelled = []

    class BlockingAdapter:
        def wait_for_result(self, handle, timeout_seconds=None):
            started.set()
            released.wait(timeout=5)
            return AgentResult(
                session_id=handle.session_id,
                status=AgentStatus.CANCELLED,
                output="cancelled",
                is_real_host=True,
            )

        def cancel_agent(self, handle):
            cancelled.append(handle.session_id)
            released.set()
            return True

    handle = AgentHandle(
        session_id="sess_builder_runner_t0089_1",
        host_id="codex_cli",
        status="running",
        is_real_host=True,
        adapter_instance_id="inst_original",
        invocation_token="tok_12345678901234567890",
    )
    original = ProductionRunner(checkpoint_store=store)
    outcome = {}

    def run_wait():
        try:
            original._wait_for_result_cancellable(BlockingAdapter(), handle, 5, "T0089")
        except BaseException as exc:
            outcome["error"] = exc

    worker = threading.Thread(target=run_wait)
    worker.start()
    assert started.wait(timeout=2)

    independent_cli_runner = ProductionRunner(checkpoint_store=store)
    cancel_result = independent_cli_runner.cancel(str(tmp_path), "T0089")
    worker.join(timeout=3)

    assert cancel_result.success is True
    assert isinstance(outcome.get("error"), RunnerCancelledError)
    assert cancelled == [handle.session_id]
    assert store.load_checkpoint("T0089").state == RunnerState.CANCELLED.value


def test_runner_resume_breakpoint_and_integrity_verification(tmp_path, monkeypatch):
    """
    P1 测试：
    resume 支持按 Reviewer / QA 断点恢复，并严格核验 Candidate Commit 与 Evidence 链。
    """
    repo_dir = tmp_path / "repo"
    repo_dir.mkdir()
    subprocess.run(["git", "init"], cwd=repo_dir, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.name", "TestDev"], cwd=repo_dir, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "test@test.com"], cwd=repo_dir, check=True, capture_output=True)

    readme = repo_dir / "README.md"
    readme.write_text("# Test Repo\n", encoding="utf-8")
    test_file = repo_dir / "test_app.py"
    test_file.write_text("def test_app(): assert True\n", encoding="utf-8")

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

    subprocess.run(["git", "add", "."], cwd=repo_dir, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "initial commit"], cwd=repo_dir, check=True, capture_output=True)
    baseline_sha = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=repo_dir, text=True).strip()

    cand_file = repo_dir / "candidate.py"
    cand_file.write_text("def candidate_ok(): return True\n", encoding="utf-8")
    subprocess.run(["git", "add", "candidate.py"], cwd=repo_dir, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "feat: candidate ready"], cwd=repo_dir, check=True, capture_output=True)
    cand_sha = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=repo_dir, text=True).strip()

    tasks_data = [
        {
            "id": "T0099",
            "name": "断点恢复测试",
            "status": "审查中",
            "assignee": "周审查",
            "owner": "李开发",
            "handler": "周审查",
            "process": "需求: 断点恢复。验收标准: 恢复成功。",
            "updated_at": "1.0",
        }
    ]
    with open(user_data_dir / "board.json", "w", encoding="utf-8") as f:
        json.dump(tasks_data, f)

    data_root = tmp_path / "data_root"
    data_root.mkdir()
    evidence_store = EvidenceStore(root_dir=str(data_root / "evidence"))
    checkpoint_store = RunnerCheckpointStore(data_root=str(data_root), project_root=str(repo_dir), project_id="test_repo")

    # 保存有效 Builder Evidence
    evi_id = "evi_builder_t0099_123"
    meta = EvidenceMetadata(
        project_id="test_repo",
        task_id="T0099",
        actor_role="BUILDER",
        host_id="codex_cli",
        adapter="codex_cli",
        host_session_id="sess_b",
        host_invocation_id="inv_b",
        is_real_host=True,
        workspace_mode="workspace_write",
        transition_from="BUILDING",
        transition_to="REVIEWING",
        created_at=time.time(),
    )
    rec = EvidenceRecord(
        evidence_id=evi_id,
        evidence_type=EvidenceType.TASK_TRANSITION,
        baseline_commit=baseline_sha,
        result_commit=cand_sha,
        artifacts=(),
        metadata=meta,
    )
    evidence_store.append(rec)

    # 1. 模拟 Builder 已提交且看板已进入审查中，但旧 Runner 在持久化
    # Reviewer 阶段前暂停；恢复必须以权威看板阶段为准，不得重复 Builder。
    ckpt = RunnerCheckpoint(
        task_id="T0099",
        project_id="test_repo",
        state=RunnerState.NEEDS_USER_INPUT.value,
        current_role="BUILDER",
        candidate_commit=cand_sha,
        candidate_generation=1,
        review_cycle=8,
        qa_cycle=13,
        total_attempts=0,
        worktree_path=str(repo_dir),
        worktree_branch="feature/t0099",
        evidence_ids=(evi_id,),
        execution_options={
            "builder_adapter_id": "codex_cli",
            "reviewer_adapter_id": "antigravity",
            "qa_adapter_id": "codex_cli",
            "test_commands": ["python -m pytest test_app.py -q"],
            "reviewer_timeout_seconds": 17,
            "qa_timeout_seconds": 19,
        },
    )
    checkpoint_store.save_checkpoint(ckpt)

    builder_called = False
    reviewer_called = False
    review_requests = {}
    qa_requests = {}
    observed_timeouts = {}

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
        nonlocal builder_called
        if req.role == "BUILDER":
            builder_called = True
        elif req.role == "QA":
            qa_requests[req.session_id] = req
        return AgentHandle(session_id=req.session_id, host_id="codex_cli", status="completed", is_real_host=True, adapter_instance_id="inst_c", invocation_token="tok_b")

    def mock_reviewer_dispatch(self, req):
        nonlocal reviewer_called
        reviewer_called = True
        review_requests[req.session_id] = req.extra_context["review_request_id"]
        return AgentHandle(session_id=req.session_id, host_id="antigravity", status="completed", is_real_host=True, adapter_instance_id="inst_a", invocation_token="tok_r")

    def mock_reviewer_wait(self, handle, timeout_seconds=None):
        observed_timeouts["reviewer"] = timeout_seconds
        out_json = {
            "task_id": "T0099",
            "baseline_commit": baseline_sha,
            "candidate_commit": cand_sha,
            "session_id": handle.session_id,
            "review_request_id": review_requests[handle.session_id],
            "decision": "PASS",
            "defects": [],
            "summary": "pass",
        }
        return AgentResult(session_id=handle.session_id, status=AgentStatus.SUCCESS, output=json.dumps(out_json), partial_results=({"invocation_id": "inv_r"},), is_real_host=True)

    def mock_qa_wait(self, handle, timeout_seconds=None):
        observed_timeouts["qa"] = timeout_seconds
        req = qa_requests[handle.session_id]
        output = {
            "task_id": "T0099",
            "baseline_commit": baseline_sha,
            "candidate_commit": cand_sha,
            "session_id": handle.session_id,
            "qa_request_id": req.extra_context["qa_request_id"],
            "acceptance_criteria_hash": req.extra_context["acceptance_criteria_hash"],
            "decision": "PASS",
            "acceptance_coverage": [
                {"criterion_id": "AC-01", "status": "PASS", "evidence": "test_app.py::test_app"},
            ],
            "test_commands": [
                {"command": command, "exit_code": 0, "summary": "passed"}
                for command in req.extra_context["required_test_commands"]
            ],
            "negative_scenarios": [
                {"name": "resume idempotency", "status": "PASS", "evidence": "checkpoint recovery test"},
            ],
            "uncovered_risks": [],
            "defects": [],
            "summary": "QA pass with complete coverage.",
        }
        return AgentResult(session_id=handle.session_id, status=AgentStatus.SUCCESS, output=json.dumps(output), partial_results=({"invocation_id": "inv_qa"},), is_real_host=True)

    monkeypatch.setattr(CodexCliAdapter, "detect_capabilities", mock_detect_caps)
    monkeypatch.setattr(AntigravityAdapter, "detect_capabilities", mock_detect_caps)
    monkeypatch.setattr(CodexCliAdapter, "dispatch_agent", mock_builder_dispatch)
    monkeypatch.setattr(AntigravityAdapter, "dispatch_agent", mock_reviewer_dispatch)
    monkeypatch.setattr(AntigravityAdapter, "wait_for_result", mock_reviewer_wait)
    monkeypatch.setattr(CodexCliAdapter, "wait_for_result", mock_qa_wait)

    registry = AdapterRegistry(context_id="test_repo")
    codex_manifest = create_codex_cli_manifest(adapter_id="codex_cli", verified_version="0.149.0")
    codex_adapter = CodexCliAdapter(is_real_host=True)
    registry.register(codex_adapter, codex_manifest)

    ag_manifest = create_antigravity_manifest(adapter_id="antigravity", verified_version="1.1.22")
    ag_adapter = AntigravityAdapter(is_real_host=True)
    registry.register(ag_adapter, ag_manifest)

    runner = ProductionRunner(
        registry=registry,
        evidence_store=evidence_store,
        checkpoint_store=checkpoint_store,
    )

    res = runner.resume(project_root=str(repo_dir), task_id="T0099", authority_root=str(repo_dir))
    assert res.success is True
    assert builder_called is False  # 断点在 Reviewer，Builder 不被重复调用
    assert reviewer_called is True
    recovered = checkpoint_store.load_checkpoint("T0099")
    assert recovered.review_cycle <= 1
    assert recovered.qa_cycle <= 1
    assert observed_timeouts["reviewer"] == 17.0
    assert 0 < observed_timeouts["qa"] <= 19.0

    # 2. 篡改 Candidate Commit 必须 Fail-Closed
    bad_ckpt = RunnerCheckpoint(
        task_id="T0099",
        project_id="test_repo",
        state=RunnerState.REVIEWING.value,
        current_role="REVIEWER",
        candidate_commit="f" * 40,
        worktree_path=str(repo_dir),
        evidence_ids=(evi_id,),
    )
    checkpoint_store.save_checkpoint(bad_ckpt)
    res_bad = runner.resume(project_root=str(repo_dir), task_id="T0099", authority_root=str(repo_dir))
    assert res_bad.success is False
    assert "does not exist in worktree Git history" in res_bad.message


def test_checkpoint_snapshot_round_trip_and_cancel_is_durable(tmp_path):
    store = RunnerCheckpointStore(
        data_root=str(tmp_path / "data"), project_root=str(tmp_path), project_id="demo"
    )
    original = RunnerCheckpoint(
        task_id="T0777", project_id="demo", state=RunnerState.BUILDING.value,
        current_role="BUILDER", candidate_commit="a" * 40,
        execution_spec_snapshot={"requirement_hash": "r", "baseline_commit": "b" * 40},
        active_elapsed_seconds=12.5,
    )
    store.save_checkpoint(original)
    loaded = store.load_checkpoint("T0777")
    assert dict(loaded.execution_spec_snapshot) == {
        "requirement_hash": "r", "baseline_commit": "b" * 40,
    }
    assert loaded.active_elapsed_seconds == 12.5

    store.save_checkpoint(replace(loaded, state=RunnerState.CANCELLED.value))
    with pytest.raises(CheckpointStoreError, match="durable cancellation"):
        store.save_checkpoint(replace(loaded, state=RunnerState.BUILDING.value))


def test_legacy_contract_snapshot_migrates_once_without_weakening_v2_guard(tmp_path):
    spec = TaskExecutionSpec(
        project_id="demo",
        project_root=str(tmp_path),
        authority_root=str(tmp_path),
        task_id="T0111",
        task_name="稳定契约",
        requirement_text="需求正文",
        acceptance_criteria="验收标准: 可观测结果",
        acceptance_criteria_hash="a" * 64,
        task_version="1",
        status_at_read="审查中",
        requirement_hash="b" * 64,
        owner="李开发",
    )
    legacy = production_runner_module._execution_spec_snapshot(spec)
    legacy.pop("contract_hash_version")
    legacy["requirement_hash"] = "c" * 64

    migrated = production_runner_module._upgrade_legacy_contract_snapshot(legacy, spec)
    assert migrated["contract_hash_version"] == 2
    assert migrated["requirement_hash"] == "b" * 64

    changed = replace(spec, requirement_hash="d" * 64)
    assert "requirement_hash" in " ".join(
        production_runner_module._snapshot_mismatches(migrated, changed)
    )

    wrong_acceptance = dict(legacy, acceptance_criteria_hash="e" * 64)
    assert production_runner_module._upgrade_legacy_contract_snapshot(
        wrong_acceptance, spec
    ) == wrong_acceptance


@pytest.mark.parametrize('corruption', [None, 'empty_commands', 'foreign_task', 'missing_capability'])
def test_accept_replays_same_sha_evidence_and_records_user_confirmation(tmp_path, monkeypatch, corruption):
    repo = tmp_path / "accept-repo"
    repo.mkdir()
    subprocess.run(["git", "init"], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=repo, check=True)
    (repo / "app.txt").write_text("base\n", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-m", "base"], cwd=repo, check=True, capture_output=True)
    baseline = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=repo, text=True).strip()
    (repo / "app.txt").write_text("candidate\n", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-m", "candidate"], cwd=repo, check=True, capture_output=True)
    candidate = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=repo, text=True).strip()

    data = tmp_path / "data"
    (repo / '.git' / 'info' / 'exclude').write_text('user_data/\n', encoding='utf-8')
    evidence = EvidenceStore(root_dir=str(repo / 'user_data' / 'runner_evidence'))
    created = time.time()
    evidence_ids = []
    for index, (role, source, target) in enumerate((
        ("BUILDER", "BUILDING", "REVIEWING"),
        ("REVIEWER", "REVIEWING", "TESTING"),
        ("QA", "TESTING", "PENDING_USER_ACCEPTANCE"),
    )):
        evidence_id = f"evi_{role.lower()}_accept"
        extra = production_runner_module._extract_capabilities_extra(HostCapabilities(is_real_host=True))
        if role == "QA":
            commands = ['python -m pytest', f'git diff --check {baseline}..{candidate} --']
            results = [{'command': cmd, 'exit_code': 0, 'output_hash': 'd' * 64} for cmd in commands]
            report = {
                'task_id': 'T0778', 'baseline_commit': baseline, 'candidate_commit': candidate,
                'session_id': 'session_qa', 'qa_request_id': 'qa_req_accept',
                'decision': 'PASS', 'acceptance_criteria_hash': 'c' * 64,
                'acceptance_coverage': [{'criterion_id': 'AC-01', 'status': 'PASS'}],
                'negative_scenarios': [{'name': 'invalid input', 'status': 'PASS'}],
                'uncovered_risks': [], 'defects': [],
                'test_commands': [{'command': cmd} for cmd in commands],
            }
            digest = lambda value: hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True).encode()).hexdigest()
            extra.update({
                "qa_decision": "PASS", "acceptance_criteria_hash": "c" * 64,
                "test_exit_codes": [0, 0], "uncovered_risk_count": 0, "defect_count": 0,
                'qa_request_id': 'qa_req_accept', 'qa_report': report, 'qa_report_hash': digest(report),
                'required_test_commands': commands, 'required_test_command_count': 2,
                'runner_test_results': results, 'test_command_hash': digest(commands),
                'test_output_hash': digest(results), 'covered_criterion_ids': ['AC-01'],
                'negative_scenario_count': 1,
            })
            if corruption == 'empty_commands':
                extra['test_exit_codes'] = []
        if corruption == 'missing_capability':
            extra.pop('capability_is_real_host')
        evidence.append(EvidenceRecord(
            evidence_id=evidence_id, evidence_type=EvidenceType.TASK_TRANSITION,
            baseline_commit=baseline, result_commit=candidate, artifacts=(),
            metadata=EvidenceMetadata(
                project_id="demo", task_id="T9999" if corruption == 'foreign_task' else "T0778", actor_role=role,
                host_id=f"host_{role.lower()}", adapter=f"adapter_{role.lower()}",
                host_session_id=f"session_{role.lower()}", host_invocation_id=f"inv_{role.lower()}",
                is_real_host=True, workspace_mode="workspace_read" if role != "BUILDER" else "workspace_write",
                transition_from=source, transition_to=target, created_at=created + index,
                extra=extra,
            ),
        ))
        evidence_ids.append(evidence_id)

    spec = TaskExecutionSpec(
        project_id="demo", project_root=str(repo), authority_root=str(repo),
        task_id="T0778", task_name="accept", requirement_text="requirement",
        acceptance_criteria="criteria", acceptance_criteria_hash="c" * 64,
        task_version="1", status_at_read="已完成", requirement_hash="r" * 64,
        baseline_commit=baseline, workspace_mode="inherit", test_commands=("python -m pytest",),
    )
    store = RunnerCheckpointStore(data_root=str(data), project_root=str(repo), project_id="demo")
    store.save_checkpoint(RunnerCheckpoint(
        task_id="T0778", project_id="demo", state=RunnerState.PENDING_USER_ACCEPTANCE.value,
        current_role="QA", candidate_commit=candidate, candidate_generation=1,
        worktree_path=str(repo), evidence_ids=tuple(evidence_ids),
        confirmation_request_id="conf-accept",
        execution_spec_snapshot=production_runner_module._execution_spec_snapshot(spec),
    ))
    runner = ProductionRunner(checkpoint_store=store, evidence_store=evidence)
    monkeypatch.setattr(production_runner_module, "load_task_execution_spec", lambda **kwargs: spec)
    from scripts._lib.core.runner_transition_gate import validate_managed_transition
    def checked_transition(*args, **kwargs):
        current = store.load_checkpoint('T0778')
        validate_managed_transition(str(data), 'T0778', args[3], args[4], current.evidence_ids[-1])
        return True, None
    monkeypatch.setattr(runner, "_do_state_transition", checked_transition)
    if corruption is None:
        for source, target, proof in zip(
            ('进行中', '审查中', '测试中'), ('审查中', '测试中', '已完成'), evidence_ids
        ):
            current = store.load_checkpoint('T0778')
            store.save_checkpoint(replace(current, execution_options={'test_commands': ['python -m pytest']}))
            validate_managed_transition(str(data), 'T0778', source, target, proof)

    result = runner.accept(str(repo), "T0778", "conf-accept", str(repo))

    if corruption:
        assert result.success is False
        assert store.load_checkpoint('T0778').state == RunnerState.PENDING_USER_ACCEPTANCE.value
        return
    assert result.success is True
    assert result.state == RunnerState.ACCEPTED.value
    assert len(result.evidence_ids) == 4
    assert evidence.read(result.evidence_ids[-1]).evidence_type == EvidenceType.USER_CONFIRMATION
