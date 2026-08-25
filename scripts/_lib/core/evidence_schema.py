from dataclasses import dataclass, field, is_dataclass
from typing import List, Dict, Any, Optional, Tuple, Mapping
from enum import Enum
import types

class EvidenceType(str, Enum):
    TASK_START = "task_start"
    TASK_TRANSITION = "task_transition"
    TASK_COMPLETE = "task_complete"
    USER_CONFIRMATION = "user_confirmation"

def freeze_value(val: Any) -> Any:
    if isinstance(val, dict):
        return types.MappingProxyType({k: freeze_value(v) for k, v in val.items()})
    elif isinstance(val, list):
        return tuple(freeze_value(v) for v in val)
    return val

@dataclass(frozen=True)
class ArtifactRecord:
    relative_path: str
    sha256_hash: str
    description: Optional[str] = None

@dataclass(frozen=True)
class EvidenceMetadata:
    project_id: str
    task_id: str
    actor_role: str
    host_id: str
    adapter: str
    host_session_id: str
    host_invocation_id: str
    is_real_host: bool
    workspace_mode: str
    transition_from: str
    transition_to: str
    created_at: float
    extra: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self):
        object.__setattr__(self, 'extra', freeze_value(self.extra))

@dataclass(frozen=True)
class EvidenceRecord:
    evidence_id: str
    evidence_type: EvidenceType
    baseline_commit: str
    result_commit: str
    artifacts: Tuple[ArtifactRecord, ...]
    metadata: EvidenceMetadata
    content_hash: Optional[str] = None

    def __post_init__(self):
        if isinstance(self.artifacts, list):
            object.__setattr__(self, 'artifacts', tuple(self.artifacts))

class EvidenceError(Exception): pass
class EvidenceSecurityError(EvidenceError): pass
class EvidenceIntegrityError(EvidenceError): pass
class EvidenceGateError(EvidenceError): pass
