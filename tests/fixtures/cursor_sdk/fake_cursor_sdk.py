# -*- coding: utf-8 -*-
"""
tests/fixtures/cursor_sdk/fake_cursor_sdk.py
In-memory mock implementation of official Cursor Python SDK.
Used for deterministic, zero-network unit testing and Runner integration testing.
"""
from dataclasses import dataclass, field
import threading
import time
from typing import Any, Callable, Dict, List, Optional, Union
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


VALID_TOOLS = {"read", "grep", "shell", "edit", "task"}


@dataclass
class AgentDefinition:
    description: str
    prompt: str
    model: str = "inherit"
    tools: Optional[List[str]] = None
    disallowed_tools: Optional[List[str]] = None

    def __post_init__(self) -> None:
        if self.tools is not None:
            for t in self.tools:
                if t not in VALID_TOOLS:
                    raise BadRequestError(
                        f"Unknown tool '{t}'. Valid tools are: {sorted(list(VALID_TOOLS))}"
                    )
        if self.disallowed_tools is not None:
            for t in self.disallowed_tools:
                if t not in VALID_TOOLS:
                    raise BadRequestError(
                        f"Unknown tool '{t}'. Valid tools are: {sorted(list(VALID_TOOLS))}"
                    )


@dataclass
class LocalAgentOptions:
    cwd: str
    setting_sources: Optional[List[str]] = None


@dataclass
class AgentOptions:
    model: Optional[str] = None
    api_key: Optional[str] = None
    local: Optional[LocalAgentOptions] = None
    tools: Optional[List[str]] = None
    disallowed_tools: Optional[List[str]] = None
    agents: Optional[Dict[str, AgentDefinition]] = None

    def __post_init__(self) -> None:
        if self.tools is not None:
            for t in self.tools:
                if t not in VALID_TOOLS:
                    raise BadRequestError(
                        f"Unknown tool '{t}'. Valid tools are: {sorted(list(VALID_TOOLS))}"
                    )
        if self.disallowed_tools is not None:
            for t in self.disallowed_tools:
                if t not in VALID_TOOLS:
                    raise BadRequestError(
                        f"Unknown tool '{t}'. Valid tools are: {sorted(list(VALID_TOOLS))}"
                    )


@dataclass
class SendOptions:
    pass


class SDKMessage:
    """Represents a message or event in official cursor-sdk run.messages() stream."""
    def __init__(self, msg_type: str, **kwargs: Any):
        self.type = msg_type
        for k, v in kwargs.items():
            setattr(self, k, v)

    def __repr__(self) -> str:
        attrs = {k: v for k, v in self.__dict__.items() if k != "type"}
        return f"SDKMessage(type={self.type!r}, {attrs})"


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
    _next_refuse_cancel: bool = False
    _next_messages: Optional[List[Any]] = None
    _next_tool_calls: Optional[List[Dict[str, Any]]] = None
    _executed_subagents: List[Dict[str, Any]] = []
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
            cls._next_refuse_cancel = False
            cls._next_messages = None
            cls._next_tool_calls = None
            cls._executed_subagents.clear()
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
    def set_next_refuse_cancel(cls, refuse: bool = True) -> None:
        with cls._lock:
            cls._next_refuse_cancel = refuse

    @classmethod
    def set_next_messages(cls, messages: Optional[List[Any]]) -> None:
        with cls._lock:
            cls._next_messages = messages

    @classmethod
    def set_next_tool_calls(cls, tool_calls: Optional[List[Dict[str, Any]]]) -> None:
        with cls._lock:
            if tool_calls is None:
                cls._next_messages = None
            else:
                msgs = []
                for tc in tool_calls:
                    tool_name = tc.get("tool") or tc.get("name") or ""
                    cid = f"call_{uuid.uuid4().hex[:8]}"
                    if str(tool_name).lower() == "task":
                        sub = tc.get("subagent") or tc.get("subagent_name") or ""
                        msgs.append(SDKMessage("tool_call", name="task", status="started", args=tc, id=cid))
                        msgs.append(SDKMessage("task", subagent=sub, status="completed", text=f"Subagent {sub} executed"))
                        msgs.append(SDKMessage("tool_call", name="task", status="completed", id=cid))
                    else:
                        msgs.append(SDKMessage("tool_call", name=tool_name, status="started", args=tc.get("args", {}), id=cid))
                        msgs.append(SDKMessage("tool_call", name=tool_name, status="completed", id=cid))
                cls._next_messages = msgs

    @classmethod
    def get_executed_subagents(cls) -> List[Dict[str, Any]]:
        with cls._lock:
            return list(cls._executed_subagents)

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
        refuse_cancel: bool = False,
        messages: Optional[List[Any]] = None,
    ):
        self.id = run_id
        self.agent_id = agent_id
        self.status = status
        self.result = result
        self.error = error
        self.delay_seconds = delay_seconds
        self.refuse_cancel = refuse_cancel
        self._messages = list(messages) if messages is not None else []
        self.is_cancelled = False
        FakeCursorSdkState._runs[run_id] = self

    def messages(self):
        for m in self._messages:
            if self.is_cancelled:
                break
            yield m

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
        if not self.refuse_cancel:
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
        tools: Optional[List[str]] = None,
        disallowed_tools: Optional[List[str]] = None,
        agents: Optional[Dict[str, AgentDefinition]] = None,
    ):
        self.agent_id = agent_id
        self.model = model
        self.local = local
        self.api_key = api_key
        self.is_closed = False
        self.tools = tools
        self.disallowed_tools = disallowed_tools
        self.agents = agents or {}
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
        options: Optional[Union[AgentOptions, str]] = None,
        model: Optional[str] = None,
        api_key: Optional[str] = None,
        local: Optional[LocalAgentOptions] = None,
        tools: Optional[List[str]] = None,
        disallowed_tools: Optional[List[str]] = None,
        agents: Optional[Dict[str, AgentDefinition]] = None,
        **kwargs: Any,
    ) -> "Agent":
        if kwargs:
            raise TypeError(
                f"Agent.create() got unexpected keyword argument(s): {list(kwargs.keys())}. "
                "Pass options directly to Agent.create(options=AgentOptions(...))."
            )
        with FakeCursorSdkState._lock:
            if FakeCursorSdkState._next_create_error is not None:
                err = FakeCursorSdkState._next_create_error
                FakeCursorSdkState._next_create_error = None
                raise err

        effective_model = model
        effective_api_key = api_key
        effective_local = local
        effective_tools = tools
        effective_disallowed_tools = disallowed_tools
        effective_agents = agents

        if isinstance(options, AgentOptions):
            effective_model = options.model or effective_model
            effective_api_key = options.api_key or effective_api_key
            effective_local = options.local or effective_local
            effective_tools = options.tools if options.tools is not None else effective_tools
            effective_disallowed_tools = options.disallowed_tools if options.disallowed_tools is not None else effective_disallowed_tools
            effective_agents = options.agents if options.agents is not None else effective_agents
        elif isinstance(options, str):
            effective_model = options

        if not effective_model or not str(effective_model).strip():
            raise ConfigurationError("Agent model must be specified.")

        if effective_tools is not None:
            for t in effective_tools:
                if t not in VALID_TOOLS:
                    raise BadRequestError(
                        f"Unknown tool '{t}'. Valid tools are: {sorted(list(VALID_TOOLS))}"
                    )
        if effective_disallowed_tools is not None:
            for t in effective_disallowed_tools:
                if t not in VALID_TOOLS:
                    raise BadRequestError(
                        f"Unknown tool '{t}'. Valid tools are: {sorted(list(VALID_TOOLS))}"
                    )

        agent_id = f"ag_{uuid.uuid4().hex[:12]}"
        return cls(
            agent_id=agent_id,
            model=effective_model,
            local=effective_local,
            api_key=effective_api_key,
            tools=effective_tools,
            disallowed_tools=effective_disallowed_tools,
            agents=effective_agents,
        )

    @classmethod
    def resume(
        cls,
        agent_id: str,
        options: Optional[AgentOptions] = None,
        **kwargs: Any,
    ) -> "Agent":
        if kwargs:
            raise TypeError(
                f"Agent.resume() got unexpected keyword argument(s): {list(kwargs.keys())}. "
                "Official signature is Agent.resume(agent_id, options=AgentOptions(api_key=...))."
            )
        if options is not None and not isinstance(options, AgentOptions):
            raise TypeError(f"options must be an instance of AgentOptions, got {type(options).__name__}")
        api_key = options.api_key if options else None
        agents = options.agents if options else None
        tools = options.tools if options else None
        disallowed_tools = options.disallowed_tools if options else None
        local = options.local if options else None
        with FakeCursorSdkState._lock:
            if agent_id in FakeCursorSdkState._agents:
                agent = FakeCursorSdkState._agents[agent_id]
                agent.is_closed = False
                if api_key:
                    agent.api_key = api_key
                if agents is not None:
                    agent.agents = agents
                if tools is not None:
                    agent.tools = tools
                if disallowed_tools is not None:
                    agent.disallowed_tools = disallowed_tools
                if local is not None:
                    agent.local = local
                return agent
        return cls(
            agent_id=agent_id,
            model="resumed-model",
            api_key=api_key,
            tools=tools,
            disallowed_tools=disallowed_tools,
            agents=agents,
            local=local,
        )

    def send(self, message: str, options: Optional[SendOptions] = None, **kwargs: Any) -> Run:
        if kwargs:
            raise TypeError(
                f"Agent.send() got unexpected keyword argument(s): {list(kwargs.keys())}. "
                "Official signature is send(message, options=SendOptions(...))."
            )
        if options is not None and not isinstance(options, SendOptions):
            raise TypeError(f"options must be an instance of SendOptions, got {type(options).__name__}")
        with FakeCursorSdkState._lock:
            hook = FakeCursorSdkState._on_send_hook
            resp = FakeCursorSdkState._next_response
            status = FakeCursorSdkState._next_status
            err = FakeCursorSdkState._next_error
            refuse_cancel = FakeCursorSdkState._next_refuse_cancel
            custom_messages = FakeCursorSdkState._next_messages
            # Clear one-shot errors
            FakeCursorSdkState._next_error = None
            FakeCursorSdkState._next_refuse_cancel = False
            FakeCursorSdkState._next_messages = None
            FakeCursorSdkState._next_tool_calls = None

        hook_result = None
        if hook:
            hook_result = hook(message, self)

        run_id = f"run_{uuid.uuid4().hex[:12]}"
        if hook_result is not None:
            text_result = hook_result
        elif resp is not None:
            text_result = resp
        else:
            text_result = f"Mock execution complete for {self.agent_id}"

        if custom_messages is not None:
            effective_messages = list(custom_messages)
            if not any(getattr(m, "type", None) == "assistant" for m in effective_messages):
                effective_messages.append(SDKMessage("assistant", text=text_result))
        elif self.agents:
            subagent_name = None
            for name in self.agents.keys():
                if f"'{name}'" in message or f'"{name}"' in message or name in message:
                    subagent_name = name
                    break
            if not subagent_name:
                subagent_name = list(self.agents.keys())[0]

            subagent_def = self.agents.get(subagent_name)
            with FakeCursorSdkState._lock:
                FakeCursorSdkState._executed_subagents.append({
                    "subagent_name": subagent_name,
                    "agent_id": self.agent_id,
                    "prompt": message,
                    "subagent_def": subagent_def,
                })

            cid = f"call_{uuid.uuid4().hex[:8]}"
            effective_messages = [
                SDKMessage("tool_call", name="task", status="started", args={"subagent": subagent_name, "prompt": message}, id=cid),
                SDKMessage("task", subagent=subagent_name, status="completed", text=f"Subagent {subagent_name} executed"),
                SDKMessage("tool_call", name="task", status="completed", id=cid),
                SDKMessage("assistant", text=text_result),
            ]
        else:
            effective_messages = [
                SDKMessage("assistant", text=text_result),
            ]

        if err is not None:
            if isinstance(err, CursorAgentError):
                return Run(
                    run_id=run_id,
                    agent_id=self.agent_id,
                    status="error",
                    error=err,
                    refuse_cancel=refuse_cancel,
                    messages=effective_messages,
                )
            raise err

        return Run(
            run_id=run_id,
            agent_id=self.agent_id,
            status=status,
            result=text_result,
            refuse_cancel=refuse_cancel,
            messages=effective_messages,
        )

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
