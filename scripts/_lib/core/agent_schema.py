from dataclasses import dataclass, field
from typing import List, Dict, Any, Optional
from enum import Enum

class AgentStatus(str, Enum):
    SUCCESS = "success"
    TIMEOUT = "timeout"
    CANCELLED = "cancelled"
    PARTIAL = "partial"
    FAILED = "failed"
    UNKNOWN = "unknown"

class CapabilitySupport(str, Enum):
    SUPPORTED = "supported"
    UNSUPPORTED = "unsupported"
    UNKNOWN = "unknown"

@dataclass
class HostCapabilities:
    is_real_host: bool = False
    supports_mcp: CapabilitySupport = CapabilitySupport.UNKNOWN
    supports_worktree: CapabilitySupport = CapabilitySupport.UNKNOWN
    supports_interactive_confirmation: CapabilitySupport = CapabilitySupport.UNKNOWN
    max_concurrent_agents: int = 0
    extra: Dict[str, Any] = field(default_factory=dict)

@dataclass
class AgentRequest:
    session_id: str
    prompt: str
    role: str
    workspace_dir: str
    capabilities_required: List[str] = field(default_factory=list)
    timeout_seconds: int = 3600
    extra_context: Dict[str, Any] = field(default_factory=dict)

@dataclass
class DispatchPlan:
    plan_id: str
    requests: List[AgentRequest] = field(default_factory=list)
    strategy: str = "parallel"  # parallel or sequential

@dataclass
class AgentHandle:
    session_id: str
    host_id: str
    status: str = "running"
    is_real_host: bool = False

@dataclass
class AgentResult:
    session_id: str
    status: AgentStatus
    output: str
    partial_results: List[Any] = field(default_factory=list)
    error_message: Optional[str] = None
    is_real_host: bool = False

@dataclass
class ConfirmationRequest:
    request_id: str
    prompt: str
    options: List[str] = field(default_factory=list)

@dataclass
class ConfirmationResult:
    request_id: str
    selected_option: str
    is_confirmed: bool
    is_real_host: bool = False

class AgentError(Exception):
    """Base class for Agent exceptions."""
    pass

class AgentTimeoutError(AgentError):
    pass

class AgentCancelledError(AgentError):
    pass

class AgentPartialResultError(AgentError):
    def __init__(self, message: str, partial_result: AgentResult):
        super().__init__(message)
        self.partial_result = partial_result

class AgentNotSupportedError(AgentError):
    pass
