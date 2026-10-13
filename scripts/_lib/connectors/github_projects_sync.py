# -*- coding: utf-8 -*-
"""GitHub Projects 投影同步。只读对齐不写 YY-Flow，也不写 GitHub。

真实 HTTP 未验证。凭证不得进入日志、差异报告或返回值。
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable, Mapping, Protocol

from scripts._lib.mcp.registry import ProjectRecord, Registry

PAGE_BUDGET_DEFAULT = 40
PAGE_SIZE = 50
BACKOFF_LIMIT = 3
BACKOFF_CAP_SECONDS = 60
_ACCEPTANCE_TEXT = {"已验收"}
_PROJECTION_FIELDS = ("title", "body", "status", "assignee")

_ITEMS_QUERY = """
query($projectId: ID!, $after: String) {
  node(id: $projectId) {
    ... on ProjectV2 {
      items(first: 50, after: $after) {
        pageInfo { hasNextPage endCursor }
        nodes {
          id
          updatedAt
          isArchived
          content {
            ... on DraftIssue { title body }
            ... on Issue { title body }
          }
          fieldValues(first: 20) {
            nodes {
              ... on ProjectV2ItemFieldTextValue {
                text
                field { ... on ProjectV2FieldCommon { name } }
              }
              ... on ProjectV2ItemFieldSingleSelectValue {
                name
                field { ... on ProjectV2FieldCommon { name } }
              }
            }
          }
        }
      }
    }
  }
}
""".strip()

_ARCHIVE_QUERY = """
mutation($projectId: ID!, $itemId: ID!) {
  archiveProjectV2Item(input: {projectId: $projectId, itemId: $itemId}) {
    item { id }
  }
}
""".strip()

_UNARCHIVE_QUERY = """
mutation($projectId: ID!, $itemId: ID!) {
  unarchiveProjectV2Item(input: {projectId: $projectId, itemId: $itemId}) {
    item { id }
  }
}
""".strip()

_CREATE_QUERY = """
mutation($projectId: ID!, $title: String!, $body: String!) {
  addProjectV2DraftIssue(input: {projectId: $projectId, title: $title, body: $body}) {
    projectItem { id }
  }
}
""".strip()

_STATUS_FIELDS_QUERY = """
query($projectId: ID!) {
  node(id: $projectId) {
    ... on ProjectV2 {
      fields(first: 50) {
        nodes {
          ... on ProjectV2SingleSelectField {
            id
            name
            options { id name }
          }
        }
      }
    }
  }
}
""".strip()

_UPDATE_QUERY = """
mutation($projectId: ID!, $itemId: ID!, $fieldId: ID!, $optionId: String!) {
  updateProjectV2ItemFieldValue(input: {
    projectId: $projectId,
    itemId: $itemId,
    fieldId: $fieldId,
    value: {singleSelectOptionId: $optionId}
  }) {
    projectV2Item { id }
  }
}
""".strip()

_TASK_MARKER = "yy-flow-task-id:"
_DELIVERY_MARKER = "yy-flow-delivery-id:"


@dataclass(frozen=True)
class GithubResult:
    """成功数据、GraphQL 错误、HTTP 状态、速率重置与分页彼此分开。空字典不是成功。"""

    data: Mapping[str, Any] | None = None
    graphql_errors: tuple = ()
    http_status: int | None = None
    rate_limit_reset_at: float | None = None
    page_info: Mapping[str, Any] | None = None

    @property
    def ok(self) -> bool:
        if self.graphql_errors:
            return False
        if self.http_status is not None and int(self.http_status) >= 400:
            return False
        if self.rate_limit_reset_at is not None and self.http_status == 429:
            return False
        return isinstance(self.data, Mapping) and len(self.data) > 0


class GithubTransport(Protocol):
    def query(self, query: str, variables: dict) -> GithubResult:
        """发送一条 GraphQL。失败必须体现在 GithubResult 上。"""


@dataclass
class SyncReport:
    ok: bool = False
    reason: str = ""
    reasons: list = field(default_factory=list)
    cursor: str = ""
    network_writes: int = 0
    network_reads: int = 0
    partial: bool = False
    event_outcomes: list = field(default_factory=list)
    projection_writes: int = 0
    task_results: list = field(default_factory=list)
    diffs: list = field(default_factory=list)
    attempted_network: bool = False

    def public_dict(self) -> dict:
        return {
            "ok": self.ok,
            "reason": self.reason,
            "reasons": list(self.reasons),
            "cursor": self.cursor,
            "network_writes": self.network_writes,
            "network_reads": self.network_reads,
            "partial": self.partial,
            "event_outcomes": list(self.event_outcomes),
            "projection_writes": self.projection_writes,
            "task_results": list(self.task_results),
            "diffs": list(self.diffs),
            "attempted_network": self.attempted_network,
        }


class HttpGithubTransport:
    """真实 GraphQL。本环境没有核对过沙箱项目，调用结果保持未验证。"""

    def __init__(self, token: str, opener: Callable | None = None) -> None:
        self._token = token
        self._opener = opener or urllib.request.urlopen

    def query(self, query: str, variables: dict) -> GithubResult:
        if not self._token:
            return GithubResult(data=None, graphql_errors=({"code": "CREDENTIALS_ABSENT"},))
        payload = json.dumps({"query": query, "variables": variables or {}}).encode("utf-8")
        request = urllib.request.Request(
            "https://api.github.com/graphql",
            data=payload,
            method="POST",
            headers={
                "Authorization": "Bearer " + self._token,
                "Content-Type": "application/json",
                "Accept": "application/json",
                "User-Agent": "yy-flow-sync",
            },
        )
        try:
            with self._opener(request, timeout=10) as response:
                status = getattr(response, "status", None) or response.getcode()
                raw = response.read()
                headers = getattr(response, "headers", {})
        except urllib.error.HTTPError as exc:
            raw = exc.read() if exc.fp is not None else b""
            return _result_from_http(exc.code, raw, exc.headers)
        except urllib.error.URLError:
            return GithubResult(data=None, graphql_errors=({"code": "HTTP_ERROR"},))
        return _result_from_http(int(status or 0), raw, headers)


def run_sync(
    project: ProjectRecord,
    *,
    mode: str = "read-only",
    transport: GithubTransport | None = None,
    token: str | None = None,
    environ: Mapping[str, str] | None = None,
    registry: Registry | None = None,
    now: Callable[[], float] | None = None,
    sleep: Callable[[float], None] | None = None,
    page_budget: int = PAGE_BUDGET_DEFAULT,
    opener: Callable | None = None,
) -> SyncReport:
    """执行一轮同步。注入的 transport 仅供测试；缺省传输才会访问 GitHub。"""
    report = SyncReport()
    if mode not in {"read-only", "write"}:
        report.reason = "VALIDATION"
        return report
    if mode == "write" and not project.write_enabled:
        report.reason = "WRITE_DISABLED"
        report.network_writes = 0
        return report
    clock = now or _wall_time
    pause = sleep or _wall_sleep
    resolved = _resolve_token(project, token, environ, registry)
    if transport is None:
        if not resolved:
            report.reason = "CREDENTIALS_ABSENT"
            report.attempted_network = False
            report.network_writes = 0
            return report
        transport = HttpGithubTransport(resolved, opener=opener)
    if not project.github_project_id:
        report.reason = "FIELD_MISSING"
        report.partial = True
        return report
    ledger = _load_ledger(project)
    tasks = _load_tasks(project)
    pulled = _pull_pages(
        project,
        transport,
        ledger,
        report,
        clock,
        pause,
        page_budget,
    )
    if pulled.early_reason:
        if pulled.items and pulled.early_reason == "PARTIAL":
            _remember_items(ledger, [_normalize_item(node) for node in pulled.items])
        report.reason = pulled.early_reason
        report.ok = False
        report.partial = pulled.early_reason in {"PARTIAL", "FIELD_MISSING", "RATE_LIMITED"}
        report.cursor = str(ledger.get("cursor") or "")
        _save_ledger(project, ledger)
        _save_diff(project, report)
        return report
    normalized = [_normalize_item(node) for node in pulled.items]
    _remember_items(ledger, normalized)
    outcomes, writes = _apply_events(pulled.events, ledger, project)
    report.event_outcomes = outcomes
    report.projection_writes = writes
    normalized = _overlay_event_state(normalized, ledger)
    _diff_tasks(project, tasks, normalized, ledger, report)
    if mode == "write" and pulled.completed and not report.partial:
        _write_projection(project, tasks, normalized, ledger, transport, report)
    if any(item.get("status") == "failed" for item in ledger.get("actions", [])):
        report.ok = False
        if not report.reason:
            report.reason = "FAILED"
    elif any(row.get("outcome") == "NO_EXTERNAL_ITEM" for row in report.task_results):
        report.ok = False
        if not report.reason:
            report.reason = "NO_EXTERNAL_ITEM"
    elif any(row.get("outcome") == "CONTRACT_CHANGED" for row in report.task_results):
        report.ok = False
        if not report.reason:
            report.reason = "CONTRACT_CHANGED"
    elif pulled.completed and not report.partial and report.reason not in {"CONFLICT", "FIELD_MISSING"}:
        if not any(row.get("outcome") == "CONFLICT" for row in report.task_results):
            report.ok = True
            report.reason = ""
        else:
            report.ok = False
            report.reason = "CONFLICT"
    else:
        report.ok = False
        if not report.reason:
            report.reason = "PARTIAL" if report.partial else "FIELD_MISSING"
    report.cursor = str(ledger.get("cursor") or "")
    _save_ledger(project, ledger)
    _save_diff(project, report)
    return report


@dataclass
class _Pull:
    items: list = field(default_factory=list)
    events: list = field(default_factory=list)
    completed: bool = False
    early_reason: str = ""


def _pull_pages(project, transport, ledger, report, clock, pause, page_budget: int) -> _Pull:
    pulled = _Pull()
    cursor = ledger.get("cursor") or None
    if cursor == "":
        cursor = None
    pages = 0
    while pages < page_budget:
        result = _query_with_backoff(
            transport,
            _ITEMS_QUERY,
            {"projectId": project.github_project_id, "after": cursor},
            ledger,
            report,
            clock,
            pause,
            cursor,
        )
        if isinstance(result, str):
            pulled.early_reason = result
            return pulled
        if _is_forbidden(result):
            pulled.early_reason = "FORBIDDEN"
            return pulled
        if not result.ok:
            pulled.early_reason = "FIELD_MISSING" if _missing_payload(result) else "FAILED"
            report.partial = True
            return pulled
        page_info = _page_info(result)
        nodes = _nodes(result)
        if page_info is None or "hasNextPage" not in page_info or not isinstance(nodes, list):
            report.partial = True
            report.reason = "FIELD_MISSING"
            pulled.early_reason = "FIELD_MISSING"
            return pulled
        for node in nodes:
            if not isinstance(node, dict) or not node.get("id"):
                report.partial = True
                report.reason = "FIELD_MISSING"
                pulled.early_reason = "FIELD_MISSING"
                return pulled
            content = node.get("content")
            if not isinstance(content, Mapping) or "title" not in content:
                report.partial = True
                report.reason = "FIELD_MISSING"
                pulled.early_reason = "FIELD_MISSING"
                return pulled
            pulled.items.append(node)
        extra = result.data.get("events") if isinstance(result.data, Mapping) else None
        if isinstance(extra, list):
            pulled.events.extend(extra)
        for node in nodes:
            event = _event_from_node(node)
            if event is not None:
                pulled.events.append(event)
        pages += 1
        if not page_info.get("hasNextPage"):
            ledger["cursor"] = ""
            pulled.completed = True
            return pulled
        end_cursor = page_info.get("endCursor") or ""
        if not end_cursor:
            report.partial = True
            report.reason = "FIELD_MISSING"
            pulled.early_reason = "FIELD_MISSING"
            return pulled
        cursor = end_cursor
        ledger["cursor"] = end_cursor
    report.partial = True
    report.reason = "PARTIAL"
    pulled.early_reason = "PARTIAL"
    return pulled


def _query_with_backoff(transport, query, variables, ledger, report, clock, pause, cursor):
    backoffs = 0
    while True:
        result = transport.query(query, dict(variables))
        if _error_code(result) == "CREDENTIALS_ABSENT":
            report.attempted_network = False
            report.network_reads = 0
            report.network_writes = 0
            return "CREDENTIALS_ABSENT"
        report.attempted_network = True
        if _is_mutation(query):
            report.network_writes += 1
        else:
            report.network_reads += 1
        if not _is_rate_limited(result):
            return result
        if backoffs >= BACKOFF_LIMIT:
            if cursor:
                ledger["cursor"] = cursor
            report.reason = "RATE_LIMITED"
            return "RATE_LIMITED"
        pause(_backoff_seconds(result.rate_limit_reset_at, clock))
        backoffs += 1


def _backoff_seconds(reset_at: float | None, clock: Callable[[], float]) -> float:
    if reset_at is None:
        return float(BACKOFF_CAP_SECONDS)
    remaining = float(reset_at) - float(clock())
    if remaining < 0:
        remaining = 0
    if remaining > BACKOFF_CAP_SECONDS:
        return float(BACKOFF_CAP_SECONDS)
    return remaining


def _write_projection(project, tasks, normalized, ledger, transport, report: SyncReport) -> None:
    by_task = {}
    for item in normalized:
        if item.get("task_id"):
            by_task[item["task_id"]] = item
    for link_id, link in ledger.get("tasks", {}).items():
        item_id = link.get("item_id") or ""
        if item_id and link_id not in by_task:
            by_task.setdefault(link_id, {"id": item_id, "task_id": link_id, "archived": bool(link.get("archived"))})
    blocked = {
        row["task_id"]
        for row in report.task_results
        if row.get("outcome") in {"CONFLICT", "CONTRACT_CHANGED", "NO_EXTERNAL_ITEM"}
    }
    actions = ledger.setdefault("actions", [])
    index = {action["key"]: action for action in actions if isinstance(action, dict) and action.get("key")}
    ordered = sorted((task for task in tasks if task.get("id")), key=lambda task: str(task["id"]))
    for task in ordered:
        task_id = str(task["id"])
        if task_id in blocked and not _acceptance_only(report, task_id):
            continue
        if task_id in blocked and _blocked_completely(report, task_id):
            continue
        link = ledger.setdefault("tasks", {}).setdefault(task_id, {})
        item = by_task.get(task_id)
        item_id = link.get("item_id") or (item or {}).get("id") or ""
        if item_id:
            link["item_id"] = item_id
        archived = bool((item or {}).get("archived") or link.get("archived"))
        if task.get("is_deleted"):
            if not item_id:
                _add_result(report, task_id, "NO_EXTERNAL_ITEM")
                continue
            _ensure_action(actions, index, f"archive:{task_id}", task_id, "archive", item_id)
            continue
        if archived and item_id:
            _ensure_action(actions, index, f"unarchive:{task_id}", task_id, "unarchive", item_id)
            continue
        if not item_id:
            _ensure_action(actions, index, f"create:{task_id}", task_id, "create", "")
            continue
        if _needs_status_write(task, item, report, task_id):
            _ensure_action(actions, index, f"project:{task_id}", task_id, "project", item_id)
    start = 0
    for offset, action in enumerate(actions):
        if action.get("status") in {"pending", "failed"}:
            start = offset
            break
    else:
        start = len(actions)
    tasks_by_id = {str(task.get("id")): task for task in tasks if task.get("id")}
    option_index = _load_status_options(transport, report, project.github_project_id)
    for action in actions[start:]:
        if action.get("status") == "applied":
            continue
        _execute_action(
            project,
            action,
            tasks_by_id.get(action["task_id"], {}),
            normalized,
            transport,
            report,
            ledger,
            option_index,
        )


def _acceptance_only(report: SyncReport, task_id: str) -> bool:
    rows = [row for row in report.task_results if row.get("task_id") == task_id]
    return bool(rows) and all(row.get("outcome") == "EXTERNAL_ACCEPTANCE_IGNORED" for row in rows)


def _blocked_completely(report: SyncReport, task_id: str) -> bool:
    return any(
        row.get("task_id") == task_id and row.get("outcome") in {"CONFLICT", "CONTRACT_CHANGED"}
        for row in report.task_results
    )


def _needs_status_write(task, item, report: SyncReport, task_id: str) -> bool:
    if any(row.get("task_id") == task_id and row.get("outcome") == "EXTERNAL_ACCEPTANCE_IGNORED" for row in report.task_results):
        return True
    external = (item or {}).get("status") or ""
    return bool(external) and external != task.get("status")


def _ensure_action(actions, index, key, task_id, op, item_id) -> None:
    current = index.get(key)
    if current is None:
        action = {
            "key": key,
            "task_id": task_id,
            "op": op,
            "status": "pending",
            "item_id": item_id,
            "delivery_id": key,
        }
        actions.append(action)
        index[key] = action
        return
    if current.get("status") != "applied" and item_id and not current.get("item_id"):
        current["item_id"] = item_id


def _marker_value(text: str, marker: str) -> str:
    for line in str(text).splitlines():
        if line.startswith(marker):
            return line[len(marker):].strip()
    return ""


def _strip_markers(text: str) -> str:
    kept = []
    for line in str(text).splitlines():
        if line.startswith(_TASK_MARKER) or line.startswith(_DELIVERY_MARKER):
            continue
        kept.append(line)
    return "\n".join(kept).strip()


def _draft_body(task: Mapping[str, Any], task_id: str, delivery_id: str) -> str:
    remarks = task.get("remarks")
    if remarks is None:
        remarks = task.get("body") or ""
    visible = _strip_markers(str(remarks))
    lines = [f"{_TASK_MARKER}{task_id}"]
    if delivery_id:
        lines.append(f"{_DELIVERY_MARKER}{delivery_id}")
    if visible:
        lines.append(visible)
    return "\n".join(lines)


def _load_status_options(transport, report: SyncReport, project_id: str) -> dict[str, tuple[str, str]]:
    report.attempted_network = True
    report.network_reads += 1
    result = transport.query(_STATUS_FIELDS_QUERY, {"projectId": project_id})
    if not isinstance(result, GithubResult) or not result.ok:
        return {}
    data = result.data or {}
    node = data.get("node") if isinstance(data, Mapping) else None
    fields = []
    if isinstance(node, Mapping):
        raw_fields = node.get("fields")
        if isinstance(raw_fields, Mapping) and isinstance(raw_fields.get("nodes"), list):
            fields = raw_fields["nodes"]
    found: dict[str, tuple[str, str]] = {}
    for entry in fields:
        if not isinstance(entry, Mapping):
            continue
        name = str(entry.get("name") or "")
        if name.lower() not in {"status", "状态"}:
            continue
        field_id = str(entry.get("id") or "")
        options = entry.get("options") or []
        if not field_id or not isinstance(options, list):
            continue
        for option in options:
            if not isinstance(option, Mapping):
                continue
            option_name = str(option.get("name") or "").strip()
            option_id = str(option.get("id") or "")
            if option_name and option_id:
                found[option_name] = (field_id, option_id)
    return found


def _execute_action(project, action, task, normalized, transport, report: SyncReport, ledger, option_index: Mapping[str, tuple]) -> None:
    op = action.get("op")
    item_id = action.get("item_id") or ""
    if op == "create":
        found = _find_existing(normalized, action, ledger)
        if found:
            action["status"] = "applied"
            action["item_id"] = found
            ledger.setdefault("tasks", {}).setdefault(action["task_id"], {})["item_id"] = found
            ledger.setdefault("deliveries", {})[action["delivery_id"]] = {"applied": True, "item_id": found}
            return
        result = _mutate(
            transport,
            report,
            _CREATE_QUERY,
            {
                "projectId": project.github_project_id,
                "title": str(task.get("name") or action["task_id"]),
                "body": _draft_body(task, str(action["task_id"]), str(action.get("delivery_id") or "")),
            },
        )
        if result.ok:
            action["status"] = "applied"
            created = _created_id(result) or item_id
            action["item_id"] = created
            ledger.setdefault("tasks", {}).setdefault(action["task_id"], {})["item_id"] = created
            ledger.setdefault("deliveries", {})[action["delivery_id"]] = {"applied": True, "item_id": created}
            return
        action["status"] = "failed"
        return
    if op == "archive":
        if not item_id:
            action["status"] = "failed"
            _add_result(report, action["task_id"], "NO_EXTERNAL_ITEM")
            return
        result = _mutate(
            transport,
            report,
            _ARCHIVE_QUERY,
            {"projectId": project.github_project_id, "itemId": item_id},
        )
        action["status"] = "applied" if result.ok else "failed"
        if result.ok:
            ledger.setdefault("tasks", {}).setdefault(action["task_id"], {})["archived"] = True
        return
    if op == "unarchive":
        result = _mutate(
            transport,
            report,
            _UNARCHIVE_QUERY,
            {"projectId": project.github_project_id, "itemId": item_id},
        )
        action["status"] = "applied" if result.ok else "failed"
        if result.ok:
            ledger.setdefault("tasks", {}).setdefault(action["task_id"], {})["archived"] = False
        return
    if op == "project":
        status_name = str(task.get("status") or "")
        matched = option_index.get(status_name)
        if not matched or not item_id:
            action["status"] = "failed"
            return
        field_id, option_id = matched
        result = _mutate(
            transport,
            report,
            _UPDATE_QUERY,
            {
                "projectId": project.github_project_id,
                "itemId": item_id,
                "fieldId": field_id,
                "optionId": option_id,
            },
        )
        action["status"] = "applied" if result.ok else "failed"
        return
    action["status"] = "failed"


def _mutate(transport, report: SyncReport, query: str, variables: dict) -> GithubResult:
    if "deleteProjectV2Item" in query:
        report.network_writes += 0
        return GithubResult(data=None, graphql_errors=({"code": "FORBIDDEN"},))
    report.attempted_network = True
    result = transport.query(query, dict(variables))
    report.network_writes += 1
    return result


def _find_existing(normalized, action, ledger) -> str:
    delivery_id = action.get("delivery_id") or ""
    known_item = str(ledger.get("tasks", {}).get(action.get("task_id"), {}).get("item_id") or "")
    for item in normalized:
        item_id = str(item.get("id") or "")
        if not item_id:
            continue
        body = str(item.get("body") or "")
        if item.get("task_id") == action.get("task_id") or _marker_value(body, _TASK_MARKER) == action.get("task_id"):
            return item_id
        if delivery_id and (item.get("delivery_id") == delivery_id or _marker_value(body, _DELIVERY_MARKER) == delivery_id):
            return item_id
        if known_item and item_id == known_item:
            return item_id
    return ""


def _created_id(result: GithubResult) -> str:
    data = result.data or {}
    for key in ("addProjectV2DraftIssue",):
        node = data.get(key) or {}
        if isinstance(node, Mapping):
            item = node.get("projectItem") or node.get("item") or {}
            if isinstance(item, Mapping) and item.get("id"):
                return str(item["id"])
    return ""


def _overlay_event_state(normalized: list, ledger: Mapping[str, Any]) -> list:
    items = list(normalized)
    by_task = {item.get("task_id"): item for item in items if item.get("task_id")}
    for task_id, link in ledger.get("tasks", {}).items():
        if not isinstance(link, Mapping):
            continue
        external = link.get("external")
        if not isinstance(external, dict) or not external:
            continue
        if task_id in by_task:
            continue
        current = {
            "id": link.get("item_id") or "",
            "task_id": task_id,
            "title": str(external.get("title") or ""),
            "body": str(external.get("body") or ""),
            "status": str(external.get("status") or ""),
            "login": str(external.get("login") or ""),
            "archived": bool(link.get("archived")),
        }
        items.append(current)
        by_task[task_id] = current
    return items


def _event_sort_key(raw: Mapping[str, Any]):
    stamp = str(raw.get("external_updated_at") or raw.get("updatedAt") or "")
    parsed = _parse_time(stamp)
    if parsed is None:
        return datetime.min.replace(tzinfo=timezone.utc)
    return parsed


def _is_acceptance_status(status: str) -> bool:
    text = status.strip()
    if text in _ACCEPTANCE_TEXT:
        return True
    return text.lower() in {"accepted", "acceptance"}


def _contract_released(task: Mapping[str, Any], link: Mapping[str, Any]) -> bool:
    stamp = task.get("contract_updated_at")
    locked = link.get("contract_locked_at")
    if not isinstance(stamp, str) or not stamp or not isinstance(locked, str) or not locked:
        return False
    if _is_older(stamp, locked):
        return False
    return stamp != locked


def _error_code(result: GithubResult) -> str:
    for err in result.graphql_errors:
        if isinstance(err, Mapping):
            code = err.get("code") or err.get("type") or ""
            if code:
                return str(code)
        elif isinstance(err, str) and err:
            return err
    return ""


def _diff_tasks(project, tasks, normalized, ledger, report: SyncReport) -> None:
    by_id = {str(task.get("id")): task for task in tasks if isinstance(task, dict) and task.get("id")}
    links = ledger.setdefault("tasks", {})
    seen = set()
    for item in normalized:
        task_id = item.get("task_id") or _task_id_for_item(links, item.get("id"))
        if not task_id or task_id not in by_id:
            continue
        seen.add(task_id)
        task = by_id[task_id]
        link = links.setdefault(task_id, {})
        if item.get("id"):
            link["item_id"] = item["id"]
        link["archived"] = bool(item.get("archived"))
        external = {
            "title": item.get("title") or "",
            "body": item.get("body") or "",
            "status": item.get("status") or "",
            "assignee": "",
        }
        login = item.get("login") or ""
        if login:
            mapped = project.identity_map.get(login)
            if mapped is None:
                _add_result(report, task_id, "unmapped_actor", login=login)
            else:
                external["assignee"] = mapped
        yy_view = _yy_view(task)
        stored = link.get("baseline")
        link["external"] = external
        if _is_acceptance_status(str(external.get("status") or "")):
            _add_result(report, task_id, "EXTERNAL_ACCEPTANCE_IGNORED")
        if not isinstance(stored, dict) or not isinstance(stored.get("yy"), dict) or not isinstance(stored.get("gh"), dict):
            link["baseline"] = {"yy": dict(yy_view), "gh": dict(external)}
        elif link.get("contract_locked") and not _contract_released(task, link):
            _add_result(report, task_id, "CONTRACT_CHANGED")
        else:
            conflict_fields = []
            contract = False
            for name in _PROJECTION_FIELDS:
                yy_changed = yy_view.get(name, "") != stored["yy"].get(name, "")
                gh_changed = external.get(name, "") != stored["gh"].get(name, "")
                if name in {"title", "body"} and gh_changed and not yy_changed:
                    contract = True
                if yy_changed and gh_changed:
                    conflict_fields.append(name)
            if conflict_fields:
                _add_result(report, task_id, "CONFLICT", fields=conflict_fields)
            elif contract:
                link["contract_locked"] = True
                link["contract_locked_at"] = str(link.get("external_updated_at") or "locked")
                _add_result(report, task_id, "CONTRACT_CHANGED")
        if task.get("is_deleted") and not link.get("item_id"):
            _add_result(report, task_id, "NO_EXTERNAL_ITEM")
    for task_id, task in by_id.items():
        if task_id in seen:
            continue
        link = links.get(task_id, {})
        if task.get("is_deleted") and not link.get("item_id"):
            _add_result(report, task_id, "NO_EXTERNAL_ITEM")


def _remember_items(ledger, normalized) -> None:
    links = ledger.setdefault("tasks", {})
    for item in normalized:
        task_id = item.get("task_id") or ""
        if not task_id:
            continue
        link = links.setdefault(task_id, {})
        if item.get("id"):
            link["item_id"] = item["id"]
        if item.get("archived"):
            link["archived"] = True


def _apply_events(events, ledger, project: ProjectRecord):
    outcomes = []
    writes = 0
    deliveries = ledger.setdefault("deliveries", {})
    links = ledger.setdefault("tasks", {})
    pending = []
    for raw in events:
        if not isinstance(raw, dict):
            outcomes.append({"outcome": "field_missing"})
            continue
        pending.append(raw)
    pending.sort(key=_event_sort_key, reverse=True)
    for raw in pending:
        delivery_id = str(raw.get("delivery_id") or raw.get("deliveryId") or "")
        updated = str(raw.get("external_updated_at") or raw.get("updatedAt") or "")
        item_id = str(raw.get("item_id") or raw.get("itemId") or raw.get("id") or "")
        task_id = str(raw.get("task_id") or raw.get("taskId") or item_id or "")
        if not delivery_id or not updated:
            outcomes.append({"delivery_id": delivery_id, "outcome": "field_missing"})
            continue
        if delivery_id in deliveries:
            outcomes.append({"delivery_id": delivery_id, "outcome": "duplicate"})
            continue
        link = links.setdefault(task_id, {})
        previous = str(link.get("external_updated_at") or "")
        if previous and _is_older(updated, previous):
            outcomes.append({"delivery_id": delivery_id, "outcome": "stale"})
            continue
        link["external_updated_at"] = updated
        if item_id:
            link["item_id"] = item_id
        external = link.setdefault("external", {})
        if not isinstance(external, dict):
            external = {}
            link["external"] = external
        for name in ("title", "body", "status"):
            if raw.get(name) is not None:
                external[name] = raw.get(name)
        login = raw.get("actor_login") or raw.get("login")
        if isinstance(login, str) and login:
            external["login"] = login
            if login not in project.identity_map:
                outcomes.append({"delivery_id": delivery_id, "outcome": "unmapped_actor", "login": login})
        deliveries[delivery_id] = {
            "applied": True,
            "item_id": item_id,
            "external_updated_at": updated,
        }
        writes += 1
        if not outcomes or outcomes[-1].get("delivery_id") != delivery_id or outcomes[-1].get("outcome") == "duplicate":
            outcomes.append({"delivery_id": delivery_id, "outcome": "applied"})
        elif outcomes[-1].get("outcome") != "unmapped_actor":
            outcomes.append({"delivery_id": delivery_id, "outcome": "applied"})
    return outcomes, writes


def _event_from_node(node: Mapping[str, Any]) -> dict | None:
    delivery_id = node.get("deliveryId") or node.get("delivery_id")
    updated = node.get("updatedAt") or node.get("external_updated_at")
    if not delivery_id or not updated:
        return None
    content = node.get("content") if isinstance(node.get("content"), Mapping) else {}
    return {
        "delivery_id": delivery_id,
        "external_updated_at": updated,
        "item_id": node.get("id") or "",
        "task_id": node.get("taskId") or node.get("task_id") or "",
        "title": content.get("title"),
        "body": content.get("body"),
    }


def _normalize_item(node: Mapping[str, Any]) -> dict:
    content = node.get("content") if isinstance(node.get("content"), Mapping) else {}
    status = ""
    task_id = str(node.get("taskId") or node.get("task_id") or "")
    values = ((node.get("fieldValues") or {}) if isinstance(node.get("fieldValues"), Mapping) else {}).get("nodes") or []
    if isinstance(values, list):
        for entry in values:
            if not isinstance(entry, Mapping):
                continue
            field_info = entry.get("field") if isinstance(entry.get("field"), Mapping) else {}
            name = str(field_info.get("name") or "")
            if name.lower() in {"status", "状态"}:
                status = str(entry.get("name") or entry.get("text") or "")
            if name in {"task_id", "Task", "YY-Flow"}:
                task_id = str(entry.get("text") or task_id)
    login = ""
    actors = node.get("actors") if isinstance(node.get("actors"), list) else []
    if actors and isinstance(actors[0], Mapping):
        login = str(actors[0].get("login") or "")
    raw_body = str(content.get("body") or "")
    if not task_id:
        task_id = _marker_value(raw_body, _TASK_MARKER)
    delivery_id = str(node.get("deliveryId") or "")
    if not delivery_id:
        delivery_id = _marker_value(raw_body, _DELIVERY_MARKER)
    return {
        "id": str(node.get("id") or ""),
        "task_id": task_id,
        "title": str(content.get("title") or ""),
        "body": _strip_markers(raw_body),
        "status": status,
        "login": login,
        "archived": bool(node.get("isArchived")),
        "updated_at": str(node.get("updatedAt") or ""),
        "delivery_id": delivery_id,
    }


def _yy_view(task: Mapping[str, Any]) -> dict:
    return {
        "title": str(task.get("name") or ""),
        "body": str(task.get("remarks") if task.get("remarks") is not None else task.get("body") or ""),
        "status": str(task.get("status") or ""),
        "assignee": str(task.get("assignee") or ""),
    }


def _add_result(report: SyncReport, task_id: str, outcome: str, **extra) -> None:
    row = {"task_id": task_id, "outcome": outcome}
    row.update(extra)
    report.task_results.append(row)
    report.diffs.append(dict(row))
    if outcome not in report.reasons:
        report.reasons.append(outcome)


def _task_id_for_item(links: Mapping[str, Any], item_id: str) -> str:
    for task_id, link in links.items():
        if isinstance(link, Mapping) and link.get("item_id") == item_id:
            return str(task_id)
    return ""


def _page_info(result: GithubResult) -> Mapping[str, Any] | None:
    extracted = None
    data = result.data or {}
    node = data.get("node") if isinstance(data, Mapping) else None
    if isinstance(node, Mapping):
        items = node.get("items")
        if isinstance(items, Mapping) and isinstance(items.get("pageInfo"), Mapping):
            extracted = items["pageInfo"]
    if isinstance(result.page_info, Mapping) and "hasNextPage" in result.page_info:
        return result.page_info
    return extracted


def _nodes(result: GithubResult):
    data = result.data or {}
    node = data.get("node") if isinstance(data, Mapping) else None
    if not isinstance(node, Mapping):
        return None
    items = node.get("items")
    if not isinstance(items, Mapping) or "nodes" not in items:
        return None
    nodes = items.get("nodes")
    return nodes if isinstance(nodes, list) else None


def _missing_payload(result: GithubResult) -> bool:
    return not result.graphql_errors and result.http_status in {None, 200} and not result.ok


def _is_forbidden(result: GithubResult) -> bool:
    if result.http_status in {401, 403}:
        return True
    for err in result.graphql_errors:
        if not isinstance(err, Mapping):
            continue
        code = str(err.get("type") or err.get("code") or "")
        if code.upper() in {"FORBIDDEN", "UNAUTHORIZED", "INSUFFICIENT_SCOPES"}:
            return True
    return False


def _is_rate_limited(result: GithubResult) -> bool:
    if result.http_status == 429:
        return True
    if result.rate_limit_reset_at is not None and not result.ok:
        return True
    for err in result.graphql_errors:
        text = err if isinstance(err, str) else ""
        if isinstance(err, Mapping):
            text = "".join(str(err.get(key) or "") for key in ("message", "type", "code"))
        upper = text.upper()
        if "RATE_LIMITED" in upper or "RATE LIMIT" in upper:
            return True
    return False


def _is_mutation(query: str) -> bool:
    return query.lstrip().lower().startswith("mutation")


def _is_older(stamp: str, previous: str) -> bool:
    left = _parse_time(stamp)
    right = _parse_time(previous)
    if left is None or right is None:
        return False
    return left < right


def _parse_time(value: str):
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def _resolve_token(project, token, environ, registry: Registry | None) -> str:
    if token is not None:
        return token if isinstance(token, str) and token.strip() else ""
    env = os.environ if environ is None else environ
    if registry is not None:
        return registry.credential_value(project.project_id, env)
    named = env.get(getattr(project, "github_credential_env", "GITHUB_TOKEN"), "")
    if isinstance(named, str) and named.strip():
        return named
    fallback = env.get("GITHUB_TOKEN", "")
    if isinstance(fallback, str) and fallback.strip():
        return fallback
    return ""


def _load_tasks(project: ProjectRecord) -> list:
    path = project.board_file
    if not path or not os.path.isfile(path):
        return []
    try:
        with open(path, "r", encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, UnicodeError, json.JSONDecodeError):
        return []
    if isinstance(data, list):
        return [item for item in data if isinstance(item, dict)]
    if isinstance(data, dict) and isinstance(data.get("tasks"), list):
        return [item for item in data["tasks"] if isinstance(item, dict)]
    return []


def _sync_dir(project: ProjectRecord) -> str:
    root = os.path.realpath(project.data_root)
    target = os.path.realpath(os.path.join(root, "mcp_github_sync"))
    if os.path.commonpath([root, target]) != root:
        raise OSError("同步目录无效")
    return target


def _load_ledger(project: ProjectRecord) -> dict:
    path = os.path.join(_sync_dir(project), "ledger.json")
    if not os.path.isfile(path):
        return {"cursor": "", "tasks": {}, "deliveries": {}, "actions": []}
    try:
        with open(path, "r", encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, UnicodeError, json.JSONDecodeError):
        return {"cursor": "", "tasks": {}, "deliveries": {}, "actions": []}
    if not isinstance(data, dict):
        return {"cursor": "", "tasks": {}, "deliveries": {}, "actions": []}
    data.setdefault("cursor", "")
    data.setdefault("tasks", {})
    data.setdefault("deliveries", {})
    data.setdefault("actions", [])
    return data


def _save_ledger(project: ProjectRecord, ledger: Mapping[str, Any]) -> None:
    directory = _sync_dir(project)
    os.makedirs(directory, exist_ok=True)
    target = os.path.join(directory, "ledger.json")
    temporary = target + ".tmp"
    payload = json.dumps(ledger, ensure_ascii=False, indent=2)
    with open(temporary, "w", encoding="utf-8") as handle:
        handle.write(payload)
    os.replace(temporary, target)


def _save_diff(project: ProjectRecord, report: SyncReport) -> None:
    directory = _sync_dir(project)
    os.makedirs(directory, exist_ok=True)
    target = os.path.join(directory, "diff_report.json")
    temporary = target + ".tmp"
    with open(temporary, "w", encoding="utf-8") as handle:
        json.dump(report.public_dict(), handle, ensure_ascii=False, indent=2)
    os.replace(temporary, target)


def _result_from_http(status: int, raw: bytes, headers) -> GithubResult:
    reset = _reset_header(headers)
    try:
        body = json.loads(raw.decode("utf-8")) if raw else {}
    except (UnicodeError, json.JSONDecodeError):
        body = {}
    if not isinstance(body, dict):
        body = {}
    errors = body.get("errors") or ()
    if not isinstance(errors, list):
        errors = ()
    cleaned = []
    for err in errors:
        if isinstance(err, dict):
            extensions = err.get("extensions") if isinstance(err.get("extensions"), dict) else {}
            code = extensions.get("code") or err.get("type") or err.get("code") or "GRAPHQL_ERROR"
            cleaned.append({"code": str(code)[:64]})
        else:
            cleaned.append({"code": "GRAPHQL_ERROR"})
    errors = tuple(cleaned)
    data = body.get("data")
    if not isinstance(data, dict) or (not data and (errors or status >= 400)):
        data = None
    page_info = None
    if isinstance(data, dict):
        node = data.get("node")
        if isinstance(node, dict):
            items = node.get("items")
            if isinstance(items, dict) and isinstance(items.get("pageInfo"), dict):
                page_info = items["pageInfo"]
    return GithubResult(
        data=data,
        graphql_errors=tuple(errors),
        http_status=status,
        rate_limit_reset_at=reset,
        page_info=page_info,
    )


def _reset_header(headers) -> float | None:
    if headers is None:
        return None
    raw = None
    try:
        raw = headers.get("X-RateLimit-Reset")
    except AttributeError:
        raw = None
    if raw is None:
        return None
    try:
        return float(raw)
    except (TypeError, ValueError):
        return None


def _wall_time() -> float:
    return datetime.now(timezone.utc).timestamp()


def _wall_sleep(seconds: float) -> None:
    import time

    time.sleep(seconds)
