# -*- coding: utf-8 -*-
"""稳定错误码。对外 message 使用固定中文短句，不拼接异常、路径或令牌。"""

from __future__ import annotations

from typing import Any, Dict, Mapping


class McpError(Exception):
    def __init__(
        self,
        code: str,
        message: str,
        *,
        retryable: bool = False,
        details: Mapping[str, Any] | None = None,
        is_error: bool = True,
        replayed: bool = False,
        rpc_code: int | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.retryable = retryable
        self.details = dict(details or {})
        self.is_error = is_error
        self.replayed = replayed
        self.rpc_code = rpc_code if rpc_code is not None else RPC_CODES[code]

    def body(self) -> Dict[str, Any]:
        return {
            "code": self.code,
            "message": self.message,
            "retryable": self.retryable,
            "replayed": self.replayed,
            "details": self.details,
        }


RPC_CODES: Dict[str, int] = {
    "UNKNOWN_PROJECT": -32001,
    "FORBIDDEN": -32002,
    "PATH_REJECTED": -32003,
    "VALIDATION": -32004,
    "CONFLICT": -32005,
    "IDEMPOTENCY_REPLAY": -32006,
    "NOT_FOUND": -32007,
    "RATE_LIMITED": -32008,
    "UNAUTHENTICATED": -32009,
    "REVOKED": -32010,
    "GATE_REJECTED": -32011,
    "ACCEPTANCE_REQUIRED": -32012,
}

MESSAGES: Dict[str, str] = {
    "UNKNOWN_PROJECT": "项目未登记",
    "FORBIDDEN": "无权访问该项目",
    "PATH_REJECTED": "拒绝路径参数",
    "VALIDATION": "参数校验失败",
    "CONFLICT": "请求冲突",
    "IDEMPOTENCY_REPLAY": "幂等重放",
    "NOT_FOUND": "记录不存在",
    "RATE_LIMITED": "请求过于频繁",
    "UNAUTHENTICATED": "主体无法确认",
    "REVOKED": "凭证已撤销",
    "GATE_REJECTED": "证据未通过服务端门禁",
    "ACCEPTANCE_REQUIRED": "验收需要独立授权",
}


def error(
    code: str,
    *,
    details: Mapping[str, Any] | None = None,
    retryable: bool | None = None,
    message: str | None = None,
) -> McpError:
    return McpError(
        code,
        message or MESSAGES[code],
        retryable=RETRYABLE[code] if retryable is None else retryable,
        details=details,
        is_error=code != "IDEMPOTENCY_REPLAY",
    )


RETRYABLE: Dict[str, bool] = {code: code == "RATE_LIMITED" for code in RPC_CODES}
