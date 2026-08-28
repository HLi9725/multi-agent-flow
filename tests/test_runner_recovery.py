# -*- coding: utf-8 -*-
"""
tests/test_runner_recovery.py
Runner 状态恢复、原子 Checkpoint、防路径逃逸与并发锁测试 (DEF-T0061-6)。
"""
import os
import pytest
import tempfile
import time

from scripts._lib.core.runner_checkpoint_store import RunnerCheckpointStore, CheckpointStoreError
from scripts._lib.core.runner_schema import RunnerCheckpoint, RunnerState
from scripts._lib.core.production_runner import ProductionRunner


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
    )

    store.save_checkpoint(ckpt)
    loaded = store.load_checkpoint("T0099")

    assert loaded is not None
    assert loaded.task_id == "T0099"
    assert loaded.project_id == "proj_alpha"
    assert loaded.state == RunnerState.REVIEWING.value
    assert loaded.evidence_ids == ("evi_b_1",)


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

    # 释放后可再次获取
    h3, f3 = store.acquire_runner_lock("T0099")
    assert h3 is not None
    store.release_runner_lock((h3, f3))


def test_runner_cancel_safely_preserves_worktree(tmp_path):
    store = RunnerCheckpointStore(data_root=str(tmp_path), project_id="proj_alpha")
    ckpt = RunnerCheckpoint(
        task_id="T0099",
        project_id="proj_alpha",
        state=RunnerState.BUILDING.value,
        current_role="BUILDER",
        worktree_path=str(tmp_path / "worktree_test"),
        evidence_ids=("evi_b_1",),
    )
    store.save_checkpoint(ckpt)

    runner = ProductionRunner(checkpoint_store=store)
    res = runner.cancel(project_root=str(tmp_path), task_id="T0099")

    assert res.success is True
    assert res.state == RunnerState.CANCELLED.value

    loaded = store.load_checkpoint("T0099")
    assert loaded.state == RunnerState.CANCELLED.value
    assert loaded.worktree_path == str(tmp_path / "worktree_test")
    assert loaded.evidence_ids == ("evi_b_1",)
