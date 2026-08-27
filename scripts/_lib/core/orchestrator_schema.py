from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import Enum
import time
from types import MappingProxyType
from typing import Any, Dict, List, Mapping as TMapping, Optional, Sequence, Tuple

from .adapter_manifest import VerificationLevel


class OrchestrationRole(str, Enum):
    BUILDER = "BUILDER"
    REVIEWER = "REVIEWER"
    QA = "QA"
    USER = "USER"
    PM = "PM"


class OrchestrationState(str, Enum):
    IDLE = "IDLE"
    BUILDING = "BUILDING"
    REVIEWING = "REVIEWING"
    TESTING = "TESTING"
    PENDING_USER_ACCEPTANCE = "PENDING_USER_ACCEPTANCE"
    ACCEPTED = "ACCEPTED"
    REJECTED_BY_REVIEWER = "REJECTED_BY_REVIEWER"
    REJECTED_BY_QA = "REJECTED_BY_QA"
    BLOCKED = "BLOCKED"


class OrchestrationMode(str, Enum):
    MANUAL = "manual"
    ASSISTED = "assisted"
    VERIFIED_AUTOMATIC = "verified_automatic"


class DualHostVerificationStatus(str, Enum):
    READY = "READY"
    BLOCKED = "BLOCKED"
    NOT_READY = "NOT_READY"


def _freeze_orchestration_value(val: Any) -> Any:
    if val is None or isinstance(val, (int, float, str, bool, bytes, Enum)):
        return val
    if isinstance(val, Mapping):
        return MappingProxyType({k: _freeze_orchestration_value(v) for k, v in val.items()})
    if isinstance(val, (list, tuple)):
        return tuple(_freeze_orchestration_value(v) for v in val)
    if isinstance(val, (set, frozenset)):
        raise TypeError("set/frozenset is strictly forbidden in Orchestration schema due to non-deterministic serialization.")
    raise TypeError(f"Unsupported mutable or complex type for Orchestration schema: {type(val).__name__}")


@dataclass(frozen=True)
class BuilderToReviewerHandover:
    task_id: str
    project_id: str
    branch: str
    baseline_commit: str
    candidate_commit: str
    modified_files: Tuple[str, ...]
    diff_stat: TMapping[str, Any]
    test_summary: TMapping[str, Any]
    workspace_dir: str
    worktree_dir: str
    builder_session_id: str
    builder_invocation_id: str
    auth_context: str
    billing_context: str
    created_at: float = field(default_factory=time.time)

    def __post_init__(self) -> None:
        if not self.task_id or not self.task_id.strip():
            raise ValueError("task_id cannot be empty")
        if not self.project_id or not self.project_id.strip():
            raise ValueError("project_id cannot be empty")
        if not self.candidate_commit or not self.candidate_commit.strip():
            raise ValueError("candidate_commit cannot be empty")
        if not self.builder_session_id or not self.builder_session_id.strip():
            raise ValueError("builder_session_id cannot be empty")
        if not self.builder_invocation_id or not self.builder_invocation_id.strip():
            raise ValueError("builder_invocation_id cannot be empty")

        object.__setattr__(self, "modified_files", _freeze_orchestration_value(self.modified_files))
        object.__setattr__(self, "diff_stat", _freeze_orchestration_value(self.diff_stat))
        object.__setattr__(self, "test_summary", _freeze_orchestration_value(self.test_summary))


@dataclass(frozen=True)
class ReviewerToQAHandover:
    task_id: str
    project_id: str
    branch: str
    candidate_commit: str
    reviewer_decision: str  # Must be "PASS"
    review_comments: str
    review_evidence_id: str
    reviewer_session_id: str
    reviewer_invocation_id: str
    workspace_dir: str
    worktree_dir: str
    auth_context: str
    billing_context: str
    created_at: float = field(default_factory=time.time)

    def __post_init__(self) -> None:
        if not self.task_id or not self.task_id.strip():
            raise ValueError("task_id cannot be empty")
        if not self.project_id or not self.project_id.strip():
            raise ValueError("project_id cannot be empty")
        if not self.candidate_commit or not self.candidate_commit.strip():
            raise ValueError("candidate_commit cannot be empty")
        if self.reviewer_decision != "PASS":
            raise ValueError("reviewer_decision must be 'PASS' to handover to QA")
        if not self.review_evidence_id or not self.review_evidence_id.strip():
            raise ValueError("review_evidence_id cannot be empty")
        if not self.reviewer_session_id or not self.reviewer_session_id.strip():
            raise ValueError("reviewer_session_id cannot be empty")
        if not self.reviewer_invocation_id or not self.reviewer_invocation_id.strip():
            raise ValueError("reviewer_invocation_id cannot be empty")


@dataclass(frozen=True)
class DefectRejectionHandover:
    task_id: str
    project_id: str
    source_role: OrchestrationRole
    defect_list: Tuple[str, ...]
    comments: str
    candidate_commit: str
    source_session_id: str
    source_invocation_id: str
    target_builder_role: OrchestrationRole
    target_builder_assignee: str
    created_at: float = field(default_factory=time.time)

    def __post_init__(self) -> None:
        if not self.task_id or not self.task_id.strip():
            raise ValueError("task_id cannot be empty")
        if self.source_role not in (OrchestrationRole.REVIEWER, OrchestrationRole.QA):
            raise ValueError("Defect rejection source_role must be REVIEWER or QA")
        if not self.defect_list:
            raise ValueError("defect_list cannot be empty on rejection")
        if not self.candidate_commit or not self.candidate_commit.strip():
            raise ValueError("candidate_commit cannot be empty")
        if not self.source_session_id or not self.source_session_id.strip():
            raise ValueError("source_session_id cannot be empty")
        if not self.source_invocation_id or not self.source_invocation_id.strip():
            raise ValueError("source_invocation_id cannot be empty")
        if not self.target_builder_assignee or not self.target_builder_assignee.strip():
            raise ValueError("target_builder_assignee cannot be empty")

        object.__setattr__(self, "defect_list", _freeze_orchestration_value(self.defect_list))


@dataclass(frozen=True)
class UserAcceptanceRequest:
    task_id: str
    project_id: str
    branch: str
    candidate_commit: str
    qa_report: TMapping[str, Any]
    reviewer_report: TMapping[str, Any]
    artifacts: Tuple[str, ...]
    user_confirmation_prompt: str
    auth_context: str
    created_at: float = field(default_factory=time.time)

    def __post_init__(self) -> None:
        if not self.task_id or not self.task_id.strip():
            raise ValueError("task_id cannot be empty")
        if not self.candidate_commit or not self.candidate_commit.strip():
            raise ValueError("candidate_commit cannot be empty")
        if not self.user_confirmation_prompt or not self.user_confirmation_prompt.strip():
            raise ValueError("user_confirmation_prompt cannot be empty")

        object.__setattr__(self, "qa_report", _freeze_orchestration_value(self.qa_report))
        object.__setattr__(self, "reviewer_report", _freeze_orchestration_value(self.reviewer_report))
        object.__setattr__(self, "artifacts", _freeze_orchestration_value(self.artifacts))


@dataclass(frozen=True)
class UserAcceptanceDecision:
    task_id: str
    user_source: str  # Must be "explicit_user"
    is_accepted: bool
    remarks: str
    user_signature: Optional[str] = None
    confirmed_at: float = field(default_factory=time.time)

    def __post_init__(self) -> None:
        if not self.task_id or not self.task_id.strip():
            raise ValueError("task_id cannot be empty")
        if self.user_source != "explicit_user":
            raise ValueError(f"user_source must be 'explicit_user', rejected fake source '{self.user_source}'")


@dataclass(frozen=True)
class DualHostVerificationResult:
    status: DualHostVerificationStatus
    builder_adapter_id: str
    builder_verification_level: VerificationLevel
    reviewer_adapter_id: str
    reviewer_verification_level: VerificationLevel
    is_dual_host_verified: bool
    reason: str


class OrchestrationError(Exception):
    """Base exception for all orchestration errors."""
    pass


class OrchestrationStateError(OrchestrationError):
    """Raised when an invalid state transition or state jumping is attempted."""
    pass


class OrchestrationSecurityError(OrchestrationError):
    """Raised when a security boundary, role permission, or user confirmation constraint is violated."""
    pass


class OrchestrationGateError(OrchestrationError):
    """Raised when an evidence, capability, or verification level gate rejects an action."""
    pass


class OrchestrationSessionIsolationError(OrchestrationError):
    """Raised when session, invocation, handle, or workspace boundary isolation is violated."""
    pass
