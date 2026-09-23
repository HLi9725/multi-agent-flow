# -*- coding: utf-8 -*-
"""
tests/fixtures/cursor_sdk/fake_cursor_sdk.py
In-memory mock implementation of official Cursor Python SDK.
Used for deterministic, zero-network unit testing and Runner integration testing.
"""
from dataclasses import dataclass, field
import threading
import time
from typing import Any, Callable, Dict, List, Optional
import uuid


class CursorAgentError(Exception):
    """Base class for all Cursor SDK agent errors."""
    def __init__(
        self,
        message: str = "",
        *,
        is_retryable: bool = False,
        retry_after: Optional[float] = None,
        code: Optional[str] = None,
        status_code: Optional[int] = None,
    ):
        super().__init__(message)
        self.message = message
        self.is_retryable = is_retryable
        self.retry_after = retry_after
        self.code = code
        self.status_code = status_code


class AuthenticationError(CursorAgentError):
    def __init__(self, message: str = "Invalid API key or unauthorized", **kwargs: Any):
        super().__init__(message, is_retryable=False, code=kwargs.get("code", "authentication_error"), status_code=401)


class PermissionDeniedError(CursorAgentError):
    def __init__(self, message: str = "Permission denied", **kwargs: Any):
        super().__init__(message, is_retryable=False, code=kwargs.get("code", "permission_denied"), status_code=403)


class RateLimitError(CursorAgentError):
    def __init__(self, message: str = "Rate limit exceeded", retry_after: Optional[float] = 2.0, **kwargs: Any):
        super().__init__(message, is_retryable=True, retry_after=retry_after, code=kwargs.get("code", "rate_limit_error"), status_code=429)


class ConfigurationError(CursorAgentError):
    def __init__(self, message: str = "Invalid agent configuration", **kwargs: Any):
        super().__init__(message, is_retryable=False, code=kwargs.get("code", "configuration_error"), status_code=400)


class AgentBusyError(CursorAgentError):
    def __init__(self, message: str = "Agent is currently busy processing another run", **kwargs: Any):
        super().__init__(message, is_retryable=True, retry_after=1.0, code=kwargs.get("code", "agent_busy"), status_code=409)


class BadRequestError(CursorAgentError):
    def __init__(self, message: str = "Bad request", **kwargs: Any):
        super().__init__(message, is_retryable=False, code=kwargs.get("code", "bad_request"), status_code=400)


@dataclass
class LocalAgentOptions:
    cwd: str
    setting_sources: Optional[List[str]] = None


@dataclass
class AgentOptions:
    model: Optional[str] = None


@dataclass
class SendOptions:
    tools: Optional[List[Any]] = None
    disallowed_tools: Optional[List[str]] = None


@dataclass
class RunResult:
    status: str  # finished, error, cancelled, expired
    result: Optional[str] = None
    duration_ms: Optional[int] = None
    usage: Optional[Dict[str, Any]] = None


class FakeCursorSdkState:
    """Thread-safe mutable state controlling mock SDK behaviors in tests."""
    _lock = threading.Lock()
    _next_response: Optional[str] = None
    _next_status: str = "finished"
    _next_error: Optional[Exception] = None
    _next_create_error: Optional[Exception] = None
    _on_send_hook: Optional[Callable[[str, "Agent"], None]] = None
    _runs: Dict[str, "Run"] = {}
    _agents: Dict[str, "Agent"] = {}

    @classmethod
    def reset(cls) -> None:
        with cls._lock:
            cls._next_response = None
            cls._next_status = "finished"
            cls._next_error = None
            cls._next_create_error = None
            cls._on_send_hook = None
            cls._runs.clear()
            cls._agents.clear()

    @classmethod
    def set_next_response(cls, text: str, status: str = "finished") -> None:
        with cls._lock:
            cls._next_response = text
            cls._next_status = status
            cls._next_error = None

    @classmethod
    def set_next_error(cls, exc: Exception) -> None:
        with cls._lock:
            cls._next_error = exc

    @classmethod
    def set_next_create_error(cls, exc: Exception) -> None:
        with cls._lock:
            cls._next_create_error = exc

    @classmethod
    def set_on_send_hook(cls, hook: Optional[Callable[[str, "Agent"], None]]) -> None:
        with cls._lock:
            cls._on_send_hook = hook


class Run:
    def __init__(
        self,
        run_id: str,
        agent_id: str,
        status: str = "finished",
        result: Optional[str] = None,
        error: Optional[CursorAgentError] = None,
        delay_seconds: float = 0.0,
    ):
        self.id = run_id
        self.agent_id = agent_id
        self.status = status
        self.result = result
        self.error = error
        self.delay_seconds = delay_seconds
        self.is_cancelled = False
        FakeCursorSdkState._runs[run_id] = self

    def wait(self, *args: Any, **kwargs: Any) -> RunResult:
        if args or kwargs:
            raise TypeError(
                f"Run.wait() takes 1 positional argument but {1 + len(args) + len(kwargs)} were given "
                "(official cursor-sdk Run.wait does not accept timeout parameter; timeout must be enforced externally)"
            )
        if self.delay_seconds > 0:
            step = 0.05
            elapsed = 0.0
            while elapsed < self.delay_seconds:
                if self.is_cancelled:
                    return RunResult(status="cancelled", result=None)
                time.sleep(step)
                elapsed += step
        if self.is_cancelled:
            return RunResult(status="cancelled", result=None)
        with FakeCursorSdkState._lock:
            if FakeCursorSdkState._next_error is not None:
                err = FakeCursorSdkState._next_error
                FakeCursorSdkState._next_error = None
                raise err
            if FakeCursorSdkState._next_response is not None:
                resp = FakeCursorSdkState._next_response
                FakeCursorSdkState._next_response = None
                status = FakeCursorSdkState._next_status
                return RunResult(
                    status=status,
                    result=resp,
                    duration_ms=120,
                    usage={"prompt_tokens": 10, "completion_tokens": 20},
                )
        if self.error is not None:
            raise self.error
        return RunResult(
            status=self.status,
            result=self.result,
            duration_ms=120,
            usage={"prompt_tokens": 10, "completion_tokens": 20},
        )

    def cancel(self) -> None:
        self.is_cancelled = True
        self.status = "cancelled"


class CursorClient:
    def __init__(self, api_key: Optional[str] = None):
        self.api_key = api_key

    @classmethod
    def launch_bridge(cls, api_key: Optional[str] = None, **kwargs: Any) -> "CursorClient":
        return cls(api_key=api_key)


class Agent:
    def __init__(
        self,
        agent_id: str,
        model: str,
        local: Optional[LocalAgentOptions] = None,
        api_key: Optional[str] = None,
    ):
        self.agent_id = agent_id
        self.model = model
        self.local = local
        self.api_key = api_key
        self.is_closed = False
        self.tools = None
        self.disallowed_tools = None
        FakeCursorSdkState._agents[agent_id] = self

    def close(self) -> None:
        self.is_closed = True

    def __enter__(self) -> "Agent":
        return self

    def __exit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        self.close()

    @classmethod
    def create(
        cls,
        model: str,
        api_key: Optional[str] = None,
        local: Optional[LocalAgentOptions] = None,
        **kwargs: Any,
    ) -> "Agent":
        if "client" in kwargs and kwargs["client"] is not None:
            raise TypeError(
                "Agent.create() got an unexpected keyword argument 'client'. "
                "Pass api_key directly to Agent.create(model=..., api_key=..., local=...)."
            )
        with FakeCursorSdkState._lock:
            if FakeCursorSdkState._next_create_error is not None:
                err = FakeCursorSdkState._next_create_error
                FakeCursorSdkState._next_create_error = None
                raise err
        if not model or not str(model).strip():
            raise ConfigurationError("Agent model must be specified.")
        agent_id = f"ag_{uuid.uuid4().hex[:12]}"
        return cls(agent_id=agent_id, model=model, local=local, api_key=api_key)

    @classmethod
    def resume(
        cls,
        agent_id: str,
        api_key: Optional[str] = None,
        **kwargs: Any,
    ) -> "Agent":
        if "client" in kwargs and kwargs["client"] is not None:
            raise TypeError(
                "Agent.resume() got an unexpected keyword argument 'client'. "
                "Pass api_key directly to Agent.resume(agent_id=..., api_key=...)."
            )
        with FakeCursorSdkState._lock:
            if agent_id in FakeCursorSdkState._agents:
                agent = FakeCursorSdkState._agents[agent_id]
                agent.is_closed = False
                return agent
        return cls(agent_id=agent_id, model="resumed-model", api_key=api_key)

    def send(self, prompt: str, **kwargs: Any) -> Run:
        self.tools = kwargs.get("tools")
        self.disallowed_tools = kwargs.get("disallowed_tools")
        with FakeCursorSdkState._lock:
            hook = FakeCursorSdkState._on_send_hook
            resp = FakeCursorSdkState._next_response
            status = FakeCursorSdkState._next_status
            err = FakeCursorSdkState._next_error
            # Clear one-shot errors
            FakeCursorSdkState._next_error = None

        hook_result = None
        if hook:
            hook_result = hook(prompt, self)

        run_id = f"run_{uuid.uuid4().hex[:12]}"
        if err is not None:
            if isinstance(err, CursorAgentError):
                return Run(run_id=run_id, agent_id=self.agent_id, status="error", error=err)
            raise err

        if hook_result is not None:
            text_result = hook_result
        elif resp is not None:
            text_result = resp
        else:
            text_result = f"Mock execution complete for {self.agent_id}"
        return Run(run_id=run_id, agent_id=self.agent_id, status=status, result=text_result)

    @classmethod
    def get_run(cls, run_id: str, client: Optional[CursorClient] = None) -> Run:
        with FakeCursorSdkState._lock:
            if run_id in FakeCursorSdkState._runs:
                return FakeCursorSdkState._runs[run_id]
        return Run(run_id=run_id, agent_id="unknown", status="finished", result="resumed run")

    @classmethod
    def cancel_run(cls, run_id: str, client: Optional[CursorClient] = None) -> None:
        with FakeCursorSdkState._lock:
            if run_id in FakeCursorSdkState._runs:
                FakeCursorSdkState._runs[run_id].cancel()
