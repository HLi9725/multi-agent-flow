# -*- coding: utf-8 -*-
"""复用 EvidenceStore 的脱敏规则，并裁剪响应白名单。"""

from __future__ import annotations

import json
from typing import Any, Dict, Iterable, Tuple

from ..core.evidence_schema import EvidenceSecurityError
from ..core.evidence_store import EvidenceStore
from .schema import AUDIT_LINE_MAX_BYTES, RESPONSE_MAX_BYTES, TEXT_FIELD_MAX_BYTES

_FORBIDDEN_KEYS = {
    "data_root",
    "board_file",
    "board_config",
    "evidence_root",
    "cursor_secret",
    "registry",
    "workspace",
    "path",
    "authorization",
    "cookie",
    "password",
    "secret",
    "api_key",
    "api-key",
    "private_key",
    "private-key",
    "access_token",
    "refresh_token",
    "id_token",
    "session_token",
    "github_token",
    "invocation_token",
    "credentials",
}


class RedactionRejected(Exception):
    """脱敏规则要求拒绝而不是改写后继续。"""


def _masker() -> EvidenceStore:
    return EvidenceStore.__new__(EvidenceStore)


def mask_text(text: str) -> str:
    try:
        return _masker()._mask_text(text)
    except EvidenceSecurityError as exc:
        raise RedactionRejected(str(exc.__class__.__name__)) from None


def mask_data(data: Any) -> Any:
    try:
        return _masker()._mask_secrets(data)
    except EvidenceSecurityError as exc:
        raise RedactionRejected(str(exc.__class__.__name__)) from None


def truncate_text(text: str) -> Tuple[str, bool, int]:
    raw = text.encode("utf-8")
    original = len(raw)
    if original <= TEXT_FIELD_MAX_BYTES:
        return text, False, original
    cut = raw[:TEXT_FIELD_MAX_BYTES]
    while cut:
        try:
            return cut.decode("utf-8"), True, original
        except UnicodeDecodeError:
            cut = cut[:-1]
    return "", True, original


def prepare_text(text: str) -> Tuple[str, bool, int]:
    return truncate_text(mask_text(text))


def scrub_mapping(payload: Dict[str, Any], allowed: Iterable[str]) -> Dict[str, Any]:
    allow = set(allowed)
    cleaned: Dict[str, Any] = {}
    for key in allow:
        if key not in payload:
            continue
        if str(key).lower() in _FORBIDDEN_KEYS:
            continue
        cleaned[key] = payload[key]
    return cleaned


def strip_forbidden(value: Any) -> Any:
    if isinstance(value, dict):
        out = {}
        for key, item in value.items():
            if str(key).lower() in _FORBIDDEN_KEYS or "token" in str(key).lower():
                continue
            if str(key).lower() in {"data_root", "board_file", "workspace", "path"}:
                continue
            out[key] = strip_forbidden(item)
        return out
    if isinstance(value, list):
        return [strip_forbidden(item) for item in value]
    return value


def scrub_literals(value: Any, literals: Iterable[str]) -> Any:
    banned = [item for item in literals if isinstance(item, str) and len(item) >= 4]
    if isinstance(value, str):
        redacted = value
        for item in banned:
            if item in redacted:
                redacted = redacted.replace(item, "***MASKED***")
        return redacted
    if isinstance(value, dict):
        return {key: scrub_literals(item, banned) for key, item in value.items()}
    if isinstance(value, list):
        return [scrub_literals(item, banned) for item in value]
    return value


def fit_response(payload: Dict[str, Any]) -> Dict[str, Any]:
    encoded = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    if len(encoded) <= RESPONSE_MAX_BYTES:
        return payload
    return {"truncated": True, "summary": "响应超过长度上限"}


def fit_audit_line(record: Dict[str, Any]) -> str:
    line = json.dumps(record, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
    raw = line.encode("utf-8")
    if len(raw) <= AUDIT_LINE_MAX_BYTES:
        return line
    record = {
        "ts": record.get("ts", ""),
        "principal_id": record.get("principal_id", ""),
        "project_id": record.get("project_id", ""),
        "tool": record.get("tool", ""),
        "code": record.get("code", ""),
        "truncated": True,
    }
    line = json.dumps(record, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
    raw = line.encode("utf-8")
    if len(raw) <= AUDIT_LINE_MAX_BYTES:
        return line
    return raw[:AUDIT_LINE_MAX_BYTES].decode("utf-8", errors="ignore")
