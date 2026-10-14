# -*- coding: utf-8 -*-
"""Cursor 原生子代理交接尚未实现。

Task 返回的编号会回到同一个模型，本进程没有可独立核验的完成回执。
这里只拒绝这类自述，不执行命令，也不把编号写成真实宿主。
"""

from __future__ import annotations

from typing import Any, Mapping


class NativeReceiptRejected(Exception):
    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


def verify_cursor_task_receipt(claimed: Mapping[str, Any] | None) -> None:
    """原生交接未实现。模型可见的编号、角色和完成自述都不是宿主证明。"""
    del claimed
    raise NativeReceiptRejected("RECEIPT_UNVERIFIED")


def bind_native_handover(claimed: Mapping[str, Any] | None, **_unused: Any) -> None:
    """不读取声称的角色、会话、调用或测试结果。"""
    verify_cursor_task_receipt(claimed)
