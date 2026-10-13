# -*- coding: utf-8 -*-
"""4C Streamable HTTP、Bearer 与副本恢复。使用临时目录，不读取仓库看板。"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import time
from http.client import HTTPConnection
from pathlib import Path

import yaml

from scripts._lib.mcp.auth import sign_token
from scripts._lib.mcp.protocol import MCP_PROTOCOL_HEADER, MCP_PROTOCOL_VERSION, MCP_SESSION_HEADER

REPO = Path(__file__).resolve().parents[1]
SERVER = REPO / "scripts" / "mcp_server.py"
SECRET = "SIGNINGKEY9f3aUNIT"
AUDIENCE = "yy-flow-test"
OUTSIDE = "C:\\outside\\not-a-repo\\secret-path"


def _config(data_root: Path, board: Path) -> Path:
    path = data_root / "user_data" / "workflow.config.yaml"
    path.parent.mkdir(parents=True, exist_ok=True)
    document = {
        "project": {"name": "sample", "version": "1", "root_dir": "."},
        "board": {
            "provider": "local",
            "board_file": str(board.resolve()),
            "fields": {
                "task_id": "id",
                "task_name": "name",
                "status": "status",
                "assignee": "assignee",
                "owner": "owner",
                "remarks": "remarks",
                "end_time": "end_date",
                "handler": "handler",
            },
        },
        "roles": {"DEV": {"name": "dev", "max_parallel_tasks": 3, "can_self_claim": True}},
        "paths": {
            "docs_root": "docs",
            "dev_reports_dir": "docs",
            "review_reports_dir": "docs",
            "qa_reports_dir": "docs",
        },
        "duplicate_check": {"enabled": False},
    }
    path.write_text(yaml.safe_dump(document, allow_unicode=True), encoding="utf-8")
    return path


def _project(root: Path, project_id: str, org_id: str, with_config: bool) -> dict:
    data_root = root / project_id
    board = data_root / "user_data" / "board.json"
    evidence = data_root / "evidence"
    board.parent.mkdir(parents=True, exist_ok=True)
    evidence.mkdir(parents=True, exist_ok=True)
    board.write_text("[]", encoding="utf-8")
    item = {
        "project_id": project_id,
        "display_name": project_id,
        "org_id": org_id,
        "data_root": str(data_root.resolve()),
        "board_file": str(board.resolve()),
        "evidence_root": str(evidence.resolve()),
        "board_kind": "offline",
    }
    if with_config:
        item["board_config"] = str(_config(data_root, board).resolve())
    return item


def _registry(tmp: Path, grants: dict | None = None, disabled: bool = False) -> Path:
    live = tmp / "live"
    live.mkdir()
    path = live / "mcp_registry.json"
    document = {
        "server_revision": "rev-4c-test",
        "token_secret": SECRET,
        "token_audience": AUDIENCE,
        "projects": [
            _project(live, "alpha", "org-1", True),
            _project(live, "beta", "org-1", False),
            _project(live, "gamma", "org-2", False),
        ],
        "local_principals": [
            {
                "os_user": "http-test-user",
                "principal_id": "principal-dev",
                "org_id": "org-1",
                "role": "DEV",
                "disabled": disabled,
                "grants": grants or {"alpha": ["read", "task.transition"]},
            }
        ],
    }
    path.write_text(json.dumps(document, ensure_ascii=False), encoding="utf-8")
    return path


def _run(args: list[str], check: bool = True) -> subprocess.CompletedProcess:
    proc = subprocess.run(
        [sys.executable, str(SERVER), *args],
        cwd=str(REPO),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    if check and proc.returncode != 0:
        raise AssertionError(proc.stderr)
    return proc


def _issue(registry: Path, scopes: list[str]) -> dict:
    args = ["auth", "issue", "--registry", str(registry), "--principal-id", "principal-dev", "--ttl-seconds", "600"]
    for scope in scopes:
        args.extend(["--scope", scope])
    proc = _run(args)
    assert SECRET not in proc.stdout
    return json.loads(proc.stdout)


def _token(payload: dict, key: bytes | None = None) -> str:
    return sign_token(key or SECRET.encode("utf-8"), payload)


def _claims(**extra: object) -> dict:
    now = int(time.time())
    body = {
        "sub": "principal-dev",
        "org": "org-1",
        "aud": AUDIENCE,
        "iat": now,
        "exp": now + 600,
        "jti": "jti-" + str(now) + str(extra.get("tag") or "base"),
        "scope": "read",
    }
    extra.pop("tag", None)
    body.update(extra)
    return body


class HttpServer:
    def __init__(self, registry: Path, env: dict | None = None) -> None:
        self.registry = registry
        merged = os.environ.copy()
        merged["YY_FLOW_PROJECT_ROOT"] = str(registry.parent)
        if env:
            merged.update(env)
        self.proc = subprocess.Popen(
            [
                sys.executable,
                str(SERVER),
                "http",
                "--registry",
                str(registry),
                "--host",
                "127.0.0.1",
                "--port",
                "0",
            ],
            cwd=str(REPO),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=merged,
        )
        self.port = self._port()

    def _port(self) -> int:
        box: dict = {}

        def read() -> None:
            assert self.proc.stderr is not None
            chunks = []
            while True:
                line = self.proc.stderr.readline()
                if not line:
                    box["err"] = b"".join(chunks).decode("utf-8", errors="replace")
                    return
                chunks.append(line)
                if line.startswith(b"MCP_HTTP_LISTENING"):
                    box["port"] = int(line.split()[-1])
                    return

        thread = threading.Thread(target=read, daemon=True)
        thread.start()
        thread.join(8)
        if "port" not in box:
            self.proc.kill()
            raise AssertionError(box.get("err") or "监听未启动")
        return int(box["port"])

    def close(self) -> None:
        if self.proc.poll() is None:
            self.proc.kill()
            self.proc.wait(timeout=5)

    def request(
        self,
        payload: dict | None,
        *,
        token: str = "",
        session: str = "",
        protocol: bool = True,
        accept: str | None = "application/json, text/event-stream",
        method: str = "POST",
        content_type: str = "application/json",
    ) -> tuple[int, dict, bytes]:
        headers = {}
        if accept is not None:
            headers["Accept"] = accept
        if token:
            headers["Authorization"] = "Bearer " + token
        if session:
            headers[MCP_SESSION_HEADER] = session
        if protocol:
            headers[MCP_PROTOCOL_HEADER] = MCP_PROTOCOL_VERSION
        if method == "POST":
            headers["Content-Type"] = content_type
            raw = json.dumps(payload or {}, ensure_ascii=False).encode("utf-8")
            headers["Content-Length"] = str(len(raw))
        else:
            raw = None
        conn = HTTPConnection("127.0.0.1", self.port, timeout=5)
        conn.request(method, "/mcp", body=raw, headers=headers)
        response = conn.getresponse()
        body = response.read()
        header_map = {name: value for name, value in response.getheaders()}
        conn.close()
        return response.status, header_map, body

    def handshake(self, token: str) -> str:
        status, headers, body = self.request(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": MCP_PROTOCOL_VERSION,
                    "capabilities": {},
                    "clientInfo": {"name": "http-test", "version": "0"},
                },
            },
            token=token,
            protocol=False,
        )
        assert status == 200, body
        assert SECRET not in body.decode("utf-8", errors="replace")
        assert token not in body.decode("utf-8", errors="replace")
        session = headers.get(MCP_SESSION_HEADER) or headers.get(MCP_SESSION_HEADER.lower())
        assert session
        notified, _, _ = self.request(
            {"jsonrpc": "2.0", "method": "notifications/initialized"},
            token=token,
            session=session,
        )
        assert notified == 202
        return session

    def call(self, token: str, session: str, name: str, arguments: dict, request_id: int = 2) -> tuple[int, dict]:
        status, _, body = self.request(
            {
                "jsonrpc": "2.0",
                "id": request_id,
                "method": "tools/call",
                "params": {"name": name, "arguments": arguments},
            },
            token=token,
            session=session,
        )
        text = body.decode("utf-8", errors="replace")
        assert SECRET not in text
        assert token not in text
        return status, json.loads(text)


def _error_code(payload: dict) -> str:
    if "error" in payload:
        return str(payload["error"]["data"]["code"])
    return str(payload["result"]["structuredContent"]["code"])


def _clean(text: str) -> None:
    assert SECRET not in text
    assert OUTSIDE not in text


def test_sdk_backend_is_disabled_without_import() -> None:
    try:
        import mcp  # noqa: F401
    except ImportError:
        imported = False
    else:
        imported = True
    proc = _run(["http", "--transport-backend", "sdk"], check=False)
    if imported:
        return
    assert proc.returncode == 2
    assert "未启用" in proc.stderr
    assert SECRET not in proc.stderr


def test_rejects_non_loopback_bind(tmp_path: Path) -> None:
    registry = _registry(tmp_path)
    for host in ("0.0.0.0", "", "8.8.8.8"):
        proc = _run(
            ["http", "--registry", str(registry), "--host", host, "--port", "9"],
            check=False,
        )
        assert proc.returncode == 2
        assert "拒绝绑定" in proc.stderr
        assert SECRET not in proc.stderr


def test_unauthenticated_expired_audience_and_bad_signature(tmp_path: Path) -> None:
    registry = _registry(tmp_path)
    server = HttpServer(registry)
    try:
        status, _, body = server.request(
            {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": MCP_PROTOCOL_VERSION, "capabilities": {}, "clientInfo": {"name": "x", "version": "0"}}},
            protocol=False,
        )
        text = body.decode("utf-8", errors="replace")
        _clean(text)
        assert status == 401
        assert json.loads(text)["error"]["data"]["code"] == "UNAUTHENTICATED"
        assert json.loads(text)["error"]["data"]["details"] == {}

        expired = _token(_claims(exp=int(time.time()) - 120, jti="expired-jti"))
        status, _, body = server.request(
            {"jsonrpc": "2.0", "id": 2, "method": "initialize", "params": {"protocolVersion": MCP_PROTOCOL_VERSION, "capabilities": {}, "clientInfo": {"name": "x", "version": "0"}}},
            token=expired,
            protocol=False,
        )
        text = body.decode("utf-8", errors="replace")
        _clean(text)
        assert expired not in text
        assert status == 401
        assert json.loads(text)["error"]["data"]["code"] == "UNAUTHENTICATED"

        mismatched = _token(_claims(aud="other-aud", jti="aud-jti"))
        status, _, body = server.request(
            {"jsonrpc": "2.0", "id": 3, "method": "initialize", "params": {"protocolVersion": MCP_PROTOCOL_VERSION, "capabilities": {}, "clientInfo": {"name": "x", "version": "0"}}},
            token=mismatched,
            protocol=False,
        )
        text = body.decode("utf-8", errors="replace")
        assert mismatched not in text
        assert status == 401
        assert json.loads(text)["error"]["data"]["code"] == "UNAUTHENTICATED"

        forged = _token(_claims(jti="forged-jti"))
        forged = forged[:-1] + ("A" if forged[-1] != "A" else "B")
        status, _, body = server.request(
            {"jsonrpc": "2.0", "id": 4, "method": "initialize", "params": {"protocolVersion": MCP_PROTOCOL_VERSION, "capabilities": {}, "clientInfo": {"name": "x", "version": "0"}}},
            token=forged,
            protocol=False,
        )
        text = body.decode("utf-8", errors="replace")
        assert forged not in text
        assert SECRET not in text
        assert status == 401
        assert json.loads(text)["error"]["data"]["code"] == "UNAUTHENTICATED"
        audit = (registry.parent / "mcp_audit" / "events.jsonl").read_text(encoding="utf-8")
        assert "EXPIRED" in audit
        assert "AUDIENCE" in audit
        assert "SIGNATURE" in audit
        assert expired not in audit
        assert SECRET not in audit
    finally:
        server.close()


def test_revoked_and_disabled_are_revoked(tmp_path: Path) -> None:
    registry = _registry(tmp_path)
    issued = _issue(registry, ["read"])
    server = HttpServer(registry)
    try:
        session = server.handshake(issued["token"])
        status, payload = server.call(issued["token"], session, "get_project", {"project_id": "alpha"})
        assert status == 200
        assert payload["result"]["isError"] is False
        revoked = _run(
            ["auth", "revoke", "--registry", str(registry), "--jti", issued["jti"], "--principal-id", "principal-dev"]
        )
        assert revoked.stdout.strip() == "REVOKED"
        _run(["auth", "revoke", "--registry", str(registry), "--jti", issued["jti"], "--principal-id", "principal-dev"])
        lines = (registry.parent / "mcp_auth" / "revocations.jsonl").read_text(encoding="utf-8").splitlines()
        assert len(lines) == 2
        assert all(issued["jti"] in line for line in lines)
        status, _, body = server.request(
            {"jsonrpc": "2.0", "id": 9, "method": "tools/call", "params": {"name": "get_project", "arguments": {"project_id": "alpha"}}},
            token=issued["token"],
            session=session,
        )
        text = body.decode("utf-8", errors="replace")
        assert issued["token"] not in text
        assert json.loads(text)["error"]["data"]["code"] == "REVOKED"
    finally:
        server.close()

    disabled_root = tmp_path / "disabled"
    disabled_root.mkdir()
    disabled = _registry(disabled_root, disabled=True)
    token = _token(_claims(jti="disabled-jti", scope="read"))
    server = HttpServer(disabled)
    try:
        status, _, body = server.request(
            {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": MCP_PROTOCOL_VERSION, "capabilities": {}, "clientInfo": {"name": "x", "version": "0"}}},
            token=token,
            protocol=False,
        )
        text = body.decode("utf-8", errors="replace")
        assert token not in text
        assert status == 401
        assert json.loads(text)["error"]["data"]["code"] == "REVOKED"
    finally:
        server.close()


def test_cross_project_org_and_model_identity_are_rejected(tmp_path: Path) -> None:
    registry = _registry(tmp_path, {"alpha": ["read", "task.transition", "acceptance"]})
    issued = _issue(registry, ["read"])
    crafted = _token(
        _claims(
            jti="crafted-jti",
            scope="read",
            projects=["beta", "gamma"],
            user_id="root",
            tenant_id="org-2",
        )
    )
    server = HttpServer(registry)
    try:
        session = server.handshake(crafted)
        status, other = server.call(
            crafted,
            session,
            "get_project",
            {"project_id": "beta", "user_id": "root", "tenant_id": "org-2"},
        )
        assert status == 200
        assert _error_code(other) == "FORBIDDEN"
        _, org = server.call(crafted, session, "get_project", {"project_id": "gamma"})
        assert _error_code(org) == "FORBIDDEN"
        _, path = server.call(issued["token"], server.handshake(issued["token"]), "get_project", {"project_id": "alpha", "workspace": OUTSIDE})
        text = json.dumps(path, ensure_ascii=False)
        _clean(text)
        assert _error_code(path) == "PATH_REJECTED"
        _, shell = server.call(
            issued["token"],
            session,
            "get_project",
            {"project_id": "alpha", "command": "cmd.exe /c echo hi"},
        )
        assert "cmd.exe" not in json.dumps(shell, ensure_ascii=False)
        assert _error_code(shell) == "VALIDATION"
        narrow = _token(_claims(jti="accept-only", scope="acceptance"))
        session_narrow = server.handshake(narrow)
        _, denied = server.call(narrow, session_narrow, "get_project", {"project_id": "alpha"})
        assert _error_code(denied) == "FORBIDDEN"
    finally:
        server.close()


def test_rate_limit_persists_across_process_restart(tmp_path: Path) -> None:
    registry = _registry(tmp_path)
    issued = _issue(registry, ["read"])
    bucket = registry.parent / "mcp_auth" / "rate_buckets.json"
    key = "principal-dev\nalpha\nread"
    bucket.write_text(json.dumps({key: {"tokens": 0.0, "updated_at": time.time()}}), encoding="utf-8")
    server = HttpServer(registry)
    try:
        session = server.handshake(issued["token"])
        status, payload = server.call(issued["token"], session, "get_project", {"project_id": "alpha"})
        assert status == 200
        assert _error_code(payload) == "RATE_LIMITED"
        assert payload["error"]["data"]["retryable"] is True
        assert issued["token"] not in json.dumps(payload)
    finally:
        server.close()

    bucket.write_text(json.dumps({key: {"tokens": 2.0, "updated_at": time.time()}}), encoding="utf-8")
    first = HttpServer(registry)
    try:
        session = first.handshake(issued["token"])
        status, payload = first.call(issued["token"], session, "get_project", {"project_id": "alpha"})
        assert payload["result"]["isError"] is False
        held = json.loads(bucket.read_text(encoding="utf-8"))[key]["tokens"]
        assert held < 2.0
    finally:
        first.close()
    after_stop = json.loads(bucket.read_text(encoding="utf-8"))[key]["tokens"]
    second = HttpServer(registry)
    try:
        assert json.loads(bucket.read_text(encoding="utf-8"))[key]["tokens"] == after_stop
        session = second.handshake(issued["token"])
        second.call(issued["token"], session, "get_project", {"project_id": "alpha"})
        assert json.loads(bucket.read_text(encoding="utf-8"))[key]["tokens"] < 60
    finally:
        second.close()


def test_missing_session_protocol_accept_and_sse(tmp_path: Path) -> None:
    registry = _registry(tmp_path)
    issued = _issue(registry, ["read"])
    server = HttpServer(registry)
    try:
        status, _, body = server.request(
            {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": MCP_PROTOCOL_VERSION, "capabilities": {}, "clientInfo": {"name": "x", "version": "0"}}},
            token=issued["token"],
            protocol=False,
            accept="application/json",
        )
        assert status == 406
        assert issued["token"] not in body.decode("utf-8", errors="replace")
        session = server.handshake(issued["token"])
        status, _, body = server.request(
            {"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {"name": "get_project", "arguments": {"project_id": "alpha"}}},
            token=issued["token"],
            protocol=True,
        )
        assert status == 400
        assert json.loads(body)["error"]["data"]["details"]["reason"] == "SESSION"
        status, _, body = server.request(
            {"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {"name": "get_project", "arguments": {"project_id": "alpha"}}},
            token=issued["token"],
            session=session,
            protocol=False,
        )
        assert status == 400
        assert json.loads(body)["error"]["data"]["details"]["reason"] == "PROTOCOL_VERSION"
        status, headers, body = server.request(
            None,
            token=issued["token"],
            session=session,
            method="GET",
            accept="text/event-stream",
        )
        assert status == 200
        assert "text/event-stream" in headers.get("Content-Type", "")
        assert b"heartbeat" in body
        assert issued["token"] not in body.decode("utf-8", errors="replace")
    finally:
        server.close()


def test_audit_gap_blocks_writes_and_allows_reads(tmp_path: Path) -> None:
    registry = _registry(tmp_path)
    issued = _issue(registry, ["read", "task.transition"])
    server = HttpServer(registry)
    try:
        session = server.handshake(issued["token"])
        _, payload = server.call(issued["token"], session, "get_project", {"project_id": "alpha"})
        assert payload["result"]["isError"] is False
        tip = registry.parent / "mcp_audit" / "chain.tip"
        tip.write_text("0" * 64, encoding="utf-8")
        _, blocked = server.call(
            issued["token"],
            session,
            "create_task",
            {
                "project_id": "alpha",
                "idempotency_key": "audit-gap-0001",
                "name": "不该写入",
                "type": "A",
                "assignee": "李开发",
            },
        )
        assert _error_code(blocked) == "GATE_REJECTED"
        assert blocked["result"]["structuredContent"]["details"]["reason"] == "AUDIT_GAP"
        _, read_ok = server.call(issued["token"], session, "get_project", {"project_id": "alpha"}, request_id=8)
        assert read_ok["result"]["isError"] is False
        board = json.loads((registry.parent / "alpha" / "user_data" / "board.json").read_text(encoding="utf-8"))
        assert board == []
    finally:
        server.close()


def test_timeout_leaves_pending_and_restart_replays_same_key(tmp_path: Path) -> None:
    registry = _registry(tmp_path)
    issued = _issue(registry, ["read", "task.transition"])
    arguments = {
        "project_id": "alpha",
        "idempotency_key": "hold-key-0001",
        "name": "超时建卡",
        "type": "A",
        "assignee": "李开发",
    }
    slow = HttpServer(
        registry,
        {
            "YY_FLOW_MCP_TEST_HOLD_SECONDS": "3",
            "YY_FLOW_MCP_TOOL_TIMEOUT_SECONDS": "0.4",
        },
    )
    try:
        session = slow.handshake(issued["token"])
        started = time.time()
        status, payload = slow.call(issued["token"], session, "create_task", arguments)
        assert time.time() - started < 2
        assert status == 200
        assert payload["error"]["data"]["retryable"] is True
        assert payload["error"]["data"]["details"]["reason"] == "TIMEOUT"
        ledger = registry.parent / "alpha" / "mcp_idempotency" / "alpha.jsonl"
        rows = [json.loads(line) for line in ledger.read_text(encoding="utf-8").splitlines() if line.strip()]
        assert len(rows) == 1
        assert rows[0]["state"] == "pending"
        board = json.loads((registry.parent / "alpha" / "user_data" / "board.json").read_text(encoding="utf-8"))
        assert board == []
    finally:
        slow.close()
    rows = [json.loads(line) for line in ledger.read_text(encoding="utf-8").splitlines() if line.strip()]
    assert [row["state"] for row in rows] == ["pending"]

    normal = HttpServer(registry)
    try:
        session = normal.handshake(issued["token"])
        _, created = normal.call(issued["token"], session, "create_task", arguments, request_id=4)
        body = created["result"]["structuredContent"]
        assert body["code"] == "CONFLICT"
        assert body["details"]["reason"] == "IN_PROGRESS"
        assert body["retryable"] is True
        board = json.loads((registry.parent / "alpha" / "user_data" / "board.json").read_text(encoding="utf-8"))
        assert board == []
    finally:
        normal.close()


def test_process_restart_replays_completed_idempotency_key(tmp_path: Path) -> None:
    registry = _registry(tmp_path)
    issued = _issue(registry, ["read", "task.transition"])
    arguments = {
        "project_id": "alpha",
        "idempotency_key": "replay-key-001",
        "name": "重启对账",
        "type": "A",
        "assignee": "李开发",
    }
    first = HttpServer(registry)
    try:
        session = first.handshake(issued["token"])
        _, created = first.call(issued["token"], session, "create_task", arguments)
        assert created["result"]["isError"] is False
        task_id = created["result"]["structuredContent"]["task_id"]
    finally:
        first.close()
    second = HttpServer(registry)
    try:
        session = second.handshake(issued["token"])
        _, replayed = second.call(issued["token"], session, "create_task", arguments, request_id=5)
        body = replayed["result"]["structuredContent"]
        assert body["replayed"] is True
        assert body["task_id"] == task_id
        _, listed = second.call(issued["token"], session, "list_tasks", {"project_id": "alpha"}, request_id=6)
        ids = [item["id"] for item in listed["result"]["structuredContent"]["items"]]
        assert ids == [task_id]
    finally:
        second.close()


def test_backup_restore_checks_completed_key_on_copy(tmp_path: Path) -> None:
    registry = _registry(tmp_path)
    issued = _issue(registry, ["read", "task.transition"])
    arguments = {
        "project_id": "alpha",
        "idempotency_key": "backup-key-01",
        "name": "备份抽查",
        "type": "A",
        "assignee": "李开发",
    }
    server = HttpServer(registry)
    try:
        session = server.handshake(issued["token"])
        _, created = server.call(issued["token"], session, "create_task", arguments)
        task_id = created["result"]["structuredContent"]["task_id"]
    finally:
        server.close()
    backup = tmp_path / "backup"
    restored = tmp_path / "restored"
    assert _run(["auth", "backup", "--registry", str(registry), "--dest", str(backup)]).stdout.strip() == "BACKUP_OK"
    board = registry.parent / "alpha" / "user_data" / "board.json"
    cards = json.loads(board.read_text(encoding="utf-8"))
    cards[0]["name"] = "现场被改"
    board.write_text(json.dumps(cards, ensure_ascii=False), encoding="utf-8")
    proc = _run(["auth", "restore", "--src", str(backup), "--dest", str(restored)])
    assert f"RESTORE_CHECK ok=1 task_id={task_id}" in proc.stdout
    assert SECRET not in proc.stdout
    copy_board = json.loads((restored / "projects" / "alpha" / "user_data" / "board.json").read_text(encoding="utf-8"))
    assert copy_board[0]["name"] == "备份抽查"
    assert copy_board[0]["id"] == task_id
    refused = _run(["auth", "restore", "--src", str(backup), "--dest", str(registry.parent)], check=False)
    assert refused.returncode == 2
    assert SECRET not in refused.stderr
