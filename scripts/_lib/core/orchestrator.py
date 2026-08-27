from collections.abc import Mapping
from dataclasses import dataclass, field
import hashlib
import json
import os
import platform
import sys
import threading
import time
from types import MappingProxyType
from typing import Any, Dict, List, Mapping as TMapping, Optional, Sequence, Set, Tuple

from .agent_schema import (
    AgentHandle,
    AgentInvalidHandleError,
    AgentRequest,
    AgentResult,
    AgentStatus,
    CapabilitySupport,
    ConfirmationRequest,
    ConfirmationResult,
    HostCapabilities,
)
from .adapter_manifest import (
    AdapterManifest,
    AuthBoundaryType,
    BillingBoundaryType,
    ExecutionMode,
    HostSurface,
    PlatformVerification,
    VerificationLevel,
)
from .adapter_registry import AdapterRegistry, ResolutionStatus
from .evidence_gate import EvidenceGate, EvidenceValidationContext
from .evidence_schema import ArtifactRecord, EvidenceMetadata, EvidenceRecord, EvidenceType
from .evidence_store import EvidenceStore
from .orchestrator_schema import (
    BuilderToReviewerHandover,
    DefectRejectionHandover,
    DualHostVerificationResult,
    DualHostVerificationStatus,
    OrchestrationError,
    OrchestrationGateError,
    OrchestrationMode,
    OrchestrationRole,
    OrchestrationSecurityError,
    OrchestrationSessionIsolationError,
    OrchestrationState,
    OrchestrationStateError,
    ReviewerToQAHandover,
    UserAcceptanceDecision,
    UserAcceptanceRequest,
    _freeze_orchestration_value,
)


@dataclass
class TaskExecutionSession:
    task_id: str
    project_id: str
    branch: str
    baseline_commit: str
    candidate_commit: Optional[str]
    state: OrchestrationState
    current_role: OrchestrationRole
    current_assignee: str
    workspace_dir: str
    worktree_dir: str
    auth_context: str
    billing_context: str
    builder_session_id: Optional[str] = None
    builder_invocation_id: Optional[str] = None
    builder_adapter_id: Optional[str] = None
    reviewer_session_id: Optional[str] = None
    reviewer_invocation_id: Optional[str] = None
    reviewer_adapter_id: Optional[str] = None
    qa_session_id: Optional[str] = None
    qa_invocation_id: Optional[str] = None
    qa_adapter_id: Optional[str] = None
    last_evidence_id: Optional[str] = None
    history: List[Dict[str, Any]] = field(default_factory=list)
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)


class Orchestrator:
    """
    Client-agnostic, deterministic multi-agent workflow orchestrator for Phase 2F.
    Enforces strict role/session isolation, evidence verification gates,
    deterministic state transitions (Builder -> Reviewer -> QA -> User Acceptance),
    operating mode boundaries (manual, assisted, verified_automatic), and non-destructive recovery.
    """

    def __init__(
        self,
        registry: AdapterRegistry,
        evidence_store: Optional[EvidenceStore] = None,
        evidence_gate: Optional[EvidenceGate] = None,
        project_root: Optional[str] = None,
    ) -> None:
        self.registry = registry
        self.evidence_store = evidence_store
        self.project_root = os.path.realpath(os.path.abspath(project_root or os.getcwd()))
        self.evidence_gate = evidence_gate or (
            EvidenceGate(self.evidence_store, self.project_root) if self.evidence_store else None
        )
        self._lock = threading.RLock()
        self._sessions: Dict[str, TaskExecutionSession] = {}

    def _normalize_current_os(self) -> str:
        plat = sys.platform.lower()
        if plat.startswith("win"):
            return "windows"
        if plat.startswith("darwin"):
            return "macos"
        if plat.startswith("linux"):
            return "linux"
        return plat

    def get_session(self, task_id: str) -> Optional[TaskExecutionSession]:
        with self._lock:
            return self._sessions.get(task_id)

    def start_builder(
        self,
        task_id: str,
        project_id: str,
        branch: str,
        baseline_commit: str,
        assignee: str,
        workspace_dir: str,
        worktree_dir: str,
        auth_context: str,
        billing_context: str,
        builder_session_id: str,
        builder_invocation_id: str,
        builder_adapter_id: str,
    ) -> TaskExecutionSession:
        """Start or re-activate the Builder role on a task."""
        with self._lock:
            if not task_id or not task_id.strip():
                raise ValueError("task_id cannot be empty")
            if not project_id or not project_id.strip():
                raise ValueError("project_id cannot be empty")
            if not builder_session_id or not builder_session_id.strip():
                raise ValueError("builder_session_id cannot be empty")
            if not builder_invocation_id or not builder_invocation_id.strip():
                raise ValueError("builder_invocation_id cannot be empty")

            existing = self._sessions.get(task_id)
            if existing:
                if existing.state not in (
                    OrchestrationState.IDLE,
                    OrchestrationState.REJECTED_BY_REVIEWER,
                    OrchestrationState.REJECTED_BY_QA,
                    OrchestrationState.BUILDING,
                ):
                    raise OrchestrationStateError(
                        f"Cannot start builder for task '{task_id}' in state '{existing.state.value}'."
                    )
                # Session reuse check
                if existing.reviewer_session_id and builder_session_id == existing.reviewer_session_id:
                    raise OrchestrationSessionIsolationError(
                        f"Builder session '{builder_session_id}' conflicts with prior Reviewer session."
                    )
                if existing.qa_session_id and builder_session_id == existing.qa_session_id:
                    raise OrchestrationSessionIsolationError(
                        f"Builder session '{builder_session_id}' conflicts with prior QA session."
                    )

                existing.state = OrchestrationState.BUILDING
                existing.current_role = OrchestrationRole.BUILDER
                existing.current_assignee = assignee
                existing.builder_session_id = builder_session_id
                existing.builder_invocation_id = builder_invocation_id
                existing.builder_adapter_id = builder_adapter_id
                existing.updated_at = time.time()
                existing.history.append({
                    "action": "START_BUILDER_REWORK",
                    "role": OrchestrationRole.BUILDER.value,
                    "session_id": builder_session_id,
                    "timestamp": time.time(),
                })
                return existing

            # New session
            session = TaskExecutionSession(
                task_id=task_id,
                project_id=project_id,
                branch=branch,
                baseline_commit=baseline_commit,
                candidate_commit=None,
                state=OrchestrationState.BUILDING,
                current_role=OrchestrationRole.BUILDER,
                current_assignee=assignee,
                workspace_dir=workspace_dir,
                worktree_dir=worktree_dir,
                auth_context=auth_context,
                billing_context=billing_context,
                builder_session_id=builder_session_id,
                builder_invocation_id=builder_invocation_id,
                builder_adapter_id=builder_adapter_id,
            )
            session.history.append({
                "action": "START_BUILDER",
                "role": OrchestrationRole.BUILDER.value,
                "session_id": builder_session_id,
                "timestamp": time.time(),
            })
            self._sessions[task_id] = session
            return session

    def submit_to_reviewer(
        self,
        handover: BuilderToReviewerHandover,
        evidence_id: Optional[str] = None,
        host_handle: Optional[AgentHandle] = None,
    ) -> TaskExecutionSession:
        """Submit builder work to independent Reviewer."""
        with self._lock:
            session = self._sessions.get(handover.task_id)
            if not session:
                raise OrchestrationStateError(f"No active session for task '{handover.task_id}'")

            if session.state != OrchestrationState.BUILDING:
                # Idempotency check
                if session.state == OrchestrationState.REVIEWING and session.candidate_commit == handover.candidate_commit:
                    return session
                raise OrchestrationStateError(
                    f"Cannot submit to reviewer: Task '{handover.task_id}' is in state '{session.state.value}', expected BUILDING."
                )

            # Validate boundary isolation
            self._validate_session_boundary(session, handover.project_id, handover.auth_context, handover.billing_context)

            # Validate session ID match
            if session.builder_session_id and handover.builder_session_id != session.builder_session_id:
                raise OrchestrationSessionIsolationError(
                    f"Handover builder_session_id '{handover.builder_session_id}' does not match registered session '{session.builder_session_id}'."
                )

            # Validate evidence if provided
            if evidence_id and self.evidence_gate and self.evidence_store:
                if host_handle is None:
                    raise OrchestrationGateError("host_handle must be provided when evidence validation is requested.")
                val_ctx = EvidenceValidationContext(
                    project_id=session.project_id,
                    task_id=session.task_id,
                    actor_role=OrchestrationRole.BUILDER.value,
                    transition_from="BUILDING",
                    transition_to="REVIEWING",
                    baseline_commit=session.baseline_commit,
                    result_commit=handover.candidate_commit,
                    expected_invocation_id=handover.builder_invocation_id,
                    expected_adapter=session.builder_adapter_id or "",
                    expected_workspace_mode="workspace_write",
                    expected_evidence_type=EvidenceType.TASK_TRANSITION,
                    host_handle=host_handle,
                    expected_capabilities=HostCapabilities(
                        supports_streaming=True,
                        supports_structured_events=True,
                        supports_tool_calling=True,
                    ),
                )
                self.evidence_gate.validate_evidence(evidence_id, val_ctx)
                session.last_evidence_id = evidence_id

            session.candidate_commit = handover.candidate_commit
            session.state = OrchestrationState.REVIEWING
            session.current_role = OrchestrationRole.REVIEWER
            session.updated_at = time.time()
            session.history.append({
                "action": "SUBMIT_TO_REVIEWER",
                "candidate_commit": handover.candidate_commit,
                "evidence_id": evidence_id,
                "timestamp": time.time(),
            })
            return session

    def reject_by_reviewer(self, handover: DefectRejectionHandover) -> TaskExecutionSession:
        """Reviewer rejects candidate -> returns to original Builder assignee without creating a new task."""
        with self._lock:
            session = self._sessions.get(handover.task_id)
            if not session:
                raise OrchestrationStateError(f"No active session for task '{handover.task_id}'")

            if session.state != OrchestrationState.REVIEWING:
                raise OrchestrationStateError(
                    f"Cannot reject from reviewer: Task '{handover.task_id}' is in state '{session.state.value}', expected REVIEWING."
                )

            # Reviewer session must be distinct from builder session
            if handover.source_session_id == session.builder_session_id:
                raise OrchestrationSessionIsolationError(
                    f"Reviewer session '{handover.source_session_id}' cannot be identical to Builder session '{session.builder_session_id}'."
                )

            # Stale candidate check
            if session.candidate_commit and handover.candidate_commit != session.candidate_commit:
                raise OrchestrationStateError(
                    f"Stale candidate commit '{handover.candidate_commit}' does not match session candidate '{session.candidate_commit}'."
                )

            session.reviewer_session_id = handover.source_session_id
            session.reviewer_invocation_id = handover.source_invocation_id
            session.state = OrchestrationState.REJECTED_BY_REVIEWER
            session.current_role = OrchestrationRole.BUILDER
            session.current_assignee = handover.target_builder_assignee
            session.updated_at = time.time()
            session.history.append({
                "action": "REJECT_BY_REVIEWER",
                "defects": list(handover.defect_list),
                "comments": handover.comments,
                "reviewer_session_id": handover.source_session_id,
                "timestamp": time.time(),
            })
            return session

    def pass_reviewer_to_qa(
        self,
        handover: ReviewerToQAHandover,
        evidence_id: str,
        host_handle: Optional[AgentHandle] = None,
    ) -> TaskExecutionSession:
        """Reviewer approves candidate -> transitions to QA."""
        with self._lock:
            session = self._sessions.get(handover.task_id)
            if not session:
                raise OrchestrationStateError(f"No active session for task '{handover.task_id}'")

            if session.state != OrchestrationState.REVIEWING:
                # Idempotency check
                if session.state == OrchestrationState.TESTING and session.candidate_commit == handover.candidate_commit:
                    return session
                raise OrchestrationStateError(
                    f"Cannot pass reviewer: Task '{handover.task_id}' is in state '{session.state.value}', expected REVIEWING."
                )

            # Session isolation
            if handover.reviewer_session_id == session.builder_session_id:
                raise OrchestrationSessionIsolationError(
                    f"Reviewer session '{handover.reviewer_session_id}' cannot be identical to Builder session '{session.builder_session_id}'."
                )

            # Stale candidate check
            if session.candidate_commit and handover.candidate_commit != session.candidate_commit:
                raise OrchestrationStateError(
                    f"Stale candidate commit '{handover.candidate_commit}' does not match session candidate '{session.candidate_commit}'."
                )

            # Validate evidence
            if not evidence_id or not evidence_id.strip():
                raise OrchestrationGateError("Evidence is mandatory for advancing to QA state.")

            if self.evidence_gate and self.evidence_store and host_handle:
                val_ctx = EvidenceValidationContext(
                    project_id=session.project_id,
                    task_id=session.task_id,
                    actor_role=OrchestrationRole.REVIEWER.value,
                    transition_from="REVIEWING",
                    transition_to="TESTING",
                    baseline_commit=session.baseline_commit,
                    result_commit=handover.candidate_commit,
                    expected_invocation_id=handover.reviewer_invocation_id,
                    expected_adapter=session.reviewer_adapter_id or "generic_reviewer",
                    expected_workspace_mode="workspace_read",  # Reviewer is read-only
                    expected_evidence_type=EvidenceType.TASK_TRANSITION,
                    host_handle=host_handle,
                    expected_capabilities=HostCapabilities(
                        supports_streaming=True,
                        supports_structured_events=True,
                        supports_tool_calling=True,
                    ),
                )
                self.evidence_gate.validate_evidence(evidence_id, val_ctx)

            session.reviewer_session_id = handover.reviewer_session_id
            session.reviewer_invocation_id = handover.reviewer_invocation_id
            session.last_evidence_id = evidence_id
            session.state = OrchestrationState.TESTING
            session.current_role = OrchestrationRole.QA
            session.updated_at = time.time()
            session.history.append({
                "action": "PASS_REVIEWER_TO_QA",
                "evidence_id": evidence_id,
                "reviewer_session_id": handover.reviewer_session_id,
                "timestamp": time.time(),
            })
            return session

    def reject_by_qa(self, handover: DefectRejectionHandover) -> TaskExecutionSession:
        """QA test fails -> returns to original Builder assignee without creating a new task."""
        with self._lock:
            session = self._sessions.get(handover.task_id)
            if not session:
                raise OrchestrationStateError(f"No active session for task '{handover.task_id}'")

            if session.state != OrchestrationState.TESTING:
                raise OrchestrationStateError(
                    f"Cannot reject from QA: Task '{handover.task_id}' is in state '{session.state.value}', expected TESTING."
                )

            # Session isolation: QA != Builder and QA != Reviewer
            if handover.source_session_id == session.builder_session_id:
                raise OrchestrationSessionIsolationError(
                    f"QA session '{handover.source_session_id}' cannot be identical to Builder session '{session.builder_session_id}'."
                )
            if handover.source_session_id == session.reviewer_session_id:
                raise OrchestrationSessionIsolationError(
                    f"QA session '{handover.source_session_id}' cannot be identical to Reviewer session '{session.reviewer_session_id}'."
                )

            # Stale candidate check
            if session.candidate_commit and handover.candidate_commit != session.candidate_commit:
                raise OrchestrationStateError(
                    f"Stale candidate commit '{handover.candidate_commit}' does not match session candidate '{session.candidate_commit}'."
                )

            session.qa_session_id = handover.source_session_id
            session.qa_invocation_id = handover.source_invocation_id
            session.state = OrchestrationState.REJECTED_BY_QA
            session.current_role = OrchestrationRole.BUILDER
            session.current_assignee = handover.target_builder_assignee
            session.updated_at = time.time()
            session.history.append({
                "action": "REJECT_BY_QA",
                "defects": list(handover.defect_list),
                "comments": handover.comments,
                "qa_session_id": handover.source_session_id,
                "timestamp": time.time(),
            })
            return session

    def pass_qa_to_user_acceptance(
        self,
        request: UserAcceptanceRequest,
        qa_session_id: str,
        qa_invocation_id: str,
        evidence_id: str,
        host_handle: Optional[AgentHandle] = None,
    ) -> TaskExecutionSession:
        """QA passes -> transitions to PENDING_USER_ACCEPTANCE."""
        with self._lock:
            session = self._sessions.get(request.task_id)
            if not session:
                raise OrchestrationStateError(f"No active session for task '{request.task_id}'")

            if session.state != OrchestrationState.TESTING:
                # Idempotency check
                if session.state == OrchestrationState.PENDING_USER_ACCEPTANCE and session.candidate_commit == request.candidate_commit:
                    return session
                raise OrchestrationStateError(
                    f"Cannot advance to user acceptance: Task '{request.task_id}' is in state '{session.state.value}', expected TESTING."
                )

            # Session isolation: QA != Builder and QA != Reviewer
            if qa_session_id == session.builder_session_id:
                raise OrchestrationSessionIsolationError("QA session cannot be identical to Builder session.")
            if qa_session_id == session.reviewer_session_id:
                raise OrchestrationSessionIsolationError("QA session cannot be identical to Reviewer session.")

            # Stale candidate check
            if session.candidate_commit and request.candidate_commit != session.candidate_commit:
                raise OrchestrationStateError(
                    f"Stale candidate commit '{request.candidate_commit}' does not match session candidate '{session.candidate_commit}'."
                )

            # Validate evidence
            if not evidence_id or not evidence_id.strip():
                raise OrchestrationGateError("Evidence is mandatory for advancing to user acceptance.")

            if self.evidence_gate and self.evidence_store and host_handle:
                val_ctx = EvidenceValidationContext(
                    project_id=session.project_id,
                    task_id=session.task_id,
                    actor_role=OrchestrationRole.QA.value,
                    transition_from="TESTING",
                    transition_to="PENDING_USER_ACCEPTANCE",
                    baseline_commit=session.baseline_commit,
                    result_commit=request.candidate_commit,
                    expected_invocation_id=qa_invocation_id,
                    expected_adapter=session.qa_adapter_id or "generic_qa",
                    expected_workspace_mode="workspace_read",  # QA executes tests, doesn't edit source
                    expected_evidence_type=EvidenceType.TASK_TRANSITION,
                    host_handle=host_handle,
                    expected_capabilities=HostCapabilities(
                        supports_streaming=True,
                        supports_structured_events=True,
                        supports_tool_calling=True,
                    ),
                )
                self.evidence_gate.validate_evidence(evidence_id, val_ctx)

            session.qa_session_id = qa_session_id
            session.qa_invocation_id = qa_invocation_id
            session.last_evidence_id = evidence_id
            session.state = OrchestrationState.PENDING_USER_ACCEPTANCE
            session.current_role = OrchestrationRole.USER
            session.updated_at = time.time()
            session.history.append({
                "action": "PASS_QA_TO_USER_ACCEPTANCE",
                "evidence_id": evidence_id,
                "qa_session_id": qa_session_id,
                "timestamp": time.time(),
            })
            return session

    def confirm_user_acceptance(
        self,
        decision: UserAcceptanceDecision,
        evidence_id: Optional[str] = None,
        confirmation_result: Optional[ConfirmationResult] = None,
    ) -> TaskExecutionSession:
        """
        User explicit acceptance confirmation.
        Strictly rejects model self-reported or PM agent automated confirmation.
        Explicitly DOES NOT perform git merge main, push, tag, or worktree cleanup.
        """
        with self._lock:
            session = self._sessions.get(decision.task_id)
            if not session:
                raise OrchestrationStateError(f"No active session for task '{decision.task_id}'")

            if session.state != OrchestrationState.PENDING_USER_ACCEPTANCE:
                if session.state == OrchestrationState.ACCEPTED and decision.is_accepted:
                    return session
                raise OrchestrationStateError(
                    f"Cannot confirm acceptance: Task '{decision.task_id}' is in state '{session.state.value}', expected PENDING_USER_ACCEPTANCE."
                )

            # Strict user source gate
            if decision.user_source != "explicit_user":
                raise OrchestrationSecurityError(
                    f"User acceptance strictly requires 'explicit_user' confirmation. Rejected source '{decision.user_source}'."
                )

            if decision.is_accepted:
                session.state = OrchestrationState.ACCEPTED
                session.current_role = OrchestrationRole.USER
                session.updated_at = time.time()
                session.history.append({
                    "action": "USER_ACCEPTANCE_CONFIRMED",
                    "user_source": decision.user_source,
                    "remarks": decision.remarks,
                    "signature": decision.user_signature,
                    "timestamp": time.time(),
                })
            else:
                session.state = OrchestrationState.BUILDING
                session.current_role = OrchestrationRole.BUILDER
                session.updated_at = time.time()
                session.history.append({
                    "action": "USER_ACCEPTANCE_REJECTED",
                    "user_source": decision.user_source,
                    "remarks": decision.remarks,
                    "timestamp": time.time(),
                })

            return session

    def validate_handle_adapter_binding(self, handle: AgentHandle, expected_adapter_id: str) -> None:
        """Enforce strict cross-host handle isolation (Codex handle != Antigravity handle)."""
        if not isinstance(handle, AgentHandle):
            raise AgentInvalidHandleError("handle must be an AgentHandle instance")
        if handle.host_id != expected_adapter_id:
            raise OrchestrationSessionIsolationError(
                f"Handle host_id '{handle.host_id}' does not match expected adapter '{expected_adapter_id}'. "
                "Cross-host handle exchange is strictly forbidden."
            )

    def _validate_session_boundary(
        self,
        session: TaskExecutionSession,
        project_id: str,
        auth_context: str,
        billing_context: str,
    ) -> None:
        if project_id != session.project_id:
            raise OrchestrationSecurityError(
                f"Cross-project boundary violation: '{project_id}' != '{session.project_id}'"
            )
        if auth_context != session.auth_context:
            raise OrchestrationSecurityError(
                f"Cross-auth-context boundary violation: '{auth_context}' != '{session.auth_context}'"
            )
        if billing_context != session.billing_context:
            raise OrchestrationSecurityError(
                f"Cross-billing-context boundary violation: '{billing_context}' != '{session.billing_context}'"
            )

    def evaluate_orchestration_mode(
        self,
        adapter_id: str,
        target_os: Optional[str] = None,
    ) -> OrchestrationMode:
        """
        Dynamically determine if an adapter can enter verified_automatic mode
        based on its Manifest and platform verification level on target_os.
        """
        manifest = self.registry.get_manifest(adapter_id)
        if not manifest:
            raise OrchestrationGateError(f"Adapter '{adapter_id}' is not registered.")

        effective_os = target_os or self._normalize_current_os()
        plat_ver = manifest.platform_verifications.get(effective_os)
        if not plat_ver:
            return OrchestrationMode.MANUAL

        vl = plat_ver.verification_level
        if vl in (
            VerificationLevel.NATIVE_VERIFIED,
            VerificationLevel.CLI_VERIFIED,
            VerificationLevel.MCP_VERIFIED,
        ):
            adapter = self.registry.get(adapter_id)
            if adapter and adapter.detect_capabilities().is_real_host:
                return OrchestrationMode.VERIFIED_AUTOMATIC

        return OrchestrationMode.ASSISTED

    def evaluate_dual_host_verification(
        self,
        builder_adapter_id: str,
        reviewer_adapter_id: str,
        target_os: Optional[str] = None,
    ) -> DualHostVerificationResult:
        """
        Evaluate if dual-host L2 verification is ready.
        Returns BLOCKED / NOT_READY when either adapter is STATIC_ONLY (e.g. Antigravity).
        Never fakes success.
        """
        effective_os = target_os or self._normalize_current_os()
        b_manifest = self.registry.get_manifest(builder_adapter_id)
        r_manifest = self.registry.get_manifest(reviewer_adapter_id)

        if not b_manifest:
            return DualHostVerificationResult(
                status=DualHostVerificationStatus.BLOCKED,
                builder_adapter_id=builder_adapter_id,
                builder_verification_level=VerificationLevel.STATIC_ONLY,
                reviewer_adapter_id=reviewer_adapter_id,
                reviewer_verification_level=VerificationLevel.STATIC_ONLY,
                is_dual_host_verified=False,
                reason=f"Builder adapter '{builder_adapter_id}' is not registered.",
            )

        b_pv = b_manifest.platform_verifications.get(effective_os)
        b_vl = b_pv.verification_level if b_pv else VerificationLevel.STATIC_ONLY

        if not r_manifest:
            return DualHostVerificationResult(
                status=DualHostVerificationStatus.BLOCKED,
                builder_adapter_id=builder_adapter_id,
                builder_verification_level=b_vl,
                reviewer_adapter_id=reviewer_adapter_id,
                reviewer_verification_level=VerificationLevel.STATIC_ONLY,
                is_dual_host_verified=False,
                reason=f"Reviewer adapter '{reviewer_adapter_id}' is not registered.",
            )

        r_pv = r_manifest.platform_verifications.get(effective_os)
        r_vl = r_pv.verification_level if r_pv else VerificationLevel.STATIC_ONLY

        verified_levels = {
            VerificationLevel.NATIVE_VERIFIED,
            VerificationLevel.CLI_VERIFIED,
            VerificationLevel.MCP_VERIFIED,
        }

        if b_vl not in verified_levels or r_vl not in verified_levels:
            return DualHostVerificationResult(
                status=DualHostVerificationStatus.NOT_READY,
                builder_adapter_id=builder_adapter_id,
                builder_verification_level=b_vl,
                reviewer_adapter_id=reviewer_adapter_id,
                reviewer_verification_level=r_vl,
                is_dual_host_verified=False,
                reason=(
                    f"Dual-host automated verification is NOT_READY: Builder adapter '{builder_adapter_id}' is {b_vl.value}, "
                    f"Reviewer adapter '{reviewer_adapter_id}' is {r_vl.value} on {effective_os}. Both require verified status."
                ),
            )

        return DualHostVerificationResult(
            status=DualHostVerificationStatus.READY,
            builder_adapter_id=builder_adapter_id,
            builder_verification_level=b_vl,
            reviewer_adapter_id=reviewer_adapter_id,
            reviewer_verification_level=r_vl,
            is_dual_host_verified=True,
            reason=f"Both adapters '{builder_adapter_id}' ({b_vl.value}) and '{reviewer_adapter_id}' ({r_vl.value}) are verified on {effective_os}.",
        )

    def generate_assisted_handover_card(
        self,
        handover: BuilderToReviewerHandover,
    ) -> str:
        """Generate structured handover card in assisted mode for user-assisted copy to Reviewer client."""
        return (
            f"```yaml\n"
            f"handover_type: BUILDER_TO_REVIEWER\n"
            f"task_id: {handover.task_id}\n"
            f"project_id: {handover.project_id}\n"
            f"branch: {handover.branch}\n"
            f"baseline_commit: {handover.baseline_commit}\n"
            f"candidate_commit: {handover.candidate_commit}\n"
            f"modified_files:\n"
            + "".join(f"  - {f}\n" for f in handover.modified_files)
            + f"workspace_dir: {handover.workspace_dir}\n"
            f"auth_context: {handover.auth_context}\n"
            f"billing_context: {handover.billing_context}\n"
            f"status: READY_FOR_REVIEW\n"
            f"instruction: 请将本交接卡提供给独立 Reviewer 客户端执行只读审查，严禁在 Reviewer 中修改源码。\n"
            f"```"
        )

    def export_checkpoint(self, task_id: str) -> Dict[str, Any]:
        """Export session checkpoint without touching git state."""
        with self._lock:
            session = self._sessions.get(task_id)
            if not session:
                raise OrchestrationStateError(f"No session found for task '{task_id}'")
            return {
                "task_id": session.task_id,
                "project_id": session.project_id,
                "branch": session.branch,
                "baseline_commit": session.baseline_commit,
                "candidate_commit": session.candidate_commit,
                "state": session.state.value,
                "current_role": session.current_role.value,
                "current_assignee": session.current_assignee,
                "workspace_dir": session.workspace_dir,
                "worktree_dir": session.worktree_dir,
                "auth_context": session.auth_context,
                "billing_context": session.billing_context,
                "builder_session_id": session.builder_session_id,
                "builder_invocation_id": session.builder_invocation_id,
                "builder_adapter_id": session.builder_adapter_id,
                "reviewer_session_id": session.reviewer_session_id,
                "reviewer_invocation_id": session.reviewer_invocation_id,
                "reviewer_adapter_id": session.reviewer_adapter_id,
                "qa_session_id": session.qa_session_id,
                "qa_invocation_id": session.qa_invocation_id,
                "qa_adapter_id": session.qa_adapter_id,
                "last_evidence_id": session.last_evidence_id,
                "history": list(session.history),
                "created_at": session.created_at,
                "updated_at": session.updated_at,
            }

    def import_checkpoint(self, data: Dict[str, Any]) -> TaskExecutionSession:
        """Import session checkpoint non-destructively."""
        with self._lock:
            task_id = data["task_id"]
            session = TaskExecutionSession(
                task_id=task_id,
                project_id=data["project_id"],
                branch=data["branch"],
                baseline_commit=data["baseline_commit"],
                candidate_commit=data.get("candidate_commit"),
                state=OrchestrationState(data["state"]),
                current_role=OrchestrationRole(data["current_role"]),
                current_assignee=data["current_assignee"],
                workspace_dir=data["workspace_dir"],
                worktree_dir=data["worktree_dir"],
                auth_context=data["auth_context"],
                billing_context=data["billing_context"],
                builder_session_id=data.get("builder_session_id"),
                builder_invocation_id=data.get("builder_invocation_id"),
                builder_adapter_id=data.get("builder_adapter_id"),
                reviewer_session_id=data.get("reviewer_session_id"),
                reviewer_invocation_id=data.get("reviewer_invocation_id"),
                reviewer_adapter_id=data.get("reviewer_adapter_id"),
                qa_session_id=data.get("qa_session_id"),
                qa_invocation_id=data.get("qa_invocation_id"),
                qa_adapter_id=data.get("qa_adapter_id"),
                last_evidence_id=data.get("last_evidence_id"),
                history=list(data.get("history", [])),
                created_at=data.get("created_at", time.time()),
                updated_at=data.get("updated_at", time.time()),
            )
            self._sessions[task_id] = session
            return session
