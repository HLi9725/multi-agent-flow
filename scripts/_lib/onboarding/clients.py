# -*- coding: utf-8 -*-
"""三客户端配置的外科合并。

Antigravity 项目级 MCP 查证来源（2026-10-15）：
https://antigravity.google/docs/mcp/
https://www.antigravity.google/docs/ide/mcp
官方工作区文件是 ``.agents/mcp_config.json``，服务器表字段是 ``mcpServers``
（stdio 使用 command/args/env）。全局文件 ``~/.gemini/config/mcp_config.json``
不在本模块写入范围内。
Cursor 项目文件是 ``.cursor/mcp.json``，同样使用 ``mcpServers``。
Codex 项目文件是 ``.codex/config.toml`` 的 ``[mcp_servers.<name>]`` 表。
受管服务名固定为 yy-flow。TOML 只替换受管注释区块，不整文件重写。
"""

from __future__ import annotations

import json
import tomllib
from typing import Mapping

from .common import (
    MANAGED_BEGIN,
    MANAGED_END,
    SERVER_NAME,
    OnboardError,
    limited_diff,
    sha256_bytes,
)


def mcp_argv(python: str, script: str, registry: str) -> list[str]:
    return [python, script, "stdio", "--registry", registry]


def mcp_entry(python: str, script: str, registry: str) -> dict:
    return {"command": python, "args": [script, "stdio", "--registry", registry]}


def canonical_entry(entry: Mapping[str, object]) -> str:
    return json.dumps(entry, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _toml_string(value: str) -> str:
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def managed_toml_block(python: str, script: str, registry: str) -> str:
    args = ", ".join(_toml_string(item) for item in mcp_argv(python, script, registry)[1:])
    return "\n".join(
        (
            MANAGED_BEGIN,
            f"[mcp_servers.{SERVER_NAME}]",
            f"command = {_toml_string(python)}",
            f"args = [{args}]",
            MANAGED_END,
            "",
        )
    )


def _loads_toml(text: str, label: str) -> None:
    try:
        tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        raise OnboardError("BAD_CONFIG", f"{label} 无法解析") from exc


def merge_toml(original: str | None, block: str, recorded_hash: str | None) -> str:
    """返回应落盘的全文。无变化时返回原文，避免改动注释和其他表。"""
    planned_hash = sha256_bytes(block.encode("utf-8"))
    if original is None or original.strip() == "":
        _loads_toml(block, "Codex 配置")
        return block
    _loads_toml(original, "Codex 配置")
    if MANAGED_BEGIN in original:
        start = original.find(MANAGED_BEGIN)
        end = original.find(MANAGED_END, start)
        if end < 0 or original.find(MANAGED_BEGIN, start + len(MANAGED_BEGIN)) >= 0:
            raise OnboardError("BAD_CONFIG", "Codex 受管区块标记不完整")
        end += len(MANAGED_END)
        if original[end:end + 1] == "\r":
            end += 1
        if original[end:end + 1] == "\n":
            end += 1
        current = original[start:end]
        if not current.endswith("\n"):
            current_for_hash = current + "\n"
        else:
            current_for_hash = current
        current_hash = sha256_bytes(current_for_hash.encode("utf-8"))
        if current_hash == planned_hash:
            return original
        if recorded_hash is None or current_hash != recorded_hash:
            raise OnboardError(
                "CONFLICT",
                "Codex 受管区块已被修改，拒绝覆盖",
                diff=limited_diff(current, block),
            )
        updated = original[:start] + block + original[end:]
        _loads_toml(updated, "Codex 配置")
        return updated
    if f"[mcp_servers.{SERVER_NAME}]" in original or f'[mcp_servers."{SERVER_NAME}"]' in original:
        raise OnboardError(
            "CONFLICT",
            "已存在同名且非受管的 Codex MCP 表，拒绝覆盖或改名",
            diff=limited_diff(original, original + "\n" + block),
        )
    separator = "" if original.endswith("\n") else "\n"
    updated = original + separator + "\n" + block
    _loads_toml(updated, "Codex 配置")
    return updated


def strip_managed_toml(original: str, recorded_hash: str | None) -> str:
    if original is None or MANAGED_BEGIN not in original:
        if original and (f"[mcp_servers.{SERVER_NAME}]" in original or f'[mcp_servers."{SERVER_NAME}"]' in original):
            raise OnboardError("CONFLICT", "Codex 同名条目不是受管区块，拒绝删除")
        return original or ""
    start = original.find(MANAGED_BEGIN)
    end = original.find(MANAGED_END, start)
    if end < 0:
        raise OnboardError("BAD_CONFIG", "Codex 受管区块标记不完整")
    end += len(MANAGED_END)
    if original[end:end + 1] == "\r":
        end += 1
    if original[end:end + 1] == "\n":
        end += 1
    current = original[start:end]
    current_hash = sha256_bytes((current if current.endswith("\n") else current + "\n").encode("utf-8"))
    if recorded_hash is None or current_hash != recorded_hash:
        raise OnboardError("CONFLICT", "Codex 受管区块已变化，拒绝自动删除", diff=limited_diff(current, ""))
    updated = original[:start] + original[end:]
    if updated.strip():
        _loads_toml(updated, "Codex 配置")
    return updated


def merge_mcp_json(original: str | None, entry: dict, recorded_hash: str | None, label: str) -> str:
    planned = canonical_entry(entry)
    planned_hash = sha256_bytes(planned.encode("utf-8"))
    if original is None or original.strip() == "":
        document = {"mcpServers": {SERVER_NAME: entry}}
        return json.dumps(document, ensure_ascii=False, indent=2) + "\n"
    try:
        data = json.loads(original)
    except json.JSONDecodeError as exc:
        raise OnboardError("BAD_CONFIG", f"{label} 无法解析") from exc
    if not isinstance(data, dict):
        raise OnboardError("BAD_CONFIG", f"{label} 必须是 JSON 对象")
    servers = data.get("mcpServers", {})
    if servers is None:
        servers = {}
    if not isinstance(servers, dict):
        raise OnboardError("BAD_CONFIG", f"{label} 的 mcpServers 必须是对象")
    current = servers.get(SERVER_NAME)
    if current is not None:
        if not isinstance(current, dict):
            raise OnboardError("CONFLICT", f"{label} 已有同名非对象条目，拒绝覆盖")
        current_hash = sha256_bytes(canonical_entry(current).encode("utf-8"))
        if current_hash == planned_hash:
            return original if original.endswith("\n") else original + "\n"
        if recorded_hash is None or current_hash != recorded_hash:
            raise OnboardError(
                "CONFLICT",
                f"{label} 已有同名或已修改的 {SERVER_NAME} 条目，拒绝覆盖",
                diff=limited_diff(canonical_entry(current), planned),
            )
    servers = dict(servers)
    servers[SERVER_NAME] = entry
    data = dict(data)
    data["mcpServers"] = servers
    return json.dumps(data, ensure_ascii=False, indent=2) + "\n"


def strip_mcp_json(original: str, recorded_hash: str | None, label: str) -> str:
    try:
        data = json.loads(original)
    except json.JSONDecodeError as exc:
        raise OnboardError("BAD_CONFIG", f"{label} 无法解析") from exc
    if not isinstance(data, dict):
        raise OnboardError("BAD_CONFIG", f"{label} 必须是 JSON 对象")
    servers = data.get("mcpServers", {})
    if not isinstance(servers, dict) or SERVER_NAME not in servers:
        return original if original.endswith("\n") else original + "\n"
    current = servers.get(SERVER_NAME)
    if not isinstance(current, dict):
        raise OnboardError("CONFLICT", f"{label} 的同名条目不是对象，拒绝删除")
    current_hash = sha256_bytes(canonical_entry(current).encode("utf-8"))
    if recorded_hash is None or current_hash != recorded_hash:
        raise OnboardError("CONFLICT", f"{label} 的 {SERVER_NAME} 条目已变化，拒绝自动删除")
    servers = dict(servers)
    servers.pop(SERVER_NAME, None)
    data = dict(data)
    data["mcpServers"] = servers
    return json.dumps(data, ensure_ascii=False, indent=2) + "\n"
