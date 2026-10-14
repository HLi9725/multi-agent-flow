# -*- coding: utf-8 -*-
"""工具名、入参 JSON Schema 与出参字段白名单。"""

from __future__ import annotations

import re
from typing import Any, Dict, List, Mapping

from .errors import error

SCHEMA_VERSION = "4.0.0"
SCHEMA_DIALECT = "https://json-schema.org/draft/2020-12/schema"

PROJECT_ID_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$"
TASK_ID_PATTERN = r"^T[0-9]{4,}$"
PROJECT_ID_RE = re.compile(PROJECT_ID_PATTERN)
TASK_ID_RE = re.compile(TASK_ID_PATTERN)

READ_TOOL_NAMES = ("get_project", "get_task", "list_tasks", "get_status")
WRITE_TOOL_NAMES = (
    "create_task",
    "submit_evidence",
    "transition_task",
    "request_acceptance",
    "soft_delete",
    "restore",
)
TOOL_SCOPE = {
    "get_project": "read",
    "get_task": "read",
    "list_tasks": "read",
    "get_status": "read",
    "create_task": "task.transition",
    "transition_task": "task.transition",
    "submit_evidence": "evidence.submit",
    "request_acceptance": "acceptance",
    "soft_delete": "admin",
    "restore": "admin",
}
IDEMPOTENCY_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9._:-]{7,127}$"
VERSION_PATTERN = r"^[0-9a-f]{64}$"
EVIDENCE_TYPES = ("task_start", "task_transition", "task_complete", "user_confirmation")
TASK_TYPES = ("A", "B", "C", "D", "E", "F", "G")

STRING_PARAM_MAX_BYTES = 8 * 1024
TEXT_FIELD_MAX_BYTES = 4 * 1024
RESPONSE_MAX_BYTES = 256 * 1024
AUDIT_LINE_MAX_BYTES = 4 * 1024
PAGE_DEFAULT = 20
PAGE_MAX = 50
PROCESS_NODE_MAX = 20
DISPLAY_NAME_MAX = 128

PROJECT_FIELDS = (
    "project_id",
    "display_name",
    "org_id",
    "board_kind",
    "schema_version",
    "server_revision",
)
TASK_FIELDS = (
    "id",
    "name",
    "status",
    "assignee",
    "owner",
    "type",
    "task_version",
    "is_deleted",
    "start_time",
    "end_time",
    "remarks",
)
TASK_META_FIELDS = ("board_version", "truncated", "original_length", "process")
STATUS_FIELDS = (
    "task_id",
    "status",
    "assignee",
    "task_version",
    "board_version",
    "profile_id",
)
LIST_FIELDS = ("items", "next_cursor", "has_more", "board_version", "stale")


def _object_schema(properties: Dict[str, Any], required: List[str]) -> Dict[str, Any]:
    return {
        "$schema": SCHEMA_DIALECT,
        "type": "object",
        "additionalProperties": False,
        "properties": properties,
        "required": required,
    }


def _project_id_schema() -> Dict[str, Any]:
    return {"type": "string", "pattern": PROJECT_ID_PATTERN, "maxLength": 64}


def _task_id_schema() -> Dict[str, Any]:
    return {"type": "string", "pattern": TASK_ID_PATTERN, "maxLength": 64}


def tool_specs(status_enum: List[str], scopes: Any = None) -> List[Dict[str, Any]]:
    status_schema = {"type": "string", "enum": list(status_enum), "maxLength": 32}
    specs = [
        {
            "name": "get_project",
            "description": "读取已登记项目的公开信息",
            "inputSchema": _object_schema(
                {"project_id": _project_id_schema()},
                ["project_id"],
            ),
        },
        {
            "name": "get_task",
            "description": "读取单张任务卡的公开字段",
            "inputSchema": _object_schema(
                {
                    "project_id": _project_id_schema(),
                    "task_id": _task_id_schema(),
                    "include_process": {"type": "boolean"},
                    "include_deleted": {"type": "boolean"},
                },
                ["project_id", "task_id"],
            ),
        },
        {
            "name": "list_tasks",
            "description": "按任务编号升序分页列出任务",
            "inputSchema": _object_schema(
                {
                    "project_id": _project_id_schema(),
                    "status": status_schema,
                    "assignee": {"type": "string", "maxLength": 256},
                    "cursor": {"type": "string", "maxLength": 4096},
                    "limit": {"type": "integer", "minimum": 1, "maximum": PAGE_MAX},
                    "include_deleted": {"type": "boolean"},
                },
                ["project_id"],
            ),
        },
        {
            "name": "get_status",
            "description": "读取任务状态、版本与档案标识",
            "inputSchema": _object_schema(
                {
                    "project_id": _project_id_schema(),
                    "task_id": _task_id_schema(),
                },
                ["project_id", "task_id"],
            ),
        },
        {
            "name": "create_task",
            "description": "创建待开始任务卡",
            "inputSchema": _object_schema(
                {
                    "project_id": _project_id_schema(),
                    "idempotency_key": {"type": "string", "pattern": IDEMPOTENCY_PATTERN, "maxLength": 128},
                    "name": {"type": "string", "maxLength": 200},
                    "type": {"type": "string", "enum": list(TASK_TYPES), "maxLength": 8},
                    "assignee": {"type": "string", "maxLength": 256},
                    "stage": {"type": "string", "maxLength": 256},
                    "wp": {"type": "string", "maxLength": 256},
                    "wbs": {"type": "string", "maxLength": 256},
                    "owner": {"type": "string", "maxLength": 256},
                    "force_duplicate": {"type": "boolean"},
                },
                ["project_id", "idempotency_key", "name", "type", "assignee"],
            ),
        },
        {
            "name": "submit_evidence",
            "description": "上传证据记录，不推进状态",
            "inputSchema": _object_schema(
                {
                    "project_id": _project_id_schema(),
                    "task_id": _task_id_schema(),
                    "idempotency_key": {"type": "string", "pattern": IDEMPOTENCY_PATTERN, "maxLength": 128},
                    "expected_version": {"type": "string", "pattern": VERSION_PATTERN, "maxLength": 64},
                    "evidence_type": {"type": "string", "enum": list(EVIDENCE_TYPES), "maxLength": 64},
                    "baseline_commit": {"type": "string", "maxLength": 128},
                    "result_commit": {"type": "string", "maxLength": 128},
                    "metadata": {"type": "object"},
                    "artifacts": {"type": "array"},
                },
                [
                    "project_id",
                    "task_id",
                    "idempotency_key",
                    "expected_version",
                    "evidence_type",
                    "baseline_commit",
                    "result_commit",
                    "metadata",
                    "artifacts",
                ],
            ),
        },
        {
            "name": "transition_task",
            "description": "按现有门禁推进任务状态",
            "inputSchema": _object_schema(
                {
                    "project_id": _project_id_schema(),
                    "task_id": _task_id_schema(),
                    "idempotency_key": {"type": "string", "pattern": IDEMPOTENCY_PATTERN, "maxLength": 128},
                    "expected_version": {"type": "string", "pattern": VERSION_PATTERN, "maxLength": 64},
                    "from_status": status_schema,
                    "to_status": status_schema,
                    "assignee": {"type": "string", "maxLength": 256},
                    "task_type": {"type": "string", "enum": list(TASK_TYPES), "maxLength": 8},
                    "end_time": {"type": "string", "maxLength": 64},
                    "remarks": {"type": "string", "maxLength": 4096},
                    "evidence_id": {"type": "string", "maxLength": 64},
                },
                [
                    "project_id",
                    "task_id",
                    "idempotency_key",
                    "expected_version",
                    "from_status",
                    "to_status",
                    "assignee",
                    "task_type",
                ],
            ),
        },
        {
            "name": "request_acceptance",
            "description": "申请用户验收并返回确认编号，不把任务改为已验收",
            "inputSchema": _object_schema(
                {
                    "project_id": _project_id_schema(),
                    "task_id": _task_id_schema(),
                    "idempotency_key": {"type": "string", "pattern": IDEMPOTENCY_PATTERN, "maxLength": 128},
                    "expected_version": {"type": "string", "pattern": VERSION_PATTERN, "maxLength": 64},
                    "evidence_id": {"type": "string", "maxLength": 64},
                },
                ["project_id", "task_id", "idempotency_key", "expected_version", "evidence_id"],
            ),
        },
        {
            "name": "soft_delete",
            "description": "软删除任务卡且不改变状态",
            "inputSchema": _object_schema(
                {
                    "project_id": _project_id_schema(),
                    "task_id": _task_id_schema(),
                    "idempotency_key": {"type": "string", "pattern": IDEMPOTENCY_PATTERN, "maxLength": 128},
                    "expected_version": {"type": "string", "pattern": VERSION_PATTERN, "maxLength": 64},
                },
                ["project_id", "task_id", "idempotency_key", "expected_version"],
            ),
        },
        {
            "name": "restore",
            "description": "恢复软删除任务卡且不改变状态",
            "inputSchema": _object_schema(
                {
                    "project_id": _project_id_schema(),
                    "task_id": _task_id_schema(),
                    "idempotency_key": {"type": "string", "pattern": IDEMPOTENCY_PATTERN, "maxLength": 128},
                    "expected_version": {"type": "string", "pattern": VERSION_PATTERN, "maxLength": 64},
                },
                ["project_id", "task_id", "idempotency_key", "expected_version"],
            ),
        },
    ]
    if scopes is None:
        return specs
    granted = set(scopes)
    return [item for item in specs if TOOL_SCOPE[item["name"]] in granted]


def _fail(reason: str, field: str | None = None) -> None:
    details: Dict[str, Any] = {"reason": reason}
    if field:
        details["field"] = field
    raise error("VALIDATION", details=details)


def _check_string(name: str, value: Any, prop: Mapping[str, Any]) -> None:
    if not isinstance(value, str):
        _fail("BAD_TYPE", name)
    raw = value.encode("utf-8")
    limit = prop.get("maxLength")
    if len(raw) > STRING_PARAM_MAX_BYTES or (isinstance(limit, int) and len(value) > limit):
        _fail("STRING_LENGTH", name)
    pattern = prop.get("pattern")
    if pattern and re.fullmatch(pattern, value) is None:
        _fail("BAD_FORMAT", name)
    enum = prop.get("enum")
    if enum is not None and value not in enum:
        _fail("BAD_STATUS" if name == "status" else "BAD_FORMAT", name)


def _check_integer(name: str, value: Any, prop: Mapping[str, Any]) -> None:
    if isinstance(value, bool) or not isinstance(value, int):
        _fail("BAD_TYPE", name)
    minimum = prop.get("minimum")
    maximum = prop.get("maximum")
    if (minimum is not None and value < minimum) or (maximum is not None and value > maximum):
        _fail("LIMIT", name)


def _check_boolean(name: str, value: Any) -> None:
    if not isinstance(value, bool):
        _fail("BAD_TYPE", name)


def validate_arguments(spec: Mapping[str, Any], arguments: Mapping[str, Any]) -> None:
    schema = spec["inputSchema"]
    if schema.get("type") != "object" or schema.get("additionalProperties") is not False:
        _fail("SCHEMA")
    properties = schema.get("properties") or {}
    required = schema.get("required") or []
    for field in required:
        if field not in arguments:
            _fail("MISSING_FIELD", field)
    for key in arguments:
        if key not in properties:
            _fail("ADDITIONAL_PROPERTY", str(key))
    for key, value in arguments.items():
        prop = properties[key]
        kind = prop.get("type")
        if kind == "string":
            _check_string(key, value, prop)
        elif kind == "integer":
            _check_integer(key, value, prop)
        elif kind == "boolean":
            _check_boolean(key, value)
        elif kind == "object":
            if not isinstance(value, dict):
                _fail("BAD_TYPE", key)
        elif kind == "array":
            if not isinstance(value, list):
                _fail("BAD_TYPE", key)
        else:
            _fail("BAD_TYPE", key)
