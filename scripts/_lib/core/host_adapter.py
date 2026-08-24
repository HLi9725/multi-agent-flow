import abc
import time
import uuid
from typing import Optional

from .agent_schema import (
    HostCapabilities, AgentRequest, AgentHandle, AgentResult,
    ConfirmationRequest, ConfirmationResult, AgentStatus,
    AgentCancelledError, AgentTimeoutError, AgentNotSupportedError,
    CapabilitySupport
)

class BaseHostAdapter(abc.ABC):
    @abc.abstractmethod
    def detect_capabilities(self) -> HostCapabilities:
        """
        探测当前 Host 的能力。必须是纯内存、零副作用操作。
        不确定的能力必须返回 CapabilitySupport.UNKNOWN 或 UNSUPPORTED。
        """
        pass

    @abc.abstractmethod
    def dispatch_agent(self, request: AgentRequest) -> AgentHandle:
        """
        发起一个 Agent 任务，返回异步 Handle。
        """
        pass

    @abc.abstractmethod
    def wait_for_result(self, handle: AgentHandle, timeout_seconds: Optional[float] = None) -> AgentResult:
        """
        阻塞等待 Agent 结果。超时抛出 AgentTimeoutError。
        """
        pass

    @abc.abstractmethod
    def cancel_agent(self, handle: AgentHandle) -> bool:
        """
        取消正在运行的 Agent 任务。
        """
        pass

    @abc.abstractmethod
    def request_confirmation(self, req: ConfirmationRequest) -> ConfirmationResult:
        """
        请求用户/Host进行确认。
        """
        pass

class FakeHostAdapter(BaseHostAdapter):
    """
    假装自己是一个 Host 的 Adapter，用于 2A 阶段及后续纯测试流程中。
    所有返回结果必须严格标注 is_real_host=False。
    """
    def __init__(self):
        self._running_agents = {}
        self._capabilities = HostCapabilities(
            is_real_host=False,
            supports_real_subagents=CapabilitySupport.UNSUPPORTED,
            supports_parallelism=CapabilitySupport.UNSUPPORTED,
            supports_isolated_context=CapabilitySupport.UNSUPPORTED,
            supports_worktree=CapabilitySupport.UNSUPPORTED,
            supports_permission_approval=CapabilitySupport.UNSUPPORTED,
            supports_mcp=CapabilitySupport.UNSUPPORTED,
            supports_interactive_confirmation=CapabilitySupport.UNSUPPORTED,
            supports_usage_telemetry=CapabilitySupport.UNSUPPORTED,
            max_concurrent_agents=0
        )

    def detect_capabilities(self) -> HostCapabilities:
        # HostCapabilities is frozen, safe to return directly
        return self._capabilities

    def _validate_handle(self, handle: AgentHandle):
        if handle.is_real_host is not False:
            raise ValueError("Handle claims to be a real host, which is invalid for FakeHostAdapter.")
        if handle.host_id != "fake_host":
            raise ValueError(f"Invalid host_id: {handle.host_id}")
        if handle.session_id not in self._running_agents:
            raise ValueError(f"Session {handle.session_id} not found or forged.")

    def dispatch_agent(self, request: AgentRequest) -> AgentHandle:
        # Internal fake session namespace
        session_id = f"fake-session:{uuid.uuid4()}"
        handle = AgentHandle(
            session_id=session_id,
            host_id="fake_host",
            status="running",
            is_real_host=False
        )
        self._running_agents[session_id] = {
            "request": request,
            "start_time": time.time(),
            "status": "running",
            # We simulate a 0.05s latency for fake execution to allow timeout testing
            "simulated_latency": 0.05
        }
        return handle

    def wait_for_result(self, handle: AgentHandle, timeout_seconds: Optional[float] = None) -> AgentResult:
        self._validate_handle(handle)
        session = self._running_agents[handle.session_id]

        if session["status"] == "cancelled":
            raise AgentCancelledError(f"Session {handle.session_id} was cancelled.")

        elapsed = time.time() - session["start_time"]
        latency = session["simulated_latency"]

        # Real timeout semantics
        if timeout_seconds is not None:
            if timeout_seconds < latency:
                # Need to simulate waiting up to the timeout
                time.sleep(max(0, timeout_seconds - elapsed))
                raise AgentTimeoutError(f"Session {handle.session_id} timed out.")

        # Simulate execution finishing
        remaining = latency - elapsed
        if remaining > 0:
            time.sleep(remaining)

        req = session["request"]

        # Test specific behaviour: if prompt says "PARTIAL", simulate partial result
        if "PARTIAL" in req.prompt:
            return AgentResult(
                session_id=handle.session_id,
                status=AgentStatus.PARTIAL,
                output="Partial result simulated.",
                partial_results=["part1", "part2"],
                is_real_host=False
            )

        session["status"] = "completed"
        return AgentResult(
            session_id=handle.session_id,
            status=AgentStatus.SUCCESS,
            output=f"Simulated FakeHost execution for role {req.role}.",
            is_real_host=False
        )

    def cancel_agent(self, handle: AgentHandle) -> bool:
        self._validate_handle(handle)
        session = self._running_agents[handle.session_id]
        if session["status"] == "running":
            session["status"] = "cancelled"
            return True
        return False

    def request_confirmation(self, req: ConfirmationRequest) -> ConfirmationResult:
        # Capability is UNSUPPORTED, so we must raise NotSupportedError
        raise AgentNotSupportedError("FakeHostAdapter does not support interactive confirmation.")
