# -*- coding: utf-8 -*-
"""多文件写入的排他锁、原子替换、备份与恢复。恢复不覆盖事务之后的用户改动。"""

from __future__ import annotations

import json
import os
import tempfile
import uuid
from typing import Iterable

from scripts._lib.core.file_lock import LockBusyError, acquire_lock, release_lock

from .common import OnboardError, guard_project_path, read_bytes, sha256_bytes


def lock_path(data_root: str) -> str:
    return os.path.join(data_root, "user_data", "locks", "onboarding.lock")


def progress_path(data_root: str) -> str:
    return os.path.join(data_root, "user_data", "onboarding", "progress.json")


def state_path(data_root: str) -> str:
    return os.path.join(data_root, "user_data", "onboarding", "state.json")


def atomic_write(path: str, data: bytes, *, project_root: str | None = None) -> None:
    if project_root is not None:
        guard_project_path(project_root, path)
    directory = os.path.dirname(path)
    os.makedirs(directory, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=".yy-flow-onboard-", dir=directory)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        if project_root is not None:
            guard_project_path(project_root, path)
        os.replace(temporary, path)
    except Exception:
        try:
            os.unlink(temporary)
        except OSError:
            pass
        raise


def load_json(path: str) -> dict | None:
    if not os.path.isfile(path):
        return None
    try:
        with open(path, "r", encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise OnboardError("BAD_CONFIG", "接入进度无法解析") from exc
    if not isinstance(data, dict):
        raise OnboardError("BAD_CONFIG", "接入进度无法解析")
    return data


def save_json(path: str, data: dict) -> None:
    raw = json.dumps(data, ensure_ascii=False, indent=2).encode("utf-8") + b"\n"
    atomic_write(path, raw)


def acquire(data_root: str):
    try:
        return acquire_lock(lock_path(data_root), blocking=False, write_meta=True)
    except LockBusyError as exc:
        raise OnboardError("LOCKED", "项目接入锁被占用。请等待其他接入结束，不要同时写入。", exit_code=3) from exc


def new_progress(data_root: str, files: Iterable[dict]) -> dict:
    return {
        "txn_id": uuid.uuid4().hex,
        "phase": "writing",
        "complete": False,
        "files": list(files),
    }


def file_record(path: str, data: bytes, role: str, config_index: int, entry_hash: str, backup_dir: str) -> dict:
    current = read_bytes(path)
    backup = ""
    backup_sha = ""
    had_backup = current is not None
    if had_backup:
        os.makedirs(backup_dir, exist_ok=True)
        backup = os.path.join(backup_dir, sha256_bytes(os.path.abspath(path).encode("utf-8")))
        atomic_write(backup, current)
        backup_sha = sha256_bytes(current)
    return {
        "path": os.path.abspath(path),
        "role": role,
        "config_index": config_index,
        "sha256": sha256_bytes(data),
        "entry_hash": entry_hash,
        "status": "pending",
        "had_backup": had_backup,
        "backup": backup,
        "backup_sha256": backup_sha,
    }


def mark_status(progress: dict, path: str, status: str) -> None:
    for item in progress["files"]:
        if os.path.normcase(item["path"]) == os.path.normcase(path):
            item["status"] = status
            return


def mark_written(progress: dict, path: str) -> None:
    mark_status(progress, path, "written")


def incomplete(progress: dict | None) -> bool:
    return bool(progress) and progress.get("phase") in {"writing", "failed", "self_check_failed"}


def rollback(progress: dict, project_root: str) -> list[str]:
    """把仍等于本次计划内容的文件恢复到备份。用户后续改动会中止恢复。

    pending / replacing 也要检查：替换已经落盘、进度还没记成 written 时同样恢复。
    """
    if not incomplete(progress):
        return []
    problems = []
    actions = []
    for item in progress.get("files", []):
        status = item.get("status")
        if status not in {"written", "replacing", "pending"}:
            continue
        path = item["path"]
        guard_project_path(project_root, path)
        if item.get("had_backup"):
            backup_path = item.get("backup")
            if not isinstance(backup_path, str) or not backup_path:
                raise OnboardError("RESTORE", "备份记录无效，已停止恢复")
            guard_project_path(project_root, backup_path)
        current = read_bytes(path)
        planned = item.get("sha256", "")
        backup_sha = item.get("backup_sha256", "")
        if current is None:
            if item.get("had_backup") and status in {"written", "replacing"}:
                problems.append(path)
            continue
        current_sha = sha256_bytes(current)
        if item.get("had_backup") and current_sha == backup_sha:
            continue
        if current_sha == planned:
            actions.append(item)
            continue
        problems.append(path)
    if problems:
        raise OnboardError("USER_CHANGE", "恢复已停止，事务之后有文件被修改，未覆盖这些文件：" + "、".join(problems))
    restored = []
    for item in reversed(actions):
        path = item["path"]
        guard_project_path(project_root, path)
        if item.get("had_backup"):
            guard_project_path(project_root, item["backup"])
            backup = read_bytes(item.get("backup") or "")
            if backup is None or sha256_bytes(backup) != item.get("backup_sha256"):
                raise OnboardError("RESTORE", "备份不可用，已停止恢复")
            atomic_write(path, backup, project_root=project_root)
        else:
            guard_project_path(project_root, path)
            os.remove(path)
        item["status"] = "restored"
        restored.append(path)
    progress["phase"] = "rolled_back"
    progress["complete"] = False
    return restored
