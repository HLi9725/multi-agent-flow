from dataclasses import dataclass, field, is_dataclass
from typing import List, Dict, Any, Optional, Tuple, Mapping, Set, FrozenSet
from enum import Enum
import types

class EvidenceType(str, Enum):
    TASK_START = "task_start"
    TASK_TRANSITION = "task_transition"
    TASK_COMPLETE = "task_complete"
    USER_CONFIRMATION = "user_confirmation"

def freeze_value(val: Any) -> Any:
    if val is None or isinstance(val, (int, float, str, bool, bytes, Enum)):
        return val
    if isinstance(val, Mapping):
        return types.MappingProxyType({k: freeze_value(v) for k, v in val.items()})
    if isinstance(val, (list, tuple)):
        return tuple(freeze_value(v) for v in val)
    if isinstance(val, (set, frozenset)):
        return frozenset(freeze_value(v) for v in val)
    raise TypeError(f"Unsupported mutable or complex type for Evidence: {type(val)}")

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
        if not isinstance(self.artifacts, tuple):
            object.__setattr__(self, 'artifacts', tuple(self.artifacts))
        for art in self.artifacts:
            if not isinstance(art, ArtifactRecord):
                raise TypeError(f"Artifact must be an ArtifactRecord, got {type(art)}")
            
class EvidenceError(Exception): pass
class EvidenceSecurityError(EvidenceError): pass
class EvidenceIntegrityError(EvidenceError): pass
class EvidenceGateError(EvidenceError): pass
