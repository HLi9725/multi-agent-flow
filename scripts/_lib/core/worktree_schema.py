from dataclasses import dataclass, field
from typing import Optional, Dict, Any, List
import types

class WorktreeError(Exception):
    pass

class WorktreeSecurityError(WorktreeError):
    pass

class WorktreeGitError(WorktreeError):
    pass

def freeze_value(val: Any) -> Any:
    if val is None or isinstance(val, (int, float, str, bool, bytes)):
        return val
    if isinstance(val, dict):
        return types.MappingProxyType({k: freeze_value(v) for k, v in val.items()})
    if isinstance(val, (list, tuple)):
        return tuple(freeze_value(v) for v in val)
    raise TypeError(f"Unsupported mutable type for Worktree: {type(val)}")

@dataclass(frozen=True)
class WorktreeRequest:
    project_id: str
    task_id: str
    actor_role: str
    host_session_id: str
    baseline_commit: str

@dataclass(frozen=True)
class WorktreeDescriptor:
    worktree_id: str
    absolute_path: str
    branch_name: str
    baseline_commit: str
    created_at: float
    request: WorktreeRequest

@dataclass(frozen=True)
class WorktreeStatus:
    is_valid: bool
    is_clean: bool
    current_commit: str
    head_ref: str
    untracked_files: int
    modified_files: int
