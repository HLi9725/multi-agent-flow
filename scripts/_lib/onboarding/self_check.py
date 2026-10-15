# -*- coding: utf-8 -*-
"""有界 STDIO 协议自检。结果不是真实 Cursor、Codex 或 Antigravity 连接。"""

from __future__ import annotations

import json
import os
import subprocess
import threading
import time
from typing import Mapping

from scripts._lib.mcp import MCP_PROTOCOL_VERSION
from scripts._lib.mcp.protocol import encode_message, read_message

from .common import CREDENTIAL_ENV, OnboardError

_TIMEOUT_SECONDS = 20


def run_self_check(python: str, script: str, registry: str, project_id: str, data_root: str) -> dict:
    deadline = time.monotonic() + _TIMEOUT_SECONDS
    env = os.environ.copy()
    for key in CREDENTIAL_ENV:
        env.pop(key, None)
    env.pop("YY_FLOW_MCP_REGISTRY", None)
    env["YY_FLOW_PROJECT_ROOT"] = data_root
    command = [python, script, "stdio", "--registry", registry]
    try:
        proc = subprocess.Popen(
            command,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=env,
            cwd=data_root if os.path.isdir(data_root) else None,
        )
    except OSError as exc:
        raise OnboardError("SELF_CHECK", "协议自检进程无法启动", exit_code=4) from exc
    try:
        assert proc.stdin is not None and proc.stdout is not None
        started = _request(
            proc,
            1,
            "initialize",
            {
                "protocolVersion": MCP_PROTOCOL_VERSION,
                "capabilities": {},
                "clientInfo": {"name": "yy-flow-protocol-self-check", "version": "0"},
            },
            deadline,
        )
        if "error" in started:
            raise OnboardError("SELF_CHECK", "协议自检 initialize 失败", exit_code=4)
        _notify(proc, "notifications/initialized")
        listed = _request(proc, 2, "tools/list", {}, deadline)
        tools = [item.get("name") for item in (listed.get("result") or {}).get("tools", []) if isinstance(item, dict)]
        project = _call(proc, 3, "get_project", {"project_id": project_id}, deadline)
        tasks = _call(proc, 4, "list_tasks", {"project_id": project_id}, deadline)
        unknown = _call(proc, 5, "get_project", {"project_id": "missing-project"}, deadline)
        denied = _call(
            proc,
            6,
            "create_task",
            {
                "project_id": project_id,
                "idempotency_key": "selfcheck1",
                "name": "probe",
                "type": "G",
                "assignee": "dev",
            },
            deadline,
        )
        if project.get("isError") or project.get("structuredContent", {}).get("project_id") != project_id:
            raise OnboardError("SELF_CHECK", "协议自检未能读取项目", exit_code=4)
        if tasks.get("isError") or not isinstance(tasks.get("structuredContent", {}).get("items"), list):
            raise OnboardError("SELF_CHECK", "协议自检未能读取任务列表", exit_code=4)
        if unknown.get("structuredContent", {}).get("code") != "UNKNOWN_PROJECT":
            raise OnboardError("SELF_CHECK", "协议自检未拒绝未知项目", exit_code=4)
        if not denied.get("isError") or denied.get("structuredContent", {}).get("code") != "FORBIDDEN":
            raise OnboardError("SELF_CHECK", "协议自检未拒绝写入工具", exit_code=4)
        write_names = {"create_task", "transition_task", "submit_evidence", "request_acceptance", "soft_delete", "restore"}
        if write_names.intersection(tools):
            raise OnboardError("SELF_CHECK", "只读主体仍列出写入工具", exit_code=4)
        return {
            "verification": "protocol_self_check",
            "client_connection_verified": False,
            "ac14_live_client_verified": False,
            "tools": tools,
            "note": "这是本地 STDIO 协议自检，不是真实客户端连接，也不能作为 AC-14 现场证据",
        }
    finally:
        if proc.stdin is not None:
            try:
                proc.stdin.close()
            except OSError:
                pass
        try:
            proc.wait(timeout=3)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=3)


def _notify(proc: subprocess.Popen, method: str) -> None:
    assert proc.stdin is not None
    proc.stdin.write(encode_message({"jsonrpc": "2.0", "method": method}))
    proc.stdin.flush()


def _request(proc: subprocess.Popen, req_id: int, method: str, params: Mapping[str, object] | None, deadline: float) -> dict:
    payload = {"jsonrpc": "2.0", "id": req_id, "method": method}
    if params is not None:
        payload["params"] = dict(params)
    assert proc.stdin is not None
    proc.stdin.write(encode_message(payload))
    proc.stdin.flush()
    message = _read(proc, deadline)
    if message is None:
        raise OnboardError("SELF_CHECK", "协议自检没有返回", exit_code=4)
    if message.get("id") != req_id:
        raise OnboardError("SELF_CHECK", "协议自检响应编号不匹配", exit_code=4)
    return message


def _call(proc: subprocess.Popen, req_id: int, name: str, arguments: dict, deadline: float) -> dict:
    message = _request(proc, req_id, "tools/call", {"name": name, "arguments": arguments}, deadline)
    if "error" in message and "result" not in message:
        raise OnboardError("SELF_CHECK", "协议自检工具调用失败", exit_code=4)
    result = message.get("result")
    if not isinstance(result, dict):
        raise OnboardError("SELF_CHECK", "协议自检响应不完整", exit_code=4)
    return result


def _read(proc: subprocess.Popen, deadline: float) -> dict | None:
    holder: dict = {}

    def run() -> None:
        try:
            holder["msg"] = read_message(proc.stdout)
        except Exception as exc:  # noqa: BLE001 - 自检需要失败原因类别，不回传正文
            holder["err"] = type(exc).__name__

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        proc.kill()
        raise OnboardError("SELF_CHECK", "协议自检超时", exit_code=4)
    thread.join(remaining)
    if thread.is_alive():
        proc.kill()
        raise OnboardError("SELF_CHECK", "协议自检超时", exit_code=4)
    if "err" in holder:
        raise OnboardError("SELF_CHECK", "协议自检帧无法解析", exit_code=4)
    message = holder.get("msg")
    if message is not None and not isinstance(message, dict):
        raise OnboardError("SELF_CHECK", "协议自检响应不完整", exit_code=4)
    return message


def scrub_text(text: str) -> str:
    """避免把环境中的凭据带进错误输出。"""
    scrubbed = text
    for key in CREDENTIAL_ENV:
        value = os.environ.get(key, "")
        if value:
            scrubbed = scrubbed.replace(value, "***")
    return scrubbed


def loads_result(raw: str) -> dict:
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return {}
    return data if isinstance(data, dict) else {}
