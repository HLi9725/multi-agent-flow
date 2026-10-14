# -*- coding: utf-8 -*-
"""人类验收确认。MCP 只申请；本机 CLI 才批准或拒绝。"""

from __future__ import annotations

import contextlib
import ctypes
import glob
import hashlib
import io
import json
import os
import re
import secrets
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Mapping

from ..core import file_lock
from ..core.evidence_schema import EvidenceError, EvidenceType
from ..core.evidence_store import EvidenceStore
from . import auth
from .audit_store import AuditStore, AuditWriteError
from .errors import McpError, error
from .registry import ProjectRecord, Registry, assert_registered_path

ACCEPTANCE_TTL = timedelta(minutes=15)
AUDIT_PENDING_MESSAGE = "业务已应用、审计待补记"
_ID_RE = re.compile(r"^[A-Za-z0-9_-]{8,128}$")
_SHA_RE = re.compile(r"^[0-9a-f]{40}$")
_PUBLIC_FIELDS = (
    "project_id",
    "task_id",
    "candidate_sha",
    "contract_hash",
    "quality_evidence_summary",
    "action",
    "confirmation_id",
    "expires_at",
    "status",
    "task_version",
    "evidence_id",
)


def acceptance_directory(project: ProjectRecord) -> str:
    path = os.path.realpath(os.path.join(project.data_root, "mcp_auth", "acceptance"))
    assert_registered_path(project, path)
    return path


def load_acceptance_record(registry: Registry, confirmation_id: str) -> Dict[str, Any] | None:
    """从磁盘读回申请。进程中断后仍可查询，不依赖内存。"""
    found = _locate(registry, confirmation_id)
    if found is None:
        return None
    _project, _path, record = found
    return dict(record)


def matching_application(
    project: ProjectRecord,
    *,
    principal_id: str,
    task_id: str,
    idempotency_key: str,
    digest: str,
) -> Dict[str, Any] | None:
    directory = acceptance_directory(project)
    if not os.path.isdir(directory):
        return None
    for name in os.listdir(directory):
        if not name.endswith(".json"):
            continue
        path = os.path.join(directory, name)
        record = _read_json(path)
        if not isinstance(record, dict):
            continue
        if (
            record.get("principal_id") == principal_id
            and record.get("task_id") == task_id
            and record.get("idempotency_key") == idempotency_key
            and record.get("request_sha256") == digest
            and record.get("project_id") == project.project_id
        ):
            return record
    return None


def create_application(
    project: ProjectRecord,
    principal: auth.AuthContext,
    cleaned: Mapping[str, Any],
    ledger: Any,
    digest: str,
) -> Dict[str, Any]:
    """写入申请文件。不改看板状态，也不把环境变量当成同意。"""
    task_id = str(cleaned["task_id"])
    card = _card(project, task_id)
    if card is None or card.get("is_deleted"):
        raise error("NOT_FOUND")
    if str(card.get("status") or "") != "已完成":
        raise error("ACCEPTANCE_REQUIRED", details={"reason": "STATUS", "current": str(card.get("status") or "")})
    from .service import task_version

    current = task_version(card)
    if current != cleaned["expected_version"]:
        raise error("CONFLICT", details={"reason": "VERSION", "task_version": current})
    checkpoint = _authority(project, card)
    evidence = _read_evidence(project, str(cleaned["evidence_id"]))
    problem = _quality_problem(project, checkpoint, task_id, evidence)
    if problem == "MISSING_CANDIDATE":
        raise error("GATE_REJECTED", details={"reason": "CANDIDATE"})
    if problem == "CANDIDATE":
        raise error("CONFLICT", details={"reason": "CANDIDATE"})
    if problem is not None:
        raise error("GATE_REJECTED", details={"reason": problem})
    if checkpoint is None or evidence is None:
        raise error("GATE_REJECTED", details={"reason": "MISSING_QUALITY"})
    existing = matching_application(
        project,
        principal_id=principal.principal_id,
        task_id=task_id,
        idempotency_key=str(cleaned["idempotency_key"]),
        digest=digest,
    )
    if existing is not None:
        record = existing
    else:
        confirmation_id = _new_id(project)
        now = datetime.now(timezone.utc)
        record = {
            "confirmation_id": confirmation_id,
            "project_id": project.project_id,
            "task_id": task_id,
            "principal_id": principal.principal_id,
            "evidence_id": evidence.evidence_id,
            "evidence_content_hash": evidence.content_hash,
            "baseline_commit": evidence.baseline_commit,
            "candidate_sha": str(checkpoint.get("candidate_commit") or ""),
            "contract_hash": _contract_hash(project, card, evidence, checkpoint),
            "quality_evidence_summary": _quality_summary(project, checkpoint, task_id),
            "action": "accept",
            "expires_at": (now + ACCEPTANCE_TTL).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "idempotency_key": str(cleaned["idempotency_key"]),
            "request_sha256": digest,
            "decision": "",
            "decided_by": "",
            "consumed": False,
            "result": {},
        }
        _write_record(project, record)
    body = _public_body(record, card)
    try:
        ledger.bind(
            principal_id=principal.principal_id,
            tool="request_acceptance",
            key=str(cleaned["idempotency_key"]),
            digest=digest,
            target=dict(body),
        )
    except (OSError, file_lock.LockBusyError):
        raise error("GATE_REJECTED", details={"reason": "LEDGER"}) from None
    body["_project_id"] = project.project_id
    body["_task_id"] = task_id
    body["_allow"] = list(_PUBLIC_FIELDS) + ["replayed", "code"]
    return body


def decide_acceptance(
    registry: Registry,
    confirmation_id: str,
    decision: str,
    *,
    user_id: str = "",
    audit: AuditStore | None = None,
) -> Dict[str, Any]:
    """本机批准或拒绝。身份只看操作系统用户，忽略 user_id。"""
    del user_id
    if decision not in {"approve", "reject"}:
        return _failure("VALIDATION", "DECISION", "")
    os_user = _os_user()
    principal = auth.resolve_stdio_principal(registry, os_user)
    if principal is None or not os_user:
        return _failure("UNAUTHENTICATED", "OS_USER", "")
    if principal.disabled:
        return _failure("REVOKED", "OS_USER", "")
    located = _locate(registry, confirmation_id)
    if located is None:
        return _failure("NOT_FOUND", "CONFIRMATION", "")
    project, path, _record = located
    if "acceptance" not in auth.scopes_for(principal, project.project_id):
        return _failure("FORBIDDEN", "SCOPE", project.project_id)
    lock_path = path + ".lock"
    deadline = time.time() + 30
    while True:
        try:
            with file_lock.acquire_lock(lock_path, blocking=True, timeout=5.0):
                fresh = _read_json(path)
                if not isinstance(fresh, dict):
                    return _failure("NOT_FOUND", "CONFIRMATION", project.project_id)
                return _decide_locked(registry, project, path, fresh, principal, os_user, decision, audit)
        except file_lock.LockBusyError:
            if time.time() >= deadline:
                return _failure("CONFLICT", "IN_PROGRESS", project.project_id)
        except PermissionError:
            if time.time() >= deadline:
                return _failure("CONFLICT", "IN_PROGRESS", project.project_id)
            time.sleep(0.02)
        except AuditWriteError:
            return _failure("GATE_REJECTED", "AUDIT", project.project_id)


def _decide_locked(
    registry: Registry,
    project: ProjectRecord,
    path: str,
    record: Dict[str, Any],
    principal: auth.AuthContext,
    os_user: str,
    decision: str,
    audit: AuditStore | None,
) -> Dict[str, Any]:
    del registry
    stored = str(record.get("decision") or "")
    if record.get("consumed"):
        if str(record.get("project_id") or "") != project.project_id:
            return _failure("FORBIDDEN", "PROJECT", project.project_id, record)
        if str(record.get("principal_id") or "") != principal.principal_id:
            return _failure("FORBIDDEN", "PRINCIPAL", project.project_id, record)
        if stored != decision:
            return _failure("CONFLICT", "DECISION", project.project_id, record)
        if record.get("audit_ok") is False:
            return _backfill_audit(project, path, record, principal, decision, audit)
        return _replay_consumed(project, record)
    problem = _binding_problem(project, record, principal)
    if problem is not None:
        code, reason = problem
        return _failure(code, reason, project.project_id, record)
    if stored and stored != decision:
        return _failure("CONFLICT", "DECISION", project.project_id, record)
    if audit is not None:
        audit.append(
            principal_id=principal.principal_id,
            project_id=project.project_id,
            tool="acceptance_decide",
            code="PENDING",
            task_id=str(record.get("task_id") or ""),
            arguments={"confirmation_id": record.get("confirmation_id"), "decision": decision},
            required=True,
            event_id=_event_id(record, decision),
        )
    if decision == "approve":
        gate_problem = _approve_gate(project, record)
        if gate_problem is not None:
            return _failure("GATE_REJECTED", gate_problem, project.project_id, record)
    if not stored:
        record["decision"] = decision
        record["decided_by"] = os_user
        if not _persist(project, path, record):
            return _failure("GATE_REJECTED", "LEDGER", project.project_id, record)
    finished = _apply_decision(project, record, decision)
    if not finished.get("ok"):
        return finished
    record["consumed"] = True
    record["audit_ok"] = False
    record["result"] = {
        "task_id": str(record.get("task_id") or ""),
        "status": finished.get("status") or "",
        "task_version": finished.get("task_version") or "",
        "decision": decision,
        "confirmation_id": record.get("confirmation_id"),
    }
    if not _persist(project, path, record):
        return _failure("GATE_REJECTED", "LEDGER", project.project_id, record)
    reloaded = _read_json(path)
    if not isinstance(reloaded, dict) or reloaded.get("consumed") is not True:
        return _failure("GATE_REJECTED", "LEDGER", project.project_id, record)
    record = reloaded
    if not _append_ok_audit(project, record, principal, decision, audit):
        return _pending_audit(project, record, decision, finished.get("status") or "", finished.get("task_version") or "")
    record["audit_ok"] = True
    if not _persist(project, path, record):
        return _pending_audit(project, record, decision, finished.get("status") or "", finished.get("task_version") or "")
    return _success(project, record, decision, finished.get("status") or "", finished.get("task_version") or "", "")


def _backfill_audit(
    project: ProjectRecord,
    path: str,
    record: Dict[str, Any],
    principal: auth.AuthContext,
    decision: str,
    audit: AuditStore | None,
) -> Dict[str, Any]:
    """业务已经落盘时只补记审计，不再次流转。"""
    card = _card(project, str(record.get("task_id") or ""))
    status = "" if card is None or card.get("status") is None else str(card.get("status"))
    expected = "已验收" if decision == "approve" else "已退回"
    if status != expected:
        return _failure("CONFLICT", "DECISION", project.project_id, record)
    result = record.get("result") if isinstance(record.get("result"), dict) else {}
    version = str(result.get("task_version") or _version(card or {}))
    if not _append_ok_audit(project, record, principal, decision, audit):
        return _pending_audit(project, record, decision, status, version)
    record["audit_ok"] = True
    if not _persist(project, path, record):
        return _pending_audit(project, record, decision, status, version)
    return _success(project, record, decision, status, version, "REPLAY", replayed=True)


def _append_ok_audit(
    project: ProjectRecord,
    record: Mapping[str, Any],
    principal: auth.AuthContext,
    decision: str,
    audit: AuditStore | None,
) -> bool:
    if audit is None:
        return True
    try:
        audit.append(
            principal_id=principal.principal_id,
            project_id=project.project_id,
            tool="acceptance_decide",
            code="OK",
            task_id=str(record.get("task_id") or ""),
            arguments={"confirmation_id": record.get("confirmation_id"), "decision": decision},
            required=True,
            event_id=_event_id(record, decision + ":done"),
        )
    except AuditWriteError:
        return False
    return True


def _pending_audit(
    project: ProjectRecord,
    record: Mapping[str, Any],
    decision: str,
    status: str,
    task_version: str,
) -> Dict[str, Any]:
    return {
        "ok": True,
        "code": "APPLIED_AUDIT_PENDING",
        "reason": "AUDIT_PENDING",
        "message": AUDIT_PENDING_MESSAGE,
        "project_id": project.project_id,
        "task_id": str(record.get("task_id") or ""),
        "confirmation_id": str(record.get("confirmation_id") or ""),
        "decision": decision,
        "status": status,
        "task_version": task_version,
        "consumed": True,
        "replayed": False,
    }


def _success(
    project: ProjectRecord,
    record: Mapping[str, Any],
    decision: str,
    status: str,
    task_version: str,
    reason: str,
    *,
    replayed: bool = False,
) -> Dict[str, Any]:
    body = {
        "ok": True,
        "code": "OK",
        "reason": reason,
        "message": "",
        "project_id": project.project_id,
        "task_id": str(record.get("task_id") or ""),
        "confirmation_id": str(record.get("confirmation_id") or ""),
        "decision": decision,
        "status": status,
        "task_version": task_version,
        "consumed": True,
    }
    if replayed:
        body["replayed"] = True
    return body


def _replay_consumed(project: ProjectRecord, record: Mapping[str, Any]) -> Dict[str, Any]:
    card = _card(project, str(record.get("task_id") or ""))
    status = "" if card is None or card.get("status") is None else str(card.get("status"))
    decision = str(record.get("decision") or "")
    expected = "已验收" if decision == "approve" else "已退回"
    if status != expected:
        return _failure("CONFLICT", "DECISION", project.project_id, record)
    result = record.get("result") if isinstance(record.get("result"), dict) else {}
    return {
        "ok": True,
        "code": "OK",
        "reason": "REPLAY",
        "project_id": project.project_id,
        "task_id": str(record.get("task_id") or ""),
        "confirmation_id": str(record.get("confirmation_id") or ""),
        "decision": decision,
        "status": status,
        "task_version": str(result.get("task_version") or _version(card or {})),
        "consumed": True,
        "replayed": True,
    }


def _apply_decision(project: ProjectRecord, record: Mapping[str, Any], decision: str) -> Dict[str, Any]:
    task_id = str(record.get("task_id") or "")
    card = _card(project, task_id)
    if card is None:
        return _failure("NOT_FOUND", "TASK", project.project_id, record)
    status = str(card.get("status") or "")
    if decision == "approve" and status == "已验收":
        return {"ok": True, "status": status, "task_version": _version(card)}
    if decision == "reject" and status == "已退回":
        return {"ok": True, "status": status, "task_version": _version(card)}
    if status != "已完成":
        return _failure("ACCEPTANCE_REQUIRED", "STATUS", project.project_id, record)
    target = "已验收" if decision == "approve" else "已退回"
    assignee = str(card.get("assignee") or card.get("owner") or "李开发")
    end_time = str(card.get("end_time") or card.get("end_date") or "")
    version_state: Dict[str, Any] = {}

    def run() -> bool:
        from scripts.transition_task import transition_task_pipeline

        sink = io.StringIO()
        with contextlib.redirect_stdout(sink):
            return transition_task_pipeline(
                config_path=project.board_config or None,
                task_id=task_id,
                current_role="PM",
                from_status="已完成",
                to_status=target,
                assignee=assignee,
                task_type=str(card.get("type") or "A"),
                end_time=end_time if target == "已验收" else "",
                delegated_by="USER",
                delegation_reason="explicit_user",
                expected_card_version=_version(card),
                version_state=version_state,
                remarks="用户拒绝验收" if decision == "reject" else "",
            )

    from scripts import paths

    token = paths.bind_request_data_root(project.data_root)
    try:
        ok = run()
    finally:
        paths.reset_request_data_root(token)
    if version_state.get("conflict"):
        return _failure("CONFLICT", "VERSION", project.project_id, record)
    if not ok:
        return _failure("GATE_REJECTED", "TRANSITION", project.project_id, record)
    updated = _card(project, task_id) or {}
    if str(updated.get("status") or "") != target:
        return _failure("GATE_REJECTED", "TRANSITION", project.project_id, record)
    return {"ok": True, "status": target, "task_version": _version(updated)}


def _approve_gate(project: ProjectRecord, record: Mapping[str, Any]) -> str | None:
    task_id = str(record.get("task_id") or "")
    card = _card(project, task_id)
    checkpoint = None if card is None else _authority(project, card)
    evidence = _read_evidence(project, str(record.get("evidence_id") or ""))
    problem = _quality_problem(project, checkpoint, task_id, evidence)
    if problem == "MISSING_CANDIDATE":
        return "CANDIDATE"
    if problem is not None:
        return problem
    try:
        _mint_and_validate(project, record)
    except McpError:
        return "GATE"
    except Exception:
        return "GATE"
    return None


def _mint_and_validate(project: ProjectRecord, record: Mapping[str, Any]) -> str:
    from ..core.agent_schema import AgentHandle, ConfirmationResult, HostCapabilities
    from ..core.evidence_gate import EvidenceGate, EvidenceGateError
    from ..core.evidence_schema import EvidenceMetadata, EvidenceRecord
    from ..core.evidence_store import EvidenceStore
    from ..core.production_runner import _extract_capabilities_extra
    from ..core.evidence_gate import EvidenceValidationContext

    confirmation_id = str(record.get("confirmation_id") or "")
    evidence_id = "mcpacc" + hashlib.sha256(confirmation_id.encode("utf-8")).hexdigest()[:24]
    invocation = "accept-" + hashlib.sha256(b"inv" + confirmation_id.encode("utf-8")).hexdigest()[:20]
    caps = HostCapabilities(is_real_host=True)
    extra = _extract_capabilities_extra(caps)
    extra.update(
        {
            "confirmation_id": confirmation_id,
            "user_source": "explicit_user",
            "confirmed_at": time.time(),
        }
    )
    store = EvidenceStore(project.evidence_root)
    try:
        store.read(evidence_id)
    except EvidenceError:
        store.append(
            EvidenceRecord(
                evidence_id=evidence_id,
                evidence_type=EvidenceType.USER_CONFIRMATION,
                baseline_commit=str(record.get("baseline_commit") or ""),
                result_commit=str(record.get("candidate_sha") or ""),
                artifacts=(),
                metadata=EvidenceMetadata(
                    project_id=project.project_id,
                    task_id=str(record.get("task_id") or ""),
                    actor_role="PM",
                    host_id="yy-flow-host",
                    adapter="yy-flow",
                    host_session_id="mcp-human-session",
                    host_invocation_id=invocation,
                    is_real_host=True,
                    workspace_mode="user_confirmation",
                    transition_from="已完成",
                    transition_to="已验收",
                    created_at=time.time(),
                    extra=extra,
                ),
            )
        )
    try:
        EvidenceGate(store, project.data_root).validate_evidence(
            evidence_id,
            EvidenceValidationContext(
                project_id=project.project_id,
                task_id=str(record.get("task_id") or ""),
                actor_role="PM",
                transition_from="已完成",
                transition_to="已验收",
                baseline_commit=str(record.get("baseline_commit") or ""),
                result_commit=str(record.get("candidate_sha") or ""),
                expected_invocation_id=invocation,
                expected_adapter="yy-flow",
                expected_workspace_mode="user_confirmation",
                expected_evidence_type=EvidenceType.USER_CONFIRMATION,
                host_handle=AgentHandle(
                    session_id="mcp-human-session",
                    host_id="yy-flow-host",
                    is_real_host=True,
                ),
                expected_capabilities=caps,
                confirmation_result=ConfirmationResult(confirmation_id, "accept", True, True),
            ),
        )
    except EvidenceGateError as exc:
        raise error("GATE_REJECTED", details={"reason": "GATE"}) from exc
    return evidence_id


def _binding_problem(
    project: ProjectRecord,
    record: Mapping[str, Any],
    principal: auth.AuthContext,
) -> tuple[str, str] | None:
    if str(record.get("project_id") or "") != project.project_id:
        return "FORBIDDEN", "PROJECT"
    if str(record.get("principal_id") or "") != principal.principal_id:
        return "FORBIDDEN", "PRINCIPAL"
    if not record.get("consumed") and _expired(str(record.get("expires_at") or "")):
        return "ACCEPTANCE_REQUIRED", "EXPIRED"
    evidence = _read_evidence(project, str(record.get("evidence_id") or ""))
    card = _card(project, str(record.get("task_id") or ""))
    if evidence is None or card is None:
        return "GATE_REJECTED", "MISSING_QUALITY"
    if evidence.metadata.task_id != record.get("task_id") or str(card.get("id") or "") != record.get("task_id"):
        return "FORBIDDEN", "TASK"
    if evidence.metadata.project_id != project.project_id:
        return "FORBIDDEN", "PROJECT"
    checkpoint = None if card is None else _authority(project, card)
    latest = "" if checkpoint is None else str(checkpoint.get("candidate_commit") or "")
    if not _SHA_RE.fullmatch(latest) or latest != str(record.get("candidate_sha") or "") or latest != evidence.result_commit:
        return "CONFLICT", "CANDIDATE"
    if str(record.get("contract_hash") or "") != _contract_hash(project, card, evidence, checkpoint):
        return "CONFLICT", "CONTRACT"
    if str(record.get("evidence_content_hash") or "") != str(evidence.content_hash or ""):
        return "CONFLICT", "CONTRACT"
    return None


def _public_body(record: Mapping[str, Any], card: Mapping[str, Any]) -> Dict[str, Any]:
    return {
        "project_id": str(record.get("project_id") or ""),
        "task_id": str(record.get("task_id") or ""),
        "candidate_sha": str(record.get("candidate_sha") or ""),
        "contract_hash": str(record.get("contract_hash") or ""),
        "quality_evidence_summary": record.get("quality_evidence_summary") or {},
        "action": "accept",
        "confirmation_id": str(record.get("confirmation_id") or ""),
        "expires_at": str(record.get("expires_at") or ""),
        "status": "" if card.get("status") is None else str(card.get("status")),
        "task_version": _version(card),
        "evidence_id": str(record.get("evidence_id") or ""),
    }


def _quality_summary(project: ProjectRecord, checkpoint: Mapping[str, Any], task_id: str) -> Dict[str, Any]:
    candidate = str(checkpoint.get("candidate_commit") or "")
    profile = _profile(checkpoint)
    roles: list[str] = []
    evidence_ids: list[str] = []
    for item in _iter_evidence(project):
        role = _matched_role(item, profile, candidate, project.project_id, task_id, checkpoint)
        if role is None:
            continue
        roles.append(role)
        evidence_ids.append(item.evidence_id)
    return {
        "profile": profile,
        "candidate_sha": candidate,
        "roles": sorted(set(roles)),
        "evidence_ids": sorted(evidence_ids),
        "acceptance_criteria_hash": str(checkpoint.get("acceptance_criteria_hash") or ""),
        "acceptance_criteria_items": _criteria_items(checkpoint),
    }


def _contract_hash(project: ProjectRecord, card: Mapping[str, Any], evidence: Any, checkpoint: Mapping[str, Any]) -> str:
    material = {
        "acceptance_criteria": str(checkpoint.get("acceptance_criteria") or ""),
        "acceptance_criteria_hash": str(checkpoint.get("acceptance_criteria_hash") or ""),
        "acceptance_criteria_items": _criteria_items(checkpoint),
        "baseline_commit": evidence.baseline_commit,
        "candidate_sha": str(checkpoint.get("candidate_commit") or ""),
        "card_description": str(card.get("description") or ""),
        "card_process": str(card.get("process") or ""),
        "card_remarks": str(card.get("remarks") or ""),
        "evidence_content_hash": evidence.content_hash or "",
        "evidence_id": evidence.evidence_id,
        "profile_id": _profile(checkpoint),
        "project_id": project.project_id,
        "task_id": str(card.get("id") or evidence.metadata.task_id),
        "task_name": str(card.get("name") or ""),
        "task_type": str(card.get("type") or ""),
    }
    raw = json.dumps(material, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def _failure(code: str, reason: str, project_id: str, record: Mapping[str, Any] | None = None) -> Dict[str, Any]:
    task_id = "" if record is None else str(record.get("task_id") or "")
    return {
        "ok": False,
        "code": code,
        "reason": reason,
        "project_id": project_id,
        "task_id": task_id,
        "confirmation_id": "" if record is None else str(record.get("confirmation_id") or ""),
        "decision": "" if record is None else str(record.get("decision") or ""),
        "status": "",
        "consumed": False,
    }


def _event_id(record: Mapping[str, Any], decision: str) -> str:
    raw = f"{record.get('project_id')}|{record.get('confirmation_id')}|{decision}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:64]


def _os_user() -> str:
    """读取进程令牌上的账户名。不读取环境变量，也不使用线程模拟令牌。"""
    if os.name == "nt":
        return _windows_process_user()
    try:
        import pwd

        return pwd.getpwuid(os.geteuid()).pw_name
    except (ImportError, KeyError, OSError, AttributeError):
        return ""


def _windows_process_user() -> str:
    try:
        advapi = ctypes.WinDLL("advapi32", use_last_error=True)
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.GetCurrentProcess.restype = ctypes.c_void_p
        kernel.CloseHandle.argtypes = [ctypes.c_void_p]
        advapi.OpenProcessToken.argtypes = [ctypes.c_void_p, ctypes.c_ulong, ctypes.POINTER(ctypes.c_void_p)]
        advapi.OpenProcessToken.restype = ctypes.c_int
        advapi.GetTokenInformation.argtypes = [
            ctypes.c_void_p,
            ctypes.c_ulong,
            ctypes.c_void_p,
            ctypes.c_ulong,
            ctypes.POINTER(ctypes.c_ulong),
        ]
        advapi.GetTokenInformation.restype = ctypes.c_int
        advapi.LookupAccountSidW.argtypes = [
            ctypes.c_wchar_p,
            ctypes.c_void_p,
            ctypes.c_wchar_p,
            ctypes.POINTER(ctypes.c_ulong),
            ctypes.c_wchar_p,
            ctypes.POINTER(ctypes.c_ulong),
            ctypes.POINTER(ctypes.c_ulong),
        ]
        advapi.LookupAccountSidW.restype = ctypes.c_int
        token = ctypes.c_void_p()
        if not advapi.OpenProcessToken(kernel.GetCurrentProcess(), 0x0008, ctypes.byref(token)):
            return ""
    except (AttributeError, OSError):
        return ""
    try:
        length = ctypes.c_ulong(0)
        advapi.GetTokenInformation(token, 1, None, 0, ctypes.byref(length))
        if length.value <= 0:
            return ""
        raw = ctypes.create_string_buffer(length.value)
        if not advapi.GetTokenInformation(token, 1, raw, length, ctypes.byref(length)):
            return ""
        sid = ctypes.cast(raw, ctypes.POINTER(ctypes.c_void_p))[0]
        name = ctypes.create_unicode_buffer(256)
        name_size = ctypes.c_ulong(len(name))
        domain = ctypes.create_unicode_buffer(256)
        domain_size = ctypes.c_ulong(len(domain))
        sid_use = ctypes.c_ulong()
        if not advapi.LookupAccountSidW(
            None,
            sid,
            name,
            ctypes.byref(name_size),
            domain,
            ctypes.byref(domain_size),
            ctypes.byref(sid_use),
        ):
            return ""
        return str(name.value or "").strip()
    except (AttributeError, OSError, ValueError):
        return ""
    finally:
        if token:
            kernel.CloseHandle(token)


def interactive_confirmation(confirmation_id: str) -> bool:
    """只从控制台设备读取确认编号。管道标准输入不能批准。"""
    import sys

    if confirmation_id == "" or not sys.stdin.isatty():
        return False
    try:
        if os.name == "nt":
            handle = open("CONIN$", "r", encoding="utf-8", errors="replace")
        else:
            handle = open("/dev/tty", "r", encoding="utf-8", errors="replace")
    except OSError:
        return False
    try:
        typed = handle.readline()
    except OSError:
        return False
    finally:
        handle.close()
    return isinstance(typed, str) and typed.strip() == confirmation_id


def _expired(value: str) -> bool:
    try:
        moment = datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    except ValueError:
        return True
    return moment <= datetime.now(timezone.utc)


def _new_id(project: ProjectRecord) -> str:
    directory = acceptance_directory(project)
    os.makedirs(directory, exist_ok=True)
    for _index in range(8):
        confirmation_id = secrets.token_urlsafe(32)
        if _ID_RE.fullmatch(confirmation_id) is None:
            continue
        path = os.path.join(directory, confirmation_id + ".json")
        if not os.path.exists(path):
            return confirmation_id
    raise error("GATE_REJECTED", details={"reason": "CONFIRMATION"})


def _write_record(project: ProjectRecord, record: Mapping[str, Any]) -> None:
    confirmation_id = str(record.get("confirmation_id") or "")
    if _ID_RE.fullmatch(confirmation_id) is None:
        raise error("VALIDATION", details={"reason": "CONFIRMATION"})
    directory = acceptance_directory(project)
    os.makedirs(directory, exist_ok=True)
    path = os.path.join(directory, confirmation_id + ".json")
    assert_registered_path(project, os.path.realpath(path))
    if not _atomic_write(path, record):
        raise error("GATE_REJECTED", details={"reason": "LEDGER"})


def _persist(project: ProjectRecord, path: str, record: Mapping[str, Any]) -> bool:
    try:
        assert_registered_path(project, os.path.realpath(path))
    except McpError:
        return False
    return _atomic_write(path, record)


def _atomic_write(path: str, record: Mapping[str, Any]) -> bool:
    temporary = path + ".tmp"
    try:
        payload = json.dumps(record, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        with open(temporary, "w", encoding="utf-8") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    except OSError:
        try:
            if os.path.exists(temporary):
                os.remove(temporary)
        except OSError:
            pass
        return False
    return True


def _locate(registry: Registry, confirmation_id: str) -> tuple[ProjectRecord, str, Dict[str, Any]] | None:
    if not isinstance(confirmation_id, str) or _ID_RE.fullmatch(confirmation_id) is None:
        return None
    found: list[tuple[ProjectRecord, str, Dict[str, Any]]] = []
    for project in registry.projects.values():
        path = os.path.join(acceptance_directory(project), confirmation_id + ".json")
        if not os.path.isfile(path):
            continue
        record = _read_json(path)
        if isinstance(record, dict) and record.get("confirmation_id") == confirmation_id:
            found.append((project, path, record))
    if len(found) != 1:
        return None
    return found[0]


def _read_json(path: str) -> Dict[str, Any] | None:
    try:
        with open(path, "r", encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, UnicodeError, json.JSONDecodeError):
        return None
    if not isinstance(data, dict):
        return None
    return data


def _authority(project: ProjectRecord, card: Mapping[str, Any]) -> Dict[str, Any] | None:
    """最新候选以 Runner 检查点为准；没有检查点时才读任务卡上的候选。"""
    task_id = str(card.get("id") or "")
    matches = glob.glob(os.path.join(project.data_root, "runner_checkpoints", "*", task_id + ".json"))
    if len(matches) > 1:
        return None
    if len(matches) == 1:
        data = _read_json(matches[0])
        if not isinstance(data, dict):
            return None
        try:
            from ..core.runner_schema import RunnerCheckpoint

            checkpoint = RunnerCheckpoint.from_dict(data)
        except (TypeError, ValueError, KeyError):
            return None
        snapshot = checkpoint.execution_spec_snapshot if isinstance(checkpoint.execution_spec_snapshot, Mapping) else {}
        options = checkpoint.execution_options if isinstance(checkpoint.execution_options, Mapping) else {}
        policy = options.get("workflow_policy") if isinstance(options.get("workflow_policy"), Mapping) else {}
        profile = "standard"
        found = str(policy.get("profile_id") or "")
        if found == "docs-short":
            profile = "docs"
        elif found in {"standard", "lite", "docs"}:
            profile = found
        candidate = str(checkpoint.candidate_commit or "")
        criteria_hash = str(snapshot.get("acceptance_criteria_hash") or "")
        if not _SHA_RE.fullmatch(candidate) or not criteria_hash:
            return None
        return {
            "candidate_commit": candidate,
            "acceptance_criteria_hash": criteria_hash,
            "acceptance_criteria": str(snapshot.get("acceptance_criteria") or ""),
            "acceptance_criteria_items": _criteria_items(snapshot),
            "profile_id": profile,
            "builder_session_id": str(checkpoint.builder_session_id or ""),
        }
    candidate = str(card.get("candidate_commit") or "")
    if not _SHA_RE.fullmatch(candidate):
        return None
    return {
        "candidate_commit": candidate,
        "acceptance_criteria_hash": str(card.get("acceptance_criteria_hash") or ""),
        "acceptance_criteria": str(card.get("acceptance_criteria") or ""),
        "acceptance_criteria_items": _criteria_items(card),
        "profile_id": "standard",
        "builder_session_id": "",
    }


def _profile(checkpoint: Mapping[str, Any]) -> str:
    profile = str(checkpoint.get("profile_id") or "")
    if profile in {"standard", "lite", "docs"}:
        return profile
    return "standard"


def _criteria_items(checkpoint: Mapping[str, Any]) -> list[Dict[str, str]]:
    raw = checkpoint.get("acceptance_criteria_items")
    items: list[Dict[str, str]] = []
    if not isinstance(raw, list):
        return items
    for item in raw:
        if isinstance(item, dict):
            items.append(
                {
                    "id": str(item.get("id") or item.get("criterion_id") or ""),
                    "text": str(item.get("text") or ""),
                }
            )
        elif isinstance(item, str):
            items.append({"id": "", "text": item})
    return items


def _iter_evidence(project: ProjectRecord) -> list[Any]:
    root = project.evidence_root
    if not root or not os.path.isdir(root):
        return []
    found = []
    for name in sorted(os.listdir(root)):
        if not name.endswith(".json"):
            continue
        record = _read_evidence(project, name[: -len(".json")])
        if record is not None:
            found.append(record)
    return found


def _matched_role(
    evidence: Any,
    profile: str,
    candidate: str,
    project_id: str,
    task_id: str,
    checkpoint: Mapping[str, Any],
) -> str | None:
    meta = evidence.metadata
    if meta.project_id != project_id or meta.task_id != task_id:
        return None
    if not meta.is_real_host or evidence.result_commit != candidate:
        return None
    if evidence.evidence_type == EvidenceType.USER_CONFIRMATION:
        return None
    role = str(meta.actor_role or "")
    if profile == "lite":
        builder = str(checkpoint.get("builder_session_id") or "")
        session = str(meta.host_session_id or "")
        if role != "VERIFIER" or not _role_passed(meta, "VERIFIER"):
            return None
        if "verifier" not in session.lower() or (builder and session == builder):
            return None
        return "VERIFIER"
    if profile == "docs":
        if role != "DOCS_CHECK" or not _role_passed(meta, "DOCS_CHECK"):
            return None
        return "DOCS_CHECK"
    if role not in {"REVIEWER", "QA"} or not _role_passed(meta, role):
        return None
    return role


def _role_passed(meta: Any, role: str) -> bool:
    extra = meta.extra
    if role == "REVIEWER":
        marked = str(extra.get("decision") or extra.get("review_decision") or "").upper()
        return marked == "PASS" or str(meta.transition_to or "") == "TESTING"
    if role == "QA":
        return str(extra.get("qa_decision") or extra.get("decision") or "").upper() == "PASS"
    if role == "VERIFIER":
        return extra.get("verifier_decision") == "PASS"
    if role == "DOCS_CHECK":
        marked = str(extra.get("decision") or "").upper()
        return marked == "PASS" or extra.get("verifier_decision") == "PASS"
    return False


def _quality_problem(
    project: ProjectRecord,
    checkpoint: Mapping[str, Any] | None,
    task_id: str,
    evidence: Any,
) -> str | None:
    if evidence is None:
        return "MISSING_QUALITY"
    if not evidence.baseline_commit or not evidence.content_hash:
        return "MISSING_QUALITY"
    if evidence.evidence_type == EvidenceType.USER_CONFIRMATION:
        return "EVIDENCE_TYPE"
    if checkpoint is None or not _SHA_RE.fullmatch(str(checkpoint.get("candidate_commit") or "")):
        return "MISSING_CANDIDATE"
    if evidence.metadata.task_id != task_id or evidence.metadata.project_id != project.project_id:
        return "EVIDENCE"
    candidate = str(checkpoint.get("candidate_commit") or "")
    if evidence.result_commit != candidate:
        return "CANDIDATE"
    profile = _profile(checkpoint)
    matched = [
        item
        for item in _iter_evidence(project)
        if _matched_role(item, profile, candidate, project.project_id, task_id, checkpoint) is not None
    ]
    if profile == "standard":
        reviewers = [item for item in matched if item.metadata.actor_role == "REVIEWER"]
        testers = [item for item in matched if item.metadata.actor_role == "QA"]
        if not reviewers or not testers:
            return "QUALITY_CHAIN"
        review_sessions = {str(item.metadata.host_session_id or "") for item in reviewers}
        test_sessions = {str(item.metadata.host_session_id or "") for item in testers}
        if review_sessions & test_sessions:
            return "QUALITY_CHAIN"
    elif not matched:
        return "QUALITY_CHAIN"
    allowed = {item.evidence_id for item in matched}
    if evidence.evidence_id not in allowed:
        return "QUALITY_CHAIN"
    return None


def _read_evidence(project: ProjectRecord, evidence_id: str) -> Any:
    if not evidence_id:
        return None
    try:
        return EvidenceStore(project.evidence_root).read(evidence_id)
    except (EvidenceError, OSError):
        return None


def _card(project: ProjectRecord, task_id: str) -> Dict[str, Any] | None:
    from .service import _read_card

    if not task_id:
        return None
    return _read_card(project, task_id)


def _version(card: Mapping[str, Any]) -> str:
    from .service import task_version

    return task_version(card)
