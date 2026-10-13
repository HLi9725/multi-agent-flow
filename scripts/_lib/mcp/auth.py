# -*- coding: utf-8 -*-
"""从传输上下文取主体。STDIO 映射操作系统用户；HTTP 只接受服务端校验过的 Bearer。"""

from __future__ import annotations

import base64
import getpass
import hashlib
import hmac
import json
import os
import time
import uuid
from dataclasses import dataclass
from typing import Mapping

from ..core import file_lock
from .registry import PrincipalRecord, Registry

CLOCK_SKEW_SECONDS = 60
TOKEN_TTL_MAX_SECONDS = 12 * 3600
KNOWN_SCOPES = frozenset({"read", "evidence.submit", "task.transition", "acceptance", "admin"})
_MAX_TOKEN_CHARS = 4096


@dataclass(frozen=True)
class AuthContext:
    principal_id: str
    org_id: str
    grants: Mapping[str, frozenset]
    disabled: bool
    role: str = "DEV"
    jti: str = ""
    token_scopes: frozenset | None = None


@dataclass(frozen=True)
class AuthDecision:
    context: AuthContext | None
    code: str
    reason: str


class AuthAdminError(Exception):
    """管理命令失败。消息不得携带密钥或令牌。"""


def current_os_user() -> str:
    try:
        name = getpass.getuser()
    except Exception:
        name = ""
    if isinstance(name, str):
        return name.strip()
    return ""


def resolve_stdio_principal(registry: Registry, os_user: str) -> AuthContext | None:
    if not os_user:
        return None
    folded = os_user.casefold()
    for record in registry.principals:
        if record.os_user.casefold() != folded:
            continue
        return _from_record(record)
    return None


def resolve_request_principal(registry: Registry, context: Mapping[str, object]) -> AuthContext | None:
    """HTTP 只使用服务端放入上下文的 AuthContext，不回退到操作系统用户或模型字段。"""
    if context.get("transport") == "http":
        supplied = context.get("auth")
        if isinstance(supplied, AuthContext):
            return supplied
        return None
    return resolve_stdio_principal(registry, str(context.get("os_user") or ""))


def _from_record(record: PrincipalRecord, *, jti: str = "", token_scopes: frozenset | None = None) -> AuthContext:
    return AuthContext(
        principal_id=record.principal_id,
        org_id=record.org_id,
        grants=record.grants,
        disabled=record.disabled,
        role=record.role,
        jti=jti,
        token_scopes=token_scopes,
    )


def scopes_for(auth: AuthContext, project_id: str) -> frozenset:
    scopes = auth.grants.get(project_id)
    if scopes is None:
        return frozenset()
    granted = frozenset(scopes)
    if auth.token_scopes is None:
        return granted
    return granted & frozenset(auth.token_scopes)


def auth_dir(registry: Registry) -> str:
    return os.path.join(os.path.dirname(registry.path), "mcp_auth")


def grants_path(registry: Registry) -> str:
    return os.path.join(auth_dir(registry), "grants.json")


def revocations_path(registry: Registry) -> str:
    return os.path.join(auth_dir(registry), "revocations.jsonl")


def ensure_grants(registry: Registry) -> None:
    path = grants_path(registry)
    if os.path.isfile(path):
        return
    principals = []
    for record in registry.principals:
        principals.append(
            {
                "principal_id": record.principal_id,
                "org_id": record.org_id,
                "role": record.role,
                "disabled": record.disabled,
                "grants": {project_id: sorted(scopes) for project_id, scopes in record.grants.items()},
            }
        )
    _write_json(path, {"principals": principals})


def issue_token(
    registry: Registry,
    principal_id: str,
    scopes: list[str],
    ttl_seconds: int,
) -> dict:
    if not registry.token_key or not registry.token_audience:
        raise AuthAdminError("登记缺少签名配置")
    if not isinstance(ttl_seconds, int) or ttl_seconds < 60 or ttl_seconds > TOKEN_TTL_MAX_SECONDS:
        raise AuthAdminError("令牌有效期无效")
    if not scopes or any(scope not in KNOWN_SCOPES for scope in scopes):
        raise AuthAdminError("权限范围无效")
    ensure_grants(registry)
    record = _grant_record(registry, principal_id)
    if record is None:
        raise AuthAdminError("主体不在授权表")
    if record.disabled:
        raise AuthAdminError("主体已停用")
    now = int(time.time())
    jti = uuid.uuid4().hex
    token = sign_token(
        registry.token_key,
        {
            "sub": record.principal_id,
            "org": record.org_id,
            "aud": registry.token_audience,
            "iat": now,
            "exp": now + ttl_seconds,
            "jti": jti,
            "scope": " ".join(scopes),
        },
    )
    return {"jti": jti, "token": token, "exp": now + ttl_seconds, "principal_id": record.principal_id}


def revoke_jti(registry: Registry, jti: str, principal_id: str = "") -> None:
    if not isinstance(jti, str) or not jti.strip() or len(jti) > 128:
        raise AuthAdminError("撤销标识无效")
    if any(char.isspace() for char in jti):
        raise AuthAdminError("撤销标识无效")
    directory = auth_dir(registry)
    path = revocations_path(registry)
    os.makedirs(directory, exist_ok=True)
    record = {
        "jti": jti.strip(),
        "principal_id": principal_id[:128],
        "revoked_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }
    line = json.dumps(record, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
    lock = path + ".lock"
    with file_lock.acquire_lock(lock, blocking=True, timeout=5.0):
        with open(path, "a", encoding="utf-8") as handle:
            handle.write(line + "\n")
            handle.flush()
            os.fsync(handle.fileno())


def sign_token(key: bytes, payload: Mapping[str, object]) -> str:
    header = {"alg": "HS256", "typ": "MCP"}
    signing = _b64(json.dumps(header, separators=(",", ":"), sort_keys=True).encode("utf-8"))
    body = _b64(json.dumps(dict(payload), separators=(",", ":"), sort_keys=True).encode("utf-8"))
    digest = hmac.new(key, f"{signing}.{body}".encode("ascii"), hashlib.sha256).digest()
    return f"{signing}.{body}.{_b64(digest)}"


def verify_bearer(registry: Registry, authorization: str) -> AuthDecision:
    token = _bearer_value(authorization)
    if not token or not registry.token_key or not registry.token_audience:
        return AuthDecision(None, "UNAUTHENTICATED", "MALFORMED")
    if len(token) > _MAX_TOKEN_CHARS:
        return AuthDecision(None, "UNAUTHENTICATED", "MALFORMED")
    parts = token.split(".")
    if len(parts) != 3 or any(not part for part in parts):
        return AuthDecision(None, "UNAUTHENTICATED", "MALFORMED")
    try:
        header = json.loads(_b64decode(parts[0]))
        payload = json.loads(_b64decode(parts[1]))
    except (ValueError, json.JSONDecodeError, UnicodeError):
        return AuthDecision(None, "UNAUTHENTICATED", "MALFORMED")
    if not isinstance(header, dict) or header.get("alg") != "HS256":
        return AuthDecision(None, "UNAUTHENTICATED", "SIGNATURE")
    expected = hmac.new(registry.token_key, f"{parts[0]}.{parts[1]}".encode("ascii"), hashlib.sha256).digest()
    try:
        actual = _b64decode(parts[2])
    except ValueError:
        return AuthDecision(None, "UNAUTHENTICATED", "SIGNATURE")
    if not hmac.compare_digest(expected, actual):
        return AuthDecision(None, "UNAUTHENTICATED", "SIGNATURE")
    if not isinstance(payload, dict):
        return AuthDecision(None, "UNAUTHENTICATED", "MALFORMED")
    if payload.get("aud") != registry.token_audience:
        return AuthDecision(None, "UNAUTHENTICATED", "AUDIENCE")
    exp = payload.get("exp")
    if isinstance(exp, bool) or not isinstance(exp, (int, float)):
        return AuthDecision(None, "UNAUTHENTICATED", "EXPIRED")
    if time.time() > float(exp) + CLOCK_SKEW_SECONDS:
        return AuthDecision(None, "UNAUTHENTICATED", "EXPIRED")
    subject = payload.get("sub")
    jti = payload.get("jti")
    org = payload.get("org")
    if not isinstance(subject, str) or not subject.strip():
        return AuthDecision(None, "UNAUTHENTICATED", "UNKNOWN_SUBJECT")
    if not isinstance(jti, str) or not jti.strip():
        return AuthDecision(None, "UNAUTHENTICATED", "MALFORMED")
    if _jti_revoked(registry, jti):
        return AuthDecision(None, "REVOKED", "REVOKED_JTI")
    try:
        ensure_grants(registry)
        record = _grant_record(registry, subject.strip())
    except (OSError, RegistryError, AuthAdminError):
        return AuthDecision(None, "UNAUTHENTICATED", "UNKNOWN_SUBJECT")
    if record is None:
        return AuthDecision(None, "UNAUTHENTICATED", "UNKNOWN_SUBJECT")
    if record.disabled:
        return AuthDecision(None, "REVOKED", "DISABLED")
    if isinstance(org, str) and org.strip() and org.strip() != record.org_id:
        return AuthDecision(None, "UNAUTHENTICATED", "ORG")
    return AuthDecision(
        _from_record(record, jti=jti.strip(), token_scopes=_token_scopes(payload.get("scope"))),
        "OK",
        "OK",
    )


def _token_scopes(raw: object) -> frozenset:
    if isinstance(raw, str):
        names = [item for item in raw.split() if item]
    elif isinstance(raw, list):
        names = [item for item in raw if isinstance(item, str)]
    else:
        names = []
    return frozenset(name for name in names if name in KNOWN_SCOPES)


def _bearer_value(authorization: str) -> str:
    if not isinstance(authorization, str):
        return ""
    text = authorization.strip()
    if len(text) < 8 or text[:7].lower() != "bearer ":
        return ""
    return text[7:].strip()


def _grant_record(registry: Registry, principal_id: str) -> PrincipalRecord | None:
    path = grants_path(registry)
    if not os.path.isfile(path):
        return None
    try:
        with open(path, "r", encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, UnicodeError, json.JSONDecodeError):
        raise AuthAdminError("授权表无法读取") from None
    if not isinstance(data, dict) or not isinstance(data.get("principals"), list):
        raise AuthAdminError("授权表无法读取")
    for item in data["principals"]:
        if not isinstance(item, dict) or item.get("principal_id") != principal_id:
            continue
        return _principal_from_grant(item)
    return None


def _principal_from_grant(item: Mapping[str, object]) -> PrincipalRecord:
    principal_id = item.get("principal_id")
    org_id = item.get("org_id")
    grants_raw = item.get("grants")
    if not isinstance(principal_id, str) or not isinstance(org_id, str) or not isinstance(grants_raw, dict):
        raise AuthAdminError("授权表无法读取")
    grants = {}
    for project_id, scopes in grants_raw.items():
        if not isinstance(project_id, str) or not isinstance(scopes, list):
            raise AuthAdminError("授权表无法读取")
        grants[project_id] = frozenset(scope for scope in scopes if isinstance(scope, str))
    role = item.get("role", "DEV")
    if not isinstance(role, str):
        role = "DEV"
    return PrincipalRecord(
        os_user="",
        principal_id=principal_id,
        org_id=org_id,
        grants=grants,
        disabled=bool(item.get("disabled", False)),
        role=role.upper(),
    )


def _jti_revoked(registry: Registry, jti: str) -> bool:
    path = revocations_path(registry)
    if not os.path.isfile(path):
        return False
    try:
        with open(path, "r", encoding="utf-8") as handle:
            for raw in handle:
                line = raw.strip()
                if not line:
                    continue
                try:
                    item = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(item, dict) and item.get("jti") == jti and item.get("revoked_at"):
                    return True
    except OSError:
        return True
    return False


def _write_json(path: str, payload: Mapping[str, object]) -> None:
    directory = os.path.dirname(path)
    os.makedirs(directory, exist_ok=True)
    temporary = path + ".tmp"
    data = json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8")
    with open(temporary, "wb") as handle:
        handle.write(data)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)


def _b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _b64decode(text: str) -> bytes:
    pad = "=" * (-len(text) % 4)
    return base64.urlsafe_b64decode(text + pad)
