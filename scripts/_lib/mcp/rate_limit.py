# -*- coding: utf-8 -*-
"""按 (principal_id, project_id) 持久化令牌桶。进程重启不重置当前余量。"""

from __future__ import annotations

import json
import os
import time

from ..core import file_lock
from .auth import auth_dir
from .registry import Registry

READ_PER_MINUTE = 60
WRITE_PER_MINUTE = 20


def allow(registry: Registry, principal_id: str, project_id: str, kind: str) -> bool:
    capacity = READ_PER_MINUTE if kind == "read" else WRITE_PER_MINUTE
    rate = capacity / 60.0
    path = os.path.join(auth_dir(registry), "rate_buckets.json")
    lock = path + ".lock"
    now = time.time()
    key = principal_id + "\n" + project_id + "\n" + kind
    with file_lock.acquire_lock(lock, blocking=True, timeout=5.0):
        state = _load(path)
        bucket = state.get(key)
        if not isinstance(bucket, dict):
            bucket = {"tokens": capacity, "updated_at": now}
        updated = bucket.get("updated_at")
        held = bucket.get("tokens")
        try:
            tokens = float(held)
            stamp = float(updated)
        except (TypeError, ValueError):
            tokens = float(capacity)
            stamp = now
        elapsed = max(0.0, now - stamp)
        tokens = min(float(capacity), tokens + elapsed * rate)
        if tokens < 1.0:
            state[key] = {"tokens": tokens, "updated_at": now}
            _store(path, state)
            return False
        state[key] = {"tokens": tokens - 1.0, "updated_at": now}
        _store(path, state)
        return True


def _load(path: str) -> dict:
    if not os.path.isfile(path):
        return {}
    try:
        with open(path, "r", encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, UnicodeError, json.JSONDecodeError):
        return {}
    if not isinstance(data, dict):
        return {}
    return data


def _store(path: str, state: dict) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    temporary = path + ".tmp"
    raw = json.dumps(state, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    with open(temporary, "wb") as handle:
        handle.write(raw)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)
