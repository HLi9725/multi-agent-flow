#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""MCP 进程入口。stdio 保持本地帧；http 只启动回环 compat 服务。"""

from __future__ import annotations

import argparse
import json
import os
import sys

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from scripts._lib.connectors.github_projects_sync import run_sync
from scripts._lib.mcp.audit_store import AuditStore
from scripts._lib.mcp.auth import AuthAdminError, current_os_user, ensure_grants, issue_token, revoke_jti
from scripts._lib.mcp.backup import BackupError, backup_tree, restore_tree
from scripts._lib.mcp.http_server import BindError, mcp_sdk_importable, serve_http
from scripts._lib.mcp.registry import RegistryError, load_registry
from scripts._lib.mcp.stdio_server import create_stdio_server, serve


def _stderr(text: str) -> None:
    sys.stderr.buffer.write(text.encode("utf-8"))
    sys.stderr.buffer.flush()


def _load(path: str):
    if not path.strip():
        _stderr("缺少登记文件\n")
        return None
    try:
        return load_registry(path)
    except RegistryError as exc:
        _stderr(str(exc) + "\n")
        return None


def _stdio(args: argparse.Namespace) -> int:
    if args.transport_backend != "compat":
        _stderr("官方 SDK 后端未启用\n")
        return 2
    registry = _load(args.registry or os.environ.get("YY_FLOW_MCP_REGISTRY", ""))
    if registry is None:
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


def _http(args: argparse.Namespace) -> int:
    if args.transport_backend == "sdk":
        if not mcp_sdk_importable():
            _stderr("官方 SDK 后端未启用\n")
            return 2
        _stderr("SDK 包可导入，但官方监听未核对认证边界，本进程仍拒绝单独启用 SDK\n")
        return 2
    if args.transport_backend != "compat":
        _stderr("官方 SDK 后端未启用\n")
        return 2
    registry = _load(args.registry or os.environ.get("YY_FLOW_MCP_REGISTRY", ""))
    if registry is None:
        return 2
    if not registry.token_key or not registry.token_audience:
        _stderr("缺少签名配置\n")
        return 2
    try:
        ensure_grants(registry)
    except AuthAdminError:
        _stderr("授权表无法读取\n")
        return 2
    timeout = _timeout_setting()
    context = {
        "registry": registry,
        "audit": AuditStore(os.path.join(os.path.dirname(registry.path), "mcp_audit")),
        "acceptance_confirmed": os.environ.get("YY_FLOW_MCP_ACCEPTANCE_CONFIRMED") == "1",
        "transport": "http",
        "tool_timeout_seconds": timeout,
    }
    hold = os.environ.get("YY_FLOW_MCP_TEST_HOLD_SECONDS", "").strip()
    if hold:
        context["test_hold_seconds"] = hold
    try:
        serve_http(args.host, args.port, context)
    except BindError:
        _stderr("拒绝绑定\n")
        return 2
    return 0


def _timeout_setting() -> float:
    raw = os.environ.get("YY_FLOW_MCP_TOOL_TIMEOUT_SECONDS", "").strip()
    if not raw:
        return 15.0
    try:
        value = float(raw)
    except ValueError:
        return 15.0
    if value <= 0:
        return 15.0
    return min(value, 15.0)


def _auth(args: argparse.Namespace) -> int:
    try:
        if args.auth_command == "restore":
            task_id = restore_tree(args.src, args.dest)
            sys.stdout.write(f"RESTORE_CHECK ok=1 task_id={task_id}\n")
            sys.stdout.flush()
            return 0
        registry = _load(args.registry)
        if registry is None:
            return 2
        if args.auth_command == "issue":
            issued = issue_token(registry, args.principal_id, list(args.scope), args.ttl_seconds)
            sys.stdout.write(json.dumps({"jti": issued["jti"], "token": issued["token"], "exp": issued["exp"]}, ensure_ascii=False) + "\n")
            sys.stdout.flush()
            return 0
        if args.auth_command == "revoke":
            revoke_jti(registry, args.jti, args.principal_id)
            sys.stdout.write("REVOKED\n")
            sys.stdout.flush()
            return 0
        if args.auth_command == "backup":
            backup_tree(registry, args.dest)
            sys.stdout.write("BACKUP_OK\n")
            sys.stdout.flush()
            return 0
    except (AuthAdminError, BackupError) as exc:
        _stderr(str(exc) + "\n")
        return 2
    _stderr("管理命令无效\n")
    return 2


def _looks_like_path(value: str) -> bool:
    if not isinstance(value, str) or not value.strip():
        return True
    if ".." in value:
        return True
    return any(mark in value for mark in ("/", "\\", ":"))


def _sync_github(args: argparse.Namespace) -> int:
    if _looks_like_path(args.project_id):
        _stderr("项目标识无效\n")
        return 2
    registry = _load(os.environ.get("YY_FLOW_MCP_REGISTRY", ""))
    if registry is None:
        return 2
    project = registry.get_project(args.project_id)
    if project is None:
        _stderr("项目未登记\n")
        return 2
    report = run_sync(project, mode=args.mode, registry=registry, environ=os.environ)
    sys.stdout.write(json.dumps(report.public_dict(), ensure_ascii=False, sort_keys=True) + "\n")
    sys.stdout.flush()
    if report.reason in {"WRITE_DISABLED", "CREDENTIALS_ABSENT", "VALIDATION"}:
        return 2
    if args.mode != "write" and report.network_writes:
        return 2
    return 0 if report.ok else 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="mcp_server.py", add_help=True)
    sub = parser.add_subparsers(dest="command", required=True)
    stdio = sub.add_parser("stdio")
    stdio.add_argument("--registry", default="")
    stdio.add_argument("--transport-backend", default="compat")
    http = sub.add_parser("http")
    http.add_argument("--registry", default="")
    http.add_argument("--transport-backend", default="compat")
    http.add_argument("--host", default="127.0.0.1")
    http.add_argument("--port", type=int, default=8765)
    auth = sub.add_parser("auth")
    auth_sub = auth.add_subparsers(dest="auth_command", required=True)
    issue = auth_sub.add_parser("issue")
    issue.add_argument("--registry", required=True)
    issue.add_argument("--principal-id", required=True)
    issue.add_argument("--scope", action="append", required=True)
    issue.add_argument("--ttl-seconds", type=int, default=3600)
    revoke = auth_sub.add_parser("revoke")
    revoke.add_argument("--registry", required=True)
    revoke.add_argument("--jti", required=True)
    revoke.add_argument("--principal-id", default="")
    backup = auth_sub.add_parser("backup")
    backup.add_argument("--registry", required=True)
    backup.add_argument("--dest", required=True)
    restore = auth_sub.add_parser("restore")
    restore.add_argument("--src", required=True)
    restore.add_argument("--dest", required=True)
    sync = sub.add_parser("sync-github")
    sync.add_argument("--project-id", required=True)
    sync.add_argument("--mode", choices=("read-only", "write"), default="read-only")
    args = parser.parse_args(sys.argv[1:] if argv is None else argv)
    if args.command == "stdio":
        return _stdio(args)
    if args.command == "http":
        return _http(args)
    if args.command == "auth":
        return _auth(args)
    if args.command == "sync-github":
        return _sync_github(args)
    _stderr("官方 SDK 后端未启用\n")
    return 2


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except BrokenPipeError:
        raise SystemExit(0)
