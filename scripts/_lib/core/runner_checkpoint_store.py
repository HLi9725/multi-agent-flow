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
import uuid
from typing import Any, Dict, Optional, Tuple

_SCRIPTS_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
if _SCRIPTS_ROOT not in sys.path:
    sys.path.insert(0, _SCRIPTS_ROOT)

try:
    from ... import paths
except Exception:
    try:
        from scripts import paths
    except Exception:
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

        # 命名空间同时绑定可读项目名和规范根路径哈希。仅做字符替换会让
        # ``a/b`` 与 ``a?b`` 发生命名空间碰撞，因此不得单独依赖调用方传入的 project_id。
        readable_id = re.sub(r"[^a-zA-Z0-9_-]", "_", (project_id or "project").strip()) or "project"
        identity_root = os.path.realpath(project_root or self.data_root)
        identity = hashlib.sha256(f"{readable_id}\0{identity_root}".encode("utf-8")).hexdigest()[:16]
        self.project_id = f"{readable_id[:48]}_{identity}"

        self.checkpoint_dir = os.path.join(self.data_root, "runner_checkpoints", self.project_id)

    def _ensure_dir(self):
        os.makedirs(self.checkpoint_dir, exist_ok=True)

    def get_checkpoint_path(self, task_id: str) -> str:
        clean_id = _validate_task_id(task_id)
        return os.path.join(self.checkpoint_dir, f"{clean_id}.json")

    def save_checkpoint(self, checkpoint: RunnerCheckpoint) -> None:
        self._ensure_dir()
        with file_lock.acquire_lock(self.get_checkpoint_path(checkpoint.task_id) + '.write.lock', blocking=True, timeout=10):
            self._save_locked(checkpoint)

    def initialize_checkpoint(self, checkpoint: RunnerCheckpoint, *, restart_cancelled=False) -> None:
        """Create once, or explicitly archive a cancelled run before replacing it."""
        self._ensure_dir()
        with file_lock.acquire_lock(self.get_checkpoint_path(checkpoint.task_id) + '.write.lock', blocking=True, timeout=10):
            old = self.load_checkpoint(checkpoint.task_id)
            if old:
                if not restart_cancelled or old.state != 'CANCELLED':
                    raise CheckpointStoreError('Checkpoint already exists; use resume, not start.')
                archive = self.get_checkpoint_path(checkpoint.task_id) + '.archive_' + uuid.uuid4().hex
                with open(archive, 'xb') as handle:
                    handle.write(json.dumps(old.to_dict(), ensure_ascii=False).encode('utf-8'))
                    handle.flush()
                    os.fsync(handle.fileno())
            self._save_locked(checkpoint, initializing=True)

    def _save_locked(self, checkpoint: RunnerCheckpoint, initializing=False) -> None:
        """
        原子写入 Checkpoint 文件（Create-or-Replace with fsync）
        """
        self._ensure_dir()
        target_path = self.get_checkpoint_path(checkpoint.task_id)
        temp_path = target_path + '.tmp_' + uuid.uuid4().hex

        data = checkpoint.to_dict()
        payload = json.dumps(data, indent=2, ensure_ascii=False).encode("utf-8")

        try:
            if os.path.isfile(target_path):
                with open(target_path, "r", encoding="utf-8") as existing_file:
                    existing = json.load(existing_file)
                if not initializing and existing.get('run_id', '') != checkpoint.run_id:
                    raise CheckpointStoreError('Stale run cannot overwrite a newer run.')
                if not initializing and existing.get("state") == "CANCELLED" and checkpoint.state != "CANCELLED":
                    raise CheckpointStoreError(
                        f"Refusing to overwrite durable cancellation for {checkpoint.task_id}."
                    )
            with open(temp_path, "wb") as f:
                f.write(payload)
                f.flush()
                os.fsync(f.fileno())
            if os.path.isfile(target_path):
                with open(target_path, "r", encoding="utf-8") as existing_file:
                    latest = json.load(existing_file)
                if not initializing and latest.get("state") == "CANCELLED" and checkpoint.state != "CANCELLED":
                    raise CheckpointStoreError(
                        f"Refusing to overwrite durable cancellation for {checkpoint.task_id}."
                    )
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
        # Bind locks to the same explicit data root as checkpoints.  Resolving
        # from the process CWD/skill location can put a shared-installation lock
        # in another project's namespace.
        locks_dir = os.path.join(self.data_root, "user_data", "locks")
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
