import abc
import math
import secrets
import time
import uuid
from typing import Optional

from .agent_schema import (
    AgentCancelledError,
    AgentHandle,
    AgentInvalidHandleError,
    AgentNotSupportedError,
    AgentRequest,
    AgentResult,
    AgentStatus,
    AgentTimeoutError,
    CapabilitySupport,
    ConfirmationRequest,
    ConfirmationResult,
    HostCapabilities,
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
        self._instance_id = f"fake-adapter:{uuid.uuid4()}"
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
        # HostCapabilities recursively freezes its nested metadata.
        return self._capabilities

    def _validate_handle(self, handle: AgentHandle) -> None:
        if not isinstance(handle, AgentHandle):
            raise AgentInvalidHandleError("Handle must be an AgentHandle instance.")
        if handle.is_real_host is not False:
            raise AgentInvalidHandleError(
                "Handle claims to be a real host, which is invalid for FakeHostAdapter."
            )
        if handle.host_id != "fake_host":
            raise AgentInvalidHandleError(f"Invalid host_id: {handle.host_id}")
        if not all(
            isinstance(value, str)
            for value in (
                handle.session_id,
                handle.adapter_instance_id,
                handle.invocation_token,
            )
        ):
            raise AgentInvalidHandleError("Handle identity fields must be strings.")
        if not handle.session_id.startswith("fake-session:"):
            raise AgentInvalidHandleError("Fake session ID has an invalid namespace.")
        if not secrets.compare_digest(handle.adapter_instance_id, self._instance_id):
            raise AgentInvalidHandleError("Handle belongs to another adapter instance.")
        session = self._running_agents.get(handle.session_id)
        if session is None:
            raise AgentInvalidHandleError(
                f"Session {handle.session_id} not found or forged."
            )
        expected_handle = session["handle"]
        if not secrets.compare_digest(
            handle.invocation_token, expected_handle.invocation_token
        ):
            raise AgentInvalidHandleError("Handle invocation token is invalid.")

    def dispatch_agent(self, request: AgentRequest) -> AgentHandle:
        # Internal fake session namespace
        session_id = f"fake-session:{uuid.uuid4()}"
        handle = AgentHandle(
            session_id=session_id,
            host_id="fake_host",
            status="running",
            is_real_host=False,
            adapter_instance_id=self._instance_id,
            invocation_token=secrets.token_urlsafe(32),
        )
        self._running_agents[session_id] = {
            "handle": handle,
            "request": request,
            "start_time": time.monotonic(),
            "status": "running",
            "result": None,
            # We simulate a 0.05s latency for fake execution to allow timeout testing
            "simulated_latency": 0.05
        }
        return handle

    def wait_for_result(
        self,
        handle: AgentHandle,
        timeout_seconds: Optional[float] = None,
    ) -> AgentResult:
        self._validate_handle(handle)
        session = self._running_agents[handle.session_id]

        if session["status"] == "cancelled":
            raise AgentCancelledError(f"Session {handle.session_id} was cancelled.")
        if session["result"] is not None:
            return session["result"]

        elapsed = time.monotonic() - session["start_time"]
        latency = session["simulated_latency"]
        remaining = max(0.0, latency - elapsed)
        req = session["request"]

        effective_timeout = (
            req.timeout_seconds if timeout_seconds is None else timeout_seconds
        )
        if (
            isinstance(effective_timeout, bool)
            or not isinstance(effective_timeout, (int, float))
            or not math.isfinite(effective_timeout)
            or effective_timeout < 0
        ):
            raise ValueError("timeout_seconds must be a finite non-negative number")
        effective_timeout = float(effective_timeout)
        if remaining > effective_timeout:
            time.sleep(effective_timeout)
            raise AgentTimeoutError(f"Session {handle.session_id} timed out.")

        # Simulate execution finishing
        if remaining > 0:
            time.sleep(remaining)

        # Test specific behaviour: if prompt says "PARTIAL", simulate partial result
        if "PARTIAL" in req.prompt:
            result = AgentResult(
                session_id=handle.session_id,
                status=AgentStatus.PARTIAL,
                output="Partial result simulated.",
                partial_results=("part1", "part2"),
                is_real_host=False
            )
            session["status"] = "partial"
            session["result"] = result
            return result

        session["status"] = "completed"
        result = AgentResult(
            session_id=handle.session_id,
            status=AgentStatus.SUCCESS,
            output=f"Simulated FakeHost execution for role {req.role}.",
            is_real_host=False
        )
        session["result"] = result
        return result

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
