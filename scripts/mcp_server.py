#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""MCP 进程入口。4A 只启动 STDIO 只读；HTTP 监听拒绝启动。"""

from __future__ import annotations

import argparse
import os
import sys

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from scripts._lib.mcp.audit_store import AuditStore
from scripts._lib.mcp.auth import current_os_user
from scripts._lib.mcp.registry import RegistryError, load_registry
from scripts._lib.mcp.stdio_server import create_stdio_server, serve


def _stderr(text: str) -> None:
    sys.stderr.buffer.write(text.encode("utf-8"))
    sys.stderr.buffer.flush()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="mcp_server.py", add_help=True)
    sub = parser.add_subparsers(dest="command", required=True)
    stdio = sub.add_parser("stdio")
    stdio.add_argument("--registry", default="")
    stdio.add_argument("--transport-backend", default="compat")
    http = sub.add_parser("http")
    http.add_argument("--registry", default="")
    http.add_argument("--transport-backend", default="compat")
    args = parser.parse_args(sys.argv[1:] if argv is None else argv)
    if args.command == "http":
        _stderr("HTTP 监听未在本批次启用\n")
        return 2
    if args.transport_backend != "compat":
        _stderr("官方 SDK 后端未启用\n")
        return 2
    registry_path = args.registry or os.environ.get("YY_FLOW_MCP_REGISTRY", "")
    if not registry_path.strip():
        _stderr("缺少登记文件\n")
        return 2
    try:
        registry = load_registry(registry_path)
    except RegistryError as exc:
        _stderr(str(exc) + "\n")
        return 2
    create_stdio_server()
    audit_dir = os.path.join(os.path.dirname(registry.path), "mcp_audit")
    context = {
        "registry": registry,
        "audit": AuditStore(audit_dir),
        "os_user": current_os_user(),
        "acceptance_confirmed": os.environ.get("YY_FLOW_MCP_ACCEPTANCE_CONFIRMED") == "1",
    }
    serve(sys.stdin.buffer, sys.stdout.buffer, context)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except BrokenPipeError:
        raise SystemExit(0)
