# -*- coding: utf-8 -*-
"""
scripts/_lib/hosts/cursor_sdk_adapter.py
Cursor Python SDK Reference Host Adapter.
Enforces local runtime constraints, isolated sessions, deterministic identity binding,
and fail-closed workspace immutability for multi-agent flow orchestration.
"""
from collections.abc import Mapping
import logging
import os
import secrets
import threading
import time
from types import MappingProxyType
from typing import Any, Dict, List, Optional, Sequence, Set, Tuple
import uuid
import yaml

logger = logging.getLogger("cursor_sdk_adapter")

ROLE_AGENT_MAP: Dict[str, str] = {
    "DEV": "flow-dev",
    "BUILDER": "flow-dev",
    "REVIEWER": "flow-reviewer",
    "QA": "flow-qa",
    "ARCHITECT": "flow-architect",
    "PM": "flow-pm",
    "DOCS": "flow-docs",
    "DEVOPS": "flow-devops",
    "FRONTEND": "flow-frontend",
}

RUNNER_MANAGED_AGENT_MAP: Dict[str, str] = {
    "BUILDER": "flow-runner-builder",
    "REVIEWER": "flow-runner-reviewer",
    "QA": "flow-runner-qa",
}

VALID_SDK_TOOLS: Set[str] = {"read", "grep", "glob", "shell", "edit", "task", "mcp"}

CURSOR_SESSION_TO_SDK_TOOL_MAP: Dict[str, str] = {
    "read": "read",
    "view_file": "read",
    "grep": "grep",
    "grep_search": "grep",
    "glob": "glob",
    "find_by_name": "glob",
    "shell": "shell",
    "bash": "shell",
    "run_command": "shell",
    "edit": "edit",
    "write": "edit",
    "write_to_file": "edit",
    "replace_file_content": "edit",
    "task": "task",
}

from ..core.agent_schema import (
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
from ..core.host_adapter import BaseHostAdapter
from ..core.adapter_manifest import (
    AdapterManifest,
    AuthBoundaryType,
    BillingBoundaryType,
    HostSurface,
    PlatformVerification,
    VerificationLevel,
)


def resolve_subagent_tool_policy(
    agent_id_or_name: str,
    raw_tools: Optional[List[str]] = None,
    enable_write_tools: Optional[bool] = None,
) -> Tuple[Optional[List[str]], Optional[List[str]]]:
    """
    Map the tool list declared in a subagent markdown file to SDK tool names.
    The file list is the only allowlist. Role names do not add or remove tools.
    """
    del agent_id_or_name  # The markdown tools list is authoritative; the name is not a second policy.

    mapped_tools: Set[str] = set()
    if isinstance(raw_tools, list):
        for t in raw_tools:
            normalized = str(t).strip().lower()
            sdk_tool = CURSOR_SESSION_TO_SDK_TOOL_MAP.get(normalized)
            if sdk_tool and sdk_tool in VALID_SDK_TOOLS:
                mapped_tools.add(sdk_tool)
    else:
        mapped_tools = set(VALID_SDK_TOOLS)

    if enable_write_tools is False:
        mapped_tools -= {"edit", "shell"}
    mapped_tools.discard("task")

    disallowed = sorted(VALID_SDK_TOOLS - mapped_tools)
    return sorted(mapped_tools), disallowed


class CursorSdkAdapter(BaseHostAdapter):
    """
    Host Adapter for Cursor Python SDK (cursor-sdk).
    Supports Phase 3 Local Runtime execution with independent Builder/Reviewer/QA agents.
    """

    def __init__(
        self,
        adapter_id: str = "cursor_sdk",
        is_real_host: bool = True,
        default_model: Optional[str] = None,
        default_timeout_seconds: float = 300.0,
        api_key_env_var: str = "CURSOR_API_KEY",
        sdk_module: Optional[Any] = None,
    ):
        self.adapter_id = adapter_id
        self._is_real_host = bool(is_real_host)
        self.default_model = default_model
        self._default_timeout_seconds = float(default_timeout_seconds)
        self.api_key_env_var = api_key_env_var
        self._sdk_module = sdk_module
        self._instance_id = str(uuid.uuid4())
        self._lock = threading.Lock()
        self._running_sessions: Dict[str, Dict[str, Any]] = {}
        self._session_history: Dict[str, Dict[str, Any]] = {}

    def get_conformance_probe_spec(self) -> Tuple[Tuple[Any, ...], Dict[str, Any]]:
        """Return spawn-safe constructor inputs for isolated capability probing."""
        return (), {"adapter_id": self.adapter_id, "is_real_host": self._is_real_host}

    def detect_capabilities(self) -> HostCapabilities:
        """
        Probe adapter capabilities without observable side effects.
        Zero disk writes, zero subprocesses, and zero secret access during probe.
        """
        return HostCapabilities(
            is_real_host=self._is_real_host,
            supports_real_subagents=CapabilitySupport.SUPPORTED,
            supports_parallelism=CapabilitySupport.SUPPORTED,
            supports_isolated_context=CapabilitySupport.SUPPORTED,
            supports_worktree=CapabilitySupport.SUPPORTED,
            supports_permission_approval=CapabilitySupport.UNKNOWN,
            supports_mcp=CapabilitySupport.SUPPORTED,
            supports_interactive_confirmation=CapabilitySupport.UNSUPPORTED,
            supports_usage_telemetry=CapabilitySupport.UNKNOWN,
            max_concurrent_agents=8,
            extra={
                "runtime": "local",
                "default_model": self.default_model,
            },
        )

    def _get_sdk(self) -> Any:
        """Lazily resolve Cursor SDK module with zero side effects on unrequested paths."""
        if self._sdk_module is not None:
            return self._sdk_module

        if not self._is_real_host:
            try:
                from tests.fixtures.cursor_sdk import fake_cursor_sdk
                return fake_cursor_sdk
            except Exception:
                pass

        try:
            import cursor_sdk  # type: ignore[import-untyped]
            return cursor_sdk
        except (ImportError, ModuleNotFoundError) as exc:
            raise AgentNotSupportedError(
                "Cursor Python SDK is not installed or unavailable. "
                "Install it using 'pip install cursor-sdk' to enable cursor_sdk adapter."
            ) from exc

    def _resolve_model(self, request: AgentRequest) -> str:
        """Extract explicit model ID from request context or adapter configuration."""
        model = None
        if isinstance(request.extra_context, Mapping):
            model = request.extra_context.get("cursor_model") or request.extra_context.get("model")
        if not model and self.default_model:
            model = self.default_model
        if not model or not str(model).strip():
            raise AgentNotSupportedError(
                "Cursor model must be explicitly specified (e.g. via --cursor-model "
                "or extra_context['cursor_model']). Default implicit models are disallowed."
            )
        return str(model).strip()

    def _resolve_target_agent(self, role: str, extra_context: Any) -> str:
        """Resolve role to specialized Cursor subagent ID."""
        role_upper = (role or "").upper().strip()
        is_managed = False
        if isinstance(extra_context, Mapping):
            val = extra_context.get("production_runner_managed")
            if val is True or str(val).lower() in ("true", "1", "yes"):
                is_managed = True

        if is_managed:
            if role_upper not in RUNNER_MANAGED_AGENT_MAP:
                raise AgentNotSupportedError(
                    f"Role '{role}' is not supported under production_runner_managed mode. "
                    f"Only {sorted(list(RUNNER_MANAGED_AGENT_MAP.keys()))} are permitted."
                )
            return RUNNER_MANAGED_AGENT_MAP[role_upper]

        if role_upper not in ROLE_AGENT_MAP:
            raise AgentNotSupportedError(
                f"Role '{role}' is not supported by CursorSdkAdapter. Known roles: {sorted(list(ROLE_AGENT_MAP.keys()))}"
            )
        return ROLE_AGENT_MAP[role_upper]

    def _load_subagent_definition(
        self, workspace_dir: str, agent_id: str
    ) -> Tuple[str, str, str, Optional[List[str]], Optional[List[str]], str]:
        """
        Load subagent name, description, body prompt, tools, and disallowed_tools from .cursor/agents/{agent_id}.md.
        Enforces Fail-Closed verification on file presence and frontmatter integrity,
        and translates session-level tool names to official SDK lowercase tool names.
        """
        repo_root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
        candidate_paths = [
            os.path.join(workspace_dir, ".cursor", "agents", f"{agent_id}.md"),
            os.path.join(repo_root, ".cursor", "agents", f"{agent_id}.md"),
        ]
        try:
            from ... import paths as _paths
            candidate_paths.append(os.path.join(_paths.project_root(), ".cursor", "agents", f"{agent_id}.md"))
            candidate_paths.append(os.path.join(_paths.skill_root(), ".cursor", "agents", f"{agent_id}.md"))
        except Exception:
            pass

        target_file = None
        for p in candidate_paths:
            if p and os.path.isfile(p):
                target_file = p
                break

        if not target_file:
            raise AgentNotSupportedError(
                f"Subagent markdown definition for '{agent_id}' does not exist at .cursor/agents/{agent_id}.md (Fail-Closed)."
            )

        try:
            with open(target_file, "r", encoding="utf-8") as f:
                content = f.read()
        except Exception as exc:
            raise AgentNotSupportedError(f"Failed to read subagent definition '{target_file}': {exc}") from exc

        if not content.startswith("---"):
            raise AgentNotSupportedError(
                f"Subagent definition '{target_file}' is missing YAML frontmatter header (Fail-Closed)."
            )

        parts = content.split("---", 2)
        if len(parts) < 3:
            raise AgentNotSupportedError(
                f"Subagent definition '{target_file}' has incomplete frontmatter structure (Fail-Closed)."
            )

        try:
            fm = yaml.safe_load(parts[1])
        except Exception as exc:
            raise AgentNotSupportedError(f"Subagent definition '{target_file}' has invalid YAML frontmatter: {exc}") from exc

        if not isinstance(fm, dict):
            raise AgentNotSupportedError(f"Subagent frontmatter in '{target_file}' is not a valid dictionary mapping.")

        name = fm.get("name")
        desc = fm.get("description")
        if not name or not str(name).strip():
            raise AgentNotSupportedError(f"Subagent frontmatter in '{target_file}' is missing required 'name' field.")
        if not desc or not str(desc).strip():
            raise AgentNotSupportedError(f"Subagent frontmatter in '{target_file}' is missing required 'description' field.")

        prompt_body = parts[2].strip()
        if not prompt_body:
            raise AgentNotSupportedError(f"Subagent body prompt in '{target_file}' is empty (Fail-Closed).")

        raw_tools = fm.get("tools")
        enable_write_tools = fm.get("enable_write_tools")
        sub_tools, sub_disallowed_tools = resolve_subagent_tool_policy(
            agent_id, raw_tools=raw_tools, enable_write_tools=enable_write_tools
        )

        return str(name).strip(), str(desc).strip(), prompt_body, sub_tools, sub_disallowed_tools, target_file

    def _validate_handle(self, handle: AgentHandle, include_history: bool = True) -> Dict[str, Any]:
        """Validate handle ownership and authenticity using constant-time comparisons."""
        if not isinstance(handle, AgentHandle):
            raise AgentInvalidHandleError("Invalid handle instance")
        if handle.host_id != self.adapter_id:
            raise AgentInvalidHandleError(
                f"Handle host_id '{handle.host_id}' does not match adapter '{self.adapter_id}'"
            )
        if handle.is_real_host != self._is_real_host:
            raise AgentInvalidHandleError(
                f"Handle is_real_host '{handle.is_real_host}' does not match adapter '{self._is_real_host}'"
            )
        if not handle.invocation_token:
            raise AgentInvalidHandleError("Handle invocation_token is missing")
        if not secrets.compare_digest(handle.adapter_instance_id or "", self._instance_id):
            raise AgentInvalidHandleError("Handle adapter_instance_id does not match this adapter instance")

        with self._lock:
            session_data = self._running_sessions.get(handle.invocation_token)
            if session_data is None and include_history:
                session_data = self._session_history.get(handle.invocation_token)
            if session_data is None:
                raise AgentInvalidHandleError("Session invocation token not found in this adapter instance")

            stored_handle = session_data.get("handle")
            if not isinstance(stored_handle, AgentHandle):
                raise AgentInvalidHandleError("Stored session has no valid handle")
            if not secrets.compare_digest(handle.invocation_token, stored_handle.invocation_token):
                raise AgentInvalidHandleError("Handle invocation token mismatch")
            if not secrets.compare_digest(handle.session_id or "", stored_handle.session_id or ""):
                raise AgentInvalidHandleError("Handle session_id mismatch")
            return session_data

    def dispatch_agent(self, request: AgentRequest) -> AgentHandle:
        """
        Dispatch an isolated Cursor agent using Local Runtime.
        Enforces workspace directory validation, model specification, and role immutability.
        """
        role = (request.role or "").upper().strip()
        workspace_dir = os.path.realpath(request.workspace_dir)
        if not os.path.isdir(workspace_dir):
            raise AgentNotSupportedError(f"Workspace directory '{request.workspace_dir}' does not exist or is not a directory.")

        runtime = "local"
        if isinstance(request.extra_context, Mapping):
            runtime = request.extra_context.get("cursor_runtime", "local")
            sandbox_mode = request.extra_context.get("sandbox_mode")
            if role in ("REVIEWER", "QA") and sandbox_mode == "workspace-write":
                raise AgentNotSupportedError(
                    f"Role '{role}' is strictly read-only and cannot request writable workspace sandbox."
                )

        if runtime != "local":
            raise AgentNotSupportedError(
                f"Cursor SDK runtime '{runtime}' is unsupported in Phase 3. Only 'local' runtime is supported."
            )

        model = self._resolve_model(request)
        sdk = self._get_sdk()

        api_key_env = self.api_key_env_var
        if isinstance(request.extra_context, Mapping):
            custom_env = request.extra_context.get("cursor_api_key_env")
            if custom_env and str(custom_env).strip():
                api_key_env = str(custom_env).strip()

        api_key = os.environ.get(api_key_env)
        if self._is_real_host and not api_key:
            raise AgentNotSupportedError(
                f"Cursor API key environment variable '{api_key_env}' is not set or empty."
            )

        token = secrets.token_urlsafe(32)

        try:
            subagent_id = self._resolve_target_agent(role, request.extra_context)
            (
                subagent_name,
                _subagent_desc,
                subagent_prompt,
                sub_tools,
                sub_disallowed,
                _source_file,
            ) = self._load_subagent_definition(workspace_dir, subagent_id)
            # Do not set setting_sources or dirs. Project settings would load every
            # .cursor/agents file plus the target repo's rules and MCP, and dirs does not
            # discover subagents. The role runs as this agent, with the file's tool
            # allowlist applied at create time so disallowed tools are not offered.
            local_opts = sdk.LocalAgentOptions(cwd=workspace_dir)
            parent_agents = None
            parent_tools = list(sub_tools or [])
            parent_disallowed_tools = list(sub_disallowed or [])

            resume_id = (
                request.extra_context.get("resume_agent_id")
                or (request.session_id if request.extra_context.get("is_resume") else None)
            ) if isinstance(request.extra_context, Mapping) else None

            if resume_id and hasattr(sdk.Agent, "resume"):
                resume_opts = None
                if hasattr(sdk, "AgentOptions"):
                    resume_opts = sdk.AgentOptions(
                        api_key=api_key,
                        local=local_opts,
                        agents=parent_agents,
                        tools=parent_tools,
                        disallowed_tools=parent_disallowed_tools,
                    )
                if resume_opts is not None:
                    try:
                        agent = sdk.Agent.resume(str(resume_id), options=resume_opts)
                    except TypeError:
                        agent = sdk.Agent.resume(str(resume_id), resume_opts)
                else:
                    agent = sdk.Agent.resume(str(resume_id))
            else:
                if hasattr(sdk, "AgentOptions"):
                    agent_options = sdk.AgentOptions(
                        model=model,
                        api_key=api_key,
                        local=local_opts,
                        tools=parent_tools,
                        disallowed_tools=parent_disallowed_tools,
                        agents=parent_agents,
                    )
                    try:
                        agent = sdk.Agent.create(agent_options)
                    except TypeError:
                        agent = sdk.Agent.create(
                            model=model,
                            api_key=api_key,
                            local=local_opts,
                            tools=parent_tools,
                            disallowed_tools=parent_disallowed_tools,
                            agents=parent_agents,
                        )
                else:
                    agent = sdk.Agent.create(
                        model=model,
                        api_key=api_key,
                        local=local_opts,
                        tools=parent_tools,
                        disallowed_tools=parent_disallowed_tools,
                        agents=parent_agents,
                    )

            parent_prompt = (
                f"{subagent_prompt}\n\n"
                f"Task Description:\n{request.prompt}"
            )

            send_opts = sdk.SendOptions() if hasattr(sdk, "SendOptions") else None
            if send_opts is not None:
                try:
                    run = agent.send(parent_prompt, options=send_opts)
                except TypeError:
                    run = agent.send(parent_prompt)
            else:
                run = agent.send(parent_prompt)
        except Exception as exc:
            self._handle_dispatch_error(exc)
            raise

        if not hasattr(agent, "agent_id") or not getattr(agent, "agent_id"):
            raise AgentNotSupportedError(
                "Cursor SDK Agent does not expose required 'agent_id' attribute. Verify official cursor-sdk version."
            )
        canonical_agent_id = str(agent.agent_id)
        session_id = request.session_id.strip() if request.session_id and request.session_id.strip() else canonical_agent_id
        run_id = str(getattr(run, "id", f"run_{uuid.uuid4().hex[:8]}"))
        handle = AgentHandle(
            session_id=session_id,
            host_id=self.adapter_id,
            status="running",
            is_real_host=self._is_real_host,
            adapter_instance_id=self._instance_id,
            invocation_token=token,
        )

        with self._lock:
            self._running_sessions[token] = {
                "handle": handle,
                "agent": agent,
                "run": run,
                "request": request,
                "model": model,
                "agent_id": canonical_agent_id,
                "expected_subagent": subagent_name,
                "expected_subagent_tools": sub_tools,
                "expected_subagent_disallowed": sub_disallowed,
                "created_at": time.time(),
                "completed": False,
                "result": None,
            }

        return handle

    def get_agent_id(self, handle: AgentHandle) -> Optional[str]:
        """Retrieve the canonical Cursor agent_id for a given handle."""
        with self._lock:
            data = self._running_sessions.get(handle.invocation_token) or self._session_history.get(handle.invocation_token)
            if data:
                return data.get("agent_id")
        return None

    def _handle_dispatch_error(self, exc: Exception) -> None:
        """Map SDK errors during dispatch to typed framework exceptions."""
        exc_name = type(exc).__name__
        msg = str(exc)
        is_retryable = getattr(exc, "is_retryable", False)
        retry_after = getattr(exc, "retry_after", None)
        status_code = getattr(exc, "status_code", None)

        if is_retryable or status_code in (429, 502, 503, 504):
            if retry_after is not None:
                try:
                    time.sleep(min(float(retry_after), 5.0))
                except Exception:
                    pass
            raise RuntimeError(
                f"transient_cursor_host_failure: 503 service unavailable or 429 rate limit exceeded: {msg}"
            ) from exc

        if "Authentication" in exc_name or status_code == 401 or "unauthorized" in msg.lower():
            raise AgentNotSupportedError(f"Cursor authentication failed: {msg}") from exc
        if "Configuration" in exc_name:
            raise AgentNotSupportedError(f"Cursor configuration error: {msg}") from exc
        if "BadRequest" in exc_name or status_code == 400:
            raise AgentNotSupportedError(f"Cursor bad request error: {msg}") from exc

    def _extract_assistant_text(self, collected_messages: Sequence[Any]) -> str:
        """
        Extract assistant response text from official run.messages() stream.
        Handles official message structure: message.type == 'assistant' with
        message.message.content containing text blocks, with defensive fallbacks.
        """
        def _get_val(obj: Any, *keys: str, default: Any = None) -> Any:
            for k in keys:
                if isinstance(obj, Mapping):
                    if k in obj and obj[k] is not None:
                        return obj[k]
                elif hasattr(obj, k):
                    v = getattr(obj, k)
                    if v is not None:
                        return v
            return default

        text_pieces: List[str] = []
        for msg in (collected_messages or []):
            msg_type = str(_get_val(msg, "type", default="") or "").lower()
            if msg_type != "assistant":
                continue

            inner_msg = _get_val(msg, "message", default=None)
            target = inner_msg if inner_msg is not None else msg
            content = _get_val(target, "content", default=None)

            msg_extracted: List[str] = []
            if isinstance(content, str) and content.strip():
                msg_extracted.append(content)
            elif isinstance(content, (list, tuple)):
                for block in content:
                    b_type = str(_get_val(block, "type", default="") or "").lower()
                    if b_type == "text":
                        b_text = _get_val(block, "text", default="")
                        if b_text and str(b_text).strip():
                            msg_extracted.append(str(b_text))
            if not msg_extracted:
                raw_text = _get_val(msg, "text", default="") or _get_val(target, "text", default="")
                if raw_text and str(raw_text).strip():
                    msg_extracted.append(str(raw_text))

            if msg_extracted:
                text_pieces.append("".join(msg_extracted))

        return "\n".join(text_pieces).strip()

    def _extract_gate_events(
        self,
        collected_messages: Sequence[Any],
        run: Any,
        run_result: Any,
        expected_subagent: Optional[str] = None,
        expected_subagent_tools: Optional[Sequence[str]] = None,
        expected_subagent_disallowed: Optional[Sequence[str]] = None,
    ) -> Tuple[List[str], List[str], List[str]]:
        """
        Extract task subagent invocations, parent prohibited tool calls, and subagent tool violations
        from official run.messages() events, with defensive fallback to run/run_result attributes.
        Returns:
            (task_calls, parent_prohibited_calls, subagent_violations)
        """
        task_calls: List[str] = []
        parent_prohibited_calls: List[str] = []
        subagent_violations: List[str] = []
        seen_call_ids: Set[str] = set()
        seen_tool_invocations: Set[str] = set()
        task_events: List[str] = []

        sub_tools_set = set(expected_subagent_tools) if expected_subagent_tools is not None else None
        sub_disallowed_set = set(expected_subagent_disallowed) if expected_subagent_disallowed is not None else set()

        def _get_val(obj: Any, *keys: str, default: Any = None) -> Any:
            for k in keys:
                if isinstance(obj, Mapping):
                    if k in obj and obj[k] is not None:
                        return obj[k]
                elif hasattr(obj, k):
                    v = getattr(obj, k)
                    if v is not None:
                        return v
            return default

        current_subagent = None

        def _process_tool_invocation(
            raw_tool: str,
            args: Any,
            call_id: str,
            status: str,
            msg_err: Any,
            sub_sender: Optional[str] = None,
        ):
            nonlocal current_subagent
            if not raw_tool:
                return
            tool_name = CURSOR_SESSION_TO_SDK_TOOL_MAP.get(raw_tool, raw_tool)

            if call_id:
                if call_id in seen_call_ids:
                    return
                seen_call_ids.add(call_id)
            elif status == "completed" and tool_name in seen_tool_invocations:
                return

            if status in ("started", "") or not call_id:
                seen_tool_invocations.add(tool_name)

            if tool_name and sub_tools_set is not None:
                if tool_name not in sub_tools_set:
                    parent_prohibited_calls.append(tool_name)
                return

            if tool_name == "task":
                target_sub = (
                    _get_val(args, "subagent", "subagent_name", "subagent_type", "agent", "name", "type", default="")
                    or sub_sender
                    or ""
                )
                sub_str = str(target_sub).strip()
                task_calls.append(sub_str)
                current_subagent = sub_str
            elif tool_name:
                msg_subagent = sub_sender or current_subagent
                if msg_subagent or task_calls:
                    if status == "error" or msg_err:
                        subagent_violations.append(
                            f"Tool '{raw_tool}' call failed with error: {msg_err or 'status=error'}"
                        )
                    sub_label = msg_subagent or expected_subagent or "subagent"
                    if tool_name in sub_disallowed_set:
                        subagent_violations.append(
                            f"Subagent '{sub_label}' attempted prohibited tool '{tool_name}'"
                        )
                    elif sub_tools_set is not None and tool_name not in sub_tools_set:
                        subagent_violations.append(
                            f"Subagent '{sub_label}' attempted unauthorized tool '{tool_name}'. Allowed: {sorted(list(sub_tools_set))}"
                        )
                else:
                    parent_prohibited_calls.append(tool_name)

        for msg in (collected_messages or []):
            msg_type = str(_get_val(msg, "type", default="") or "").lower()
            call_id = str(_get_val(msg, "id", "call_id", default="") or "")
            status = str(_get_val(msg, "status", default="") or "").lower()
            msg_err = _get_val(msg, "error", "error_message", default="")

            if msg_type in ("tool_call", "tool_use"):
                raw_tool = str(_get_val(msg, "name", "tool", "tool_name", default="") or "").lower()
                args = _get_val(msg, "args", "arguments", "input", default={}) or {}
                sub_sender = _get_val(msg, "subagent", "author", "sender", default="")
                _process_tool_invocation(raw_tool, args, call_id, status, msg_err, sub_sender=sub_sender)

            elif msg_type == "tool_result":
                is_err = _get_val(msg, "is_error", default=False)
                if status == "error" or msg_err or is_err:
                    subagent_violations.append(
                        f"Tool execution failed: {msg_err or 'status=error'}"
                    )

            elif msg_type == "assistant":
                inner_msg = _get_val(msg, "message", default=None)
                target = inner_msg if inner_msg is not None else msg
                content = _get_val(target, "content", default=None)
                if isinstance(content, (list, tuple)):
                    for block in content:
                        b_type = str(_get_val(block, "type", default="") or "").lower()
                        if b_type in ("tool_use", "tool_call"):
                            raw_tool = str(_get_val(block, "name", "tool", "tool_name", default="") or "").lower()
                            b_args = _get_val(block, "input", "args", "arguments", default={}) or {}
                            b_id = str(_get_val(block, "id", "call_id", default="") or "")
                            b_status = str(_get_val(block, "status", default="") or "").lower()
                            b_err = _get_val(block, "error", "error_message", default="")
                            _process_tool_invocation(raw_tool, b_args, b_id, b_status, b_err)

            elif msg_type == "task":
                target_sub = (
                    _get_val(msg, "subagent", "subagent_name", "target", "agent", "name", default="")
                    or _get_val(_get_val(msg, "args", "arguments", default={}) or {}, "subagent", "name", default="")
                )
                if target_sub:
                    sub_str = str(target_sub).strip()
                    task_events.append(sub_str)
                    current_subagent = sub_str

        # If no tool_call events with name='task' were emitted, but task events exist, use task events
        if not task_calls and task_events:
            task_calls.extend(task_events)

        # Defensive fallback: if neither tool_calls nor task events in messages, check run / run_result tool_calls
        if not task_calls and not parent_prohibited_calls and not subagent_violations:
            fallback_calls = getattr(run_result, "tool_calls", None) or getattr(run, "tool_calls", None) or []
            for tc in fallback_calls:
                raw_tc = str(_get_val(tc, "tool", "name", "tool_name", default="") or "").lower()
                tc_name = CURSOR_SESSION_TO_SDK_TOOL_MAP.get(raw_tc, raw_tc)
                tc_args = _get_val(tc, "args", "arguments", default=tc) or {}
                if tc_name == "task":
                    sub = _get_val(tc_args, "subagent", "subagent_name", "agent", "name", default="")
                    task_calls.append(str(sub).strip())
                elif tc_name:
                    tc_sub = _get_val(tc, "subagent", default="") or (task_calls[-1] if task_calls else "")
                    if tc_sub or task_calls:
                        if tc_name in sub_disallowed_set:
                            subagent_violations.append(
                                f"Subagent '{tc_sub or expected_subagent}' attempted prohibited tool '{tc_name}'"
                            )
                        elif sub_tools_set is not None and tc_name not in sub_tools_set:
                            subagent_violations.append(
                                f"Subagent '{tc_sub or expected_subagent}' attempted unauthorized tool '{tc_name}'"
                            )
                    else:
                        parent_prohibited_calls.append(tc_name)

        return task_calls, parent_prohibited_calls, subagent_violations

    def wait_for_result(
        self,
        handle: AgentHandle,
        timeout_seconds: Optional[float] = None,
    ) -> AgentResult:
        """Wait for Run completion, enforce thread timeouts, map SDK error hierarchy, and extract result."""
        session_data = self._validate_handle(handle, include_history=True)
        if session_data.get("completed") and session_data.get("result") is not None:
            return session_data["result"]

        return self._wait_for_result_locked(handle, session_data, timeout_seconds)

    def _wait_for_result_locked(
        self,
        handle: AgentHandle,
        session_data: Dict[str, Any],
        timeout_seconds: Optional[float],
    ) -> AgentResult:
        run = session_data["run"]
        agent = session_data["agent"]
        timeout = timeout_seconds if timeout_seconds is not None else self._default_timeout_seconds

        run_id = getattr(run, "id", f"run_{uuid.uuid4().hex[:8]}")
        agent_id = getattr(agent, "agent_id", handle.session_id)

        run_result_holder: List[Any] = []
        run_exc_holder: List[Exception] = []
        collected_messages: List[Any] = []

        def _wait_target():
            try:
                # 1. Stream and consume run.messages() if available
                if hasattr(run, "messages"):
                    try:
                        m_iter = run.messages() if callable(run.messages) else run.messages
                        if m_iter is not None:
                            for msg in m_iter:
                                collected_messages.append(msg)
                                _, parent_bad, sub_bad = self._extract_gate_events(
                                    collected_messages,
                                    run,
                                    None,
                                    expected_subagent=session_data.get("expected_subagent"),
                                    expected_subagent_tools=session_data.get("expected_subagent_tools"),
                                    expected_subagent_disallowed=session_data.get("expected_subagent_disallowed"),
                                )
                                if parent_bad or sub_bad:
                                    try:
                                        run.cancel()
                                    except Exception:
                                        pass
                                    break
                    except Exception as m_exc:
                        logger.warning(f"Error consuming run.messages(): {m_exc}")
                res = run.wait()
                run_result_holder.append(res)
            except Exception as e:
                run_exc_holder.append(e)

        try:
            worker = threading.Thread(
                target=_wait_target,
                name=f"cursor_sdk_wait_{run_id}",
                daemon=True,
            )
            with self._lock:
                session_data["worker"] = worker
            worker.start()
            worker.join(timeout=timeout)

            if worker.is_alive():
                # Timed out! Proactively cancel run and close agent to interrupt socket/subprocess
                try:
                    run.cancel()
                except Exception:
                    pass
                if agent is not None and hasattr(agent, "close") and callable(agent.close):
                    try:
                        agent.close()
                    except Exception:
                        pass
                # Bounded grace join: check repeatedly up to 2.0s to ensure thread exits cleanly
                grace_deadline = time.monotonic() + 2.0
                while worker.is_alive() and time.monotonic() < grace_deadline:
                    worker.join(timeout=0.05)
                if worker.is_alive():
                    logger.warning(
                        f"Cursor SDK worker thread {worker.name} did not exit after grace period following run.cancel()"
                    )
                raise AgentTimeoutError(f"Cursor SDK run timed out after {timeout} seconds")

            if run_exc_holder:
                return self._finalize_run_exception(handle, session_data, run, agent, run_exc_holder[0])

            if not run_result_holder:
                raise AgentTimeoutError(f"Cursor SDK run produced no result within {timeout} seconds")

            run_result = run_result_holder[0]

            # Post-wait fallback if streaming was unconsumed
            if not collected_messages and hasattr(run, "messages"):
                try:
                    m_iter = run.messages() if callable(run.messages) else run.messages
                    if m_iter is not None:
                        for msg in m_iter:
                            collected_messages.append(msg)
                except Exception:
                    pass

            status_str = getattr(run_result, "status", "finished").lower()
            assistant_output = self._extract_assistant_text(collected_messages)
            output_text = assistant_output or getattr(run_result, "result", "") or ""

            expected_subagent = session_data.get("expected_subagent")
            expected_tools = session_data.get("expected_subagent_tools")
            expected_disallowed = session_data.get("expected_subagent_disallowed")

            task_calls, prohibited_tool_calls, subagent_violations = self._extract_gate_events(
                collected_messages,
                run,
                run_result,
                expected_subagent=expected_subagent,
                expected_subagent_tools=expected_tools,
                expected_subagent_disallowed=expected_disallowed,
            )

            if subagent_violations or prohibited_tool_calls:
                if subagent_violations:
                    err_detail = (
                        f"Subagent '{expected_subagent}' tool restriction gate violated: "
                        f"{'; '.join(subagent_violations)}"
                    )
                else:
                    err_detail = (
                        f"Agent violated the role tool allowlist: used prohibited tool(s) {prohibited_tool_calls}."
                    )
                res = AgentResult(
                    session_id=handle.session_id,
                    status=AgentStatus.FAILED,
                    output="",
                    error_message=err_detail,
                    is_real_host=self._is_real_host,
                )
            elif status_str == "cancelled":
                res = AgentResult(
                    session_id=handle.session_id,
                    status=AgentStatus.CANCELLED,
                    output="",
                    error_message="Cursor Run was cancelled",
                    is_real_host=self._is_real_host,
                )
            elif status_str == "error":
                err_detail = None
                if subagent_violations:
                    err_detail = f"Subagent '{expected_subagent}' tool restriction gate violated: {'; '.join(subagent_violations)}"
                elif prohibited_tool_calls:
                    err_detail = f"Parent agent violated tool restriction gate: used prohibited tool(s) {prohibited_tool_calls}"
                else:
                    err_detail = getattr(run_result, "error", None) or output_text or "Cursor Run completed with error status"
                res = AgentResult(
                    session_id=handle.session_id,
                    status=AgentStatus.FAILED,
                    output=output_text,
                    error_message=str(err_detail),
                    is_real_host=self._is_real_host,
                )
            elif status_str == "expired":
                res = AgentResult(
                    session_id=handle.session_id,
                    status=AgentStatus.TIMEOUT,
                    output=output_text,
                    error_message="Cursor Run expired",
                    is_real_host=self._is_real_host,
                )
            else:
                gate_error = None
                if subagent_violations:
                    gate_error = (
                        f"Subagent '{expected_subagent}' tool restriction gate violated: {'; '.join(subagent_violations)}"
                    )
                elif prohibited_tool_calls:
                    gate_error = (
                        f"Agent violated the role tool allowlist: used prohibited tool(s) {prohibited_tool_calls}."
                    )

                if gate_error:
                    res = AgentResult(
                        session_id=handle.session_id,
                        status=AgentStatus.FAILED,
                        output="",
                        error_message=gate_error,
                        is_real_host=self._is_real_host,
                    )
                else:
                    res = AgentResult(
                        session_id=handle.session_id,
                        status=AgentStatus.SUCCESS,
                        output=output_text,
                        partial_results=(
                            {
                                "invocation_id": run_id,
                                "host_invocation_id": run_id,
                                "agent_id": agent_id,
                                "subagent": expected_subagent,
                            },
                        ),
                        is_real_host=self._is_real_host,
                    )

            with self._lock:
                session_data["completed"] = True
                session_data["result"] = res
                self._session_history[handle.invocation_token] = session_data
                self._running_sessions.pop(handle.invocation_token, None)

            return res
        finally:
            if agent is not None and hasattr(agent, "close") and callable(agent.close):
                try:
                    agent.close()
                except Exception:
                    pass

    def _finalize_run_exception(
        self,
        handle: AgentHandle,
        session_data: Dict[str, Any],
        run: Any,
        agent: Any,
        exc: Exception,
    ) -> AgentResult:
        """Classify exceptions from wait() into structured AgentResult states."""
        exc_type = type(exc).__name__
        msg = str(exc)
        is_retryable = getattr(exc, "is_retryable", False)
        status_code = getattr(exc, "status_code", None)
        retry_after = getattr(exc, "retry_after", None)

        if "Timeout" in exc_type:
            try:
                run.cancel()
            except Exception:
                pass
            raise AgentTimeoutError(f"Cursor SDK run timed out: {msg}") from exc

        if is_retryable or status_code in (429, 502, 503, 504) or "rate limit" in msg.lower():
            if retry_after is not None:
                try:
                    time.sleep(min(float(retry_after), 5.0))
                except Exception:
                    pass
            err_msg = f"transient_cursor_host_failure: 429 rate limit exceeded or server temporarily unavailable: {msg}"
        elif "Authentication" in exc_type or status_code == 401 or "unauthorized" in msg.lower():
            err_msg = f"Cursor authentication failed (unauthorized): {msg}"
        elif "Permission" in exc_type or status_code == 403:
            err_msg = f"Cursor permission denied: {msg}"
        elif "Configuration" in exc_type or "Bad" in exc_type:
            err_msg = f"Cursor configuration or request error: {msg}"
        else:
            err_msg = f"Cursor SDK execution failed: {msg}"

        res = AgentResult(
            session_id=handle.session_id,
            status=AgentStatus.FAILED,
            output="",
            error_message=err_msg,
            is_real_host=self._is_real_host,
        )

        with self._lock:
            session_data["completed"] = True
            session_data["result"] = res
            self._session_history[handle.invocation_token] = session_data
            self._running_sessions.pop(handle.invocation_token, None)

        return res

    def cancel_agent(self, handle: AgentHandle) -> bool:
        """Cancel a running agent run idempotently."""
        try:
            session_data = self._validate_handle(handle, include_history=True)
        except AgentInvalidHandleError:
            return False

        run = session_data.get("run")
        if run is not None:
            try:
                run.cancel()
            except Exception:
                pass

        agent = session_data.get("agent")
        if agent is not None and hasattr(agent, "close") and callable(agent.close):
            try:
                agent.close()
            except Exception:
                pass

        worker = session_data.get("worker")
        if worker is not None and worker.is_alive():
            worker.join(timeout=1.0)

        with self._lock:
            session_data["completed"] = True
            if session_data.get("result") is None:
                session_data["result"] = AgentResult(
                    session_id=handle.session_id,
                    status=AgentStatus.CANCELLED,
                    output="",
                    error_message="Agent run cancelled by request",
                    is_real_host=self._is_real_host,
                )
            self._session_history[handle.invocation_token] = session_data
            self._running_sessions.pop(handle.invocation_token, None)

        return True

    def request_confirmation(self, req: ConfirmationRequest) -> ConfirmationResult:
        """Interactive confirmation is unsupported on Cursor SDK adapter."""
        raise AgentNotSupportedError(
            "CursorSdkAdapter does not support interactive confirmation. "
            "Runner confirmations must use Runner confirmation tokens."
        )


def create_cursor_sdk_manifest(
    adapter_id: str = "cursor_sdk",
    verified_version: str = "0.1.0",
    verification_level: VerificationLevel = VerificationLevel.STATIC_ONLY,
    e2e_evidence_refs: Tuple[str, ...] = (),
    verified_at: Optional[str] = None,
) -> AdapterManifest:
    """Create the official AdapterManifest for Cursor Python SDK Reference Adapter."""
    if verification_level in (VerificationLevel.CLI_VERIFIED, VerificationLevel.NATIVE_VERIFIED):
        if not verified_at or not verified_at.strip():
            raise ValueError(f"{verification_level.value} requires an explicit verified_at timestamp")
        if not e2e_evidence_refs or any(not isinstance(r, str) or not r.strip() for r in e2e_evidence_refs):
            raise ValueError(f"{verification_level.value} requires non-empty e2e_evidence_refs")
        v_ts = verified_at.strip()
        v_refs = tuple(r.strip() for r in e2e_evidence_refs)
    else:
        v_ts = None
        v_refs = ()

    pv_win = PlatformVerification(
        operating_system="windows",
        host_surface=HostSurface.NATIVE,
        verification_level=verification_level,
        verified_version=verified_version,
        verified_at=v_ts,
        e2e_evidence_refs=v_refs,
    )
    pv_mac = PlatformVerification(
        operating_system="macos",
        host_surface=HostSurface.NATIVE,
        verification_level=VerificationLevel.STATIC_ONLY,
        verified_version=verified_version,
    )
    pv_linux = PlatformVerification(
        operating_system="linux",
        host_surface=HostSurface.NATIVE,
        verification_level=VerificationLevel.STATIC_ONLY,
        verified_version=verified_version,
    )

    return AdapterManifest(
        schema_version="2.0",
        adapter_id=adapter_id,
        display_name="Cursor Python SDK Reference Adapter",
        implementation_version="1.0.0",
        host_surface=HostSurface.NATIVE,
        verification_level=verification_level,
        capabilities={
            "real_subagents": "supported",
            "parallelism": "supported",
            "isolated_context": "supported",
            "worktree": "supported",
            "permission_approval": "unknown",
            "mcp": "supported",
            "interactive_confirmation": "unsupported",
            "usage_telemetry": "unknown",
        },
        workspace_modes=("isolated", "worktree", "shared"),
        identity_fields=("session_id", "host_id", "invocation_id"),
        auth_boundary=AuthBoundaryType.ENVIRONMENT,
        billing_boundary=BillingBoundaryType.API_KEY,
        platform_version_constraint=">=0.1.0",
        supported_operating_systems=("windows", "macos", "linux"),
        platform_verifications={"windows": pv_win, "macos": pv_mac, "linux": pv_linux},
        executable_candidates_by_os={},
        config_path_templates_by_os={},
        conformance_suite_version="2.0",
        auth_context_id="cursor_env_ctx",
        billing_context_id="cursor_api_key_ctx",
        verified_at=v_ts,
        e2e_evidence_refs=v_refs,
    )
