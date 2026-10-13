# -*- coding: utf-8 -*-
"""追加型审计。与看板、Evidence 分文件。4A 记录拒绝与读取。"""

from __future__ import annotations

import hashlib
import json
import os
from datetime import datetime, timezone
from typing import Any, Dict, Mapping

from .redaction import RedactionRejected, fit_audit_line, mask_data


class AuditStore:
    def __init__(self, directory: str) -> None:
        self.directory = os.path.realpath(directory)
        self._path = os.path.join(self.directory, "events.jsonl")

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
        line = fit_audit_line(record)
        try:
            os.makedirs(self.directory, exist_ok=True)
            with open(self._path, "a", encoding="utf-8") as handle:
                handle.write(line + "\n")
                handle.flush()
                os.fsync(handle.fileno())
        except OSError:
            return


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
