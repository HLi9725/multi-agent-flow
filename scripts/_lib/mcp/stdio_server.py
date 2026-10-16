# -*- coding: utf-8 -*-
"""本地 STDIO 循环。无网络监听。官方 SDK 未在本批次创建。"""

from __future__ import annotations

import json
import threading
from dataclasses import dataclass
from datetime import date
from typing import Any, BinaryIO, Dict, Mapping

from scripts.enums import TaskStatus

from .auth import resolve_request_principal
from .errors import McpError, error
from .protocol import (
    FRAME_CONTENT_LENGTH,
    FRAME_NEWLINE,
    FrameError,
    MCP_PROTOCOL_VERSION,
    encode_framed,
    read_framed,
)
from .schema import SCHEMA_VERSION, tool_specs
from .service import handle_tool

_SERVER_NAME = "yy-flow"


@dataclass
class RpcSession:
    announced: bool = False
    ready: bool = False
    failed: bool = False


def create_stdio_server() -> Dict[str, Any]:
    """兼容层工厂。默认后端不导入官方 mcp 包。"""
    return {"backend": "compat", "protocol": MCP_PROTOCOL_VERSION}


def serve(stdin: BinaryIO, stdout: BinaryIO, context: Mapping[str, Any]) -> None:
    session = RpcSession()
    while True:
        try:
            framed = read_framed(stdin)
        except FrameError:
            continue
        if framed is None:
            return
        message, frame = framed
        for response in dispatch_rpc(message, session, context):
            if not _write(stdout, response, frame):
                return


def dispatch_rpc(message: Mapping[str, Any], session: RpcSession, context: Mapping[str, Any]) -> list:
    if message.get("jsonrpc") != "2.0" or not isinstance(message.get("method"), str):
        return [_rpc_error(_request_id(message), error("VALIDATION", details={"reason": "FRAME"}))]
    method = message["method"]
    if "id" not in message:
        if method == "notifications/initialized" and session.announced and not session.failed:
            session.ready = True
        return []
    request_id = _request_id(message)
    if request_id is _BAD_ID:
        return [_rpc_error(None, error("VALIDATION", details={"reason": "FRAME"}))]
    try:
        if method == "initialize":
            return [_initialize(message, session, request_id)]
        if method == "ping":
            return [_rpc_ok(request_id, {})]
        if method == "tools/list":
            _require_ready(session)
            return [_rpc_ok(request_id, {"tools": _visible_tools(context)})]
        if method == "tools/call":
            _require_ready(session)
            return [_rpc_ok(request_id, _call_tool(message, context))]
    except McpError as exc:
        return [_rpc_error(request_id, exc)]
    return [_rpc_error(request_id, error("VALIDATION", details={"reason": "UNKNOWN_METHOD", "field": "method"}))]


def _initialize(message: Mapping[str, Any], session: RpcSession, request_id: Any) -> Dict[str, Any]:
    if session.announced or session.failed:
        session.failed = True
        raise error("VALIDATION", details={"reason": "SESSION"})
    params = message.get("params", {})
    if not isinstance(params, dict):
        session.failed = True
        raise error("VALIDATION", details={"reason": "PROTOCOL_VERSION"})
    version = params.get("protocolVersion")
    try:
        valid_version = isinstance(version, str) and date.fromisoformat(version).isoformat() == version
    except ValueError:
        valid_version = False
    if not valid_version:
        session.failed = True
        raise error("VALIDATION", details={"reason": "PROTOCOL_VERSION"})
    # MCP lifecycle: an unsupported offer must receive a version this server
    # implements. The client decides whether it can continue with that version.
    # Do not echo an offered version whose semantics we have not implemented.
    session.announced = True
    return _rpc_ok(
        request_id,
        {
            "protocolVersion": MCP_PROTOCOL_VERSION,
            "capabilities": {"tools": {}},
            "serverInfo": {"name": _SERVER_NAME, "version": SCHEMA_VERSION},
        },
    )


def _visible_tools(context: Mapping[str, Any]) -> list:
    principal = resolve_request_principal(context["registry"], context)
    if principal is None:
        raise error("UNAUTHENTICATED")
    if principal.disabled:
        raise error("REVOKED")
    scopes = set()
    for granted in principal.grants.values():
        scopes.update(granted)
    return tool_specs(TaskStatus.all_values(), scopes)


def _require_ready(session: RpcSession) -> None:
    if session.failed or not session.ready:
        raise error("VALIDATION", details={"reason": "NOT_INITIALIZED"})


def _call_tool(message: Mapping[str, Any], context: Mapping[str, Any]) -> Dict[str, Any]:
    params = message.get("params", {})
    if not isinstance(params, dict):
        raise error("VALIDATION", details={"reason": "BAD_TYPE", "field": "params"})
    name = params.get("name")
    arguments = params.get("arguments", {})
    if not isinstance(name, str):
        raise error("VALIDATION", details={"reason": "BAD_TYPE", "field": "name"})
    if arguments is None:
        arguments = {}
    timeout = _timeout_seconds(context)
    if timeout is None:
        outcome = _invoke_tool(name, arguments, context)
    else:
        outcome = _invoke_tool_with_timeout(name, arguments, context, timeout)
    structured = outcome.get("structured")
    if not isinstance(structured, dict):
        raise error("VALIDATION", details={"reason": "INTERNAL"})
    text = json.dumps(structured, ensure_ascii=False, separators=(",", ":"))
    return {
        "content": [{"type": "text", "text": text}],
        "structuredContent": structured,
        "isError": bool(outcome.get("isError")),
    }


def _timeout_seconds(context: Mapping[str, Any]) -> float | None:
    raw = context.get("tool_timeout_seconds")
    if raw is None:
        return None
    try:
        seconds = float(raw)
    except (TypeError, ValueError):
        return None
    if seconds <= 0:
        return None
    return min(seconds, 15.0)


def _invoke_tool(name: str, arguments: Any, context: Mapping[str, Any]) -> Dict[str, Any]:
    try:
        outcome = handle_tool(name, arguments, context)
    except McpError:
        raise
    except Exception:
        raise error("VALIDATION", details={"reason": "INTERNAL"}) from None
    if not isinstance(outcome, dict):
        raise error("VALIDATION", details={"reason": "INTERNAL"})
    return outcome


def _invoke_tool_with_timeout(name: str, arguments: Any, context: Mapping[str, Any], timeout: float) -> Dict[str, Any]:
    holder: Dict[str, Any] = {}

    def runner() -> None:
        try:
            holder["outcome"] = _invoke_tool(name, arguments, context)
        except McpError as exc:
            holder["error"] = exc
        except Exception:
            holder["error"] = error("VALIDATION", details={"reason": "INTERNAL"})

    worker = threading.Thread(target=runner, daemon=True)
    worker.start()
    worker.join(timeout)
    if worker.is_alive():
        raise error("CONFLICT", details={"reason": "TIMEOUT"}, retryable=True)
    if "error" in holder:
        raise holder["error"]
    outcome = holder.get("outcome")
    if not isinstance(outcome, dict):
        raise error("VALIDATION", details={"reason": "INTERNAL"})
    return outcome


_BAD_ID = object()


def _request_id(message: Mapping[str, Any]) -> Any:
    if "id" not in message:
        return None
    request_id = message.get("id")
    if isinstance(request_id, bool) or not isinstance(request_id, (str, int)):
        return _BAD_ID
    return request_id


def _rpc_ok(request_id: Any, result: Mapping[str, Any]) -> Dict[str, Any]:
    return {"jsonrpc": "2.0", "id": request_id, "result": result}


def _rpc_error(request_id: Any, exc: McpError) -> Dict[str, Any]:
    return {
        "jsonrpc": "2.0",
        "id": request_id,
        "error": {"code": exc.rpc_code, "message": exc.message, "data": exc.body()},
    }


def _write(stdout: BinaryIO, payload: Mapping[str, Any], frame: str = FRAME_CONTENT_LENGTH) -> bool:
    try:
        stdout.write(encode_framed(payload, frame))
        stdout.flush()
    except (BrokenPipeError, OSError, FrameError):
        return False
    return True
