# -*- coding: utf-8 -*-
"""只读工具实现。唯一允许调用领域读取函数的模块。"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import time
from typing import Any, Dict, List, Mapping, Tuple

from ..boards.offline_board_adapter import OfflineBoardAdapter
from ..core.workflow_policy import PROFILE_STANDARD, PolicyError, load_policy_file
from ..core.file_lock import LockBusyError
from . import auth
from .audit_store import AuditStore, AuditWriteError
from .errors import McpError, error
from .redaction import (
    RedactionRejected,
    fit_response,
    mask_text,
    prepare_text,
    scrub_literals,
    scrub_mapping,
    strip_forbidden,
)
from .registry import ProjectRecord, Registry, assert_registered_path
from .schema import (
    LIST_FIELDS,
    PAGE_DEFAULT,
    PROCESS_NODE_MAX,
    PROJECT_FIELDS,
    SCHEMA_VERSION,
    STATUS_FIELDS,
    TASK_FIELDS,
    TASK_META_FIELDS,
    TOOL_SCOPE,
    WRITE_TOOL_NAMES,
    tool_specs,
    validate_arguments,
)

try:
    from enums import TaskStatus
except ImportError:  # 以仓库根为路径导入时
    from scripts.enums import TaskStatus

_PATH_KEYS = {"path", "data_root", "board_file", "workspace"}
_DROP_KEYS = {"user_id", "tenant_id", "acceptance_confirmed"}
_ORG_KEYS = {"org", "org_id"}
_NODE_SPLIT = re.compile(r"(?=\[T\d+-N\d+\])")
_ABS_RE = re.compile(r"^(?:[A-Za-z]:[\\/]|\\\\|//)")


def handle_tool(name: str, arguments: Any, context: Mapping[str, Any]) -> Dict[str, Any]:
    registry: Registry = context["registry"]
    audit: AuditStore = context["audit"]
    principal = auth.resolve_request_principal(registry, context)
    principal_id = principal.principal_id if principal else ""
    project_id = ""
    task_id = ""
    if name in WRITE_TOOL_NAMES and not audit.chain_intact():
        return {
            "isError": True,
            "structured": error("GATE_REJECTED", details={"reason": "AUDIT_GAP"}).body(),
        }
    try:
        if name in WRITE_TOOL_NAMES:
            payload = _dispatch_write(name, arguments, registry, principal, context)
        else:
            payload = _dispatch(name, arguments, registry, principal)
        if payload.get("_direct"):
            body = payload["body"]
            if payload.get("_audit_handled") or payload.get("_audited"):
                return {"isError": False, "structured": body}
            audit.append(
                principal_id=principal_id,
                project_id=str(payload.get("_project_id") or ""),
                tool=name,
                code=str(body.get("code") or "OK"),
                task_id=str(payload.get("_task_id") or body.get("task_id") or ""),
                arguments=_argument_view(arguments),
            )
            return {"isError": False, "structured": body}
        project_id = str(payload.pop("_project_id", "") or "")
        task_id = str(payload.pop("_task_id", "") or "")
        missing = int(payload.pop("_missing_ids", 0) or 0)
        if missing:
            note_missing_ids(audit, project_id, principal_id, missing)
        body = _finalize(payload, registry, project_id)
        audit.append(
            principal_id=principal_id,
            project_id=project_id,
            tool=name,
            code="OK",
            task_id=task_id,
            arguments=_argument_view(arguments),
        )
        return {"isError": False, "structured": body}
    except McpError as exc:
        project_id, task_id = _ids_from_arguments(arguments)
        audit.append(
            principal_id=principal_id,
            project_id=project_id,
            tool=str(name),
            code=exc.code,
            task_id=task_id,
            arguments=_argument_view(arguments),
            diagnostic=str((exc.details or {}).get("reason") or ""),
        )
        return {"isError": exc.is_error, "structured": exc.body()}


def _dispatch(name: str, arguments: Any, registry: Registry, principal: auth.AuthContext | None) -> Dict[str, Any]:
    specs = {item["name"]: item for item in tool_specs(TaskStatus.all_values())}
    if name not in specs:
        raise error("VALIDATION", details={"reason": "UNKNOWN_TOOL", "field": "name"})
    if not isinstance(arguments, dict):
        raise error("VALIDATION", details={"reason": "BAD_TYPE", "field": "arguments"})
    _reject_path(arguments)
    _reject_invocation_token(arguments)
    claimed_org, cleaned = _strip_transport_fields(arguments)
    validate_arguments(specs[name], cleaned)
    if principal is None:
        raise error("UNAUTHENTICATED")
    if principal.disabled:
        raise error("REVOKED")
    project_id = cleaned["project_id"]
    project = registry.get_project(project_id)
    if project is None:
        raise error("UNKNOWN_PROJECT")
    if claimed_org is not None and claimed_org != project.org_id:
        raise error("FORBIDDEN", details={"reason": "ORG"})
    if principal.org_id != project.org_id:
        raise error("FORBIDDEN", details={"reason": "ORG"})
    scopes = auth.scopes_for(principal, project.project_id)
    if "read" not in scopes:
        raise error("FORBIDDEN")
    if name == "get_project":
        return _get_project(project, registry)
    if name == "get_task":
        return _get_task(project, cleaned, scopes)
    if name == "list_tasks":
        return _list_tasks(project, registry, cleaned, scopes)
    return _get_status(project, cleaned)


def _get_project(project: ProjectRecord, registry: Registry) -> Dict[str, Any]:
    return {
        "_project_id": project.project_id,
        "project_id": project.project_id,
        "display_name": project.display_name,
        "org_id": project.org_id,
        "board_kind": project.board_kind,
        "schema_version": SCHEMA_VERSION,
        "server_revision": registry.server_revision,
    }


def _get_task(project: ProjectRecord, arguments: Mapping[str, Any], scopes: frozenset) -> Dict[str, Any]:
    _require_offline(project)
    include_deleted = bool(arguments.get("include_deleted", False))
    if include_deleted and "admin" not in scopes:
        raise error("FORBIDDEN", details={"reason": "INCLUDE_DELETED"})
    task_id = arguments["task_id"]
    card, board_version = _read_card_stable(project, task_id)
    if card is None or (card.get("is_deleted") and not include_deleted):
        raise error("NOT_FOUND")
    public = _public_task(card, include_process=bool(arguments.get("include_process", False)))
    public["board_version"] = board_version
    public["_project_id"] = project.project_id
    public["_task_id"] = task_id
    return public


def _get_status(project: ProjectRecord, arguments: Mapping[str, Any]) -> Dict[str, Any]:
    _require_offline(project)
    task_id = arguments["task_id"]
    card, board_version = _read_card_stable(project, task_id)
    if card is None or card.get("is_deleted"):
        raise error("NOT_FOUND")
    return {
        "_project_id": project.project_id,
        "_task_id": task_id,
        "task_id": task_id,
        "status": "" if card.get("status") is None else str(card.get("status")),
        "assignee": "" if card.get("assignee") is None else str(card.get("assignee")),
        "task_version": task_version(card),
        "board_version": board_version,
        "profile_id": _profile_id(project, task_id),
    }


def _list_tasks(
    project: ProjectRecord,
    registry: Registry,
    arguments: Mapping[str, Any],
    scopes: frozenset,
) -> Dict[str, Any]:
    _require_offline(project)
    include_deleted = bool(arguments.get("include_deleted", False))
    if include_deleted and "admin" not in scopes:
        raise error("FORBIDDEN", details={"reason": "INCLUDE_DELETED"})
    limit = int(arguments.get("limit", PAGE_DEFAULT))
    status = arguments.get("status")
    assignee = arguments.get("assignee")
    filter_digest = _filter_digest(status, assignee, include_deleted)
    cursor_id = ""
    cursor_version = ""
    raw_cursor = arguments.get("cursor") or ""
    if raw_cursor:
        cursor_body = _open_cursor(registry, raw_cursor)
        if cursor_body.get("filter") != filter_digest:
            raise error("VALIDATION", details={"reason": "FILTER_MISMATCH", "field": "cursor"})
        cursor_id = str(cursor_body.get("id") or "")
        cursor_version = str(cursor_body.get("board_version") or "")
    version_before = _board_version(project)
    cards, missing_ids = _collect_cards(
        project,
        status=status,
        assignee=assignee,
        include_deleted=include_deleted,
    )
    version_after = _board_version(project)
    stale = bool(cursor_version) and cursor_version != version_after
    if version_before != version_after:
        # 读取前后版本不一致时，丢弃可能被拼接过的窗口，只保留单独一次新快照。
        cards, missing_ids = _collect_cards(
            project,
            status=status,
            assignee=assignee,
            include_deleted=include_deleted,
        )
        version_after = _board_version(project)
        stale = True
    ordered = sorted(cards, key=_id_sort_key)
    if cursor_id:
        cursor_key = _id_sort_key({"id": cursor_id})
        ordered = [card for card in ordered if _id_sort_key(card) > cursor_key]
    page = ordered[:limit]
    has_more = len(ordered) > limit
    items = [_public_task(card, include_process=False) for card in page]
    next_cursor = ""
    if has_more and page:
        next_cursor = _sign_cursor(
            registry,
            {
                "id": str(page[-1].get("id")),
                "filter": filter_digest,
                "board_version": version_after,
            },
        )
    body = {
        "_project_id": project.project_id,
        "items": items,
        "next_cursor": next_cursor,
        "has_more": has_more,
        "board_version": version_after,
        "stale": stale,
        "_missing_ids": len(missing_ids),
    }
    return body


def _collect_cards(
    project: ProjectRecord,
    *,
    status: str | None,
    assignee: str | None,
    include_deleted: bool = False,
) -> Tuple[List[Dict[str, Any]], List[str]]:
    adapter = _adapter(project)
    conditions = []
    if status:
        conditions.append({"field_name": "status", "operator": "is", "value": [status]})
    if assignee:
        conditions.append({"field_name": "assignee", "operator": "is", "value": [assignee]})
    filter_json = {"conditions": conditions} if conditions else None
    collected = adapter.list_records_readonly(
        filter_json=filter_json,
        limit=None,
        offset=0,
        include_deleted=include_deleted,
    )
    cards: List[Dict[str, Any]] = []
    missing: List[str] = []
    seen = set()
    for item in collected:
        fields = item.get("fields") if isinstance(item, dict) else None
        if not isinstance(fields, dict):
            missing.append("missing")
            continue
        task_id = fields.get("id")
        if task_id is None or str(task_id).strip() == "":
            missing.append("missing")
            continue
        identity = str(task_id)
        if identity in seen:
            continue
        seen.add(identity)
        cards.append(fields)
    return cards, missing


def _read_card_stable(project: ProjectRecord, task_id: str) -> Tuple[Dict[str, Any] | None, str]:
    """单卡与看板版本来自同一次稳定读取。版本变化时丢弃第一次结果并再读一次。"""
    version_before = _board_version(project)
    card = _read_card(project, task_id)
    version_after = _board_version(project)
    if version_before != version_after:
        card = _read_card(project, task_id)
        version_after = _board_version(project)
    return card, version_after


def _read_card(project: ProjectRecord, task_id: str) -> Dict[str, Any] | None:
    adapter = _adapter(project)
    record = adapter.get_record_readonly(task_id)
    if not isinstance(record, dict):
        return None
    fields = record.get("fields")
    if not isinstance(fields, dict):
        return None
    return fields


def _adapter(project: ProjectRecord) -> OfflineBoardAdapter:
    assert_registered_path(project, project.board_file)
    real = os.path.realpath(project.board_file)
    assert_registered_path(project, real)
    return OfflineBoardAdapter(board_file=real)


def _require_offline(project: ProjectRecord) -> None:
    if project.board_kind != "offline":
        raise error("VALIDATION", details={"reason": "BOARD_KIND"})


def _board_version(project: ProjectRecord) -> str:
    assert_registered_path(project, project.board_file)
    try:
        with open(project.board_file, "rb") as handle:
            raw = handle.read()
    except OSError:
        raw = b""
    return hashlib.sha256(raw).hexdigest()


def _profile_id(project: ProjectRecord, task_id: str) -> str:
    policy_path = os.path.realpath(os.path.join(project.data_root, "workflow_policies", f"{task_id}.json"))
    assert_registered_path(project, policy_path)
    if not os.path.isfile(policy_path):
        return PROFILE_STANDARD
    try:
        policy = load_policy_file(project.data_root, task_id)
    except (OSError, PolicyError, json.JSONDecodeError, ValueError):
        raise error("VALIDATION", details={"reason": "POLICY_UNREADABLE"}) from None
    if not isinstance(policy, dict):
        return PROFILE_STANDARD
    profile = policy.get("profile_id")
    if not isinstance(profile, str) or not profile.strip():
        return PROFILE_STANDARD
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,31}", profile):
        return PROFILE_STANDARD
    return profile


def task_version(card: Mapping[str, Any]) -> str:
    payload: Dict[str, Any] = {}
    for key in ("id", "status", "assignee", "owner", "name", "is_deleted", "remarks"):
        if key in card:
            payload[key] = card[key]
    if "end_time" in card:
        payload["end_time"] = card["end_time"]
    elif "end_date" in card:
        payload["end_date"] = card["end_date"]
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def _public_task(card: Mapping[str, Any], *, include_process: bool) -> Dict[str, Any]:
    body: Dict[str, Any] = {
        "id": str(card.get("id")),
        "task_version": task_version(card),
        "is_deleted": bool(card.get("is_deleted")),
    }
    if "name" in card:
        body["name"] = _text_field(card.get("name"))
    if "status" in card and card.get("status") is not None:
        body["status"] = _plain(card.get("status"))
    if "assignee" in card and card.get("assignee") is not None:
        body["assignee"] = _plain(card.get("assignee"))
    if "owner" in card and card.get("owner") is not None:
        body["owner"] = _plain(card.get("owner"))
    if "type" in card and card.get("type") is not None:
        body["type"] = _plain(card.get("type"))
    start = card.get("start_time", card.get("start_date"))
    if start is not None and ("start_time" in card or "start_date" in card):
        body["start_time"] = _plain(start)
    end = card.get("end_time") if "end_time" in card else card.get("end_date") if "end_date" in card else None
    if end is not None:
        body["end_time"] = _plain(end)
    truncated = False
    original_length = 0
    if "remarks" in card and card.get("remarks") is not None:
        text, cut, length = _prepare_any(card.get("remarks"))
        body["remarks"] = text
        truncated = truncated or cut
        original_length = max(original_length, length)
    if include_process and card.get("process"):
        nodes = _process_nodes(card.get("process"))
        prepared = []
        for node in nodes[-PROCESS_NODE_MAX:]:
            text, cut, length = prepare_text(node)
            prepared.append(text)
            truncated = truncated or cut
            original_length = max(original_length, length)
        body["process"] = prepared
    if truncated:
        body["truncated"] = True
        body["original_length"] = original_length
    allowed = list(TASK_FIELDS) + list(TASK_META_FIELDS)
    return scrub_mapping(body, allowed)


def _process_nodes(process: Any) -> List[str]:
    text = process if isinstance(process, str) else json.dumps(process, ensure_ascii=False)
    parts = [part.strip() for part in _NODE_SPLIT.split(text) if part.strip()]
    if not parts:
        return [text]
    return parts


def _text_field(value: Any) -> str:
    text, _cut, _length = _prepare_any(value)
    return text


def _prepare_any(value: Any) -> Tuple[str, bool, int]:
    if isinstance(value, str):
        text = value
    else:
        text = json.dumps(value, ensure_ascii=False, default=str)
    try:
        return prepare_text(text)
    except RedactionRejected:
        raise error("VALIDATION", details={"reason": "REDACTION_REJECTED"}) from None


def _plain(value: Any) -> str:
    text = value if isinstance(value, str) else str(value)
    try:
        masked = mask_text(text)
    except RedactionRejected:
        raise error("VALIDATION", details={"reason": "REDACTION_REJECTED"}) from None
    return masked


def _finalize(payload: Dict[str, Any], registry: Registry, project_id: str) -> Dict[str, Any]:
    payload.pop("_missing_ids", None)
    custom = payload.pop("_allow", None)
    project = registry.get_project(project_id) if project_id else None
    literals = [registry.path]
    if project is not None:
        literals.extend([project.data_root, project.board_file, project.evidence_root, project.board_config])
    if custom:
        allowed = list(custom)
    elif "items" in payload:
        payload["items"] = [scrub_mapping(item, list(TASK_FIELDS) + ["truncated", "original_length"]) for item in payload["items"]]
        allowed = LIST_FIELDS
    elif "task_id" in payload and "profile_id" in payload:
        allowed = STATUS_FIELDS
    elif "project_id" in payload and "schema_version" in payload:
        allowed = PROJECT_FIELDS
    else:
        allowed = list(TASK_FIELDS) + list(TASK_META_FIELDS)
    body = scrub_mapping(payload, allowed)
    body = strip_forbidden(body)
    body = scrub_literals(body, literals)
    if not isinstance(body, dict):
        body = {"truncated": True, "summary": "响应超过长度上限"}
    return fit_response(body)


def _sign_cursor(registry: Registry, body: Mapping[str, Any]) -> str:
    raw = json.dumps(body, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    digest = hmac.new(registry.cursor_key, raw, hashlib.sha256).hexdigest()
    token = _b64(raw) + "." + digest
    return token


def _open_cursor(registry: Registry, token: str) -> Dict[str, Any]:
    if not isinstance(token, str) or "." not in token:
        raise error("VALIDATION", details={"reason": "CURSOR", "field": "cursor"})
    encoded, digest = token.rsplit(".", 1)
    try:
        raw = _b64decode(encoded)
    except (ValueError, UnicodeError):
        raise error("VALIDATION", details={"reason": "CURSOR", "field": "cursor"}) from None
    expected = hmac.new(registry.cursor_key, raw, hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, digest):
        raise error("VALIDATION", details={"reason": "CURSOR", "field": "cursor"})
    try:
        body = json.loads(raw.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError):
        raise error("VALIDATION", details={"reason": "CURSOR", "field": "cursor"}) from None
    if not isinstance(body, dict) or "id" not in body or "filter" not in body:
        raise error("VALIDATION", details={"reason": "CURSOR", "field": "cursor"})
    return body


def _b64(raw: bytes) -> str:
    import base64

    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _b64decode(text: str) -> bytes:
    import base64

    pad = "=" * (-len(text) % 4)
    return base64.urlsafe_b64decode(text + pad)


def _filter_digest(status: Any, assignee: Any, include_deleted: bool) -> str:
    raw = json.dumps(
        {"assignee": assignee or "", "include_deleted": include_deleted, "status": status or ""},
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def _id_sort_key(card: Mapping[str, Any]) -> Tuple[int, str]:
    text = str(card.get("id") or "")
    match = re.fullmatch(r"T(\d+)", text)
    if match:
        return (int(match.group(1)), text)
    return (10**12, text)


def _reject_path(value: Any, key: str | None = None) -> None:
    if key is not None and key.lower() in _PATH_KEYS:
        raise error("PATH_REJECTED", details={"reason": "PATH", "field": key.lower()})
    if isinstance(value, str):
        if _is_absolute(value) or _has_dotdot(value):
            raise error("PATH_REJECTED", details={"reason": "PATH", "field": key or "value"})
        return
    if isinstance(value, dict):
        for child_key, child in value.items():
            _reject_path(child, str(child_key))
        return
    if isinstance(value, list):
        for child in value:
            _reject_path(child, key)


def _is_absolute(value: str) -> bool:
    if _ABS_RE.match(value):
        return True
    if value.startswith("/") or value.startswith("\\"):
        return True
    return os.path.isabs(value)


def _has_dotdot(value: str) -> bool:
    return ".." in re.split(r"[\\/]", value)


def _reject_invocation_token(value: Any, key: str | None = None) -> None:
    if key is not None and "invocation_token" in key.lower():
        raise error("VALIDATION", details={"reason": "INVOCATION_TOKEN", "field": "invocation_token"})
    if isinstance(value, str):
        if "invocation_token" in value.lower():
            raise error("VALIDATION", details={"reason": "INVOCATION_TOKEN", "field": "invocation_token"})
        return
    if isinstance(value, dict):
        for child_key, child in value.items():
            _reject_invocation_token(child, str(child_key))
        return
    if isinstance(value, list):
        for child in value:
            _reject_invocation_token(child, key)


def _strip_transport_fields(arguments: Mapping[str, Any]) -> Tuple[str | None, Dict[str, Any]]:
    cleaned = dict(arguments)
    claimed = None
    for key in list(cleaned):
        if key in _DROP_KEYS:
            cleaned.pop(key, None)
        elif key in _ORG_KEYS:
            value = cleaned.pop(key)
            if isinstance(value, str):
                claimed = value
    return claimed, cleaned


def _ids_from_arguments(arguments: Any) -> Tuple[str, str]:
    if not isinstance(arguments, dict):
        return "", ""
    project_id = arguments.get("project_id")
    task_id = arguments.get("task_id")
    project_text = project_id if isinstance(project_id, str) and len(project_id) <= 64 else ""
    task_text = task_id if isinstance(task_id, str) and len(task_id) <= 64 else ""
    if _is_absolute(project_text) or _has_dotdot(project_text):
        project_text = ""
    if _is_absolute(task_text) or _has_dotdot(task_text):
        task_text = ""
    return project_text, task_text


def _argument_view(arguments: Any) -> Dict[str, Any]:
    if not isinstance(arguments, dict):
        return {}
    view = {}
    for key, value in arguments.items():
        if key.lower() in _PATH_KEYS or key.lower() in _DROP_KEYS or "token" in key.lower():
            view[key] = "***MASKED***"
        else:
            view[key] = value
    return view


def _dispatch_write(
    name: str,
    arguments: Any,
    registry: Registry,
    principal: auth.AuthContext | None,
    context: Mapping[str, Any],
) -> Dict[str, Any]:
    specs = {item["name"]: item for item in tool_specs(TaskStatus.all_values())}
    if not isinstance(arguments, dict):
        raise error("VALIDATION", details={"reason": "BAD_TYPE", "field": "arguments"})
    if name == "transition_task" and arguments.get("to_status") == "已验收":
        raise error("ACCEPTANCE_REQUIRED")
    _reject_path(arguments)
    _reject_invocation_token(arguments)
    claimed_org, cleaned = _strip_transport_fields(arguments)
    validate_arguments(specs[name], cleaned)
    if principal is None:
        raise error("UNAUTHENTICATED")
    if principal.disabled:
        raise error("REVOKED")
    project_id = cleaned["project_id"]
    project = registry.get_project(project_id)
    if project is None:
        raise error("UNKNOWN_PROJECT")
    if claimed_org is not None and claimed_org != project.org_id:
        raise error("FORBIDDEN", details={"reason": "ORG"})
    if principal.org_id != project.org_id:
        raise error("FORBIDDEN", details={"reason": "ORG"})
    scopes = auth.scopes_for(principal, project.project_id)
    if TOOL_SCOPE[name] not in scopes:
        raise error("FORBIDDEN")
    digest = _request_digest(cleaned)
    ledger = _ledger(project)
    decision = ledger.start(
        principal_id=principal.principal_id,
        tool=name,
        key=cleaned["idempotency_key"],
        digest=digest,
        target=_intent_target(name, cleaned, project),
        fact=lambda record: _fact_reached(project, name, record),
    )
    if decision["action"] == "replay":
        body = dict(decision.get("result") or {})
        if decision.get("audit_pending"):
            return _settle_write_audit(name, cleaned, arguments, project, principal,
                                       context["audit"], ledger, digest, body, replayed=True)
        body["audit_status"] = "COMPLETE"
        body["replayed"] = True
        body["code"] = "IDEMPOTENCY_REPLAY"
        return {"_direct": True, "_project_id": project.project_id, "_task_id": str(body.get("task_id") or cleaned.get("task_id") or ""), "body": body}
    if decision["action"] == "conflict":
        raise error("CONFLICT", details={"reason": str(decision.get("reason") or "DIGEST")})
    if decision["action"] == "unbound":
        raise error("CONFLICT", details={"reason": "UNBOUND"})
    if decision["action"] == "in_progress":
        raise error("CONFLICT", details={"reason": "IN_PROGRESS"}, retryable=True)
    audit: AuditStore = context["audit"]

    def _audit(code: str, task_id: str) -> None:
        audit.append(
            principal_id=principal.principal_id,
            project_id=project.project_id,
            tool=name,
            code=code,
            task_id=task_id,
            arguments=_argument_view(arguments),
            required=True,
        )

    try:
        _audit("PENDING", str(cleaned.get("task_id") or ""))
    except AuditWriteError:
        ledger.fail(
            principal_id=principal.principal_id,
            tool=name,
            key=cleaned["idempotency_key"],
            digest=digest,
            code="GATE_REJECTED",
        )
        raise error("GATE_REJECTED", details={"reason": "AUDIT"}) from None
    _test_hold(context)
    business_applied = False
    try:
        payload = _execute_write(
            name,
            cleaned,
            project,
            principal,
            scopes,
            context,
            ledger,
            digest,
            decision.get("target") if isinstance(decision.get("target"), dict) else {},
        )
        business_applied = True
        task_id = str(payload.get("_task_id") or cleaned.get("task_id") or "")
        body = _finalize(payload, registry, project.project_id)
        # 持久化 APPLIED 后再补终态审计；任何后续故障都不能重执已应用的业务。
        try:
            ledger.applied(principal_id=principal.principal_id, tool=name,
                           key=cleaned["idempotency_key"], digest=digest, result=body)
        except (OSError, LockBusyError):
            # 已存在持久化 PENDING/target。只允许事实对账恢复，不能标记可重执失败。
            response = dict(body)
            response.update(code="APPLIED_LEDGER_PENDING", business_status="APPLIED",
                            reconciliation_status="PENDING", audit_status="PENDING")
            return {"_direct": True, "_audit_handled": True, "_audited": False,
                    "_project_id": project.project_id, "_task_id": task_id, "body": response}
    except McpError as exc:
        if not business_applied:
            ledger.fail(
                principal_id=principal.principal_id,
                tool=name,
                key=cleaned["idempotency_key"],
                digest=digest,
                code=exc.code,
            )
        raise
    return _settle_write_audit(name, cleaned, arguments, project, principal,
                               audit, ledger, digest, body)


def _settle_write_audit(name, cleaned, arguments, project, principal, audit, ledger,
                        digest, body, *, replayed=False):
    """同键恢复补审计。业务结果与审计状态分开报告，终态事件可幂等追加。"""
    task_id = str(body.get("task_id") or cleaned.get("task_id") or "")
    event_id = hashlib.sha256(
        f"{project.project_id}|{principal.principal_id}|{name}|{cleaned['idempotency_key']}|{digest}".encode("utf-8")
    ).hexdigest()
    response = dict(body)
    completed = False
    try:
        audit.append(principal_id=principal.principal_id, project_id=project.project_id,
                     tool=name, code="OK", task_id=task_id,
                     arguments=_argument_view(arguments), required=True, event_id=event_id)
    except AuditWriteError:
        response.update(audit_status="PENDING", business_status="APPLIED",
                        code="APPLIED_AUDIT_PENDING")
        audited = False
    else:
        response["audit_status"] = "COMPLETE"
        audited = True
        try:
            ledger.complete(principal_id=principal.principal_id, tool=name,
                            key=cleaned["idempotency_key"], digest=digest,
                            result=body, audit_event_id=event_id)
        except (OSError, LockBusyError):
            response.update(code="APPLIED_LEDGER_PENDING", business_status="APPLIED",
                            reconciliation_status="PENDING")
        else:
            completed = True
    if replayed:
        response["replayed"] = True
        if completed:
            response["code"] = "IDEMPOTENCY_REPLAY"
    return {"_direct": True, "_audit_handled": True, "_audited": audited,
            "_project_id": project.project_id, "_task_id": task_id, "body": response}


def _test_hold(context: Mapping[str, Any]) -> None:
    raw = context.get("test_hold_seconds")
    if raw is None:
        return
    try:
        seconds = float(raw)
    except (TypeError, ValueError):
        return
    if seconds <= 0:
        return
    time.sleep(min(seconds, 5.0))


def _execute_write(
    name: str,
    cleaned: Mapping[str, Any],
    project: ProjectRecord,
    principal: auth.AuthContext,
    scopes: frozenset,
    context: Mapping[str, Any],
    ledger: Any,
    digest: str,
    bound: Mapping[str, Any],
) -> Dict[str, Any]:
    if name == "create_task":
        return _create_task(cleaned, project, principal, scopes, ledger, digest, bound)
    if name == "submit_evidence":
        return _submit_evidence(cleaned, project, principal, context, ledger, digest)
    if name == "transition_task":
        return _transition_task(cleaned, project, principal)
    if name == "request_acceptance":
        return _request_acceptance(cleaned, project, principal, context, ledger, digest)
    if name == "soft_delete":
        return _set_visibility(cleaned, project, deleted=True)
    return _set_visibility(cleaned, project, deleted=False)


def _create_task(
    cleaned: Mapping[str, Any],
    project: ProjectRecord,
    principal: auth.AuthContext,
    scopes: frozenset,
    ledger: Any,
    digest: str,
    bound: Mapping[str, Any],
) -> Dict[str, Any]:
    import contextlib
    import io

    from scripts.transition_task import normalize_role_name, transition_task_pipeline

    role = principal.role
    assignee = str(cleaned["assignee"])
    if role != "PM" and normalize_role_name(assignee) != normalize_role_name(role):
        raise error("FORBIDDEN", details={"reason": "ROLE"})
    if cleaned.get("force_duplicate") and "admin" not in scopes:
        raise error("FORBIDDEN", details={"reason": "FORCE"})
    created: Dict[str, Any] = {}

    def run() -> tuple:
        sink = io.StringIO()
        with contextlib.redirect_stdout(sink):
            ok = transition_task_pipeline(
                config_path=project.board_config or None,
                current_role=role,
                assignee=assignee,
                task_name=str(cleaned["name"]),
                task_type=str(cleaned["type"]),
                stage=cleaned.get("stage"),
                wp=cleaned.get("wp"),
                wbs=cleaned.get("wbs"),
                owner=cleaned.get("owner"),
                task_id=str(bound.get("task_id") or ""),
                create_only=True,
                force=bool(cleaned.get("force_duplicate")) and "admin" in scopes,
                created_state=created,
            )
        return ok, sink.getvalue()

    ok, captured = _call_in_project(project, run)
    if not ok:
        if "DUPLICATE_TASK" in captured:
            raise error("CONFLICT", details={"reason": "DUPLICATE_TASK"})
        raise error("GATE_REJECTED", details={"reason": "CREATE"})
    task_id = str(created.get("task_id") or bound.get("task_id") or "")
    if not task_id:
        raise error("GATE_REJECTED", details={"reason": "CREATE"})
    ledger.bind(
        principal_id=principal.principal_id,
        tool="create_task",
        key=str(cleaned["idempotency_key"]),
        digest=digest,
        target={"task_id": task_id, "status": "待开始", "assignee": assignee},
    )
    card = _read_card(project, task_id) or {}
    return {
        "_project_id": project.project_id,
        "_task_id": task_id,
        "_allow": ["task_id", "status", "task_version", "replayed", "code"],
        "task_id": task_id,
        "status": "待开始",
        "task_version": task_version(card),
    }


def _transition_task(cleaned: Mapping[str, Any], project: ProjectRecord, principal: auth.AuthContext) -> Dict[str, Any]:
    import contextlib
    import io

    from scripts.transition_task import transition_task_pipeline

    if cleaned.get("to_status") == "已验收":
        raise error("ACCEPTANCE_REQUIRED")
    version_state: Dict[str, Any] = {}

    def run() -> tuple:
        sink = io.StringIO()
        with contextlib.redirect_stdout(sink):
            ok = transition_task_pipeline(
                config_path=project.board_config or None,
                task_id=str(cleaned["task_id"]),
                current_role=principal.role,
                from_status=str(cleaned["from_status"]),
                to_status=str(cleaned["to_status"]),
                assignee=str(cleaned["assignee"]),
                task_type=_stored_task_type(project, str(cleaned["task_id"]), str(cleaned["task_type"])),
                end_time=cleaned.get("end_time"),
                remarks=cleaned.get("remarks"),
                runner_evidence_id=cleaned.get("evidence_id"),
                expected_card_version=str(cleaned["expected_version"]),
                version_state=version_state,
            )
        return ok, sink.getvalue()

    ok, captured = _call_in_project(project, run)
    if version_state.get("conflict"):
        raise error(
            "CONFLICT",
            details={"reason": "VERSION", "task_version": version_state.get("task_version")},
        )
    if not ok:
        raise error("GATE_REJECTED", details={"reason": _gate_reason(captured)})
    card = _read_card(project, str(cleaned["task_id"])) or {}
    body = {
        "_project_id": project.project_id,
        "_task_id": str(cleaned["task_id"]),
        "_allow": ["task_id", "status", "assignee", "task_version", "evidence_id", "replayed", "code"],
        "task_id": str(cleaned["task_id"]),
        "status": "" if card.get("status") is None else str(card.get("status")),
        "assignee": "" if card.get("assignee") is None else str(card.get("assignee")),
        "task_version": task_version(card),
    }
    if cleaned.get("evidence_id"):
        body["evidence_id"] = str(cleaned["evidence_id"])
    return body


_CONFIRMATION_MODEL_KEYS = {"confirmation_id", "is_real_host"}


def _transport_capability_values(confirmed: bool) -> Dict[str, Any]:
    from scripts._lib.core.agent_schema import HostCapabilities

    caps = HostCapabilities(is_real_host=bool(confirmed))
    plain: Dict[str, Any] = {}
    for key, value in caps.__dict__.items():
        if key == "extra":
            continue
        plain[key] = value.value if hasattr(value, "value") else value
    return plain


def _transport_capabilities(confirmed: bool) -> Any:
    class _Caps:
        pass

    holder = _Caps()
    for key, value in _transport_capability_values(confirmed).items():
        setattr(holder, key, value)
    holder.extra = {}
    return holder


def _server_confirmation_id(project: ProjectRecord, task_id: str) -> str:
    import glob

    pattern = os.path.join(project.data_root, "runner_checkpoints", "*", task_id + ".json")
    matches = glob.glob(pattern)
    if len(matches) == 1:
        try:
            with open(matches[0], "r", encoding="utf-8") as handle:
                data = json.load(handle)
        except (OSError, json.JSONDecodeError):
            data = {}
        if isinstance(data, dict):
            found = data.get("confirmation_request_id")
            if isinstance(found, str) and found.strip():
                return found.strip()
    path = os.path.realpath(os.path.join(project.data_root, "mcp_auth", "confirmation", task_id + ".json"))
    try:
        assert_registered_path(project, path)
    except McpError:
        return ""
    if not os.path.isfile(path):
        return ""
    try:
        with open(path, "r", encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, json.JSONDecodeError):
        return ""
    if not isinstance(data, dict):
        return ""
    found = data.get("confirmation_request_id")
    if isinstance(found, str) and found.strip():
        return found.strip()
    return ""


def _evidence_identity(project: ProjectRecord, task_id: str, idempotency_key: str) -> str:
    return hashlib.sha256(f"{project.project_id}|{task_id}|{idempotency_key}".encode("utf-8")).hexdigest()[:32]


def _intent_target(name: str, cleaned: Mapping[str, Any], project: ProjectRecord) -> Dict[str, Any]:
    if name == "create_task":
        return {"status": "待开始", "assignee": str(cleaned.get("assignee") or ""), "name": str(cleaned.get("name") or "")}
    if name == "transition_task":
        return {
            "task_id": str(cleaned.get("task_id") or ""),
            "status": str(cleaned.get("to_status") or ""),
            "assignee": str(cleaned.get("assignee") or ""),
        }
    if name == "request_acceptance":
        return {
            "task_id": str(cleaned.get("task_id") or ""),
            "status": "已完成",
            "evidence_id": str(cleaned.get("evidence_id") or ""),
            "action": "accept",
        }
    if name == "submit_evidence":
        return {"evidence_id": _evidence_identity(project, str(cleaned.get("task_id") or ""), str(cleaned.get("idempotency_key") or ""))}
    if name == "soft_delete":
        return {"task_id": str(cleaned.get("task_id") or ""), "is_deleted": True}
    if name == "restore":
        return {"task_id": str(cleaned.get("task_id") or ""), "is_deleted": False}
    return {}


def _submit_evidence(
    cleaned: Mapping[str, Any],
    project: ProjectRecord,
    principal: auth.AuthContext,
    context: Mapping[str, Any],
    ledger: Any,
    digest: str,
) -> Dict[str, Any]:
    import hashlib

    from scripts._lib.core.evidence_schema import ArtifactRecord, EvidenceMetadata, EvidenceRecord, EvidenceSecurityError, EvidenceType
    from scripts._lib.core.evidence_store import EvidenceStore, canonical_json, _to_dict

    card = _read_card(project, str(cleaned["task_id"]))
    if card is None or card.get("is_deleted"):
        raise error("NOT_FOUND")
    current = task_version(card)
    if current != cleaned["expected_version"]:
        raise error("CONFLICT", details={"reason": "VERSION", "task_version": current})
    artifacts = []
    for item in cleaned["artifacts"]:
        if not isinstance(item, dict):
            raise error("VALIDATION", details={"reason": "BAD_TYPE", "field": "artifacts"})
        relative = item.get("relative_path")
        artifact_hash = item.get("sha256") or item.get("sha256_hash")
        if not isinstance(relative, str) or not isinstance(artifact_hash, str):
            raise error("VALIDATION", details={"reason": "MISSING_FIELD", "field": "artifacts"})
        if _is_absolute(relative) or _has_dotdot(relative):
            raise error("PATH_REJECTED", details={"reason": "PATH", "field": "artifacts"})
        artifacts.append(ArtifactRecord(relative_path=relative, sha256_hash=artifact_hash))
    metadata = cleaned["metadata"]
    if not isinstance(metadata, dict):
        raise error("VALIDATION", details={"reason": "BAD_TYPE", "field": "metadata"})
    _reject_invocation_token(metadata)
    evidence_id = _evidence_identity(project, str(cleaned["task_id"]), str(cleaned["idempotency_key"]))
    confirmed = bool(context.get("acceptance_confirmed"))
    user_confirmation = str(cleaned["evidence_type"]) == "user_confirmation"
    extra = {}
    for key, value in metadata.items():
        name = str(key)
        if name in _CONFIRMATION_MODEL_KEYS or name.startswith("capability_"):
            continue
        if name in {"path", "data_root", "board_file", "workspace"}:
            continue
        extra[name] = value
    server_confirmation = _server_confirmation_id(project, str(cleaned["task_id"]))
    if user_confirmation and server_confirmation:
        extra["confirmation_id"] = server_confirmation
    if confirmed:
        for key, value in _transport_capability_values(True).items():
            extra["capability_" + key] = value
    store = EvidenceStore(project.evidence_root)
    try:
        extra = store.prepare_metadata(extra)
    except EvidenceSecurityError:
        raise error("VALIDATION", details={"reason": "REDACTION_REJECTED"}) from None
    record = EvidenceRecord(
        evidence_id=evidence_id,
        evidence_type=EvidenceType(str(cleaned["evidence_type"])),
        baseline_commit=str(cleaned["baseline_commit"]),
        result_commit=str(cleaned["result_commit"]),
        artifacts=tuple(artifacts),
        metadata=EvidenceMetadata(
            project_id=project.project_id,
            task_id=str(cleaned["task_id"]),
            actor_role=principal.role,
            host_id="yy-flow-host",
            adapter="yy-flow",
            host_session_id="stdio-session",
            host_invocation_id="invoke-" + evidence_id[:12],
            is_real_host=confirmed,
            workspace_mode="stdio",
            transition_from="" if card.get("status") is None else str(card.get("status")),
            transition_to="" if card.get("status") is None else str(card.get("status")),
            created_at=1,
            extra=extra,
        ),
    )
    data = _to_dict(record)
    data.pop("content_hash", None)
    content_hash = hashlib.sha256(canonical_json(data)).hexdigest()
    ledger.bind(
        principal_id=principal.principal_id,
        tool="submit_evidence",
        key=str(cleaned["idempotency_key"]),
        digest=digest,
        target={"evidence_id": evidence_id, "content_hash": content_hash},
    )
    try:
        existing = store.read(evidence_id)
    except Exception:
        existing = None
    if existing is not None:
        if existing.content_hash == content_hash:
            return _evidence_body(project, evidence_id, content_hash, replayed=True)
        raise error("CONFLICT", details={"reason": "EVIDENCE"})
    try:
        stored = store.append(record)
    except EvidenceSecurityError:
        raise error("CONFLICT", details={"reason": "EVIDENCE"}) from None
    return _evidence_body(project, evidence_id, stored, replayed=False)


def _evidence_body(project: ProjectRecord, evidence_id: str, content_hash: str, replayed: bool) -> Dict[str, Any]:
    body = {
        "_project_id": project.project_id,
        "_allow": ["evidence_id", "content_hash", "gate_status", "replayed", "code", "task_id"],
        "evidence_id": evidence_id,
        "content_hash": content_hash,
        "gate_status": "NOT_EVALUATED",
    }
    if replayed:
        body["replayed"] = True
        body["code"] = "IDEMPOTENCY_REPLAY"
    return body


def _request_acceptance(
    cleaned: Mapping[str, Any],
    project: ProjectRecord,
    principal: auth.AuthContext,
    context: Mapping[str, Any],
    ledger: Any,
    digest: str,
) -> Dict[str, Any]:
    del context
    from .acceptance import create_application

    return create_application(project, principal, cleaned, ledger, digest)


def _capability_namespace(extra: Mapping[str, Any]) -> Any:
    class _Caps:
        pass

    caps = _Caps()
    for key, value in extra.items():
        if str(key).startswith("capability_"):
            setattr(caps, str(key)[len("capability_") :], value)
    caps.extra = {}
    return caps


def _set_visibility(cleaned: Mapping[str, Any], project: ProjectRecord, deleted: bool) -> Dict[str, Any]:
    task_id = str(cleaned["task_id"])
    adapter = _adapter(project)
    outcome = adapter.set_visibility(task_id, deleted, expected_card_version=str(cleaned["expected_version"]))
    if outcome is None:
        raise error("NOT_FOUND")
    if outcome.get("conflict"):
        raise error("CONFLICT", details={"reason": "VERSION", "task_version": outcome.get("task_version")})
    card = outcome.get("card") or {}
    if not card:
        raise error("GATE_REJECTED", details={"reason": "VISIBILITY"})
    return {
        "_project_id": project.project_id,
        "_task_id": task_id,
        "_allow": ["task_id", "status", "is_deleted", "task_version", "replayed", "code"],
        "task_id": task_id,
        "status": "" if card.get("status") is None else str(card.get("status")),
        "is_deleted": bool(card.get("is_deleted")),
        "task_version": task_version(card),
        "replayed": False,
    }


def _stored_task_type(project: ProjectRecord, task_id: str, requested: str) -> str:
    card = _read_card(project, task_id) or {}
    stored = str(card.get("type") or "").strip().upper()
    if not stored:
        stored = "A"
    asked = str(requested or "").strip().upper()
    if asked and asked != stored:
        raise error("FORBIDDEN", details={"reason": "TASK_TYPE"})
    return stored


def _gate_reason(captured: str) -> str:
    if "类型锁定" in captured:
        return "TASK_TYPE"
    if "结束时间" in captured:
        return "END_TIME"
    if "并发超限" in captured:
        return "PARALLEL"
    if "越权" in captured or "无权" in captured:
        return "ROLE"
    if "代行" in captured:
        return "DELEGATION"
    if "重复任务" in captured:
        return "DUPLICATE_TASK"
    return "TRANSITION"


def _record_target(record: Mapping[str, Any]) -> Dict[str, Any]:
    target = record.get("target") if isinstance(record.get("target"), dict) else {}
    result = record.get("result") if isinstance(record.get("result"), dict) else {}
    merged = dict(result)
    merged.update(target)
    return merged


def _fact_reached(project: ProjectRecord, tool: str, record: Mapping[str, Any]) -> str:
    target = _record_target(record)
    if tool == "submit_evidence":
        evidence_id = str(target.get("evidence_id") or "")
        content_digest = str(target.get("content_hash") or "")
        if not evidence_id or not content_digest:
            return "missing"
        from scripts._lib.core.evidence_schema import EvidenceError
        from scripts._lib.core.evidence_store import EvidenceStore

        try:
            stored = EvidenceStore(project.evidence_root).read(evidence_id)
        except (EvidenceError, OSError):
            return "missing"
        if stored.content_hash != content_digest:
            return "mismatch"
        return "achieved"
    task_id = str(target.get("task_id") or "")
    if tool == "create_task" and not task_id:
        return "unbound"
    if not task_id:
        return "missing"
    card = _read_card(project, task_id)
    if card is None:
        return "missing"
    if tool == "create_task":
        return "achieved"
    if tool == "request_acceptance":
        from .acceptance import matching_application

        found = matching_application(
            project,
            principal_id=str(record.get("principal_id") or ""),
            task_id=str(target.get("task_id") or ""),
            idempotency_key=str(record.get("idempotency_key") or ""),
            digest=str(record.get("request_sha256") or ""),
        )
        return "achieved" if found is not None else "missing"
    if tool == "transition_task":
        status = str(target.get("status") or "")
        assignee = str(target.get("assignee") or "")
        if status and str(card.get("status") or "") != status:
            return "missing"
        if assignee and str(card.get("assignee") or "") != assignee:
            return "missing"
        return "achieved"
    if tool == "soft_delete":
        return "achieved" if bool(card.get("is_deleted")) else "missing"
    if tool == "restore":
        return "achieved" if not bool(card.get("is_deleted")) else "missing"
    return "missing"


def _ledger(project: ProjectRecord):
    from .idempotency import IdempotencyStore

    return IdempotencyStore(project)


def _request_digest(cleaned: Mapping[str, Any]) -> str:
    raw = json.dumps(cleaned, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _call_in_project(project: ProjectRecord, action):
    from scripts import paths

    token = paths.bind_request_data_root(project.data_root)
    try:
        return action()
    finally:
        paths.reset_request_data_root(token)


def note_missing_ids(audit: AuditStore, project_id: str, principal_id: str, count: int) -> None:
    for _index in range(count):
        audit.append(
            principal_id=principal_id,
            project_id=project_id,
            tool="list_tasks",
            code="VALIDATION",
            diagnostic="MISSING_ID",
            arguments={},
        )
