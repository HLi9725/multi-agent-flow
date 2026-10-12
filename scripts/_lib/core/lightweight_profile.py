# -*- coding: utf-8 -*-
"""Runner-side Lite and docs-short execution. Standard stays on the existing chain."""
from __future__ import annotations

import json
import subprocess
import time
from dataclasses import replace
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

from .agent_schema import AgentPermissionRequiredError, AgentRequest
from .evidence_gate import EvidenceValidationContext
from .evidence_schema import EvidenceMetadata, EvidenceRecord, EvidenceType
from .runner_schema import RunnerCheckpoint, RunnerResult, RunnerState, TaskExecutionSpec
from .workflow_policy import (
    PROFILE_DOCS,
    PROFILE_LITE,
    PROFILE_STANDARD,
    PolicyError,
    assess_docs,
    evaluate_candidate_risk,
    freeze_policy,
    policy_token,
    resume_policy,
    select_profile,
)


def _git(worktree: str, args: Sequence[str]) -> str:
    return subprocess.check_output(
        ["git", *args],
        cwd=worktree,
        encoding="utf-8",
        errors="replace",
        stderr=subprocess.DEVNULL,
    )


def candidate_materials(worktree: str, baseline: str, candidate: str) -> List[Dict[str, str]]:
    if not baseline or not candidate or baseline == candidate:
        return []
    diff = _git(worktree, ["diff", "--unified=3", baseline, candidate])
    names = [line.strip() for line in _git(worktree, ["diff", "--name-only", baseline, candidate]).splitlines() if line.strip()]
    files: List[Dict[str, str]] = []
    for name in names:
        try:
            content = _git(worktree, ["show", f"{candidate}:{name}"])
        except Exception:
            content = ""
        files.append({"path": name, "content": content + "\n" + diff})
    if not files and diff.strip():
        files.append({"path": "", "content": diff})
    return files


def _policy_of(spec: TaskExecutionSpec, checkpoint: RunnerCheckpoint) -> Optional[Dict[str, Any]]:
    payload = spec.workflow_policy_payload or checkpoint.execution_options.get("workflow_policy")
    if isinstance(payload, Mapping):
        return dict(payload)
    return None


def bind_workflow_policy(runner: Any, spec: TaskExecutionSpec, checkpoint: RunnerCheckpoint, worktree: str):
    """Freeze a requested profile. Missing profile keeps the legacy Standard path."""
    requested = spec.workflow_profile or (_policy_of(spec, checkpoint) or {}).get("profile_id")
    existing = _policy_of(spec, checkpoint)
    if not requested and not existing:
        return checkpoint, spec, None
    files = candidate_materials(worktree, spec.baseline_commit, checkpoint.candidate_commit or _head(worktree))
    if existing:
        decision = resume_policy(
            existing,
            requirement_hash=spec.requirement_hash,
            acceptance_criteria_hash=spec.acceptance_criteria_hash,
            candidate_commit=checkpoint.candidate_commit,
            files=files,
            budget_consumed=checkpoint.total_attempts,
            requested_profile=spec.workflow_profile,
            downgrade_authorization=spec.workflow_downgrade_authorization or "",
            open_defects=list(checkpoint.defects_history),
        )
        if not decision.get("ok"):
            paused = replace(
                checkpoint,
                state=RunnerState.NEEDS_USER_INPUT.value,
                last_error=decision.get("message"),
                total_attempts=max(checkpoint.total_attempts, int(decision.get("budget_consumed") or 0)),
                updated_at=time.time(),
            )
            runner.checkpoint_store.save_checkpoint(paused)
            return checkpoint, spec, RunnerResult(
                False, paused.state, spec.task_id,
                candidate_commit=checkpoint.candidate_commit,
                message=str(decision.get("message") or "策略不能恢复"),
                diagnostics={"budget_consumed": paused.total_attempts, "workflow_policy": existing},
            )
        existing = dict(existing)
        budget = dict(existing.get("budget") or {})
        budget["budget_consumed"] = max(int(budget.get("budget_consumed") or 0), checkpoint.total_attempts)
        existing["budget"] = budget
        upgraded_to = decision.get("profile_id") or existing.get("profile_id")
        if upgraded_to != existing.get("profile_id"):
            existing["profile_id"] = upgraded_to
            existing["required_roles"] = list(decision.get("required_roles") or [])
            existing["gates"] = list(decision.get("added_gates") or existing.get("gates") or [])
            existing["verifier_evidence_id"] = ""
        spec = replace(spec, workflow_profile=existing.get("profile_id"), workflow_policy_payload=existing)
        checkpoint = replace(checkpoint, execution_options=runner_options(spec))
        runner.checkpoint_store.save_checkpoint(checkpoint)
        return checkpoint, spec, None

    selected = select_profile(
        task_type=spec.task_type,
        requested=requested,
        files=files,
        requirement_text=spec.requirement_text,
        title=spec.task_name,
        requirement_hash=spec.requirement_hash,
        acceptance_criteria_hash=spec.acceptance_criteria_hash,
        budget_consumed=checkpoint.total_attempts,
    )
    if selected.blocked:
        paused = replace(
            checkpoint,
            state=RunnerState.NEEDS_USER_INPUT.value,
            last_error=selected.message,
            updated_at=time.time(),
        )
        runner.checkpoint_store.save_checkpoint(paused)
        return checkpoint, spec, RunnerResult(
            False, paused.state, spec.task_id, message=selected.message,
            diagnostics={"risk_signals": selected.risk_signals},
        )
    frozen = freeze_policy(selected, spec.task_id)
    spec = replace(spec, workflow_profile=frozen["profile_id"], workflow_policy_payload=frozen)
    checkpoint = replace(checkpoint, execution_options=runner_options(spec))
    runner.checkpoint_store.save_checkpoint(checkpoint)
    return checkpoint, spec, None


def runner_options(spec: TaskExecutionSpec) -> Dict[str, Any]:
    from .production_runner import _execution_options_from_spec
    return _execution_options_from_spec(spec)


def _head(worktree: str) -> str:
    try:
        return _git(worktree, ["rev-parse", "HEAD"]).strip()
    except Exception:
        return ""


def _parse_verifier(text: str) -> Dict[str, Any]:
    raw = (text or "").strip()
    try:
        data = json.loads(raw)
    except Exception:
        start = raw.find("{")
        end = raw.rfind("}")
        if start < 0 or end <= start:
            raise PolicyError("invalid json")
        try:
            data = json.loads(raw[start:end + 1])
        except Exception as exc:
            raise PolicyError("invalid json") from exc
    if not isinstance(data, dict) or data.get("decision") not in ("PASS", "FAIL"):
        raise PolicyError("schema mismatch")
    return data


def apply_lightweight(
    runner: Any,
    *,
    spec: TaskExecutionSpec,
    checkpoint: RunnerCheckpoint,
    worktree: str,
    candidate_commit: str,
    evidence_ids: List[str],
    total_attempts: int,
    board_status: str,
    builder_session_id: str,
    pre_granted_approval: bool,
):
    """Return None to continue Standard. Otherwise ('return', result) or ('retry', payload)."""
    policy = _policy_of(spec, checkpoint)
    if not policy or policy.get("profile_id") not in (PROFILE_LITE, PROFILE_DOCS):
        return None
    files = candidate_materials(worktree, spec.baseline_commit, candidate_commit)
    risk = evaluate_candidate_risk(policy, files, spec.requirement_text)
    if risk["stop_for_replan"] or risk["risk_level"] == "high":
        message = "风险升级，停止轻量路径并重新计划"
        paused = replace(checkpoint, state=RunnerState.NEEDS_USER_INPUT.value, last_error=message, updated_at=time.time())
        runner.checkpoint_store.save_checkpoint(paused)
        return "return", RunnerResult(
            False, paused.state, spec.task_id, candidate_commit=candidate_commit,
            evidence_ids=tuple(evidence_ids), message=message,
            diagnostics={"risk_signals": risk["risk_signals"]},
        )
    if policy.get("profile_id") == PROFILE_DOCS:
        return _finish_docs(
            runner, spec, checkpoint, policy, files, candidate_commit, evidence_ids, board_status,
        )
    return _finish_lite(
        runner, spec, checkpoint, policy, worktree, candidate_commit, evidence_ids,
        total_attempts, board_status, builder_session_id, pre_granted_approval,
    )


def _remember(spec: TaskExecutionSpec, checkpoint: RunnerCheckpoint, policy: Mapping[str, Any], **changes: Any) -> Tuple[TaskExecutionSpec, RunnerCheckpoint]:
    updated_policy = dict(policy)
    spec = replace(spec, workflow_profile=updated_policy.get("profile_id"), workflow_policy_payload=updated_policy)
    checkpoint = replace(checkpoint, execution_options=runner_options(spec), updated_at=time.time(), **changes)
    return spec, checkpoint


def _finish_docs(runner, spec, checkpoint, policy, files, candidate_commit, evidence_ids, board_status):
    ok, errors = assess_docs(files)
    if not ok:
        message = "文档必要检查未通过，不能短链: " + "; ".join(errors)
        paused = replace(checkpoint, state=RunnerState.NEEDS_USER_INPUT.value, last_error=message, updated_at=time.time())
        runner.checkpoint_store.save_checkpoint(paused)
        return "return", RunnerResult(False, paused.state, spec.task_id, candidate_commit=candidate_commit, message=message)
    evidence_id = f"evi_docs_{spec.task_id.lower()}_{int(time.time() * 1000)}"
    record = EvidenceRecord(
        evidence_id=evidence_id,
        evidence_type=EvidenceType.TASK_TRANSITION,
        baseline_commit=spec.baseline_commit,
        result_commit=candidate_commit,
        artifacts=(),
        metadata=EvidenceMetadata(
            project_id=spec.project_id,
            task_id=spec.task_id,
            actor_role="DOCS_CHECK",
            host_id="local-doc-gate",
            adapter="local-doc-gate",
            host_session_id=f"sess_docs_check_{spec.task_id.lower()}",
            host_invocation_id=f"inv_docs_{int(time.time() * 1000)}",
            is_real_host=True,
            workspace_mode="workspace_read",
            transition_from="IN_PROGRESS",
            transition_to="COMPLETED",
            created_at=time.time(),
            extra={
                "workflow_profile_id": PROFILE_DOCS,
                "verifier_decision": "PASS",
                "checks": "secret_scan,doc_metadata,path_depth",
            },
        ),
    )
    runner.evidence_store.append(record)
    evidence_ids = list(evidence_ids) + [evidence_id]
    policy = dict(policy)
    policy["checks_passed"] = True
    policy["verifier_evidence_id"] = ""
    spec, checkpoint = _remember(
        spec, checkpoint, policy,
        evidence_ids=tuple(evidence_ids),
        state=RunnerState.COMPLETION_PENDING.value,
        current_role="DOCS",
    )
    runner.checkpoint_store.save_checkpoint(checkpoint)
    role = "DOCS" if spec.task_type.upper() == "C" else "DEV"
    assignee = "严经理"
    if board_status == "进行中":
        ok_trans, err = runner._do_state_transition(
            spec.authority_root, spec.task_id, role, "进行中", "已完成", assignee,
            policy_token(policy) + " 文档必要检查通过", spec.task_type,
        )
        if not ok_trans:
            return "return", RunnerResult(False, RunnerState.FAILED.value, spec.task_id, message=f"文档短链流转失败: {err}")
    checkpoint = replace(checkpoint, state=RunnerState.PENDING_USER_ACCEPTANCE.value, last_error=None, updated_at=time.time())
    runner.checkpoint_store.save_checkpoint(checkpoint)
    return "return", RunnerResult(
        True, checkpoint.state, spec.task_id, candidate_commit=candidate_commit,
        evidence_ids=tuple(evidence_ids), message="文档短链检查通过，等待用户验收",
    )


def _finish_lite(
    runner, spec, checkpoint, policy, worktree, candidate_commit, evidence_ids,
    total_attempts, board_status, builder_session_id, pre_granted_approval,
):
    adapter = runner.registry.get(spec.reviewer_adapter_id)
    if adapter is None:
        return "return", RunnerResult(False, RunnerState.FAILED.value, spec.task_id, message="Lite 缺少独立 Verifier 宿主适配器")
    protocol_attempts = 0
    while protocol_attempts < 2:
        session_id = f"sess_verifier_{spec.task_id.lower()}_{int(time.time() * 1000)}_{protocol_attempts}"
        if session_id == builder_session_id or "builder" in session_id:
            session_id = "sess_verifier_distinct_" + session_id
        prompt = (
            "You are the independent Lite Verifier, not the Builder, Reviewer, or QA. "
            f"Return JSON with decision PASS or FAIL for candidate {candidate_commit}. "
            "Do not claim to be Reviewer or QA."
        )
        request = AgentRequest(
            session_id=session_id,
            prompt=prompt,
            role="VERIFIER",
            workspace_dir=worktree,
            timeout_seconds=float(spec.reviewer_timeout_seconds),
            extra_context={
                "production_runner_managed": True,
                "workflow_profile_id": PROFILE_LITE,
                "host_managed_profile": "flow-runner-verifier",
                "sandbox_mode": "read-only",
                "permission_boundary": "workspace_read",
                "pre_granted_approval": pre_granted_approval,
                "cursor_model": spec.cursor_model,
                "cursor_api_key_env": spec.cursor_api_key_env,
                "cursor_runtime": spec.cursor_runtime,
            },
        )
        try:
            handle, result, _request, checkpoint = runner._dispatch_with_transient_host_retries(
                adapter=adapter,
                request=request,
                task_id=spec.task_id,
                state=RunnerState.VERIFYING.value,
                role="VERIFIER",
                timeout_seconds=float(spec.reviewer_timeout_seconds),
                checkpoint=checkpoint,
                spec=spec,
            )
        except AgentPermissionRequiredError as exc:
            paused = replace(
                checkpoint,
                state=RunnerState.APPROVAL_REQUIRED.value,
                current_role="VERIFIER",
                last_error=str(exc),
                updated_at=time.time(),
            )
            runner.checkpoint_store.save_checkpoint(paused)
            return "return", RunnerResult(
                False, paused.state, spec.task_id, candidate_commit=candidate_commit,
                message="Verifier 等待授权，未扩大权限",
            )
        try:
            parsed = _parse_verifier(result.output)
        except PolicyError:
            protocol_attempts += 1
            if protocol_attempts >= 2:
                paused = replace(
                    checkpoint,
                    state=RunnerState.NEEDS_USER_INPUT.value,
                    current_role="VERIFIER",
                    last_error="协议错误，不编造业务结论",
                    updated_at=time.time(),
                )
                runner.checkpoint_store.save_checkpoint(paused)
                return "return", RunnerResult(
                    False, paused.state, spec.task_id, candidate_commit=candidate_commit,
                    message="Verifier 协议错误，未写成 PASS 或 FAIL",
                    diagnostics={"failure_kind": "protocol"},
                )
            continue
        decision = parsed["decision"]
        if decision == "FAIL":
            return _reject_lite(
                runner, spec, checkpoint, policy, candidate_commit, evidence_ids,
                total_attempts, board_status, session_id, result, handle, adapter, parsed,
            )
        return _pass_lite(
            runner, spec, checkpoint, policy, candidate_commit, evidence_ids,
            board_status, builder_session_id, session_id, result, handle, adapter,
        )
    return "return", RunnerResult(False, RunnerState.NEEDS_USER_INPUT.value, spec.task_id, message="Verifier 协议重试耗尽")


def _verifier_evidence(spec, candidate, session_id, result, handle, adapter, decision, evidence_ids):
    caps = adapter.detect_capabilities()
    from .production_runner import _extract_capabilities_extra, _extract_real_invocation_id
    invocation = _extract_real_invocation_id(result, handle) or f"inv_verifier_{int(time.time() * 1000)}"
    evidence_id = f"evi_verifier_{spec.task_id.lower()}_{int(time.time() * 1000)}"
    meta = EvidenceMetadata(
        project_id=spec.project_id,
        task_id=spec.task_id,
        actor_role="VERIFIER",
        host_id=handle.host_id,
        adapter=spec.reviewer_adapter_id,
        host_session_id=session_id,
        host_invocation_id=invocation,
        is_real_host=bool(result.is_real_host),
        workspace_mode="workspace_read",
        transition_from="BUILDING",
        transition_to="PENDING_USER_ACCEPTANCE" if decision == "PASS" else "BUILDING",
        created_at=time.time(),
        extra={
            **_extract_capabilities_extra(caps),
            "workflow_profile_id": PROFILE_LITE,
            "verifier_decision": decision,
            "host_managed_profile": "flow-runner-verifier",
        },
    )
    record = EvidenceRecord(
        evidence_id=evidence_id,
        evidence_type=EvidenceType.TASK_TRANSITION,
        baseline_commit=spec.baseline_commit,
        result_commit=candidate,
        artifacts=(),
        metadata=meta,
    )
    return evidence_id, record, invocation, caps


def _pass_lite(runner, spec, checkpoint, policy, candidate, evidence_ids, board_status, builder_session_id, session_id, result, handle, adapter):
    evidence_id, record, invocation, caps = _verifier_evidence(
        spec, candidate, session_id, result, handle, adapter, "PASS", evidence_ids,
    )
    runner.evidence_store.append(record)
    try:
        runner.evidence_gate.validate_lite_verifier(
            evidence_id,
            EvidenceValidationContext(
                project_id=spec.project_id,
                task_id=spec.task_id,
                actor_role="VERIFIER",
                transition_from="BUILDING",
                transition_to="PENDING_USER_ACCEPTANCE",
                baseline_commit=spec.baseline_commit,
                result_commit=candidate,
                expected_invocation_id=invocation,
                expected_adapter=spec.reviewer_adapter_id,
                expected_workspace_mode="workspace_read",
                expected_evidence_type=EvidenceType.TASK_TRANSITION,
                host_handle=handle,
                expected_capabilities=caps,
                agent_result=result,
            ),
            builder_session_id=builder_session_id,
            require_pass=True,
        )
    except Exception as exc:
        return "return", RunnerResult(False, RunnerState.FAILED.value, spec.task_id, candidate_commit=candidate, message=f"Lite EvidenceGate 拒绝: {exc}")
    evidence_ids = list(evidence_ids) + [evidence_id]
    policy = dict(policy)
    policy["verifier_evidence_id"] = evidence_id
    policy["checks_passed"] = True
    spec, checkpoint = _remember(
        spec, checkpoint, policy,
        evidence_ids=tuple(evidence_ids),
        verifier_session_id=session_id,
        verifier_invocation_id=invocation,
        current_role="VERIFIER",
        state=RunnerState.COMPLETION_PENDING.value,
        candidate_commit=candidate,
    )
    runner.checkpoint_store.save_checkpoint(checkpoint)
    if board_status == "进行中":
        ok_trans, err = runner._do_state_transition(
            spec.authority_root, spec.task_id, "VERIFIER", "进行中", "已完成", "严经理",
            policy_token(policy) + f" Verifier PASS {evidence_id}", spec.task_type,
        )
        if not ok_trans:
            return "return", RunnerResult(False, RunnerState.FAILED.value, spec.task_id, message=f"Lite 完成流转失败: {err}")
    checkpoint = replace(checkpoint, state=RunnerState.PENDING_USER_ACCEPTANCE.value, last_error=None, updated_at=time.time())
    runner.checkpoint_store.save_checkpoint(checkpoint)
    return "return", RunnerResult(
        True, checkpoint.state, spec.task_id, candidate_commit=candidate,
        evidence_ids=tuple(evidence_ids),
        message="Lite Verifier 通过，等待用户验收",
    )


def _reject_lite(runner, spec, checkpoint, policy, candidate, evidence_ids, total_attempts, board_status, session_id, result, handle, adapter, parsed):
    evidence_id, record, invocation, caps = _verifier_evidence(
        spec, candidate, session_id, result, handle, adapter, "FAIL", evidence_ids,
    )
    runner.evidence_store.append(record)
    try:
        runner.evidence_gate.validate_lite_verifier(
            evidence_id,
            EvidenceValidationContext(
                project_id=spec.project_id,
                task_id=spec.task_id,
                actor_role="VERIFIER",
                transition_from="BUILDING",
                transition_to="BUILDING",
                baseline_commit=spec.baseline_commit,
                result_commit=candidate,
                expected_invocation_id=invocation,
                expected_adapter=spec.reviewer_adapter_id,
                expected_workspace_mode="workspace_read",
                expected_evidence_type=EvidenceType.TASK_TRANSITION,
                host_handle=handle,
                expected_capabilities=caps,
                agent_result=result,
            ),
            builder_session_id="",
            require_pass=False,
        )
    except Exception as exc:
        return "return", RunnerResult(False, RunnerState.FAILED.value, spec.task_id, message=f"Lite 失败证据无效: {exc}")
    evidence_ids = list(evidence_ids) + [evidence_id]
    defects = list(checkpoint.defects_history)
    defects.append({"role": "VERIFIER", "summary": parsed.get("summary") or "Verifier FAIL", "candidate": candidate})
    policy = dict(policy)
    policy["open_defects"] = defects
    budget = dict(policy.get("budget") or {})
    budget["budget_consumed"] = total_attempts
    policy["budget"] = budget
    spec, checkpoint = _remember(
        spec, checkpoint, policy,
        evidence_ids=tuple(evidence_ids),
        defects_history=tuple(defects),
        verifier_session_id=session_id,
        verifier_invocation_id=invocation,
        current_role="BUILDER",
        state=RunnerState.REJECTION_PENDING.value,
        last_error="Verifier FAIL",
    )
    runner.checkpoint_store.save_checkpoint(checkpoint)
    if board_status == "进行中":
        ok_back, err_back = runner._do_state_transition(
            spec.authority_root, spec.task_id, "VERIFIER", "进行中", "已退回", "李开发",
            "Verifier 退回修复", spec.task_type,
        )
        if not ok_back:
            return "return", RunnerResult(False, RunnerState.FAILED.value, spec.task_id, message=f"Lite 退回失败: {err_back}")
        ok_claim, err_claim = runner._do_state_transition(
            spec.authority_root, spec.task_id, "DEV", "已退回", "进行中", "李开发",
            "开发重新领取 Lite 退回", spec.task_type,
        )
        if not ok_claim:
            return "return", RunnerResult(False, RunnerState.FAILED.value, spec.task_id, message=f"Lite 重新领取失败: {err_claim}")
        board_status = "进行中"
    if total_attempts >= spec.max_total_attempts:
        return "return", RunnerResult(
            False, RunnerState.NEEDS_USER_INPUT.value, spec.task_id,
            candidate_commit=candidate, message="预算耗尽，停止 Lite 重试",
            diagnostics={"budget_consumed": total_attempts},
        )
    return "retry", {
        "checkpoint": checkpoint,
        "evidence_ids": evidence_ids,
        "board": board_status,
        "total_attempts": total_attempts,
    }


# Imported for callers that want the constant without the policy module.
STANDARD_PROFILE = PROFILE_STANDARD
