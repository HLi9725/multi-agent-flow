# -*- coding: utf-8 -*-
"""
scripts/_lib/core/runner_schema.py
2F-PROD 通用自动编排 Runner 不可变 Schema 定义。
"""
from dataclasses import dataclass, field
from enum import Enum
import hashlib
import json
import re
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
    COMPLETION_PENDING = "COMPLETION_PENDING"
    REJECTION_PENDING = "REJECTION_PENDING"
    PENDING_USER_ACCEPTANCE = "PENDING_USER_ACCEPTANCE"
    ACCEPTED = "ACCEPTED"
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
        "review_request_id",
        "decision",
        "defects",
        "summary",
    ],
    "properties": {
        "task_id": {"type": "string"},
        "baseline_commit": {"type": "string", "pattern": "^[0-9a-f]{40}$"},
        "candidate_commit": {"type": "string", "pattern": "^[0-9a-f]{40}$"},
        "session_id": {"type": "string"},
        "review_request_id": {"type": "string"},
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


QA_JSON_SCHEMA: Dict[str, Any] = {
    "$schema": "http://json-schema.org/draft-07/schema#",
    "title": "QAStructuredOutput",
    "type": "object",
    "required": [
        "task_id",
        "baseline_commit",
        "candidate_commit",
        "session_id",
        "qa_request_id",
        "acceptance_criteria_hash",
        "decision",
        "acceptance_coverage",
        "test_commands",
        "negative_scenarios",
        "uncovered_risks",
        "defects",
        "summary",
    ],
    "properties": {
        "task_id": {"type": "string"},
        "baseline_commit": {"type": "string", "pattern": "^[0-9a-f]{40}$"},
        "candidate_commit": {"type": "string", "pattern": "^[0-9a-f]{40}$"},
        "session_id": {"type": "string"},
        "qa_request_id": {"type": "string"},
        "acceptance_criteria_hash": {"type": "string", "pattern": "^[0-9a-f]{64}$"},
        "decision": {"type": "string", "enum": ["PASS", "FAIL"]},
        "acceptance_coverage": {
            "type": "array",
            "items": {
                "type": "object",
                "required": ["criterion_id", "status", "evidence"],
                "properties": {
                    "criterion_id": {"type": "string"},
                    "status": {"type": "string", "enum": ["PASS", "FAIL"]},
                    "evidence": {"type": "string"},
                },
                "additionalProperties": False,
            },
        },
        "test_commands": {
            "type": "array",
            "items": {
                "type": "object",
                "required": ["command", "exit_code", "summary"],
                "properties": {
                    "command": {"type": "string"},
                    "exit_code": {"type": "integer"},
                    "summary": {"type": "string"},
                },
                "additionalProperties": False,
            },
        },
        "negative_scenarios": {
            "type": "array",
            "items": {
                "type": "object",
                "required": ["name", "status", "evidence"],
                "properties": {
                    "name": {"type": "string"},
                    "status": {"type": "string", "enum": ["PASS", "FAIL"]},
                    "evidence": {"type": "string"},
                },
                "additionalProperties": False,
            },
        },
        "uncovered_risks": {"type": "array", "items": {"type": "string"}},
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
class AcceptanceCriterion:
    criterion_id: str
    text: str

    def __post_init__(self):
        if not self.criterion_id.strip() or not self.text.strip():
            raise ValueError("AcceptanceCriterion requires non-empty id and text")

    def to_dict(self) -> Dict[str, str]:
        return {"criterion_id": self.criterion_id, "text": self.text}


@dataclass(frozen=True)
class ReviewerStructuredOutput:
    task_id: str
    baseline_commit: str
    candidate_commit: str
    session_id: str
    host_invocation_id: str
    review_request_id: str
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
            "review_request_id": self.review_request_id,
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
            review_request_id=str(data.get("review_request_id", "")),
            decision=str(data.get("decision", "")),
            defects=tuple(data.get("defects", [])),
            summary=str(data.get("summary", "")),
        )


@dataclass(frozen=True)
class QAStructuredOutput:
    task_id: str
    baseline_commit: str
    candidate_commit: str
    session_id: str
    host_invocation_id: str
    qa_request_id: str
    acceptance_criteria_hash: str
    decision: str  # "PASS" or "FAIL"
    acceptance_coverage: Tuple[Dict[str, Any], ...] = field(default_factory=tuple)
    test_commands: Tuple[Dict[str, Any], ...] = field(default_factory=tuple)
    negative_scenarios: Tuple[Dict[str, Any], ...] = field(default_factory=tuple)
    uncovered_risks: Tuple[str, ...] = field(default_factory=tuple)
    defects: Tuple[Dict[str, Any], ...] = field(default_factory=tuple)
    summary: str = ""

    def __post_init__(self):
        if self.decision not in ("PASS", "FAIL"):
            raise ValueError(f"Invalid QA decision: {self.decision}. Must be 'PASS' or 'FAIL'.")
        if self.decision == "FAIL" and not self.defects:
            raise ValueError("QA decision is FAIL but defects list is empty.")
        object.__setattr__(self, "acceptance_coverage", _freeze_runner_value(list(self.acceptance_coverage)))
        object.__setattr__(self, "test_commands", _freeze_runner_value(list(self.test_commands)))
        object.__setattr__(self, "negative_scenarios", _freeze_runner_value(list(self.negative_scenarios)))
        object.__setattr__(self, "uncovered_risks", tuple(str(item) for item in self.uncovered_risks))
        object.__setattr__(self, "defects", _freeze_runner_value(list(self.defects)))

    def to_dict(self) -> Dict[str, Any]:
        return {
            "task_id": self.task_id,
            "baseline_commit": self.baseline_commit,
            "candidate_commit": self.candidate_commit,
            "session_id": self.session_id,
            "host_invocation_id": self.host_invocation_id,
            "qa_request_id": self.qa_request_id,
            "acceptance_criteria_hash": self.acceptance_criteria_hash,
            "decision": self.decision,
            "acceptance_coverage": [dict(item) for item in self.acceptance_coverage],
            "test_commands": [dict(item) for item in self.test_commands],
            "negative_scenarios": [dict(item) for item in self.negative_scenarios],
            "uncovered_risks": list(self.uncovered_risks),
            "defects": [dict(item) for item in self.defects],
            "summary": self.summary,
        }


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
    requirement_hash: str = ""
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
    test_commands: Tuple[str, ...] = field(default_factory=tuple)
    acceptance_criteria_items: Tuple[AcceptanceCriterion, ...] = field(default_factory=tuple)
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
        criteria_items = tuple(self.acceptance_criteria_items)
        if not criteria_items:
            fallback_text = re.sub(r"^验收标准\s*[:：]\s*", "", self.acceptance_criteria).strip()
            criteria_items = (AcceptanceCriterion("AC-01", fallback_text),)
        object.__setattr__(self, "acceptance_criteria_items", criteria_items)

        raw_commands = (self.test_commands,) if isinstance(self.test_commands, str) else self.test_commands
        commands = []
        for command in raw_commands:
            normalized = str(command).strip()
            if normalized and normalized not in commands:
                commands.append(normalized)
        if self.test_command and self.test_command.strip() and self.test_command.strip() not in commands:
            commands.insert(0, self.test_command.strip())
        commands = tuple(commands)
        object.__setattr__(self, "test_commands", commands)

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
            "requirement_hash": self.requirement_hash,
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
            "test_commands": list(self.test_commands),
            "acceptance_criteria_items": [item.to_dict() for item in self.acceptance_criteria_items],
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
    execution_options: Mapping[str, Any] = field(default_factory=dict)
    execution_spec_snapshot: Mapping[str, Any] = field(default_factory=dict)
    active_elapsed_seconds: float = 0.0
    confirmation_request_id: Optional[str] = None
    defects_history: Tuple[Dict[str, Any], ...] = field(default_factory=tuple)
    approval_reason: Optional[str] = None
    last_error: Optional[str] = None
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)

    def __post_init__(self):
        object.__setattr__(self, "evidence_ids", tuple(self.evidence_ids))
        object.__setattr__(self, "defects_history", tuple(self.defects_history))
        object.__setattr__(self, "execution_options", _freeze_runner_value(dict(self.execution_options)))
        object.__setattr__(self, "execution_spec_snapshot", _freeze_runner_value(dict(self.execution_spec_snapshot)))

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
            "execution_options": dict(self.execution_options),
            "execution_spec_snapshot": dict(self.execution_spec_snapshot),
            "active_elapsed_seconds": self.active_elapsed_seconds,
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
            execution_options=dict(data.get("execution_options", {})),
            execution_spec_snapshot=dict(data.get("execution_spec_snapshot", {})),
            active_elapsed_seconds=float(data.get("active_elapsed_seconds", 0.0)),
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
