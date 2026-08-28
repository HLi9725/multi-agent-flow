# -*- coding: utf-8 -*-
"""
scripts/_lib/core/runner_checkpoint_store.py
原子 Checkpoint 存储与排他并发锁管理。
保证故障恢复幂等性、防路径逃逸、防跨项目串线与零敏感凭据泄漏。
"""
import hashlib
import json
import os
import re
import sys
import tempfile
import time
from typing import Any, Dict, Optional, Tuple

_SCRIPTS_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
if _SCRIPTS_ROOT not in sys.path:
    sys.path.insert(0, _SCRIPTS_ROOT)

import paths
from _lib.core import file_lock
from .runner_schema import RunnerCheckpoint


class CheckpointStoreError(Exception):
    pass


def _validate_task_id(task_id: str) -> str:
    """严格校验 task_id 格式，防目录遍历与注入"""
    if not task_id or not isinstance(task_id, str):
        raise CheckpointStoreError("task_id must be a non-empty string")
    cleaned = task_id.strip()
    if not re.match(r"^T\d+$", cleaned):
        raise CheckpointStoreError(f"Invalid task_id format '{task_id}'. Expected 'T<digits>' format (e.g. T0054, T0061).")
    return cleaned


class RunnerCheckpointStore:
    """原子 Checkpoint 存储与排他锁管理"""

    def __init__(
        self,
        data_root: Optional[str] = None,
        project_root: Optional[str] = None,
        project_id: Optional[str] = None,
    ):
        if data_root is None:
            self.data_root = paths.resolve_data_root(cwd=project_root)
        else:
            self.data_root = os.path.realpath(data_root)

        # 绑定 project_id 命名空间，隔离多项目同编号任务
        if project_id:
            self.project_id = re.sub(r"[^a-zA-Z0-9_-]", "_", project_id.strip())
        elif project_root:
            self.project_id = hashlib.sha256(os.path.realpath(project_root).encode("utf-8")).hexdigest()[:16]
        else:
            self.project_id = "default_project"

        self.checkpoint_dir = os.path.join(self.data_root, "runner_checkpoints", self.project_id)

    def _ensure_dir(self):
        os.makedirs(self.checkpoint_dir, exist_ok=True)

    def get_checkpoint_path(self, task_id: str) -> str:
        clean_id = _validate_task_id(task_id)
        return os.path.join(self.checkpoint_dir, f"{clean_id}.json")

    def save_checkpoint(self, checkpoint: RunnerCheckpoint) -> None:
        """
        原子写入 Checkpoint 文件（Create-or-Replace with fsync）
        """
        self._ensure_dir()
        target_path = self.get_checkpoint_path(checkpoint.task_id)
        temp_path = target_path + f".tmp_{os.getpid()}_{int(time.time()*1000)}"

        data = checkpoint.to_dict()
        payload = json.dumps(data, indent=2, ensure_ascii=False).encode("utf-8")

        try:
            with open(temp_path, "wb") as f:
                f.write(payload)
                f.flush()
                os.fsync(f.fileno())
            os.replace(temp_path, target_path)
        except Exception as e:
            if os.path.exists(temp_path):
                try:
                    os.unlink(temp_path)
                except Exception:
                    pass
            raise CheckpointStoreError(f"Failed to atomically write checkpoint for {checkpoint.task_id}: {e}") from e

    def load_checkpoint(self, task_id: str) -> Optional[RunnerCheckpoint]:
        """
        读取 Checkpoint。若不存在返回 None；损坏时抛出异常 Fail-Closed。
        """
        clean_id = _validate_task_id(task_id)
        target_path = self.get_checkpoint_path(clean_id)
        if not os.path.isfile(target_path):
            return None

        try:
            with open(target_path, "r", encoding="utf-8") as f:
                data = json.load(f)
            return RunnerCheckpoint.from_dict(data)
        except Exception as e:
            raise CheckpointStoreError(f"Checkpoint for {clean_id} is corrupted or invalid: {e}") from e

    def delete_checkpoint(self, task_id: str) -> bool:
        """删除指定的 Checkpoint 文件"""
        clean_id = _validate_task_id(task_id)
        target_path = self.get_checkpoint_path(clean_id)
        if os.path.isfile(target_path):
            try:
                os.unlink(target_path)
                return True
            except Exception:
                return False
        return False

    def query_status(self, task_id: str) -> Optional[Dict[str, Any]]:
        """
        纯只读状态查询：零写目录、零锁副作用、零宿主调用。
        """
        clean_id = _validate_task_id(task_id)
        target_path = os.path.join(self.checkpoint_dir, f"{clean_id}.json")
        if not os.path.exists(target_path):
            return None
        try:
            with open(target_path, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return None

    def acquire_runner_lock(self, task_id: str) -> Tuple[Any, Optional[str]]:
        """
        获取 Runner 进程级物理排他并发锁。
        锁文件名绑定 project_id 与 task_id。
        Fail-Closed: 若锁已被占用，返回 (None, None)。
        """
        clean_id = _validate_task_id(task_id)
        locks_dir = paths.locks_dir()
        os.makedirs(locks_dir, exist_ok=True)
        lock_file = os.path.join(locks_dir, f".lock_runner_{self.project_id}_{clean_id}.lock")
        try:
            handle = file_lock.acquire_lock(lock_file, blocking=False)
            return handle, lock_file
        except file_lock.LockBusyError:
            return None, None
        except Exception:
            return None, None

    def release_runner_lock(self, lock_tuple: Tuple[Any, Optional[str]]) -> None:
        """释放排他锁"""
        if lock_tuple and lock_tuple[0]:
            try:
                file_lock.release_lock(lock_tuple[0])
            except Exception:
                pass
