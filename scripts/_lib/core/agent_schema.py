from collections.abc import Mapping
from dataclasses import dataclass, field
from math import isfinite
from numbers import Real
from types import MappingProxyType
from typing import Any, Optional, Tuple
from enum import Enum


def _freeze_value(value: Any) -> Any:
    """Recursively copy mutable containers into immutable equivalents."""
    if isinstance(value, Mapping):
        return MappingProxyType({key: _freeze_value(item) for key, item in value.items()})
    if isinstance(value, (list, tuple)):
        return tuple(_freeze_value(item) for item in value)
    if isinstance(value, (set, frozenset)):
        return frozenset(_freeze_value(item) for item in value)
    return value


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


@dataclass(frozen=True)
class HostCapabilities:
    is_real_host: bool = False
    supports_real_subagents: CapabilitySupport = CapabilitySupport.UNKNOWN
    supports_parallelism: CapabilitySupport = CapabilitySupport.UNKNOWN
    supports_isolated_context: CapabilitySupport = CapabilitySupport.UNKNOWN
    supports_worktree: CapabilitySupport = CapabilitySupport.UNKNOWN
    supports_permission_approval: CapabilitySupport = CapabilitySupport.UNKNOWN
    supports_mcp: CapabilitySupport = CapabilitySupport.UNKNOWN
    supports_interactive_confirmation: CapabilitySupport = CapabilitySupport.UNKNOWN
    supports_usage_telemetry: CapabilitySupport = CapabilitySupport.UNKNOWN
    max_concurrent_agents: int = 0
    extra: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "extra", _freeze_value(self.extra))


@dataclass(frozen=True)
class AgentRequest:
    session_id: str
    prompt: str
    role: str
    workspace_dir: str
    capabilities_required: Tuple[str, ...] = field(default_factory=tuple)
    timeout_seconds: float = 3600
    extra_context: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if (
            isinstance(self.timeout_seconds, bool)
            or not isinstance(self.timeout_seconds, Real)
            or not isfinite(self.timeout_seconds)
            or self.timeout_seconds < 0
        ):
            raise ValueError("timeout_seconds must be a finite non-negative number")
        object.__setattr__(self, "timeout_seconds", float(self.timeout_seconds))
        object.__setattr__(self, "capabilities_required", tuple(self.capabilities_required))
        object.__setattr__(self, "extra_context", _freeze_value(self.extra_context))


@dataclass(frozen=True)
class DispatchPlan:
    plan_id: str
    requests: Tuple[AgentRequest, ...] = field(default_factory=tuple)
    strategy: str = "parallel"

    def __post_init__(self) -> None:
        object.__setattr__(self, "requests", tuple(self.requests))


@dataclass(frozen=True)
class AgentHandle:
    session_id: str
    host_id: str
    status: str = "running"
    is_real_host: bool = False
    adapter_instance_id: str = ""
    invocation_token: str = ""


@dataclass(frozen=True)
class AgentResult:
    session_id: str
    status: AgentStatus
    output: str
    partial_results: Tuple[Any, ...] = field(default_factory=tuple)
    error_message: Optional[str] = None
    is_real_host: bool = False

    def __post_init__(self) -> None:
        object.__setattr__(self, "partial_results", _freeze_value(self.partial_results))


@dataclass(frozen=True)
class ConfirmationRequest:
    request_id: str
    prompt: str
    options: Tuple[str, ...] = field(default_factory=tuple)

    def __post_init__(self) -> None:
        object.__setattr__(self, "options", tuple(self.options))


@dataclass(frozen=True)
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


class AgentInvalidHandleError(AgentError):
    pass
