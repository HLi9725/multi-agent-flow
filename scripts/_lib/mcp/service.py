# -*- coding: utf-8 -*-
"""只读工具实现。唯一允许调用领域读取函数的模块。"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
from typing import Any, Dict, List, Mapping, Tuple

from ..boards.offline_board_adapter import OfflineBoardAdapter
from ..core.workflow_policy import PROFILE_STANDARD, PolicyError, load_policy_file
from . import auth
from .audit_store import AuditStore
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
    tool_specs,
    validate_arguments,
)

try:
    from enums import TaskStatus
except ImportError:  # 以仓库根为路径导入时
    from scripts.enums import TaskStatus

_PATH_KEYS = {"path", "data_root", "board_file", "workspace"}
_DROP_KEYS = {"user_id", "tenant_id"}
_ORG_KEYS = {"org", "org_id"}
_NODE_SPLIT = re.compile(r"(?=\[T\d+-N\d+\])")
_ABS_RE = re.compile(r"^(?:[A-Za-z]:[\\/]|\\\\|//)")


def handle_tool(name: str, arguments: Any, context: Mapping[str, Any]) -> Dict[str, Any]:
    registry: Registry = context["registry"]
    audit: AuditStore = context["audit"]
    os_user = str(context.get("os_user") or "")
    principal = auth.resolve_stdio_principal(registry, os_user)
    principal_id = principal.principal_id if principal else ""
    project_id = ""
    task_id = ""
    try:
        payload = _dispatch(name, arguments, registry, principal)
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
    project = registry.get_project(project_id) if project_id else None
    literals = [registry.path]
    if project is not None:
        literals.extend([project.data_root, project.board_file, project.evidence_root, project.board_config])
    if "items" in payload:
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
