# -*- coding: utf-8 -*-
"""追加型审计。与看板、Evidence 分文件。4A 记录拒绝与读取。"""

from __future__ import annotations

import hashlib
import json
import os
from datetime import datetime, timezone
from typing import Any, Dict, Mapping

from ..core import file_lock
from .redaction import RedactionRejected, fit_audit_line, mask_data


class AuditWriteError(Exception):
    """审计没有落盘。调用方不得把业务写入报告为成功。"""


class AuditStore:
    def __init__(self, directory: str) -> None:
        self.directory = os.path.realpath(directory)
        self._path = os.path.join(self.directory, "events.jsonl")
        self._tip = os.path.join(self.directory, "chain.tip")
        self._lock = self._path + ".lock"

    def chain_intact(self) -> bool:
        try:
            with file_lock.acquire_lock(self._lock, blocking=True, timeout=5.0):
                return self._chain_intact()
        except Exception:
            return False

    def append(
        self,
        *,
        principal_id: str,
        project_id: str,
        tool: str,
        code: str,
        task_id: str = "",
        arguments: Mapping[str, Any] | None = None,
        diagnostic: str = "",
        required: bool = False,
    ) -> None:
        record: Dict[str, Any] = {
            "ts": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "principal_id": principal_id[:128],
            "project_id": project_id[:64],
            "tool": tool[:64],
            "task_id": task_id[:64],
            "code": code[:64],
            "request_sha256": _digest(arguments),
        }
        if diagnostic:
            record["diagnostic"] = diagnostic[:64]
        try:
            with file_lock.acquire_lock(self._lock, blocking=True, timeout=5.0):
                if not self._chain_intact():
                    if required:
                        raise AuditWriteError()
                    return
                previous, seq = self._tail()
                record["seq"] = seq + 1
                record["prev_sha256"] = previous
                line = fit_audit_line(record)
                os.makedirs(self.directory, exist_ok=True)
                with open(self._path, "a", encoding="utf-8") as handle:
                    handle.write(line + "\n")
                    handle.flush()
                    os.fsync(handle.fileno())
                tip = hashlib.sha256(line.encode("utf-8")).hexdigest()
                temporary = self._tip + ".tmp"
                with open(temporary, "w", encoding="utf-8") as handle:
                    handle.write(tip)
                    handle.flush()
                    os.fsync(handle.fileno())
                os.replace(temporary, self._tip)
        except AuditWriteError:
            raise
        except OSError:
            if required:
                raise AuditWriteError() from None
            return

    def _chain_intact(self) -> bool:
        if not os.path.isfile(self._path):
            return not os.path.isfile(self._tip)
        previous = ""
        expected = 1
        last_line = ""
        try:
            with open(self._path, "r", encoding="utf-8") as handle:
                for raw in handle:
                    line = raw.rstrip("\r\n")
                    if not line:
                        continue
                    try:
                        item = json.loads(line)
                    except json.JSONDecodeError:
                        return False
                    if not isinstance(item, dict):
                        return False
                    if item.get("seq") != expected or item.get("prev_sha256") != previous:
                        return False
                    previous = hashlib.sha256(line.encode("utf-8")).hexdigest()
                    last_line = line
                    expected += 1
        except (OSError, UnicodeError):
            return False
        if not last_line:
            return not os.path.isfile(self._tip)
        try:
            with open(self._tip, "r", encoding="utf-8") as handle:
                tip = handle.read().strip()
        except OSError:
            return False
        return tip == hashlib.sha256(last_line.encode("utf-8")).hexdigest()

    def _tail(self) -> tuple:
        if not os.path.isfile(self._path):
            return "", 0
        previous = ""
        seq = 0
        with open(self._path, "r", encoding="utf-8") as handle:
            for raw in handle:
                line = raw.rstrip("\r\n")
                if not line:
                    continue
                item = json.loads(line)
                seq = int(item.get("seq") or 0)
                previous = hashlib.sha256(line.encode("utf-8")).hexdigest()
        return previous, seq


def _digest(arguments: Mapping[str, Any] | None) -> str:
    if arguments is None:
        payload: Any = {}
    else:
        try:
            payload = mask_data(dict(arguments))
        except RedactionRejected:
            payload = {"rejected": True}
    try:
        raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)
    except (TypeError, ValueError):
        raw = "{}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()
