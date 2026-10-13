# -*- coding: utf-8 -*-
"""4A STDIO 只读 MCP。测试进程使用临时目录，不读取仓库看板。"""

from __future__ import annotations

import getpass
import json
import os
import subprocess
import sys
import threading
from pathlib import Path

import pytest

from scripts._lib.boards.offline_board_adapter import OfflineBoardAdapter
from scripts._lib.mcp import MCP_PROTOCOL_VERSION
from scripts._lib.mcp.audit_store import AuditStore
from scripts._lib.mcp.protocol import encode_message, read_message
from scripts._lib.mcp.registry import load_registry
from scripts._lib.mcp.schema import SCHEMA_VERSION
from scripts._lib.mcp.service import handle_tool

REPO = Path(__file__).resolve().parents[1]
SERVER = REPO / "scripts" / "mcp_server.py"
MARKER = "MCPROOTMARKER9f3a"
SECRET_PEM = "SECRET_PEM_9f3a"
SECRET_PW = "SECRET_PW_9f3a"
SECRET_TOK = "SECRET_TOK_9f3a"
SECRET_CUR = "SECRET_CUR_9f3a"
SECRET_PROC = "SECRET_PROC_9f3a"
SENTINELS = (MARKER, SECRET_PEM, SECRET_PW, SECRET_TOK, SECRET_CUR, SECRET_PROC)


def _cards() -> list:
    remarks = (
        "-----BEGIN PRIVATE KEY-----\n"
        + SECRET_PEM
        + "\n-----END PRIVATE KEY-----\npassword="
        + SECRET_PW
    )
    cards = []
    for index in range(1, 26):
        cards.append(
            {
                "id": f"T{index:04d}",
                "name": f"任务{index}",
                "status": "进行中",
                "assignee": "李开发",
                "owner": "严经理",
                "type": "A",
                "remarks": remarks if index == 1 else "",
                "access_token": SECRET_TOK,
                "process": "github_token=" + SECRET_PROC,
                "workspace": MARKER,
            }
        )
    cards.append({"name": "缺编号任务", "status": "待开始", "access_token": SECRET_TOK})
    cards.append(
        {
            "id": "T0099",
            "name": "已删除任务",
            "status": "已取消",
            "is_deleted": True,
            "access_token": SECRET_TOK,
        }
    )
    return cards


def _write_project(root: Path, project_id: str) -> dict:
    data_root = root / MARKER / project_id
    board = data_root / "user_data" / "board.json"
    evidence = data_root / "evidence"
    board.parent.mkdir(parents=True, exist_ok=True)
    evidence.mkdir(parents=True, exist_ok=True)
    if project_id == "alpha":
        board.write_text(json.dumps(_cards(), ensure_ascii=False), encoding="utf-8")
        policy = data_root / "workflow_policies" / "T0001.json"
        policy.parent.mkdir(parents=True, exist_ok=True)
        policy.write_text(
            json.dumps({"task_id": "T0001", "profile_id": "lite"}, ensure_ascii=False),
            encoding="utf-8",
        )
    else:
        board.write_text("[]", encoding="utf-8")
    return {
        "project_id": project_id,
        "display_name": "Alpha" if project_id == "alpha" else "Beta",
        "org_id": "org-1",
        "data_root": str(data_root.resolve()),
        "board_file": str(board.resolve()),
        "evidence_root": str(evidence.resolve()),
        "board_kind": "offline",
    }


def _registry(tmp: Path, grants: dict | None = None) -> Path:
    path = tmp / "mcp_registry.json"
    document = {
        "server_revision": "rev-4a-test",
        "cursor_secret": SECRET_CUR,
        "projects": [_write_project(tmp, "alpha"), _write_project(tmp, "beta")],
        "local_principals": [
            {
                "os_user": getpass.getuser(),
                "principal_id": "principal-dev",
                "org_id": "org-1",
                "grants": grants or {"alpha": ["read"]},
            }
        ],
    }
    path.write_text(json.dumps(document, ensure_ascii=False), encoding="utf-8")
    return path


class _Capture:
    def __init__(self, raw) -> None:
        self.raw = raw
        self.seen = bytearray()

    def read(self, size: int = -1) -> bytes:
        data = self.raw.read(size)
        if data:
            self.seen.extend(data)
        return data


class StdioSession:
    def __init__(self, tmp: Path, registry: Path | None = None, env_registry: bool = False, env_extra: dict | None = None) -> None:
        self.tmp = tmp
        registry = registry or _registry(tmp)
        self.registry = registry
        env = os.environ.copy()
        env.pop("YY_FLOW_MCP_REGISTRY", None)
        env.pop("YY_FLOW_MCP_ACCEPTANCE_CONFIRMED", None)
        env["YY_FLOW_PROJECT_ROOT"] = str(tmp)
        if env_extra:
            env.update(env_extra)
        command = [sys.executable, str(SERVER), "stdio"]
        if env_registry:
            env["YY_FLOW_MCP_REGISTRY"] = str(registry)
        else:
            command.extend(["--registry", str(registry)])
        self.proc = subprocess.Popen(
            command,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            cwd=str(tmp),
            env=env,
            bufsize=0,
        )
        self.capture = _Capture(self.proc.stdout)
        self.err = bytearray()
        self._err_thread = threading.Thread(target=self._drain_err, daemon=True)
        self._err_thread.start()
        self._next_id = 1

    def _drain_err(self) -> None:
        assert self.proc.stderr is not None
        while True:
            chunk = self.proc.stderr.read(4096)
            if not chunk:
                break
            self.err.extend(chunk)

    def request(self, method: str, params: dict | None, req_id: int | None = None) -> dict:
        if req_id is None:
            req_id = self._next_id
            self._next_id += 1
        payload = {"jsonrpc": "2.0", "id": req_id, "method": method}
        if params is not None:
            payload["params"] = params
        assert self.proc.stdin is not None
        self.proc.stdin.write(encode_message(payload))
        self.proc.stdin.flush()
        message = self._read()
        assert message is not None
        assert message["id"] == req_id
        return message

    def notify(self, method: str) -> None:
        assert self.proc.stdin is not None
        self.proc.stdin.write(encode_message({"jsonrpc": "2.0", "method": method}))
        self.proc.stdin.flush()

    def _read(self) -> dict | None:
        box: dict = {}

        def run() -> None:
            try:
                box["msg"] = read_message(self.capture)
            except Exception as exc:  # noqa: BLE001 - 交给断言展示
                box["err"] = exc

        thread = threading.Thread(target=run)
        thread.start()
        thread.join(20)
        if thread.is_alive():
            self.proc.kill()
            raise AssertionError(self._failure("响应超时"))
        if "err" in box:
            raise AssertionError(self._failure(repr(box["err"])))
        return box["msg"]

    def handshake(self) -> dict:
        started = self.request(
            "initialize",
            {
                "protocolVersion": MCP_PROTOCOL_VERSION,
                "capabilities": {},
                "clientInfo": {"name": "stdio-test", "version": "0"},
            },
        )
        assert "error" not in started
        self.notify("notifications/initialized")
        return started

    def call(self, name: str, arguments: dict) -> dict:
        message = self.request("tools/call", {"name": name, "arguments": arguments})
        assert "error" not in message, message
        result = message["result"]
        assert result["content"][0]["text"] == json.dumps(
            result["structuredContent"], ensure_ascii=False, separators=(",", ":")
        )
        return result

    def close(self) -> bytes:
        if self.proc.stdin is not None:
            try:
                self.proc.stdin.close()
            except OSError:
                pass
        try:
            self.proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            self.proc.kill()
            self.proc.wait(timeout=5)
        rest = self.capture.read()
        self._err_thread.join(timeout=2)
        return bytes(self.capture.seen) + bytes(rest or b"")

    def _failure(self, reason: str) -> str:
        err = self.err.decode("utf-8", errors="replace")
        return reason + "\n" + err[-2000:]


def _assert_clean(blob: bytes) -> None:
    text = blob.decode("utf-8", errors="replace")
    for marker in SENTINELS:
        assert marker not in text


def _walk(session: StdioSession, limit: int) -> list:
    pages = []
    cursor = ""
    while True:
        arguments = {"project_id": "alpha", "limit": limit}
        if cursor:
            arguments["cursor"] = cursor
        result = session.call("list_tasks", arguments)
        assert result["isError"] is False
        body = result["structuredContent"]
        pages.append(body)
        if not body["has_more"]:
            assert body["next_cursor"] == ""
            break
        assert body["next_cursor"]
        cursor = body["next_cursor"]
    return pages


@pytest.fixture
def session(tmp_path: Path):
    client = StdioSession(tmp_path)
    try:
        yield client
    finally:
        blob = client.close()
        _assert_clean(blob)
        _assert_clean(bytes(client.err))


def test_package_constants_and_source_boundaries() -> None:
    assert MCP_PROTOCOL_VERSION == "2025-11-25"
    assert SCHEMA_VERSION == "4.0.0"
    root = REPO / "scripts" / "_lib" / "mcp"
    for name in (
        "protocol.py",
        "schema.py",
        "errors.py",
        "redaction.py",
        "registry.py",
        "service.py",
        "stdio_server.py",
        "auth.py",
        "audit_store.py",
        "__init__.py",
    ):
        text = (root / name).read_text(encoding="utf-8")
        assert "import mcp" not in text
        assert "subprocess" not in text
        assert "os.system" not in text
    entry = (REPO / "scripts" / "mcp_server.py").read_text(encoding="utf-8")
    assert "import mcp" not in entry
    assert "subprocess" not in entry


def test_initialize_schema_and_tool_list(session: StdioSession) -> None:
    started = session.handshake()
    assert bytes(session.capture.seen).startswith(b"Content-Length:")
    result = started["result"]
    assert result["protocolVersion"] == "2025-11-25"
    assert result["serverInfo"]["version"] == "4.0.0"
    listed = session.request("tools/list", {})
    tools = listed["result"]["tools"]
    assert [item["name"] for item in tools] == ["get_project", "get_task", "list_tasks", "get_status"]
    for item in tools:
        schema = item["inputSchema"]
        assert schema["$schema"] == "https://json-schema.org/draft/2020-12/schema"
        assert schema["additionalProperties"] is False
        assert "project_id" in schema["required"]
        restored = json.loads(json.dumps(schema))
        assert restored == schema
    ping = session.request("ping", {})
    assert ping["result"] == {}
    project = session.call("get_project", {"project_id": "alpha"})
    body = project["structuredContent"]
    assert set(body) == {
        "project_id",
        "display_name",
        "org_id",
        "board_kind",
        "schema_version",
        "server_revision",
    }
    assert body["schema_version"] == "4.0.0"
    assert body["server_revision"] == "rev-4a-test"
    assert body["board_kind"] == "offline"


def test_unknown_project_path_and_cross_project(session: StdioSession) -> None:
    session.handshake()
    unknown = session.call("get_project", {"project_id": "gamma"})
    assert unknown["isError"] is True
    assert unknown["structuredContent"]["code"] == "UNKNOWN_PROJECT"
    assert unknown["structuredContent"]["retryable"] is False
    forbidden = session.call("get_project", {"project_id": "beta"})
    assert forbidden["isError"] is True
    assert forbidden["structuredContent"]["code"] == "FORBIDDEN"
    data_root = str((session.tmp / MARKER / "alpha").resolve())
    rejected = session.call("get_project", {"project_id": "alpha", "path": data_root})
    assert rejected["isError"] is True
    assert rejected["structuredContent"]["code"] == "PATH_REJECTED"
    dotted = session.call("get_task", {"project_id": "alpha", "task_id": "../T0001"})
    assert dotted["isError"] is True
    assert dotted["structuredContent"]["code"] == "PATH_REJECTED"
    token = session.call(
        "get_project",
        {"project_id": "alpha", "invocation_token": SECRET_TOK},
    )
    assert token["isError"] is True
    assert token["structuredContent"]["code"] == "VALIDATION"
    missing = session.call("get_task", {"project_id": "alpha", "task_id": "T4040"})
    assert missing["structuredContent"]["code"] == "NOT_FOUND"
    deleted = session.call("get_task", {"project_id": "alpha", "task_id": "T0099"})
    assert deleted["structuredContent"]["code"] == "NOT_FOUND"
    include_deleted = session.call(
        "list_tasks",
        {"project_id": "alpha", "include_deleted": True, "limit": 20},
    )
    assert include_deleted["structuredContent"]["code"] == "FORBIDDEN"
    task = session.call("get_task", {"project_id": "alpha", "task_id": "T0001", "include_process": True})
    assert task["isError"] is False
    card = task["structuredContent"]
    assert "access_token" not in card
    assert "process" in card
    assert SECRET_PROC not in json.dumps(card, ensure_ascii=False)
    status = session.call("get_status", {"project_id": "alpha", "task_id": "T0001"})
    assert status["structuredContent"]["profile_id"] == "lite"
    assert status["structuredContent"]["status"] == "进行中"
    assert len(status["structuredContent"]["task_version"]) == 64
    plain = session.call("get_status", {"project_id": "alpha", "task_id": "T0002"})
    assert plain["structuredContent"]["profile_id"] == "standard"
    too_big = session.call("list_tasks", {"project_id": "alpha", "limit": 51})
    assert too_big["structuredContent"]["code"] == "VALIDATION"


def test_pagination_does_not_skip_ids(session: StdioSession) -> None:
    session.handshake()
    pages = _walk(session, 20)
    assert [page["stale"] for page in pages] == [False, False]
    assert [len(page["items"]) for page in pages] == [20, 5]
    ids = [item["id"] for page in pages for item in page["items"]]
    assert ids == [f"T{index:04d}" for index in range(1, 26)]
    assert len(ids) == len(set(ids))
    assert "T0099" not in ids
    assert all("缺编号" not in item.get("name", "") for page in pages for item in page["items"])

    first = session.call("list_tasks", {"project_id": "alpha", "limit": 10})
    body = first["structuredContent"]
    assert [item["id"] for item in body["items"]] == [f"T{index:04d}" for index in range(1, 11)]
    board = session.tmp / MARKER / "alpha" / "user_data" / "board.json"
    current = json.loads(board.read_text(encoding="utf-8"))
    current.append({"id": "T0000", "name": "插入头部", "status": "待开始", "assignee": "李开发", "type": "A"})
    current.append({"id": "T0030", "name": "插入尾部", "status": "待开始", "assignee": "李开发", "type": "A"})
    board.write_text(json.dumps(current, ensure_ascii=False), encoding="utf-8")
    second = session.call("list_tasks", {"project_id": "alpha", "limit": 10, "cursor": body["next_cursor"]})
    stale_page = second["structuredContent"]
    assert stale_page["stale"] is True
    assert [item["id"] for item in stale_page["items"]] == [f"T{index:04d}" for index in range(11, 21)]
    third = session.call(
        "list_tasks",
        {"project_id": "alpha", "limit": 10, "cursor": stale_page["next_cursor"]},
    )
    tail = third["structuredContent"]
    assert tail["has_more"] is False
    seen = [item["id"] for item in body["items"] + stale_page["items"] + tail["items"]]
    assert seen == [f"T{index:04d}" for index in range(1, 26)] + ["T0030"]
    assert "T0000" not in seen
    assert len(seen) == len(set(seen))


def test_registry_env_and_refused_transports(tmp_path: Path) -> None:
    client = StdioSession(tmp_path, env_registry=True)
    try:
        started = client.handshake()
        assert started["result"]["protocolVersion"] == "2025-11-25"
        project = client.call("get_project", {"project_id": "alpha"})
        assert project["isError"] is False
    finally:
        blob = client.close()
        _assert_clean(blob)

    http = subprocess.run(
        [sys.executable, str(SERVER), "http", "--registry", str(client.registry)],
        cwd=str(tmp_path),
        capture_output=True,
        timeout=5,
        check=False,
    )
    assert http.returncode == 2
    assert "HTTP 监听未在本批次启用" in http.stderr.decode("utf-8")
    sdk = subprocess.run(
        [sys.executable, str(SERVER), "stdio", "--transport-backend", "sdk"],
        cwd=str(tmp_path),
        capture_output=True,
        timeout=5,
        check=False,
    )
    assert sdk.returncode == 2
    assert "官方 SDK 后端未启用" in sdk.stderr.decode("utf-8")


def test_rejected_protocol_version_does_not_downgrade(tmp_path: Path) -> None:
    client = StdioSession(tmp_path)
    try:
        failed = client.request(
            "initialize",
            {"protocolVersion": "2024-11-05", "capabilities": {}, "clientInfo": {"name": "x", "version": "0"}},
        )
        assert failed["error"]["code"] == -32004
        assert failed["error"]["data"]["code"] == "VALIDATION"
        listed = client.request("tools/list", {})
        assert listed["error"]["data"]["code"] == "VALIDATION"
        assert "tools" not in listed
    finally:
        blob = client.close()
        _assert_clean(blob)


def _listed_ids(session: StdioSession, arguments: dict) -> list:
    found = []
    cursor = ""
    while True:
        payload = dict(arguments)
        if cursor:
            payload["cursor"] = cursor
        result = session.call("list_tasks", payload)
        assert result["isError"] is False, result["structuredContent"]
        body = result["structuredContent"]
        found.extend(item["id"] for item in body["items"])
        if not body["has_more"]:
            break
        cursor = body["next_cursor"]
    return found


def test_admin_include_deleted_lists_soft_deleted_cards(tmp_path: Path) -> None:
    """DEF-T0037-1：管理员列表能看到软删除；非管理员 include_deleted 仍拒绝。"""
    admin_root = tmp_path / "admin"
    admin = StdioSession(admin_root, registry=_registry(admin_root, {"alpha": ["read", "admin"]}))
    try:
        admin.handshake()
        visible = _listed_ids(admin, {"project_id": "alpha", "include_deleted": True, "limit": 20})
        assert "T0099" in visible
        assert visible.count("T0099") == 1
        hidden = _listed_ids(admin, {"project_id": "alpha", "limit": 20})
        assert "T0099" not in hidden
        deleted = admin.call(
            "get_task",
            {"project_id": "alpha", "task_id": "T0099", "include_deleted": True},
        )
        assert deleted["isError"] is False
        assert deleted["structuredContent"]["is_deleted"] is True
        plain = admin.call("get_task", {"project_id": "alpha", "task_id": "T0099"})
        assert plain["structuredContent"]["code"] == "NOT_FOUND"
        status = admin.call("get_status", {"project_id": "alpha", "task_id": "T0099"})
        assert status["structuredContent"]["code"] == "NOT_FOUND"
    finally:
        blob = admin.close()
        _assert_clean(blob)
        _assert_clean(bytes(admin.err))

    reader_root = tmp_path / "reader"
    reader = StdioSession(reader_root, registry=_registry(reader_root))
    try:
        reader.handshake()
        denied = reader.call(
            "list_tasks",
            {"project_id": "alpha", "include_deleted": True, "limit": 20},
        )
        assert denied["isError"] is True
        assert denied["structuredContent"]["code"] == "FORBIDDEN"
        assert "T0099" not in json.dumps(denied["structuredContent"], ensure_ascii=False)
        omitted = _listed_ids(reader, {"project_id": "alpha", "limit": 50})
        assert "T0099" not in omitted
        task_denied = reader.call(
            "get_task",
            {"project_id": "alpha", "task_id": "T0099", "include_deleted": True},
        )
        assert task_denied["structuredContent"]["code"] == "FORBIDDEN"
    finally:
        blob = reader.close()
        _assert_clean(blob)
        _assert_clean(bytes(reader.err))


def test_snapshot_over_100_does_not_skip_ids_when_insert_happens(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """DEF-T0037-2：超过 100 条时，读取期间插入不得按 offset 漏掉中间编号。"""
    registry_path = _registry(tmp_path)
    board = tmp_path / MARKER / "alpha" / "user_data" / "board.json"
    original = [
        {
            "id": f"T{index:04d}",
            "name": f"批量{index}",
            "status": "进行中",
            "assignee": "李开发",
            "type": "A",
        }
        for index in range(1, 121)
    ]
    board.write_text(json.dumps(original, ensure_ascii=False), encoding="utf-8")
    state = {"reads": 0}
    real_read = OfflineBoardAdapter._read_cards

    def wrapped_read(self):
        loaded = real_read(self)
        state["reads"] += 1
        if state["reads"] == 1 and loaded:
            inserted = dict(loaded[0])
            inserted["id"] = "T0150"
            inserted["name"] = "读取期间插入"
            board.write_text(json.dumps([inserted] + loaded, ensure_ascii=False), encoding="utf-8")
            return loaded
        return real_read(self)

    monkeypatch.setattr(OfflineBoardAdapter, "_read_cards", wrapped_read)
    registry = load_registry(str(registry_path))
    context = {
        "registry": registry,
        "audit": AuditStore(str(tmp_path / "mcp_audit")),
        "os_user": getpass.getuser(),
    }
    first = handle_tool("list_tasks", {"project_id": "alpha", "limit": 50}, context)
    assert first["isError"] is False
    body = first["structured"]
    assert body["stale"] is True
    found = [item["id"] for item in body["items"]]
    cursor = body["next_cursor"]
    while cursor:
        page = handle_tool(
            "list_tasks",
            {"project_id": "alpha", "limit": 50, "cursor": cursor},
            context,
        )
        assert page["isError"] is False
        payload = page["structured"]
        found.extend(item["id"] for item in payload["items"])
        cursor = payload["next_cursor"] if payload["has_more"] else ""
    final_ids = [f"T{index:04d}" for index in range(1, 121)] + ["T0150"]
    assert found == final_ids
    assert "T0101" in found
    assert found.count("T0100") == 1
    assert len(found) == len(set(found))
