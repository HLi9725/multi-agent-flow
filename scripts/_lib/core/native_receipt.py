# -*- coding: utf-8 -*-
"""原生子代理回执。当前客户端没有可独立核验的完成证明。"""

from __future__ import annotations

import subprocess
from typing import Any, Mapping, Sequence


class NativeReceiptRejected(Exception):
    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


def verify_cursor_task_receipt(claimed: Mapping[str, Any] | None) -> None:
    """拒绝把模型可见的子代理编号当成宿主证明。

    Cursor 的 Task 结果把代理编号交回同一个模型。本进程没有签名回执、
    也没有只能由客户端宿主读取的完成记录可供对照。
    """
    del claimed
    raise NativeReceiptRejected("RECEIPT_UNVERIFIED")


def execute_controlled(commands: Sequence[str], cwd: str) -> tuple[dict[str, Any], ...]:
    """由本进程执行受控命令并记录退出码，不采用调用方自称的结果。"""
    results = []
    for command in commands:
        if not isinstance(command, str) or not command.strip():
            raise NativeReceiptRejected("FAILED_TEST")
        completed = subprocess.run(
            command,
            cwd=cwd,
            shell=True,
            capture_output=True,
            check=False,
        )
        results.append({"command": command, "exit_code": int(completed.returncode)})
    return tuple(results)


def binding_problem(
    claimed: Mapping[str, Any],
    *,
    expected_role: str,
    expected_task_id: str,
    expected_candidate: str,
    expected_contract_hash: str,
    expected_criterion_ids: Sequence[str],
    executor_results: Sequence[Mapping[str, Any]],
    seen_invocations: set[str],
) -> str | None:
    """只描述绑定差异。不授予宿主真实性，也不写证据。"""
    if str(claimed.get("role") or "") != expected_role:
        return "ROLE_MISMATCH"
    if str(claimed.get("task_id") or "") != expected_task_id:
        return "TASK"
    if str(claimed.get("candidate") or "") != expected_candidate:
        return "CANDIDATE"
    if str(claimed.get("contract_hash") or "") != expected_contract_hash:
        return "CONTRACT"
    covered = [str(item) for item in claimed.get("criterion_ids") or []]
    if covered != list(expected_criterion_ids):
        return "CRITERIA"
    invocation = str(claimed.get("invocation_id") or "")
    if not invocation or invocation in seen_invocations:
        return "REPLAY"
    if any(int(item.get("exit_code", 1)) != 0 for item in executor_results):
        return "FAILED_TEST"
    return None


def bind_native_handover(claimed: Mapping[str, Any] | None, **_unused: Any) -> None:
    """在回执通过之前不读取模型给出的角色、编号或退出码。"""
    verify_cursor_task_receipt(claimed)
