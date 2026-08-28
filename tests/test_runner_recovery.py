# -*- coding: utf-8 -*-
"""
tests/test_runner_recovery.py
Runner Checkpoint、幂等恢复、排他并发锁与安全取消测试。
"""
import json
import os
import subprocess
import pytest
from unittest.mock import MagicMock

from scripts._lib.core.agent_schema import AgentHandle, AgentResult, AgentStatus
from scripts._lib.core.evidence_store import EvidenceStore
from scripts._lib.core.production_runner import ProductionRunner
from scripts._lib.core.runner_checkpoint_store import RunnerCheckpointStore
from scripts._lib.core.runner_schema import RunnerCheckpoint, RunnerState, TaskExecutionSpec
from scripts._lib.hosts.antigravity_adapter import AntigravityAdapter
from scripts._lib.hosts.codex_cli_adapter import CodexCliAdapter


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


def test_checkpoint_store_atomic_save_and_load(tmp_path):
    data_root = tmp_path / "data_root"
    data_root.mkdir()
    store = RunnerCheckpointStore(data_root=str(data_root))

    ckpt = RunnerCheckpoint(
        task_id="T0099",
        project_id="test_proj",
        state=RunnerState.BUILDING.value,
        current_role="BUILDER",
        candidate_commit="a" * 40,
        candidate_generation=1,
        review_cycle=0,
        qa_cycle=0,
        total_attempts=1,
    )

    store.save_checkpoint(ckpt)
    loaded = store.load_checkpoint("T0099")
    assert loaded is not None
    assert loaded.task_id == "T0099"
    assert loaded.state == RunnerState.BUILDING.value
    assert loaded.candidate_commit == "a" * 40


def test_runner_concurrency_lock(tmp_path):
    data_root = tmp_path / "data_root"
    data_root.mkdir()
    store = RunnerCheckpointStore(data_root=str(data_root))

    lock1 = store.acquire_runner_lock("T0099")
    assert lock1[0] is not None

    # Second acquire should fail
    lock2 = store.acquire_runner_lock("T0099")
    assert lock2[0] is None

    store.release_runner_lock(lock1)

    # After release, acquire should succeed
    lock3 = store.acquire_runner_lock("T0099")
    assert lock3[0] is not None
    store.release_runner_lock(lock3)


def test_runner_cancel_safely_preserves_worktree(mock_git_repo, tmp_path):
    repo_dir, baseline_sha = mock_git_repo
    data_root = tmp_path / "data_root"
    data_root.mkdir()
    store = RunnerCheckpointStore(data_root=str(data_root))

    ckpt = RunnerCheckpoint(
        task_id="T0099",
        project_id="test_proj",
        state=RunnerState.BUILDING.value,
        current_role="BUILDER",
        candidate_commit=baseline_sha,
        candidate_generation=1,
        worktree_path=str(repo_dir),
    )
    store.save_checkpoint(ckpt)

    runner = ProductionRunner(checkpoint_store=store)
    result = runner.cancel(project_root=str(repo_dir), task_id="T0099")

    assert result.success is True
    assert result.state == RunnerState.CANCELLED.value

    loaded = store.load_checkpoint("T0099")
    assert loaded.state == RunnerState.CANCELLED.value
    assert os.path.exists(repo_dir)  # Worktree preserved
