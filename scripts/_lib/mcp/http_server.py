# -*- coding: utf-8 -*-
"""单组织 Streamable HTTP。只绑定回环地址，不做 TLS，不进生产。"""

from __future__ import annotations

import ipaddress
import json
import secrets
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Dict, Mapping
from urllib.parse import urlparse

from . import rate_limit
from .audit_store import AuditStore
from .auth import AuthContext, verify_bearer
from .errors import error
from .protocol import (
    MCP_HTTP_PATH,
    MCP_PROTOCOL_HEADER,
    MCP_PROTOCOL_VERSION,
    MCP_SESSION_HEADER,
)
from .redaction import scrub_literals
from .schema import WRITE_TOOL_NAMES
from .stdio_server import RpcSession, dispatch_rpc

_MAX_BODY = 1024 * 1024
_JSON_ACCEPT = ("application/json", "text/event-stream")


class BindError(Exception):
    """绑定地址被拒绝。消息固定，不回显主机名。"""


def validate_bind_host(host: str) -> str:
    if not isinstance(host, str) or not host.strip():
        raise BindError("拒绝绑定")
    cleaned = host.strip()
    if cleaned.lower() == "localhost":
        return "127.0.0.1"
    if cleaned in {"0.0.0.0", "::", "[::]"}:
        raise BindError("拒绝绑定")
    try:
        address = ipaddress.ip_address(cleaned)
    except ValueError:
        raise BindError("拒绝绑定") from None
    if not address.is_loopback:
        raise BindError("拒绝绑定")
    return cleaned


def mcp_sdk_importable() -> bool:
    try:
        __import__("mcp")
    except ImportError:
        return False
    return True


def serve_http(host: str, port: int, context: Mapping[str, Any]) -> None:
    bound = validate_bind_host(host)
    server = _Server((bound, int(port)), context)
    server.daemon_threads = True
    actual = server.server_address[1]
    message = f"MCP_HTTP_LISTENING {bound} {actual}\n".encode("ascii")
    import sys

    sys.stderr.buffer.write(message)
    sys.stderr.buffer.flush()
    try:
        server.serve_forever()
    finally:
        server.server_close()


class _Server(ThreadingHTTPServer):
    def __init__(self, address: tuple, context: Mapping[str, Any]) -> None:
        self.mcp_context = dict(context)
        self.sessions: Dict[str, Dict[str, Any]] = {}
        self.session_lock = threading.Lock()
        super().__init__(address, _Handler)


class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt: str, *args: Any) -> None:
        return

    def handle_error(self, request: Any, client_address: Any) -> None:
        return

    def do_GET(self) -> None:
        if not self._path_ok():
            self._send_empty(404)
            return
        if not self._origin_ok():
            self._send_empty(403)
            return
        if not _accepts(self.headers.get("Accept"), "text/event-stream"):
            self._send_rpc(406, None, error("VALIDATION", details={"reason": "ACCEPT"}))
            return
        decision, token = self._authenticate()
        if decision.context is None:
            self._audit_auth(decision, "GET")
            self._send_rpc(401, None, error(decision.code))
            return
        session_id = self.headers.get(MCP_SESSION_HEADER)
        if not session_id:
            self._send_rpc(400, None, error("VALIDATION", details={"reason": "SESSION"}))
            return
        stored = self._session(session_id)
        if stored is None or stored["principal_id"] != decision.context.principal_id:
            self._send_empty(404)
            return
        if self.headers.get(MCP_PROTOCOL_HEADER) != MCP_PROTOCOL_VERSION:
            self._send_rpc(400, None, error("VALIDATION", details={"reason": "PROTOCOL_VERSION"}))
            return
        body = b"id: 1\nevent: heartbeat\ndata: {}\n\n"
        self._send_bytes(
            200,
            "text/event-stream",
            body,
            {MCP_SESSION_HEADER: session_id},
            self._secrets(token),
        )

    def do_POST(self) -> None:
        if not self._path_ok():
            self._send_empty(404)
            return
        if not self._origin_ok():
            self._send_empty(403)
            return
        if not _accepts(self.headers.get("Accept"), *_JSON_ACCEPT):
            self._send_rpc(406, None, error("VALIDATION", details={"reason": "ACCEPT"}))
            return
        content_type = (self.headers.get("Content-Type") or "").split(";", 1)[0].strip().lower()
        if content_type != "application/json":
            self._send_rpc(415, None, error("VALIDATION", details={"reason": "CONTENT_TYPE"}))
            return
        raw = self._read_body()
        if raw is None:
            self._send_rpc(400, None, error("VALIDATION", details={"reason": "FRAME"}))
            return
        try:
            message = json.loads(raw.decode("utf-8"))
        except (UnicodeError, json.JSONDecodeError):
            self._send_rpc(400, None, error("VALIDATION", details={"reason": "FRAME"}))
            return
        if not isinstance(message, dict):
            self._send_rpc(400, None, error("VALIDATION", details={"reason": "FRAME"}))
            return
        decision, token = self._authenticate()
        secrets_for_response = self._secrets(token)
        request_id = message.get("id") if "id" in message else None
        if decision.context is None:
            self._audit_auth(decision, str(message.get("method") or ""))
            self._send_rpc(401, request_id, error(decision.code), secrets_for_response)
            return
        method = message.get("method")
        if method == "initialize":
            self._initialize(message, decision.context, secrets_for_response)
            return
        session_id = self.headers.get(MCP_SESSION_HEADER)
        if not session_id:
            self._send_rpc(400, request_id, error("VALIDATION", details={"reason": "SESSION"}), secrets_for_response)
            return
        stored = self._session(session_id)
        if stored is None or stored["principal_id"] != decision.context.principal_id:
            self._send_empty(404)
            return
        if self.headers.get(MCP_PROTOCOL_HEADER) != MCP_PROTOCOL_VERSION:
            self._send_rpc(
                400,
                request_id,
                error("VALIDATION", details={"reason": "PROTOCOL_VERSION"}),
                secrets_for_response,
            )
            return
        if method == "tools/call" and not self._allow_rate(message, decision.context, request_id, secrets_for_response):
            return
        context = self._tool_context(decision.context)
        responses = dispatch_rpc(message, stored["rpc"], context)
        if not responses:
            self._send_empty(202)
            return
        self._send_payload(200, responses[0], secrets_for_response)

    def do_DELETE(self) -> None:
        self._send_empty(405)

    def _initialize(self, message: Mapping[str, Any], principal: AuthContext, secrets_for_response: list) -> None:
        session = RpcSession()
        context = self._tool_context(principal)
        responses = dispatch_rpc(message, session, context)
        if not responses:
            self._send_empty(202)
            return
        payload = responses[0]
        headers = {}
        if "result" in payload and session.announced and not session.failed:
            session_id = secrets.token_hex(16)
            with self.server.session_lock:
                self.server.sessions[session_id] = {"rpc": session, "principal_id": principal.principal_id}
            headers[MCP_SESSION_HEADER] = session_id
        self._send_payload(200, payload, secrets_for_response, headers)

    def _allow_rate(self, message: Mapping[str, Any], principal: AuthContext, request_id: Any, secrets_for_response: list) -> bool:
        params = message.get("params")
        if not isinstance(params, dict):
            return True
        name = params.get("name")
        arguments = params.get("arguments")
        project_id = ""
        if isinstance(arguments, dict) and isinstance(arguments.get("project_id"), str):
            project_id = arguments["project_id"]
        if not isinstance(name, str) or not project_id:
            return True
        kind = "write" if name in WRITE_TOOL_NAMES else "read"
        try:
            allowed = rate_limit.allow(self.server.mcp_context["registry"], principal.principal_id, project_id, kind)
        except Exception:
            allowed = False
        if allowed:
            return True
        self._send_rpc(200, request_id, error("RATE_LIMITED"), secrets_for_response)
        return False

    def _tool_context(self, principal: AuthContext) -> Dict[str, Any]:
        context = dict(self.server.mcp_context)
        context["transport"] = "http"
        context["auth"] = principal
        context.pop("os_user", None)
        return context

    def _authenticate(self):
        token = self.headers.get("Authorization") or ""
        decision = verify_bearer(self.server.mcp_context["registry"], token)
        return decision, _bearer_token(token)

    def _audit_auth(self, decision: Any, tool: str) -> None:
        audit = self.server.mcp_context.get("audit")
        if not isinstance(audit, AuditStore):
            return
        audit.append(
            principal_id="",
            project_id="",
            tool=tool[:64] or "http",
            code=decision.code,
            diagnostic=decision.reason,
            arguments={},
        )

    def _session(self, session_id: str) -> Dict[str, Any] | None:
        with self.server.session_lock:
            stored = self.server.sessions.get(session_id)
            if stored is None:
                return None
            return stored

    def _path_ok(self) -> bool:
        path = urlparse(self.path).path
        return path == MCP_HTTP_PATH

    def _origin_ok(self) -> bool:
        origin = self.headers.get("Origin")
        if not origin:
            return True
        parsed = urlparse(origin)
        host = (parsed.hostname or "").lower()
        return parsed.scheme in {"http", "https"} and host in {"127.0.0.1", "localhost", "::1"}

    def _read_body(self) -> bytes | None:
        raw_length = self.headers.get("Content-Length")
        if raw_length is None:
            return None
        try:
            length = int(raw_length)
        except ValueError:
            return None
        if length < 2 or length > _MAX_BODY:
            return None
        data = self.rfile.read(length)
        if len(data) != length:
            return None
        return data

    def _secrets(self, token: str) -> list:
        values = []
        if token:
            values.append(token)
        key = self.server.mcp_context["registry"].token_key
        if isinstance(key, bytes) and key:
            try:
                text = key.decode("utf-8")
            except UnicodeError:
                text = ""
            if text:
                values.append(text)
        return values

    def _send_rpc(self, status: int, request_id: Any, exc: Any, secrets_for_response: list | None = None) -> None:
        if isinstance(request_id, bool) or not isinstance(request_id, (str, int)):
            request_id = None
        payload = {
            "jsonrpc": "2.0",
            "id": request_id,
            "error": {"code": exc.rpc_code, "message": exc.message, "data": exc.body()},
        }
        self._send_payload(status, payload, secrets_for_response or [])

    def _send_payload(self, status: int, payload: Mapping[str, Any], secrets_for_response: list, headers: Mapping[str, str] | None = None) -> None:
        cleaned = scrub_literals(dict(payload), secrets_for_response)
        raw = json.dumps(cleaned, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        text = raw.decode("utf-8")
        for secret in secrets_for_response:
            if isinstance(secret, str) and len(secret) >= 4 and secret in text:
                text = text.replace(secret, "***MASKED***")
        self._send_bytes(status, "application/json", text.encode("utf-8"), headers or {}, [])

    def _send_bytes(self, status: int, content_type: str, body: bytes, headers: Mapping[str, str], secrets_for_response: list) -> None:
        text = body.decode("utf-8", errors="ignore")
        for secret in secrets_for_response:
            if isinstance(secret, str) and len(secret) >= 4 and secret in text:
                text = text.replace(secret, "***MASKED***")
        raw = text.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(raw)))
        self.send_header("Cache-Control", "no-store")
        for name, value in headers.items():
            self.send_header(name, value)
        self.end_headers()
        self.wfile.write(raw)

    def _send_empty(self, status: int) -> None:
        self.send_response(status)
        self.send_header("Content-Length", "0")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()


def _accepts(header: str | None, *required: str) -> bool:
    if not header:
        return False
    parts = {item.split(";", 1)[0].strip().lower() for item in header.split(",") if item.strip()}
    return all(item in parts for item in required)


def _bearer_token(authorization: str) -> str:
    if not isinstance(authorization, str):
        return ""
    text = authorization.strip()
    if len(text) < 8 or text[:7].lower() != "bearer ":
        return ""
    return text[7:].strip()
