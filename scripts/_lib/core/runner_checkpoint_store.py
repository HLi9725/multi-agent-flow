# -*- coding: utf-8 -*-
"""
scripts/_lib/core/runner_checkpoint_store.py
原子 Checkpoint 存储与排他并发锁管理。
保证故障恢复幂等性与零敏感凭据泄漏。
"""
import json
import os
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


class RunnerCheckpointStore:
    """原子 Checkpoint 存储管理"""

    def __init__(self, data_root: Optional[str] = None):
        if data_root is None:
            self.data_root = paths.resolve_data_root()
        else:
            self.data_root = os.path.realpath(data_root)
        self.checkpoint_dir = os.path.join(self.data_root, "runner_checkpoints")

    def _ensure_dir(self):
        os.makedirs(self.checkpoint_dir, exist_ok=True)

    def get_checkpoint_path(self, task_id: str) -> str:
        return os.path.join(self.checkpoint_dir, f"{task_id}.json")

    def save_checkpoint(self, checkpoint: RunnerCheckpoint) -> None:
        """
        原子写入 Checkpoint 文件（Create-or-Replace with fsync）
        """
        self._ensure_dir()
        target_path = self.get_checkpoint_path(checkpoint.task_id)
        temp_path = target_path + f".tmp_{os.getpid()}_{int(time.time()*1000)}"

        data = checkpoint.to_dict()
        # 序列化为规范 JSON
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
        target_path = self.get_checkpoint_path(task_id)
        if not os.path.isfile(target_path):
            return None

        try:
            with open(target_path, "r", encoding="utf-8") as f:
                data = json.load(f)
            return RunnerCheckpoint.from_dict(data)
        except Exception as e:
            raise CheckpointStoreError(f"Checkpoint for {task_id} is corrupted or invalid: {e}") from e

    def delete_checkpoint(self, task_id: str) -> bool:
        """删除指定的 Checkpoint 文件"""
        target_path = self.get_checkpoint_path(task_id)
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
        target_path = os.path.join(self.checkpoint_dir, f"{task_id}.json")
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
        Fail-Closed: 若锁已被占用，返回 (None, None)。
        """
        locks_dir = paths.locks_dir()
        os.makedirs(locks_dir, exist_ok=True)
        lock_file = os.path.join(locks_dir, f".lock_runner_{task_id}.lock")
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
