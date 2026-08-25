from dataclasses import dataclass, field
from typing import List, Dict, Any, Optional
from enum import Enum

class EvidenceType(str, Enum):
    TASK_START = "task_start"
    TASK_TRANSITION = "task_transition"
    TASK_COMPLETE = "task_complete"
    USER_CONFIRMATION = "user_confirmation"

@dataclass(frozen=True)
class ArtifactRecord:
    relative_path: str
    sha256_hash: str
    description: Optional[str] = None

@dataclass(frozen=True)
class EvidenceMetadata:
    actor_role: str
    host_id: str
    host_session_id: str
    host_invocation_id: str
    is_real_host: bool
    transition_from: str
    transition_to: str
    extra: Dict[str, Any] = field(default_factory=dict)

@dataclass(frozen=True)
class EvidenceRecord:
    evidence_id: str
    evidence_type: EvidenceType
    baseline_commit: str
    result_commit: str
    artifacts: List[ArtifactRecord]
    metadata: EvidenceMetadata
    content_hash: Optional[str] = None

class EvidenceError(Exception):
    """Base class for Evidence exceptions."""
    pass

class EvidenceSecurityError(EvidenceError):
    """Raised when path traversal or sensitive data leak is detected."""
    pass

class EvidenceIntegrityError(EvidenceError):
    """Raised when evidence hash or artifact hash mismatches."""
    pass

class EvidenceGateError(EvidenceError):
    """Raised when evidence fails the status gating rules."""
    pass
