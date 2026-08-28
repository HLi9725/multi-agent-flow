# -*- coding: utf-8 -*-
"""
scripts/_lib/core/runner_schema.py
2F-PROD 通用自动编排 Runner 不可变 Schema 定义。
"""
from dataclasses import dataclass, field
from enum import Enum
import hashlib
import json
import time
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple


def _freeze_runner_value(val: Any) -> Any:
    """深度递归冻结数据结构以保证不可变性"""
    if isinstance(val, (str, int, float, bool, bytes, type(None))):
        return val
    if isinstance(val, (list, tuple)):
        return tuple(_freeze_runner_value(item) for item in val)
    if isinstance(val, (dict, Mapping)):
        return {k: _freeze_runner_value(v) for k, v in val.items()}
    if hasattr(val, "__dataclass_fields__"):
        return val
    raise TypeError(f"Unsupported mutable type for runner freezing: {type(val)}")


class RunnerState(str, Enum):
    INIT = "INIT"
    WORKTREE_READY = "WORKTREE_READY"
    BUILDING = "BUILDING"
    REVIEWING = "REVIEWING"
    QA_TESTING = "QA_TESTING"
    PENDING_USER_ACCEPTANCE = "PENDING_USER_ACCEPTANCE"
    NEEDS_USER_INPUT = "NEEDS_USER_INPUT"
    APPROVAL_REQUIRED = "APPROVAL_REQUIRED"
    CANCELLED = "CANCELLED"
    FAILED = "FAILED"


REVIEWER_JSON_SCHEMA: Dict[str, Any] = {
    "$schema": "http://json-schema.org/draft-07/schema#",
    "title": "ReviewerStructuredOutput",
    "type": "object",
    "required": [
        "task_id",
        "baseline_commit",
        "candidate_commit",
        "session_id",
        "host_invocation_id",
        "decision",
        "defects",
        "summary",
    ],
    "properties": {
        "task_id": {"type": "string"},
        "baseline_commit": {"type": "string", "pattern": "^[0-9a-f]{40}$"},
        "candidate_commit": {"type": "string", "pattern": "^[0-9a-f]{40}$"},
        "session_id": {"type": "string"},
        "host_invocation_id": {"type": "string"},
        "decision": {"type": "string", "enum": ["PASS", "REJECT"]},
        "defects": {
            "type": "array",
            "items": {
                "type": "object",
                "required": ["defect_id", "severity", "description"],
                "properties": {
                    "defect_id": {"type": "string"},
                    "severity": {"type": "string", "enum": ["P0", "P1", "P2", "P3"]},
                    "description": {"type": "string"},
                    "file_path": {"type": ["string", "null"]},
                    "line_range": {"type": ["string", "null"]},
                    "suggested_fix": {"type": ["string", "null"]},
                },
            },
        },
        "summary": {"type": "string"},
    },
    "additionalProperties": False,
}


@dataclass(frozen=True)
class ReviewerStructuredOutput:
    task_id: str
    baseline_commit: str
    candidate_commit: str
    session_id: str
    host_invocation_id: str
    decision: str  # "PASS" or "REJECT"
    defects: Tuple[Dict[str, Any], ...] = field(default_factory=tuple)
    summary: str = ""

    def __post_init__(self):
        if self.decision not in ("PASS", "REJECT"):
            raise ValueError(f"Invalid decision: {self.decision}. Must be 'PASS' or 'REJECT'.")
        if self.decision == "REJECT" and len(self.defects) == 0:
            raise ValueError("Reviewer decision is REJECT but defects list is empty.")
        object.__setattr__(self, "defects", _freeze_runner_value(list(self.defects)))

    def to_dict(self) -> Dict[str, Any]:
        return {
            "task_id": self.task_id,
            "baseline_commit": self.baseline_commit,
            "candidate_commit": self.candidate_commit,
            "session_id": self.session_id,
            "host_invocation_id": self.host_invocation_id,
            "decision": self.decision,
            "defects": [dict(d) for d in self.defects],
            "summary": self.summary,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "ReviewerStructuredOutput":
        return cls(
            task_id=str(data.get("task_id", "")),
            baseline_commit=str(data.get("baseline_commit", "")),
            candidate_commit=str(data.get("candidate_commit", "")),
            session_id=str(data.get("session_id", "")),
            host_invocation_id=str(data.get("host_invocation_id", "")),
            decision=str(data.get("decision", "")),
            defects=tuple(data.get("defects", [])),
            summary=str(data.get("summary", "")),
        )


@dataclass(frozen=True)
class TaskExecutionSpec:
    """不可变任务执行规范，绑定项目与任务上下文"""
    project_id: str
    project_root: str
    authority_root: str
    task_id: str
    task_name: str
    requirement_text: str
    acceptance_criteria: str
    acceptance_criteria_hash: str
    task_version: str
    status_at_read: str
    owner: str = "李开发"
    handler: str = "李开发"
    task_type: str = "A"
    baseline_branch: str = "main"
    baseline_commit: str = ""
    builder_adapter_id: str = "codex_cli"
    reviewer_adapter_id: str = "antigravity"
    qa_adapter_id: str = "codex_cli"
    workspace_mode: str = "branch"
    worktree_root: Optional[str] = None
    test_command: Optional[str] = None
    builder_timeout_seconds: int = 300
    reviewer_timeout_seconds: int = 300
    qa_timeout_seconds: int = 300
    max_review_cycles: int = 3
    max_qa_cycles: int = 3
    max_total_attempts: int = 6
    total_wall_clock_timeout_seconds: int = 1800
    allow_git_push: bool = False
    allow_merge_main: bool = False
    allow_auto_accept: bool = False
    allow_release: bool = False

    def __post_init__(self):
        if not self.task_id:
            raise ValueError("TaskExecutionSpec requires non-empty task_id")
        if not self.project_root:
            raise ValueError("TaskExecutionSpec requires non-empty project_root")
        if not self.authority_root:
            raise ValueError("TaskExecutionSpec requires non-empty authority_root")
        if not self.requirement_text:
            raise ValueError("TaskExecutionSpec requires non-empty requirement_text")
        if not self.acceptance_criteria:
            raise ValueError("TaskExecutionSpec requires non-empty acceptance_criteria")
        if not self.acceptance_criteria_hash:
            calculated_hash = hashlib.sha256(self.acceptance_criteria.strip().encode("utf-8")).hexdigest()
            object.__setattr__(self, "acceptance_criteria_hash", calculated_hash)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "project_id": self.project_id,
            "project_root": self.project_root,
            "authority_root": self.authority_root,
            "task_id": self.task_id,
            "task_name": self.task_name,
            "requirement_text": self.requirement_text,
            "acceptance_criteria": self.acceptance_criteria,
            "acceptance_criteria_hash": self.acceptance_criteria_hash,
            "task_version": self.task_version,
            "status_at_read": self.status_at_read,
            "owner": self.owner,
            "handler": self.handler,
            "task_type": self.task_type,
            "baseline_branch": self.baseline_branch,
            "baseline_commit": self.baseline_commit,
            "builder_adapter_id": self.builder_adapter_id,
            "reviewer_adapter_id": self.reviewer_adapter_id,
            "qa_adapter_id": self.qa_adapter_id,
            "workspace_mode": self.workspace_mode,
            "worktree_root": self.worktree_root,
            "test_command": self.test_command,
            "builder_timeout_seconds": self.builder_timeout_seconds,
            "reviewer_timeout_seconds": self.reviewer_timeout_seconds,
            "qa_timeout_seconds": self.qa_timeout_seconds,
            "max_review_cycles": self.max_review_cycles,
            "max_qa_cycles": self.max_qa_cycles,
            "max_total_attempts": self.max_total_attempts,
            "total_wall_clock_timeout_seconds": self.total_wall_clock_timeout_seconds,
            "allow_git_push": self.allow_git_push,
            "allow_merge_main": self.allow_merge_main,
            "allow_auto_accept": self.allow_auto_accept,
            "allow_release": self.allow_release,
        }


@dataclass(frozen=True)
class RunnerCheckpoint:
    """原子 Checkpoint 状态存储实体"""
    task_id: str
    project_id: str
    state: str
    current_role: str
    candidate_commit: Optional[str] = None
    candidate_generation: int = 0
    review_cycle: int = 0
    qa_cycle: int = 0
    total_attempts: int = 0
    worktree_path: Optional[str] = None
    worktree_branch: Optional[str] = None
    builder_session_id: Optional[str] = None
    builder_invocation_id: Optional[str] = None
    reviewer_session_id: Optional[str] = None
    reviewer_invocation_id: Optional[str] = None
    qa_session_id: Optional[str] = None
    qa_invocation_id: Optional[str] = None
    evidence_ids: Tuple[str, ...] = field(default_factory=tuple)
    confirmation_request_id: Optional[str] = None
    defects_history: Tuple[Dict[str, Any], ...] = field(default_factory=tuple)
    approval_reason: Optional[str] = None
    last_error: Optional[str] = None
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)

    def __post_init__(self):
        object.__setattr__(self, "evidence_ids", tuple(self.evidence_ids))
        object.__setattr__(self, "defects_history", tuple(self.defects_history))

    def to_dict(self) -> Dict[str, Any]:
        return {
            "task_id": self.task_id,
            "project_id": self.project_id,
            "state": self.state,
            "current_role": self.current_role,
            "candidate_commit": self.candidate_commit,
            "candidate_generation": self.candidate_generation,
            "review_cycle": self.review_cycle,
            "qa_cycle": self.qa_cycle,
            "total_attempts": self.total_attempts,
            "worktree_path": self.worktree_path,
            "worktree_branch": self.worktree_branch,
            "builder_session_id": self.builder_session_id,
            "builder_invocation_id": self.builder_invocation_id,
            "reviewer_session_id": self.reviewer_session_id,
            "reviewer_invocation_id": self.reviewer_invocation_id,
            "qa_session_id": self.qa_session_id,
            "qa_invocation_id": self.qa_invocation_id,
            "evidence_ids": list(self.evidence_ids),
            "confirmation_request_id": self.confirmation_request_id,
            "defects_history": [dict(d) for d in self.defects_history],
            "approval_reason": self.approval_reason,
            "last_error": self.last_error,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "RunnerCheckpoint":
        return cls(
            task_id=str(data["task_id"]),
            project_id=str(data["project_id"]),
            state=str(data["state"]),
            current_role=str(data["current_role"]),
            candidate_commit=data.get("candidate_commit"),
            candidate_generation=int(data.get("candidate_generation", 0)),
            review_cycle=int(data.get("review_cycle", 0)),
            qa_cycle=int(data.get("qa_cycle", 0)),
            total_attempts=int(data.get("total_attempts", 0)),
            worktree_path=data.get("worktree_path"),
            worktree_branch=data.get("worktree_branch"),
            builder_session_id=data.get("builder_session_id"),
            builder_invocation_id=data.get("builder_invocation_id"),
            reviewer_session_id=data.get("reviewer_session_id"),
            reviewer_invocation_id=data.get("reviewer_invocation_id"),
            qa_session_id=data.get("qa_session_id"),
            qa_invocation_id=data.get("qa_invocation_id"),
            evidence_ids=tuple(data.get("evidence_ids", [])),
            confirmation_request_id=data.get("confirmation_request_id"),
            defects_history=tuple(data.get("defects_history", [])),
            approval_reason=data.get("approval_reason"),
            last_error=data.get("last_error"),
            created_at=float(data.get("created_at", time.time())),
            updated_at=float(data.get("updated_at", time.time())),
        )


@dataclass(frozen=True)
class RunnerResult:
    """Runner 终态执行结果"""
    success: bool
    state: str
    task_id: str
    candidate_commit: Optional[str] = None
    candidate_generation: int = 0
    evidence_ids: Tuple[str, ...] = field(default_factory=tuple)
    confirmation_request_id: Optional[str] = None
    message: str = ""
    diagnostics: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "success": self.success,
            "state": self.state,
            "task_id": self.task_id,
            "candidate_commit": self.candidate_commit,
            "candidate_generation": self.candidate_generation,
            "evidence_ids": list(self.evidence_ids),
            "confirmation_request_id": self.confirmation_request_id,
            "message": self.message,
            "diagnostics": self.diagnostics,
        }
