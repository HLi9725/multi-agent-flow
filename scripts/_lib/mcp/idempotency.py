# -*- coding: utf-8 -*-
"""MCP 幂等账本。与 Evidence 目录分开，只追加。"""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from typing import Any, Callable, Dict, Optional

from ..core import file_lock
from .registry import assert_registered_path
from .registry import ProjectRecord

_PENDING_SECONDS = 60


class IdempotencyReadError(OSError):
    """账本不可读取时禁止把它当作空账本重新执行业务。"""


class IdempotencyStore:
    def __init__(self, project: ProjectRecord) -> None:
        root = os.path.realpath(os.path.join(project.data_root, "mcp_idempotency"))
        assert_registered_path(project, root)
        self.directory = root
        self.path = os.path.join(root, project.project_id + ".jsonl")
        self.lock_path = self.path + ".lock"

    def start(
        self,
        *,
        principal_id: str,
        tool: str,
        key: str,
        digest: str,
        fact: Callable[[Dict[str, Any]], str],
        target: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        with file_lock.acquire_lock(self.lock_path, blocking=True, timeout=5.0):
            latest = self._latest(principal_id, tool, key)
            if latest is None:
                self._append(_pending(principal_id, tool, key, digest, target or {}))
                return {"action": "execute", "target": dict(target or {})}
            same = latest.get("request_sha256") == digest
            state = latest.get("state")
            if state in {"completed", "applied"}:
                if same:
                    return {"action": "replay", "result": latest.get("result") or {},
                            "audit_pending": state == "applied" or not latest.get("audit_event_id")}
                return {"action": "conflict", "reason": "DIGEST"}
            if state == "failed":
                if not same:
                    return {"action": "conflict", "reason": "DIGEST"}
                self._append(_pending(principal_id, tool, key, digest, target or {}))
                return {"action": "execute", "target": dict(target or {})}
            if state == "pending":
                age = _age_seconds(str(latest.get("created_at") or ""))
                if age < _PENDING_SECONDS:
                    return {"action": "in_progress"}
                if not same:
                    return {"action": "conflict", "reason": "DIGEST"}
                outcome = fact(latest)
                if outcome == "achieved":
                    result = latest.get("result") if isinstance(latest.get("result"), dict) else {}
                    if not result:
                        result = _replay_from_target(latest.get("target"))
                    applied = _completed(principal_id, tool, key, digest, result)
                    applied["state"] = "applied"
                    self._append(applied)
                    return {"action": "replay", "result": result, "audit_pending": True}
                if outcome == "mismatch":
                    return {"action": "conflict", "reason": "EVIDENCE"}
                if outcome == "unbound":
                    return {"action": "unbound"}
                kept = latest.get("target") if isinstance(latest.get("target"), dict) else {}
                self._append(_pending(principal_id, tool, key, digest, kept))
                return {"action": "execute", "target": dict(kept)}
            self._append(_pending(principal_id, tool, key, digest, target or {}))
            return {"action": "execute", "target": dict(target or {})}

    def bind(
        self,
        *,
        principal_id: str,
        tool: str,
        key: str,
        digest: str,
        target: Dict[str, Any],
    ) -> None:
        """领域函数已经给出编号或摘要后，先把绑定写入账本并 fsync，再允许响应。"""
        with file_lock.acquire_lock(self.lock_path, blocking=True, timeout=5.0):
            latest = self._latest(principal_id, tool, key)
            merged: Dict[str, Any] = {}
            if latest and isinstance(latest.get("target"), dict):
                merged.update(latest["target"])
            merged.update(target)
            created_at = str(latest.get("created_at") or "") if latest else ""
            record = _pending(principal_id, tool, key, digest, merged)
            if created_at:
                record["created_at"] = created_at
            self._append(record)

    def applied(self, *, principal_id: str, tool: str, key: str, digest: str, result: Dict[str, Any]) -> None:
        """业务事实已落盘；终态审计未确认，重试只能补审计不能重执业务。"""
        with file_lock.acquire_lock(self.lock_path, blocking=True, timeout=5.0):
            record = _completed(principal_id, tool, key, digest, result)
            record["state"] = "applied"
            self._append(record)

    def complete(self, *, principal_id: str, tool: str, key: str, digest: str, result: Dict[str, Any], audit_event_id: str = "") -> None:
        with file_lock.acquire_lock(self.lock_path, blocking=True, timeout=5.0):
            record = _completed(principal_id, tool, key, digest, result)
            if audit_event_id:
                record["audit_event_id"] = audit_event_id
            self._append(record)

    def fail(self, *, principal_id: str, tool: str, key: str, digest: str, code: str) -> None:
        with file_lock.acquire_lock(self.lock_path, blocking=True, timeout=5.0):
            self._append(
                {
                    "principal_id": principal_id,
                    "tool": tool,
                    "idempotency_key": key,
                    "request_sha256": digest,
                    "state": "failed",
                    "code": code,
                    "created_at": _now(),
                }
            )

    def _latest(self, principal_id: str, tool: str, key: str) -> Optional[Dict[str, Any]]:
        if not os.path.isfile(self.path):
            return None
        found = None
        try:
            with open(self.path, "r", encoding="utf-8") as handle:
                for line in handle:
                    line = line.strip()
                    if not line:
                        continue
                    item = json.loads(line)
                    if not isinstance(item, dict):
                        raise ValueError("Invalid ledger record")
                    if (
                        item.get("principal_id") == principal_id
                        and item.get("tool") == tool
                        and item.get("idempotency_key") == key
                    ):
                        found = item
        except (OSError, UnicodeError, ValueError):
            raise IdempotencyReadError("Idempotency ledger unavailable") from None
        return found

    def _append(self, record: Dict[str, Any]) -> None:
        os.makedirs(self.directory, exist_ok=True)
        line = json.dumps(record, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
        with open(self.path, "a", encoding="utf-8") as handle:
            handle.write(line + "\n")
            handle.flush()
            os.fsync(handle.fileno())


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _pending(
    principal_id: str,
    tool: str,
    key: str,
    digest: str,
    target: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    record = {
        "principal_id": principal_id,
        "tool": tool,
        "idempotency_key": key,
        "request_sha256": digest,
        "state": "pending",
        "created_at": _now(),
    }
    if target:
        record["target"] = target
    return record


def _replay_from_target(target: Any) -> Dict[str, Any]:
    if not isinstance(target, dict):
        return {}
    body = {}
    for key in ("task_id", "status", "assignee", "evidence_id", "content_hash", "task_version", "is_deleted"):
        if key in target:
            body[key] = target[key]
    return body


def _completed(principal_id: str, tool: str, key: str, digest: str, result: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "principal_id": principal_id,
        "tool": tool,
        "idempotency_key": key,
        "request_sha256": digest,
        "state": "completed",
        "result": result,
        "created_at": _now(),
    }


def _age_seconds(created_at: str) -> float:
    try:
        parsed = datetime.strptime(created_at, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    except ValueError:
        return _PENDING_SECONDS + 1
    return (datetime.now(timezone.utc) - parsed).total_seconds()
