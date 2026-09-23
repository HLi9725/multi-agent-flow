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

logger = logging.getLogger("cursor_sdk_adapter")

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
            local_opts = sdk.LocalAgentOptions(
                cwd=workspace_dir,
            )

            tools: Optional[List[str]] = None
            disallowed_tools: Optional[List[str]] = None
            if role == "REVIEWER":
                tools = ["read", "grep"]
                disallowed_tools = ["edit", "shell"]
            elif role == "QA":
                tools = []
                disallowed_tools = ["edit", "shell"]
            elif role == "BUILDER":
                tools = None
                disallowed_tools = None

            resume_id = (
                request.extra_context.get("resume_agent_id")
                or (request.session_id if request.extra_context.get("is_resume") else None)
            ) if isinstance(request.extra_context, Mapping) else None

            if resume_id and hasattr(sdk.Agent, "resume"):
                resume_opts = None
                if hasattr(sdk, "AgentOptions"):
                    resume_opts = sdk.AgentOptions(api_key=api_key)
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
                        tools=tools,
                        disallowed_tools=disallowed_tools,
                    )
                    try:
                        agent = sdk.Agent.create(agent_options)
                    except TypeError:
                        agent = sdk.Agent.create(
                            model=model,
                            api_key=api_key,
                            local=local_opts,
                            tools=tools,
                            disallowed_tools=disallowed_tools,
                        )
                else:
                    agent = sdk.Agent.create(
                        model=model,
                        api_key=api_key,
                        local=local_opts,
                        tools=tools,
                        disallowed_tools=disallowed_tools,
                    )

            send_opts = sdk.SendOptions() if hasattr(sdk, "SendOptions") else None
            if send_opts is not None:
                try:
                    run = agent.send(request.prompt, options=send_opts)
                except TypeError:
                    run = agent.send(request.prompt)
            else:
                run = agent.send(request.prompt)
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
        if "Authentication" in exc_name or "unauthorized" in msg.lower():
            raise AgentNotSupportedError(f"Cursor authentication failed: {msg}") from exc
        if "Configuration" in exc_name:
            raise AgentNotSupportedError(f"Cursor configuration error: {msg}") from exc
        if "BadRequest" in exc_name:
            raise AgentNotSupportedError(f"Cursor bad request error: {msg}") from exc

    def wait_for_result(
        self,
        handle: AgentHandle,
        timeout_seconds: Optional[float] = None,
    ) -> AgentResult:
        """Wait for Run completion, enforce thread timeouts, map SDK error hierarchy, and extract result."""
        session_data = self._validate_handle(handle, include_history=True)
        if session_data.get("completed") and session_data.get("result") is not None:
            return session_data["result"]

        run = session_data["run"]
        agent = session_data["agent"]
        timeout = timeout_seconds if timeout_seconds is not None else self._default_timeout_seconds

        run_id = getattr(run, "id", f"run_{uuid.uuid4().hex[:8]}")
        agent_id = getattr(agent, "agent_id", handle.session_id)

        run_result_holder: List[Any] = []
        run_exc_holder: List[Exception] = []

        def _wait_target():
            try:
                res = run.wait()
                run_result_holder.append(res)
            except Exception as e:
                run_exc_holder.append(e)

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

        # Ensure agent resources are closed upon completion
        if agent is not None and hasattr(agent, "close") and callable(agent.close):
            try:
                agent.close()
            except Exception:
                pass

        status_str = getattr(run_result, "status", "finished").lower()
        output_text = getattr(run_result, "result", "") or ""

        if status_str == "cancelled":
            res = AgentResult(
                session_id=handle.session_id,
                status=AgentStatus.CANCELLED,
                output="",
                error_message="Cursor Run was cancelled",
                is_real_host=self._is_real_host,
            )
        elif status_str == "error":
            res = AgentResult(
                session_id=handle.session_id,
                status=AgentStatus.FAILED,
                output=output_text,
                error_message="Cursor Run completed with error status",
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
            res = AgentResult(
                session_id=handle.session_id,
                status=AgentStatus.SUCCESS,
                output=output_text,
                partial_results=(
                    {
                        "invocation_id": run_id,
                        "host_invocation_id": run_id,
                        "agent_id": agent_id,
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

        if "Timeout" in exc_type:
            try:
                run.cancel()
            except Exception:
                pass
            raise AgentTimeoutError(f"Cursor SDK run timed out: {msg}") from exc

        if is_retryable or status_code in (429, 502, 503, 504) or "rate limit" in msg.lower():
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
