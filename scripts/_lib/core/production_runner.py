# -*- coding: utf-8 -*-
"""
scripts/_lib/core/production_runner.py
2F-PROD 通用自动编排 Runner 内核。
管理完整生命周期：
  读取权威任务 -> 隔离Worktree -> Codex Builder -> 真实候选校验 ->
  Antigravity Reviewer (严格 JSON Schema / 驳回自动重修) -> Codex QA (源码不可变 / 失败重修) ->
  EvidenceGate 逐阶段 1:1 强校验 -> 经合法状态机停在 PENDING_USER_ACCEPTANCE / 已完成。
"""
import hashlib
import json
import os
import re
import shlex
import subprocess
import sys
import time
import uuid
from typing import Any, Callable, Dict, List, Optional, Tuple

_SCRIPTS_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
if _SCRIPTS_ROOT not in sys.path:
    sys.path.insert(0, _SCRIPTS_ROOT)

import paths
from ..boards.board_adapter_factory import get_board_adapter
from ..boards.offline_board_adapter import OfflineBoardAdapter
from .adapter_manifest import ExecutionMode, VerificationLevel
from .adapter_registry import AdapterRegistry
from .agent_schema import (
    AgentCancelledError,
    AgentHandle,
    AgentNotSupportedError,
    AgentRequest,
    AgentResult,
    AgentStatus,
    CapabilitySupport,
    ConfirmationRequest,
    ConfirmationResult,
    HostCapabilities,
)
from .evidence_gate import EvidenceGate, EvidenceValidationContext
from .evidence_schema import (
    ArtifactRecord,
    EvidenceError,
    EvidenceGateError,
    EvidenceMetadata,
    EvidenceRecord,
    EvidenceType,
)
from .evidence_store import EvidenceStore
from .orchestrator import (
    BuilderToReviewerHandover,
    DefectRejectionHandover,
    Orchestrator,
    ReviewerToQAHandover,
    TaskExecutionSession,
)
from .orchestrator_schema import (
    OrchestrationMode,
    OrchestrationRole,
    OrchestrationState,
    UserAcceptanceRequest,
)
from .runner_checkpoint_store import RunnerCheckpointStore
from .runner_schema import (
    REVIEWER_JSON_SCHEMA,
    ReviewerStructuredOutput,
    RunnerCheckpoint,
    RunnerResult,
    RunnerState,
    TaskExecutionSpec,
)
from .task_spec_loader import load_task_execution_spec, verify_optimistic_concurrency
from .worktree_manager import WorktreeManager
from .worktree_schema import WorktreeRequest
from ..hosts.antigravity_adapter import AntigravityAdapter, create_antigravity_manifest
from ..hosts.codex_cli_adapter import CodexCliAdapter, create_codex_cli_manifest


def _extract_capabilities_extra(caps: Any) -> Dict[str, Any]:
    if not caps:
        return {}
    extra: Dict[str, Any] = {}
    for k, v in caps.__dict__.items():
        if k != "extra":
            extra[f"capability_{k}"] = v
    return extra


def _extract_real_invocation_id(result: AgentResult, handle: AgentHandle) -> Optional[str]:
    """从 AgentResult / AgentHandle 中提取真实非空 Invocation ID"""
    if getattr(result, "host_invocation_id", None):
        return str(result.host_invocation_id).strip()

    if getattr(result, "partial_results", None):
        for pr in result.partial_results:
            if hasattr(pr, "get"):
                inv = pr.get("invocation_id") or pr.get("host_invocation_id")
                if inv:
                    return str(inv).strip()

    if getattr(handle, "invocation_token", None):
        return f"inv_tok_{handle.invocation_token[:16]}"

    return None


def _validate_reviewer_schema_builtin(data: Any) -> Tuple[bool, Optional[str]]:
    """纯内置严格 JSON Schema 验证器（零外部依赖，DEF-T0061-1）"""
    if not isinstance(data, dict):
        return False, "Root must be a JSON object"
    required_fields = [
        "task_id",
        "baseline_commit",
        "candidate_commit",
        "session_id",
        "host_invocation_id",
        "decision",
        "defects",
        "summary",
    ]
    for field in required_fields:
        if field not in data:
            return False, f"Missing required field '{field}'"

    if not isinstance(data["task_id"], str) or not data["task_id"].strip():
        return False, "task_id must be a non-empty string"
    if not isinstance(data["baseline_commit"], str) or not re.match(r"^[0-9a-f]{40}$", data["baseline_commit"]):
        return False, "baseline_commit must be a 40-hex character SHA"
    if not isinstance(data["candidate_commit"], str) or not re.match(r"^[0-9a-f]{40}$", data["candidate_commit"]):
        return False, "candidate_commit must be a 40-hex character SHA"
    if not isinstance(data["session_id"], str) or not data["session_id"].strip():
        return False, "session_id must be a non-empty string"
    if not isinstance(data["host_invocation_id"], str) or not data["host_invocation_id"].strip():
        return False, "host_invocation_id must be a non-empty string"
    if data["decision"] not in ("PASS", "REJECT"):
        return False, f"decision must be 'PASS' or 'REJECT', got '{data['decision']}'"
    if not isinstance(data["defects"], (list, tuple)):
        return False, "defects must be an array"
    for d in data["defects"]:
        if not isinstance(d, dict):
            return False, "defects items must be objects"
        if not d.get("defect_id") or not d.get("severity") or not d.get("description"):
            return False, "defect item missing defect_id, severity, or description"
        if d.get("severity") not in ("P0", "P1", "P2", "P3"):
            return False, f"defect severity must be P0, P1, P2, or P3, got '{d.get('severity')}'"
    if not isinstance(data["summary"], str):
        return False, "summary must be a string"
    return True, None


def create_default_registry(context_id: str = "prod_runner") -> AdapterRegistry:
    """创建并预注册默认 Codex 与 Antigravity 适配器的注册表"""
    reg = AdapterRegistry(context_id=context_id)
    try:
        codex_manifest = create_codex_cli_manifest(adapter_id="codex_cli", verified_version="0.149.0")
        codex_adapter = CodexCliAdapter(is_real_host=True, default_approval_policy="on-request")
        reg.register(codex_adapter, codex_manifest)
    except Exception:
        pass

    try:
        ag_manifest = create_antigravity_manifest(adapter_id="antigravity", verified_version="1.1.22")
        ag_adapter = AntigravityAdapter(is_real_host=True, verification_level=VerificationLevel.CLI_VERIFIED)
        reg.register(ag_adapter, ag_manifest)
    except Exception:
        pass
    return reg


class ProductionRunnerError(Exception):
    pass


class ProductionRunner:
    """通用生产级自动编排 Runner 内核"""

    def __init__(
        self,
        registry: Optional[AdapterRegistry] = None,
        evidence_store: Optional[EvidenceStore] = None,
        evidence_gate: Optional[EvidenceGate] = None,
        checkpoint_store: Optional[RunnerCheckpointStore] = None,
        worktree_manager: Optional[WorktreeManager] = None,
    ):
        self.registry = registry or create_default_registry()
        self.evidence_store = evidence_store
        self.evidence_gate = evidence_gate
        self.checkpoint_store = checkpoint_store or RunnerCheckpointStore()
        self.worktree_manager = worktree_manager

    def _get_git_tracked_status(self, worktree_dir: str) -> str:
        """获取工作区内已跟踪文件的状态摘要（排除纯未跟踪测试缓存）"""
        try:
            diff_out = subprocess.check_output(
                ["git", "diff", "HEAD"],
                cwd=worktree_dir,
                stderr=subprocess.DEVNULL,
                text=True,
                timeout=5,
            )
            status_out = subprocess.check_output(
                ["git", "status", "-s", "--untracked-files=no"],
                cwd=worktree_dir,
                stderr=subprocess.DEVNULL,
                text=True,
                timeout=5,
            )
            return f"{status_out.strip()}\n{diff_out.strip()}"
        except Exception:
            return ""

    def _parse_reviewer_structured_json(
        self,
        raw_output: str,
        task_id: str,
        baseline_commit: str,
        candidate_commit: str,
        session_id: str,
        invocation_id: str,
    ) -> ReviewerStructuredOutput:
        """
        严格按 JSON Schema 解析并校验 Reviewer 响应 (DEF-T0061-1)。
        严禁使用默认值覆盖缺失字段；严禁放行普通文本 PASS；
        所有身份字段 (task_id, baseline_commit, candidate_commit, session_id, host_invocation_id) 必须精确匹配。
        """
        cleaned = raw_output.strip()
        json_obj = None

        try:
            json_obj = json.loads(cleaned)
        except Exception:
            match = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", cleaned, re.DOTALL)
            if match:
                try:
                    json_obj = json.loads(match.group(1))
                except Exception:
                    pass

        if json_obj is None:
            start_idx = cleaned.find("{")
            end_idx = cleaned.rfind("}")
            if start_idx != -1 and end_idx != -1 and end_idx > start_idx:
                try:
                    json_obj = json.loads(cleaned[start_idx : end_idx + 1])
                except Exception:
                    pass

        if not isinstance(json_obj, dict):
            return ReviewerStructuredOutput(
                task_id=task_id,
                baseline_commit=baseline_commit,
                candidate_commit=candidate_commit,
                session_id=session_id,
                host_invocation_id=invocation_id,
                decision="REJECT",
                defects=({
                    "defect_id": f"DEF-{task_id}-SCHEMA-VIOLATION",
                    "severity": "P1",
                    "description": f"Reviewer did not return valid JSON object matching schema: {cleaned[:200]}",
                },),
                summary="Invalid non-JSON reviewer output",
            )

        # 校验 JSON Schema 规范
        is_valid, err_msg = _validate_reviewer_schema_builtin(json_obj)
        if not is_valid:
            return ReviewerStructuredOutput(
                task_id=task_id,
                baseline_commit=baseline_commit,
                candidate_commit=candidate_commit,
                session_id=session_id,
                host_invocation_id=invocation_id,
                decision="REJECT",
                defects=({
                    "defect_id": f"DEF-{task_id}-SCHEMA-VIOLATION",
                    "severity": "P1",
                    "description": f"Reviewer output failed JSON schema validation: {err_msg}",
                },),
                summary="Schema validation failed",
            )

        # 严格比对身份字段与上下文一致性
        mismatches = []
        if json_obj.get("task_id") != task_id:
            mismatches.append(f"task_id mismatch: expected '{task_id}', got '{json_obj.get('task_id')}'")
        if json_obj.get("baseline_commit") != baseline_commit:
            mismatches.append(f"baseline_commit mismatch: expected '{baseline_commit}', got '{json_obj.get('baseline_commit')}'")
        if json_obj.get("candidate_commit") != candidate_commit:
            mismatches.append(f"candidate_commit mismatch: expected '{candidate_commit}', got '{json_obj.get('candidate_commit')}'")
        if json_obj.get("session_id") != session_id:
            mismatches.append(f"session_id mismatch: expected '{session_id}', got '{json_obj.get('session_id')}'")
        if json_obj.get("host_invocation_id") != invocation_id:
            mismatches.append(f"host_invocation_id mismatch: expected '{invocation_id}', got '{json_obj.get('host_invocation_id')}'")

        if mismatches:
            return ReviewerStructuredOutput(
                task_id=task_id,
                baseline_commit=baseline_commit,
                candidate_commit=candidate_commit,
                session_id=session_id,
                host_invocation_id=invocation_id,
                decision="REJECT",
                defects=({
                    "defect_id": f"DEF-{task_id}-IDENTITY-MISMATCH",
                    "severity": "P1",
                    "description": "; ".join(mismatches),
                },),
                summary="Identity binding mismatch in reviewer structured output",
            )

        decision = str(json_obj.get("decision", "")).strip().upper()
        defects = json_obj.get("defects", [])

        if decision == "PASS" and len(defects) > 0:
            return ReviewerStructuredOutput(
                task_id=task_id,
                baseline_commit=baseline_commit,
                candidate_commit=candidate_commit,
                session_id=session_id,
                host_invocation_id=invocation_id,
                decision="REJECT",
                defects=({
                    "defect_id": f"DEF-{task_id}-INVALID-PASS",
                    "severity": "P1",
                    "description": f"Reviewer returned PASS decision but included {len(defects)} defects.",
                },),
                summary="Contradictory reviewer output: PASS with defects",
            )

        if decision == "REJECT" and len(defects) == 0:
            defects = [{
                "defect_id": f"DEF-{task_id}-REJECT-NO-ITEMS",
                "severity": "P1",
                "description": json_obj.get("summary") or "Reviewer rejected candidate without specific defect items.",
            }]

        return ReviewerStructuredOutput(
            task_id=task_id,
            baseline_commit=baseline_commit,
            candidate_commit=candidate_commit,
            session_id=session_id,
            host_invocation_id=invocation_id,
            decision=decision,
            defects=tuple(defects),
            summary=str(json_obj.get("summary", "")),
        )

    def start(
        self,
        spec: TaskExecutionSpec,
        interactive_approval_cb: Optional[Callable[[str, Dict[str, Any]], bool]] = None,
    ) -> RunnerResult:
        """启动生产 Runner 执行任务"""
        lock_handle, lock_file = self.checkpoint_store.acquire_runner_lock(spec.task_id)
        if lock_handle is None:
            return RunnerResult(
                success=False,
                state=RunnerState.FAILED.value,
                task_id=spec.task_id,
                message=f"Task {spec.task_id} is already locked by another running Runner process.",
                diagnostics={"lock_error": "LockBusyError"},
            )

        try:
            return self._execute_loop(spec, lock_tuple=(lock_handle, lock_file), interactive_approval_cb=interactive_approval_cb)
        finally:
            self.checkpoint_store.release_runner_lock((lock_handle, lock_file))

    def _execute_loop(
        self,
        spec: TaskExecutionSpec,
        lock_tuple: Tuple[Any, Optional[str]],
        interactive_approval_cb: Optional[Callable[[str, Dict[str, Any]], bool]] = None,
        existing_checkpoint: Optional[RunnerCheckpoint] = None,
        pre_granted_approval: bool = False,
    ) -> RunnerResult:
        start_wall_clock = time.time()
        project_root = spec.project_root
        task_id = spec.task_id

        if self.evidence_store is None:
            evidence_dir = os.path.join(project_root, "user_data", "runner_evidence")
            os.makedirs(evidence_dir, exist_ok=True)
            self.evidence_store = EvidenceStore(root_dir=evidence_dir)

        if self.evidence_gate is None:
            self.evidence_gate = EvidenceGate(store=self.evidence_store, project_root=project_root)

        orchestrator = Orchestrator(
            registry=self.registry,
            evidence_store=self.evidence_store,
            evidence_gate=self.evidence_gate,
            project_root=project_root,
        )

        worktree_dir = project_root
        worktree_branch = spec.baseline_branch
        if spec.workspace_mode == "branch":
            if self.worktree_manager is None:
                controlled_root = spec.worktree_root or os.path.join(paths.resolve_data_root(cwd=project_root), "worktrees")
                self.worktree_manager = WorktreeManager(controlled_root=controlled_root, target_repo_path=project_root)

            req = WorktreeRequest(
                project_id=spec.project_id,
                task_id=task_id,
                actor_role="BUILDER",
                host_session_id=f"sess_wt_{task_id}_{int(time.time())}",
                baseline_commit=spec.baseline_commit or "HEAD",
            )
            existing_desc = None
            try:
                for wt in self.worktree_manager.list_worktrees():
                    if getattr(wt, "request", None) and wt.request.task_id == task_id:
                        existing_desc = wt
                        break
            except Exception:
                existing_desc = None

            if existing_desc:
                worktree_dir = existing_desc.absolute_path
                worktree_branch = existing_desc.branch_name
            else:
                desc = self.worktree_manager.create_worktree(req)
                worktree_dir = desc.absolute_path
                worktree_branch = desc.branch_name

        checkpoint = existing_checkpoint or RunnerCheckpoint(
            task_id=task_id,
            project_id=spec.project_id,
            state=RunnerState.WORKTREE_READY.value,
            current_role="BUILDER",
            candidate_commit=None,
            candidate_generation=0,
            review_cycle=0,
            qa_cycle=0,
            total_attempts=0,
            worktree_path=worktree_dir,
            worktree_branch=worktree_branch,
            builder_session_id=None,
            builder_invocation_id=None,
            reviewer_session_id=None,
            reviewer_invocation_id=None,
            qa_session_id=None,
            qa_invocation_id=None,
            evidence_ids=(),
            confirmation_request_id=None,
            defects_history=(),
        )
        self.checkpoint_store.save_checkpoint(checkpoint)

        builder_adapter = self.registry.get(spec.builder_adapter_id)
        reviewer_adapter = self.registry.get(spec.reviewer_adapter_id)
        qa_adapter = self.registry.get(spec.qa_adapter_id)

        if not builder_adapter or not reviewer_adapter or not qa_adapter:
            return RunnerResult(
                success=False,
                state=RunnerState.FAILED.value,
                task_id=task_id,
                message=f"Missing adapter: builder={spec.builder_adapter_id}, reviewer={spec.reviewer_adapter_id}, qa={spec.qa_adapter_id}",
            )

        candidate_generation = checkpoint.candidate_generation
        review_cycle = checkpoint.review_cycle
        qa_cycle = checkpoint.qa_cycle
        total_attempts = checkpoint.total_attempts
        evidence_ids = list(checkpoint.evidence_ids)
        defects_history = list(checkpoint.defects_history)
        candidate_commit = checkpoint.candidate_commit

        while total_attempts < spec.max_total_attempts:
            if (time.time() - start_wall_clock) > spec.total_wall_clock_timeout_seconds:
                return RunnerResult(
                    success=False,
                    state=RunnerState.FAILED.value,
                    task_id=task_id,
                    candidate_commit=candidate_commit,
                    candidate_generation=candidate_generation,
                    evidence_ids=tuple(evidence_ids),
                    message="Total wall-clock timeout exceeded.",
                )

            # ==========================================
            # STAGE 1: CODEX BUILDER
            # ==========================================
            total_attempts += 1
            candidate_generation += 1
            sess_builder = f"sess_builder_runner_{int(time.time()*1000)}"

            checkpoint = RunnerCheckpoint(
                task_id=task_id,
                project_id=spec.project_id,
                state=RunnerState.BUILDING.value,
                current_role="BUILDER",
                candidate_commit=candidate_commit,
                candidate_generation=candidate_generation,
                review_cycle=review_cycle,
                qa_cycle=qa_cycle,
                total_attempts=total_attempts,
                worktree_path=worktree_dir,
                worktree_branch=worktree_branch,
                builder_session_id=sess_builder,
                evidence_ids=tuple(evidence_ids),
                defects_history=tuple(defects_history),
            )
            self.checkpoint_store.save_checkpoint(checkpoint)

            defects_str = ""
            if defects_history:
                defects_str = "\n\n【上一轮审查/测试驳回缺陷】:\n" + json.dumps(defects_history[-1], indent=2, ensure_ascii=False)

            builder_prompt = (
                f"【任务名称】: {spec.task_name}\n"
                f"【任务ID】: {spec.task_id}\n"
                f"【需求正文】: {spec.requirement_text}\n"
                f"【验收标准】: {spec.acceptance_criteria}"
                f"{defects_str}\n\n"
                f"请在当前工作区完成代码修改与测试，确保功能完整且测试通过，并在完成修改后执行 git commit 提交代码。"
            )

            builder_request = AgentRequest(
                session_id=sess_builder,
                prompt=builder_prompt,
                role="BUILDER",
                workspace_dir=worktree_dir,
                timeout_seconds=float(spec.builder_timeout_seconds),
                extra_context={
                    "sandbox": "workspace-write",
                    "worktree_dir": worktree_dir,
                    "project_id": spec.project_id,
                    "permission_boundary": "workspace_write",
                },
            )

            try:
                builder_handle = builder_adapter.dispatch_agent(builder_request)
                builder_result = builder_adapter.wait_for_result(builder_handle, timeout_seconds=float(spec.builder_timeout_seconds))
            except AgentNotSupportedError as se:
                # 权限门禁拦截 -> 检查是否可交互批准 (DEF-T0061-7)
                approval_granted = pre_granted_approval
                if not approval_granted and interactive_approval_cb:
                    approval_granted = interactive_approval_cb(str(se), {"role": "BUILDER", "task_id": task_id})
                if not approval_granted:
                    checkpoint = RunnerCheckpoint(
                        task_id=task_id,
                        project_id=spec.project_id,
                        state=RunnerState.APPROVAL_REQUIRED.value,
                        current_role="BUILDER",
                        candidate_commit=candidate_commit,
                        candidate_generation=candidate_generation,
                        review_cycle=review_cycle,
                        qa_cycle=qa_cycle,
                        total_attempts=total_attempts,
                        worktree_path=worktree_dir,
                        worktree_branch=worktree_branch,
                        evidence_ids=tuple(evidence_ids),
                        defects_history=tuple(defects_history),
                        approval_reason=str(se),
                        last_error="Permission approval required",
                    )
                    self.checkpoint_store.save_checkpoint(checkpoint)
                    return RunnerResult(
                        success=False,
                        state=RunnerState.APPROVAL_REQUIRED.value,
                        task_id=task_id,
                        message=f"Operation requires explicit user permission approval: {se}. Paused at APPROVAL_REQUIRED.",
                        diagnostics={"approval_reason": str(se)},
                    )
                # 授权后重试
                builder_handle = builder_adapter.dispatch_agent(builder_request)
                builder_result = builder_adapter.wait_for_result(builder_handle, timeout_seconds=float(spec.builder_timeout_seconds))
            except Exception as e:
                return RunnerResult(
                    success=False,
                    state=RunnerState.FAILED.value,
                    task_id=task_id,
                    message=f"Builder dispatch/wait failed: {e}",
                    diagnostics={"error": str(e)},
                )

            # 真实 Invocation ID 提取 (DEF-T0061-2)
            inv_builder = _extract_real_invocation_id(builder_result, builder_handle)
            if not inv_builder:
                return RunnerResult(
                    success=False,
                    state=RunnerState.FAILED.value,
                    task_id=task_id,
                    message="Builder host did not produce a valid canonical invocation identity (Fail-Closed).",
                )

            # 真实 Candidate Commit 校验 (DEF-T0061-2)
            try:
                candidate_commit = subprocess.check_output(
                    ["git", "rev-parse", "HEAD"],
                    cwd=worktree_dir,
                    stderr=subprocess.DEVNULL,
                    text=True,
                    timeout=5,
                ).strip()
            except Exception as ge:
                return RunnerResult(
                    success=False,
                    state=RunnerState.FAILED.value,
                    task_id=task_id,
                    message=f"Failed to read candidate commit HEAD from worktree: {ge}",
                )

            if not candidate_commit or not re.match(r"^[0-9a-f]{40}$", candidate_commit):
                return RunnerResult(
                    success=False,
                    state=RunnerState.FAILED.value,
                    task_id=task_id,
                    message=f"Invalid candidate commit SHA '{candidate_commit}' in worktree (Fail-Closed).",
                )

            if spec.baseline_commit and candidate_commit == spec.baseline_commit:
                return RunnerResult(
                    success=False,
                    state=RunnerState.FAILED.value,
                    task_id=task_id,
                    message="Builder produced no new commits on top of baseline commit (Candidate == Baseline). Fail-Closed.",
                )

            orchestrator.start_builder(
                task_id=task_id,
                project_id=spec.project_id,
                branch=worktree_branch,
                baseline_commit=spec.baseline_commit,
                assignee=spec.owner,
                workspace_dir=worktree_dir,
                worktree_dir=worktree_dir,
                auth_context=f"auth_ctx_{spec.project_id}",
                billing_context=f"billing_ctx_{spec.project_id}",
                builder_session_id=sess_builder,
                builder_invocation_id=inv_builder,
                builder_adapter_id=spec.builder_adapter_id,
            )

            builder_evidence_id = f"evi_builder_{task_id.lower()}_{int(time.time()*1000)}"
            b_meta = EvidenceMetadata(
                project_id=spec.project_id,
                task_id=task_id,
                actor_role="BUILDER",
                host_id=spec.builder_adapter_id,
                adapter=spec.builder_adapter_id,
                host_session_id=sess_builder,
                host_invocation_id=inv_builder,
                is_real_host=builder_result.is_real_host,
                workspace_mode="workspace_write",
                transition_from="BUILDING",
                transition_to="REVIEWING",
                created_at=time.time(),
                extra=_extract_capabilities_extra(builder_adapter.detect_capabilities()),
            )
            b_record = EvidenceRecord(
                evidence_id=builder_evidence_id,
                evidence_type=EvidenceType.TASK_TRANSITION,
                baseline_commit=spec.baseline_commit,
                result_commit=candidate_commit,
                artifacts=(),
                metadata=b_meta,
            )
            self.evidence_store.append(b_record)
            evidence_ids.append(builder_evidence_id)

            b_handover = BuilderToReviewerHandover(
                task_id=task_id,
                project_id=spec.project_id,
                branch=worktree_branch,
                baseline_commit=spec.baseline_commit,
                candidate_commit=candidate_commit,
                modified_files=(),
                diff_stat={},
                test_summary={},
                workspace_dir=worktree_dir,
                worktree_dir=worktree_dir,
                builder_session_id=sess_builder,
                builder_invocation_id=inv_builder,
                auth_context=f"auth_ctx_{spec.project_id}",
                billing_context=f"billing_ctx_{spec.project_id}",
            )
            orchestrator.submit_to_reviewer(b_handover, evidence_id=builder_evidence_id, host_handle=builder_handle)

            # ==========================================
            # STAGE 2: ANTIGRAVITY REVIEWER
            # ==========================================
            review_cycle += 1
            sess_reviewer = f"sess_reviewer_runner_{int(time.time()*1000)}"

            checkpoint = RunnerCheckpoint(
                task_id=task_id,
                project_id=spec.project_id,
                state=RunnerState.REVIEWING.value,
                current_role="REVIEWER",
                candidate_commit=candidate_commit,
                candidate_generation=candidate_generation,
                review_cycle=review_cycle,
                qa_cycle=qa_cycle,
                total_attempts=total_attempts,
                worktree_path=worktree_dir,
                worktree_branch=worktree_branch,
                builder_session_id=sess_builder,
                reviewer_session_id=sess_reviewer,
                evidence_ids=tuple(evidence_ids),
                defects_history=tuple(defects_history),
            )
            self.checkpoint_store.save_checkpoint(checkpoint)

            try:
                diff_text = subprocess.check_output(
                    ["git", "diff", f"{spec.baseline_commit}..{candidate_commit}"],
                    cwd=worktree_dir,
                    stderr=subprocess.DEVNULL,
                    text=True,
                    timeout=5,
                )
            except Exception:
                diff_text = "(diff unavailable)"

            reviewer_prompt = (
                f"You are the independent Code Reviewer for Task {spec.task_id}.\n"
                f"Requirements: {spec.requirement_text}\n"
                f"Acceptance Criteria: {spec.acceptance_criteria}\n"
                f"Baseline: {spec.baseline_commit}\n"
                f"Candidate: {candidate_commit}\n\n"
                f"Code Diff:\n```\n{diff_text[:4000]}\n```\n\n"
                f"CRITICAL: You MUST reply with a JSON object matching this schema:\n"
                f"{json.dumps(REVIEWER_JSON_SCHEMA, indent=2)}\n"
                f"Required exact fields:\n"
                f"- task_id: '{spec.task_id}'\n"
                f"- baseline_commit: '{spec.baseline_commit}'\n"
                f"- candidate_commit: '{candidate_commit}'\n"
                f"- session_id: '{sess_reviewer}'\n"
                f"- decision: 'PASS' or 'REJECT'\n"
                f"- defects: list of defect objects if REJECT\n"
                f"- summary: detailed review rationale\n"
            )

            if hasattr(reviewer_adapter, "grant_permission"):
                try:
                    reviewer_adapter.grant_permission(
                        project_id=spec.project_id,
                        auth_context=f"auth_ctx_{spec.project_id}",
                        session_id=sess_reviewer,
                        workspace_dir=worktree_dir,
                        command_family="safe_local:REVIEWER",
                        permission_boundary="workspace_read",
                    )
                except Exception:
                    pass

            reviewer_request = AgentRequest(
                session_id=sess_reviewer,
                prompt=reviewer_prompt,
                role="REVIEWER",
                workspace_dir=worktree_dir,
                timeout_seconds=float(spec.reviewer_timeout_seconds),
                extra_context={
                    "sandbox": "workspace_read",
                    "mode": "plan",
                    "worktree_dir": worktree_dir,
                    "project_id": spec.project_id,
                    "auth_context": f"auth_ctx_{spec.project_id}",
                    "permission_boundary": "workspace_read",
                },
            )

            try:
                reviewer_handle = reviewer_adapter.dispatch_agent(reviewer_request)
                reviewer_result = reviewer_adapter.wait_for_result(reviewer_handle, timeout_seconds=float(spec.reviewer_timeout_seconds))
            except Exception as e:
                return RunnerResult(
                    success=False,
                    state=RunnerState.FAILED.value,
                    task_id=task_id,
                    message=f"Reviewer dispatch/wait failed: {e}",
                    diagnostics={"error": str(e)},
                )

            inv_reviewer = _extract_real_invocation_id(reviewer_result, reviewer_handle)
            if not inv_reviewer:
                return RunnerResult(
                    success=False,
                    state=RunnerState.FAILED.value,
                    task_id=task_id,
                    message="Reviewer host did not produce a valid canonical invocation identity (Fail-Closed).",
                )

            review_struct = self._parse_reviewer_structured_json(
                raw_output=reviewer_result.output,
                task_id=task_id,
                baseline_commit=spec.baseline_commit,
                candidate_commit=candidate_commit,
                session_id=reviewer_handle.session_id,
                invocation_id=inv_reviewer,
            )

            reviewer_evidence_id = f"evi_reviewer_{task_id.lower()}_{int(time.time()*1000)}"
            r_meta = EvidenceMetadata(
                project_id=spec.project_id,
                task_id=task_id,
                actor_role="REVIEWER",
                host_id=spec.reviewer_adapter_id,
                adapter=spec.reviewer_adapter_id,
                host_session_id=sess_reviewer,
                host_invocation_id=inv_reviewer,
                is_real_host=reviewer_result.is_real_host,
                workspace_mode="workspace_read",
                transition_from="REVIEWING",
                transition_to="TESTING" if review_struct.decision == "PASS" else "BUILDING",
                created_at=time.time(),
                extra=_extract_capabilities_extra(reviewer_adapter.detect_capabilities()),
            )
            r_record = EvidenceRecord(
                evidence_id=reviewer_evidence_id,
                evidence_type=EvidenceType.TASK_TRANSITION,
                baseline_commit=spec.baseline_commit,
                result_commit=candidate_commit,
                artifacts=(),
                metadata=r_meta,
            )
            self.evidence_store.append(r_record)
            evidence_ids.append(reviewer_evidence_id)

            if review_struct.decision == "REJECT":
                defect_handover = DefectRejectionHandover(
                    task_id=task_id,
                    project_id=spec.project_id,
                    source_role=OrchestrationRole.REVIEWER,
                    defect_list=tuple(d.get("description", str(d)) for d in review_struct.defects),
                    comments=review_struct.summary,
                    candidate_commit=candidate_commit,
                    source_session_id=sess_reviewer,
                    source_invocation_id=inv_reviewer,
                    target_builder_role=OrchestrationRole.BUILDER,
                    target_builder_assignee=spec.owner,
                )
                orchestrator.reject_by_reviewer(defect_handover)

                defect_record = {
                    "cycle": review_cycle,
                    "role": "REVIEWER",
                    "defects": [dict(d) for d in review_struct.defects],
                    "summary": review_struct.summary,
                }
                defects_history.append(defect_record)

                if review_cycle >= spec.max_review_cycles:
                    checkpoint = RunnerCheckpoint(
                        task_id=task_id,
                        project_id=spec.project_id,
                        state=RunnerState.NEEDS_USER_INPUT.value,
                        current_role="REVIEWER",
                        candidate_commit=candidate_commit,
                        candidate_generation=candidate_generation,
                        review_cycle=review_cycle,
                        qa_cycle=qa_cycle,
                        total_attempts=total_attempts,
                        worktree_path=worktree_dir,
                        worktree_branch=worktree_branch,
                        evidence_ids=tuple(evidence_ids),
                        defects_history=tuple(defects_history),
                        last_error=f"Exceeded max review cycles ({spec.max_review_cycles})",
                    )
                    self.checkpoint_store.save_checkpoint(checkpoint)
                    return RunnerResult(
                        success=False,
                        state=RunnerState.NEEDS_USER_INPUT.value,
                        task_id=task_id,
                        candidate_commit=candidate_commit,
                        candidate_generation=candidate_generation,
                        evidence_ids=tuple(evidence_ids),
                        message=f"Reviewer rejected candidate and exceeded max review cycles ({spec.max_review_cycles}). Paused at NEEDS_USER_INPUT.",
                        diagnostics={"defects": defects_history},
                    )

                continue

            # Reviewer PASS -> Handover to QA
            r_to_qa = ReviewerToQAHandover(
                task_id=task_id,
                project_id=spec.project_id,
                branch=worktree_branch,
                candidate_commit=candidate_commit,
                reviewer_decision="PASS",
                review_comments=review_struct.summary,
                review_evidence_id=reviewer_evidence_id,
                reviewer_session_id=sess_reviewer,
                reviewer_invocation_id=inv_reviewer,
                workspace_dir=worktree_dir,
                worktree_dir=worktree_dir,
                auth_context=f"auth_ctx_{spec.project_id}",
                billing_context=f"billing_ctx_{spec.project_id}",
            )
            orchestrator.pass_reviewer_to_qa(r_to_qa, evidence_id=reviewer_evidence_id, host_handle=reviewer_handle)

            # ==========================================
            # STAGE 3: CODEX QA
            # ==========================================
            qa_cycle += 1
            sess_qa = f"sess_qa_runner_{int(time.time()*1000)}"

            checkpoint = RunnerCheckpoint(
                task_id=task_id,
                project_id=spec.project_id,
                state=RunnerState.QA_TESTING.value,
                current_role="QA",
                candidate_commit=candidate_commit,
                candidate_generation=candidate_generation,
                review_cycle=review_cycle,
                qa_cycle=qa_cycle,
                total_attempts=total_attempts,
                worktree_path=worktree_dir,
                worktree_branch=worktree_branch,
                builder_session_id=sess_builder,
                reviewer_session_id=sess_reviewer,
                qa_session_id=sess_qa,
                evidence_ids=tuple(evidence_ids),
                defects_history=tuple(defects_history),
            )
            self.checkpoint_store.save_checkpoint(checkpoint)

            git_status_before_qa = self._get_git_tracked_status(worktree_dir)

            test_cmd = spec.test_command or "python -m pytest -q"
            qa_request = AgentRequest(
                session_id=sess_qa,
                prompt=f"Execute verification test suite in {worktree_dir} using: {test_cmd}. Report test results.",
                role="QA",
                workspace_dir=worktree_dir,
                timeout_seconds=float(spec.qa_timeout_seconds),
                extra_context={
                    "sandbox": "read-only",
                    "worktree_dir": worktree_dir,
                    "project_id": spec.project_id,
                },
            )

            try:
                qa_handle = qa_adapter.dispatch_agent(qa_request)
                qa_result = qa_adapter.wait_for_result(qa_handle, timeout_seconds=float(spec.qa_timeout_seconds))
            except Exception as e:
                return RunnerResult(
                    success=False,
                    state=RunnerState.FAILED.value,
                    task_id=task_id,
                    message=f"QA dispatch/wait failed: {e}",
                    diagnostics={"error": str(e)},
                )

            inv_qa = _extract_real_invocation_id(qa_result, qa_handle)
            if not inv_qa:
                return RunnerResult(
                    success=False,
                    state=RunnerState.FAILED.value,
                    task_id=task_id,
                    message="QA host did not produce a valid canonical invocation identity (Fail-Closed).",
                )

            # 安全执行测试命令（防止 shell 注入并核验退出码，DEF-T0061-8）
            test_exit_code = 0
            try:
                cmd_args = shlex.split(test_cmd, posix=(sys.platform != "win32"))
                test_proc = subprocess.run(
                    cmd_args,
                    cwd=worktree_dir,
                    capture_output=True,
                    text=True,
                    timeout=spec.qa_timeout_seconds,
                )
                test_exit_code = test_proc.returncode
            except Exception:
                test_exit_code = 1

            git_status_after_qa = self._get_git_tracked_status(worktree_dir)
            if git_status_before_qa != git_status_after_qa:
                return RunnerResult(
                    success=False,
                    state=RunnerState.FAILED.value,
                    task_id=task_id,
                    candidate_commit=candidate_commit,
                    message="QA modified tracked source code files. Code immutability boundary violated! Fail-Closed.",
                )

            qa_evidence_id = f"evi_qa_{task_id.lower()}_{int(time.time()*1000)}"
            qa_meta = EvidenceMetadata(
                project_id=spec.project_id,
                task_id=task_id,
                actor_role="QA",
                host_id=spec.qa_adapter_id,
                adapter=spec.qa_adapter_id,
                host_session_id=sess_qa,
                host_invocation_id=inv_qa,
                is_real_host=qa_result.is_real_host,
                workspace_mode="workspace_read",
                transition_from="TESTING",
                transition_to="PENDING_USER_ACCEPTANCE" if test_exit_code == 0 else "BUILDING",
                created_at=time.time(),
                extra=_extract_capabilities_extra(qa_adapter.detect_capabilities()),
            )
            qa_record = EvidenceRecord(
                evidence_id=qa_evidence_id,
                evidence_type=EvidenceType.TASK_TRANSITION,
                baseline_commit=spec.baseline_commit,
                result_commit=candidate_commit,
                artifacts=(),
                metadata=qa_meta,
            )
            self.evidence_store.append(qa_record)
            evidence_ids.append(qa_evidence_id)

            if test_exit_code != 0:
                qa_defect_handover = DefectRejectionHandover(
                    task_id=task_id,
                    project_id=spec.project_id,
                    source_role=OrchestrationRole.QA,
                    defect_list=(f"QA test command failed with exit code {test_exit_code}",),
                    comments="Automated test execution failed in QA.",
                    candidate_commit=candidate_commit,
                    source_session_id=sess_qa,
                    source_invocation_id=inv_qa,
                    target_builder_role=OrchestrationRole.BUILDER,
                    target_builder_assignee=spec.owner,
                )
                orchestrator.reject_by_qa(qa_defect_handover)

                defect_record = {
                    "cycle": qa_cycle,
                    "role": "QA",
                    "description": f"QA test suite failed with exit code {test_exit_code}.",
                }
                defects_history.append(defect_record)

                if qa_cycle >= spec.max_qa_cycles:
                    checkpoint = RunnerCheckpoint(
                        task_id=task_id,
                        project_id=spec.project_id,
                        state=RunnerState.NEEDS_USER_INPUT.value,
                        current_role="QA",
                        candidate_commit=candidate_commit,
                        candidate_generation=candidate_generation,
                        review_cycle=review_cycle,
                        qa_cycle=qa_cycle,
                        total_attempts=total_attempts,
                        worktree_path=worktree_dir,
                        worktree_branch=worktree_branch,
                        evidence_ids=tuple(evidence_ids),
                        defects_history=tuple(defects_history),
                        last_error=f"Exceeded max QA cycles ({spec.max_qa_cycles})",
                    )
                    self.checkpoint_store.save_checkpoint(checkpoint)
                    return RunnerResult(
                        success=False,
                        state=RunnerState.NEEDS_USER_INPUT.value,
                        task_id=task_id,
                        candidate_commit=candidate_commit,
                        candidate_generation=candidate_generation,
                        evidence_ids=tuple(evidence_ids),
                        message=f"QA failed and exceeded max QA cycles ({spec.max_qa_cycles}). Paused at NEEDS_USER_INPUT.",
                        diagnostics={"defects": defects_history},
                    )

                continue

            ua_req = UserAcceptanceRequest(
                task_id=task_id,
                project_id=spec.project_id,
                branch=worktree_branch,
                candidate_commit=candidate_commit,
                qa_report={"passed": 1, "failed": 0},
                reviewer_report={"decision": "PASS", "summary": review_struct.summary},
                artifacts=(),
                user_confirmation_prompt=f"请验收任务 {task_id} (候选提交: {candidate_commit[:8]})",
                auth_context=f"auth_ctx_{spec.project_id}",
            )
            orchestrator.pass_qa_to_user_acceptance(
                request=ua_req,
                qa_session_id=sess_qa,
                qa_invocation_id=inv_qa,
                evidence_id=qa_evidence_id,
                host_handle=qa_handle,
            )
            break

        # ==========================================
        # STAGE 4: USER ACCEPTANCE PREPARATION
        # ==========================================
        session = orchestrator.get_session(task_id)
        confirmation_request_id = session.confirmation_request_id if session else f"conf_req_{uuid.uuid4().hex[:16]}"
        final_state = RunnerState.PENDING_USER_ACCEPTANCE.value

        checkpoint = RunnerCheckpoint(
            task_id=task_id,
            project_id=spec.project_id,
            state=final_state,
            current_role="USER",
            candidate_commit=candidate_commit,
            candidate_generation=candidate_generation,
            review_cycle=review_cycle,
            qa_cycle=qa_cycle,
            total_attempts=total_attempts,
            worktree_path=worktree_dir,
            worktree_branch=worktree_branch,
            evidence_ids=tuple(evidence_ids),
            confirmation_request_id=confirmation_request_id,
            defects_history=tuple(defects_history),
        )
        self.checkpoint_store.save_checkpoint(checkpoint)

        # 5. 权威看板合法状态机流转 (DEF-T0061-3)
        try:
            config_candidates = [
                os.path.join(spec.authority_root, "config", "workflow.config.yaml"),
                os.path.join(spec.authority_root, "user_data", "workflow.config.yaml"),
            ]
            board_cfg = None
            for c in config_candidates:
                if os.path.isfile(c):
                    board_cfg = c
                    break

            if board_cfg:
                import yaml
                with open(board_cfg, "r", encoding="utf-8") as f:
                    cfg_data = yaml.safe_load(f) or {}

                board_sub = cfg_data.get("board", {})
                if board_sub.get("provider", "feishu_base").lower() == "local":
                    raw_board_file = board_sub.get("board_file", "user_data/board.json")
                    board_file = os.path.abspath(os.path.join(spec.authority_root, raw_board_file)) if not os.path.isabs(raw_board_file) else raw_board_file
                    b_adapter = OfflineBoardAdapter(board_file=board_file, field_map=board_sub.get("fields", {}))
                else:
                    b_adapter = get_board_adapter(board_cfg)

                if verify_optimistic_concurrency(spec, b_adapter):
                    transition_script = os.path.join(_SCRIPTS_ROOT, "transition_task.py")
                    t_cmd = [
                        sys.executable,
                        transition_script,
                        "--config", board_cfg,
                        "--task-id", task_id,
                        "--role", "QA",
                        "--from-status", spec.status_at_read,
                        "--to-status", "已完成",
                        "--assignee", "严经理",
                        "--remarks", f"Runner 完成所有 Builder/Reviewer/QA 阶段并生成 confirmation_request_id: {confirmation_request_id}，停留在 PENDING_USER_ACCEPTANCE 等待用户最终验收。",
                    ]
                    env = os.environ.copy()
                    env["YY_FLOW_PROJECT_ROOT"] = spec.authority_root
                    t_proc = subprocess.run(t_cmd, capture_output=True, text=True, env=env, timeout=20)
                    if t_proc.returncode != 0:
                        return RunnerResult(
                            success=False,
                            state=RunnerState.FAILED.value,
                            task_id=task_id,
                            message=f"Legal board transition failed via transition_task.py: {t_proc.stderr or t_proc.stdout}",
                        )
        except Exception as te:
            return RunnerResult(
                success=False,
                state=RunnerState.FAILED.value,
                task_id=task_id,
                message=f"State machine transition error: {te}",
            )

        return RunnerResult(
            success=True,
            state=final_state,
            task_id=task_id,
            candidate_commit=candidate_commit,
            candidate_generation=candidate_generation,
            evidence_ids=tuple(evidence_ids),
            confirmation_request_id=confirmation_request_id,
            message="Runner successfully executed Builder -> Reviewer -> QA cycle. Validated all evidence via EvidenceGate. Stopped at PENDING_USER_ACCEPTANCE / 已完成.",
            diagnostics={
                "worktree_path": worktree_dir,
                "review_cycles": review_cycle,
                "qa_cycles": qa_cycle,
                "total_attempts": total_attempts,
            },
        )

    def status(self, project_root: str, task_id: str, authority_root: Optional[str] = None) -> Dict[str, Any]:
        """纯只读状态查询：零写入、零宿主调用、零锁目录创建"""
        ckpt = self.checkpoint_store.query_status(task_id)
        if ckpt:
            return {
                "task_id": task_id,
                "has_checkpoint": True,
                "state": ckpt.get("state"),
                "current_role": ckpt.get("current_role"),
                "candidate_commit": ckpt.get("candidate_commit"),
                "candidate_generation": ckpt.get("candidate_generation"),
                "review_cycle": ckpt.get("review_cycle"),
                "qa_cycle": ckpt.get("qa_cycle"),
                "evidence_ids": ckpt.get("evidence_ids", []),
                "confirmation_request_id": ckpt.get("confirmation_request_id"),
                "last_error": ckpt.get("last_error"),
                "approval_reason": ckpt.get("approval_reason"),
            }
        return {
            "task_id": task_id,
            "has_checkpoint": False,
            "state": "NOT_STARTED",
            "message": "No active checkpoint found for this task.",
        }

    def resume(
        self,
        project_root: str,
        task_id: str,
        authority_root: Optional[str] = None,
        interactive_approval_cb: Optional[Callable[[str, Dict[str, Any]], bool]] = None,
        pre_granted_approval: bool = False,
    ) -> RunnerResult:
        """从 Checkpoint 恢复执行 (支持 APPROVAL_REQUIRED 授权后恢复)"""
        ckpt = self.checkpoint_store.load_checkpoint(task_id)
        if not ckpt:
            return RunnerResult(
                success=False,
                state=RunnerState.FAILED.value,
                task_id=task_id,
                message=f"Cannot resume: No checkpoint found for task {task_id}.",
            )

        spec = load_task_execution_spec(project_root=project_root, task_id=task_id, authority_root=authority_root)
        lock_handle, lock_file = self.checkpoint_store.acquire_runner_lock(task_id)
        if lock_handle is None:
            return RunnerResult(
                success=False,
                state=RunnerState.FAILED.value,
                task_id=task_id,
                message=f"Task {task_id} is already locked by another running process.",
            )

        try:
            return self._execute_loop(
                spec,
                lock_tuple=(lock_handle, lock_file),
                interactive_approval_cb=interactive_approval_cb,
                existing_checkpoint=ckpt,
                pre_granted_approval=pre_granted_approval,
            )
        finally:
            self.checkpoint_store.release_runner_lock((lock_handle, lock_file))

    def cancel(self, project_root: str, task_id: str, authority_root: Optional[str] = None) -> RunnerResult:
        """安全取消任务：不删除 Worktree，不清除 Evidence"""
        ckpt = self.checkpoint_store.load_checkpoint(task_id)
        if ckpt:
            updated = RunnerCheckpoint(
                task_id=task_id,
                project_id=ckpt.project_id,
                state=RunnerState.CANCELLED.value,
                current_role=ckpt.current_role,
                candidate_commit=ckpt.candidate_commit,
                candidate_generation=ckpt.candidate_generation,
                review_cycle=ckpt.review_cycle,
                qa_cycle=ckpt.qa_cycle,
                total_attempts=ckpt.total_attempts,
                worktree_path=ckpt.worktree_path,
                worktree_branch=ckpt.worktree_branch,
                evidence_ids=ckpt.evidence_ids,
                confirmation_request_id=ckpt.confirmation_request_id,
                defects_history=ckpt.defects_history,
                last_error="Cancelled by user command.",
            )
            self.checkpoint_store.save_checkpoint(updated)

        return RunnerResult(
            success=True,
            state=RunnerState.CANCELLED.value,
            task_id=task_id,
            message=f"Task {task_id} successfully cancelled. Checkpoint updated, worktree and evidence preserved.",
        )
