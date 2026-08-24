import abc
import time
import uuid
from typing import Optional

from .agent_schema import (
    HostCapabilities, AgentRequest, AgentHandle, AgentResult,
    ConfirmationRequest, ConfirmationResult, AgentStatus,
    AgentCancelledError, AgentTimeoutError, CapabilitySupport
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
    def wait_for_result(self, handle: AgentHandle, timeout_seconds: Optional[int] = None) -> AgentResult:
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
            supports_mcp=CapabilitySupport.UNSUPPORTED,
            supports_worktree=CapabilitySupport.UNSUPPORTED,
            supports_interactive_confirmation=CapabilitySupport.UNSUPPORTED,
            max_concurrent_agents=1
        )

    def detect_capabilities(self) -> HostCapabilities:
        return self._capabilities

    def dispatch_agent(self, request: AgentRequest) -> AgentHandle:
        handle = AgentHandle(
            session_id=request.session_id or str(uuid.uuid4()),
            host_id="fake_host",
            status="running",
            is_real_host=False
        )
        self._running_agents[handle.session_id] = {
            "request": request,
            "start_time": time.time(),
            "status": "running"
        }
        return handle

    def wait_for_result(self, handle: AgentHandle, timeout_seconds: Optional[int] = None) -> AgentResult:
        session = self._running_agents.get(handle.session_id)
        if not session:
            return AgentResult(
                session_id=handle.session_id,
                status=AgentStatus.UNKNOWN,
                output="",
                error_message="Session not found",
                is_real_host=False
            )

        if session["status"] == "cancelled":
            raise AgentCancelledError(f"Session {handle.session_id} was cancelled.")

        # Simulate execution
        req = session["request"]
        
        # Test specific behaviour: if prompt says "TIMEOUT", simulate timeout
        if "TIMEOUT" in req.prompt:
            raise AgentTimeoutError(f"Session {handle.session_id} timed out.")
        
        # Test specific behaviour: if prompt says "PARTIAL", simulate partial result
        if "PARTIAL" in req.prompt:
            return AgentResult(
                session_id=handle.session_id,
                status=AgentStatus.PARTIAL,
                output="Partial result simulated.",
                partial_results=["part1", "part2"],
                is_real_host=False
            )

        # Normal success
        session["status"] = "completed"
        return AgentResult(
            session_id=handle.session_id,
            status=AgentStatus.SUCCESS,
            output=f"Simulated FakeHost execution for role {req.role}.",
            is_real_host=False
        )

    def cancel_agent(self, handle: AgentHandle) -> bool:
        session = self._running_agents.get(handle.session_id)
        if session and session["status"] == "running":
            session["status"] = "cancelled"
            return True
        return False

    def request_confirmation(self, req: ConfirmationRequest) -> ConfirmationResult:
        # In FakeHost, we always auto-confirm to avoid blocking, but tag it as fake.
        return ConfirmationResult(
            request_id=req.request_id,
            selected_option=req.options[0] if req.options else "confirm",
            is_confirmed=True,
            is_real_host=False
        )
