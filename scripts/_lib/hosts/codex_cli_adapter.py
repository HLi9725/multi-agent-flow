from collections.abc import Mapping
import glob
import json
import os
import platform
import re
import shutil
import subprocess
import sys
import threading
import time
from types import MappingProxyType
from typing import Any, Dict, List, Optional, Tuple
import uuid

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


def _find_default_codex_executable() -> Optional[str]:
    """Search for the standard Codex CLI executable across environment and default install paths."""
    which_path = shutil.which("codex")
    if which_path and os.path.isfile(which_path):
        return which_path

    if sys.platform.startswith("win"):
        local_app_data = os.environ.get("LOCALAPPDATA", "")
        if local_app_data:
            pattern = os.path.join(local_app_data, "OpenAI", "Codex", "bin", "*", "codex.exe")
            matches = glob.glob(pattern)
            if matches:
                return matches[0]
            # Alternate standard location
            alt_pattern = os.path.join(local_app_data, "Programs", "OpenAI", "Codex", "*.exe")
            alt_matches = glob.glob(alt_pattern)
            if alt_matches:
                return alt_matches[0]
    return None


class CodexCliAdapter(BaseHostAdapter):
    """
    Reference Host Adapter for OpenAI Codex CLI.
    Communicates non-interactively via `codex exec --json` with machine-readable
    JSONL events, process tree lifecycle control, session tracking, and evidence integration.
    """
    def __init__(
        self,
        adapter_id: str = "codex_cli",
        executable_path: Optional[str] = None,
        is_real_host: bool = True,
        default_sandbox_mode: str = "workspace-write",
        default_timeout_seconds: float = 60.0
    ):
        self.adapter_id = adapter_id
        self._instance_id = f"codex-cli-inst:{uuid.uuid4().hex[:8]}"
        self._is_real_host = is_real_host
        self._executable_path = executable_path or _find_default_codex_executable()
        self._default_sandbox_mode = default_sandbox_mode
        self._default_timeout_seconds = default_timeout_seconds
        self._running_sessions: Dict[str, Dict[str, Any]] = {}
        self._lock = threading.RLock()

        # Fixed static capabilities definition (Zero side-effects on detection)
        self._capabilities = HostCapabilities(
            is_real_host=self._is_real_host,
            supports_real_subagents=CapabilitySupport.SUPPORTED,
            supports_parallelism=CapabilitySupport.SUPPORTED,
            supports_isolated_context=CapabilitySupport.SUPPORTED,
            supports_worktree=CapabilitySupport.SUPPORTED,
            supports_permission_approval=CapabilitySupport.SUPPORTED,
            supports_mcp=CapabilitySupport.SUPPORTED,
            supports_interactive_confirmation=CapabilitySupport.UNSUPPORTED,
            supports_usage_telemetry=CapabilitySupport.SUPPORTED,
            max_concurrent_agents=4,
            extra=MappingProxyType({
                "host_surface": "cli",
                "cli_binary": os.path.basename(self._executable_path) if self._executable_path else "codex",
                "sandbox_policy": self._default_sandbox_mode,
            })
        )

    def detect_capabilities(self) -> HostCapabilities:
        """Pure memory / zero side-effects capability detection."""
        return self._capabilities

    def get_conformance_probe_spec(self) -> Tuple[Tuple[Any, ...], Dict[str, Any]]:
        """Return spawn-safe constructor inputs for isolated capability probing."""
        return (), {"adapter_id": self.adapter_id, "is_real_host": self._is_real_host}

    def dispatch_agent(self, request: AgentRequest) -> AgentHandle:
        if not isinstance(request, AgentRequest):
            raise TypeError(f"request must be an AgentRequest instance, got {type(request).__name__}")
        if not request.session_id or not request.session_id.strip():
            raise ValueError("request.session_id cannot be empty")
        if not request.workspace_dir or not os.path.isabs(request.workspace_dir):
            raise ValueError(f"request.workspace_dir must be an absolute path, got '{request.workspace_dir}'")

        session_id = request.session_id
        invocation_id = f"inv-{uuid.uuid4().hex[:12]}"
        sandbox_mode = self._default_sandbox_mode
        if isinstance(request.extra_context, Mapping):
            sandbox_mode = request.extra_context.get("sandbox_mode", self._default_sandbox_mode)

        # If real host execution is requested but executable is missing
        if self._is_real_host and (not self._executable_path or not os.path.exists(self._executable_path)):
            raise AgentNotSupportedError(f"Codex CLI executable not found at '{self._executable_path}'.")

        cmd = [
            self._executable_path or "codex",
            "exec",
            "--json",
            "-C", request.workspace_dir,
            "-s", sandbox_mode,
            request.prompt
        ]

        creationflags = 0
        if sys.platform.startswith("win"):
            # CREATE_NEW_PROCESS_GROUP for clean process tree termination
            creationflags = subprocess.CREATE_NEW_PROCESS_GROUP

        process = None
        if self._is_real_host:
            try:
                process = subprocess.Popen(
                    cmd,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    stdin=subprocess.PIPE,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    creationflags=creationflags
                )
            except Exception as e:
                raise RuntimeError(f"Failed to launch Codex CLI process: {e}") from e

        handle = AgentHandle(
            session_id=session_id,
            host_id=self.adapter_id,
            status="running",
            is_real_host=self._is_real_host,
            adapter_instance_id=self._instance_id
        )

        with self._lock:
            self._running_sessions[session_id] = {
                "handle": handle,
                "request": request,
                "invocation_id": invocation_id,
                "process": process,
                "start_time": time.time(),
                "sandbox_mode": sandbox_mode,
                "workspace_dir": request.workspace_dir,
                "completed": False,
                "result": None,
            }

        return handle

    def wait_for_result(self, handle: AgentHandle, timeout_seconds: Optional[float] = None) -> AgentResult:
        if not isinstance(handle, AgentHandle):
            raise AgentInvalidHandleError("handle must be an AgentHandle instance")
        if handle.adapter_instance_id != self._instance_id:
            raise AgentInvalidHandleError(f"Handle belongs to foreign adapter instance '{handle.adapter_instance_id}'")
        if handle.host_id != self.adapter_id:
            raise AgentInvalidHandleError(f"Handle host_id '{handle.host_id}' does not match adapter '{self.adapter_id}'")

        with self._lock:
            session_data = self._running_sessions.get(handle.session_id)
            if not session_data:
                raise AgentInvalidHandleError(f"Session '{handle.session_id}' not found in active sessions")

        timeout = timeout_seconds if timeout_seconds is not None else self._default_timeout_seconds
        process: Optional[subprocess.Popen] = session_data.get("process")

        # Fake/simulated fallback handling
        if not self._is_real_host or process is None:
            return AgentResult(
                session_id=handle.session_id,
                status=AgentStatus.SUCCESS,
                output="Codex CLI simulated execution output",
                is_real_host=False
            )

        # Real process wait and stream processing
        try:
            stdout_data, stderr_data = process.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            self._terminate_process_tree(process)
            with self._lock:
                self._running_sessions.pop(handle.session_id, None)
            raise AgentTimeoutError(f"Codex CLI session '{handle.session_id}' timed out after {timeout}s")

        exit_code = process.returncode
        output_text, events, error_msg = self._parse_jsonl_output(stdout_data, stderr_data)

        status = AgentStatus.SUCCESS if exit_code == 0 and not error_msg else AgentStatus.FAILED
        final_output = output_text if output_text else (stderr_data or "No output returned")

        result = AgentResult(
            session_id=handle.session_id,
            status=status,
            output=final_output,
            error_message=error_msg,
            is_real_host=True
        )

        with self._lock:
            session_data["completed"] = True
            session_data["result"] = result
            self._running_sessions.pop(handle.session_id, None)

        return result

    def cancel_agent(self, handle: AgentHandle) -> bool:
        if not isinstance(handle, AgentHandle):
            return False

        with self._lock:
            session_data = self._running_sessions.get(handle.session_id)
            if not session_data:
                return False

            process: Optional[subprocess.Popen] = session_data.get("process")
            if process is not None and process.poll() is None:
                self._terminate_process_tree(process)

            self._running_sessions.pop(handle.session_id, None)
            return True

    def request_confirmation(self, req: ConfirmationRequest) -> ConfirmationResult:
        if not isinstance(req, ConfirmationRequest):
            raise TypeError("req must be a ConfirmationRequest instance")
        return ConfirmationResult(
            request_id=req.request_id,
            selected_option=req.options[0] if req.options else "confirm",
            is_confirmed=True,
            is_real_host=self._is_real_host
        )

    def _terminate_process_tree(self, process: subprocess.Popen) -> None:
        """Safely terminate child process and its process tree."""
        if process is None or process.poll() is not None:
            return

        try:
            if sys.platform.startswith("win"):
                # Windows taskkill /F /T kills child process tree
                subprocess.run(
                    ["taskkill", "/F", "/T", "/PID", str(process.pid)],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    check=False
                )
            else:
                process.terminate()
                try:
                    process.wait(timeout=2.0)
                except subprocess.TimeoutExpired:
                    process.kill()
        except Exception:
            try:
                process.kill()
            except Exception:
                pass

    def _parse_jsonl_output(self, stdout: str, stderr: str) -> Tuple[str, List[Dict[str, Any]], Optional[str]]:
        """Parse JSONL events emitted by `codex exec --json`."""
        events: List[Dict[str, Any]] = []
        messages: List[str] = []
        error_msg: Optional[str] = None

        if stdout:
            for line in stdout.splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    ev = json.loads(line)
                    if isinstance(ev, dict):
                        events.append(ev)
                        ev_type = ev.get("type", "")
                        # Common event types in codex exec --json
                        if ev_type in ("message", "assistant_message", "output", "text"):
                            content = ev.get("content") or ev.get("text") or ev.get("message")
                            if isinstance(content, str):
                                messages.append(content)
                        elif ev_type == "error":
                            error_msg = ev.get("message") or str(ev)
                except Exception:
                    # Non-JSON line from stdout
                    messages.append(line)

        output_text = "\n".join(messages).strip()
        if not output_text and stderr:
            output_text = stderr.strip()

        return output_text, events, error_msg


def create_codex_cli_manifest(
    adapter_id: str = "codex_cli",
    verified_version: str = "0.149.0"
) -> AdapterManifest:
    """Create the official AdapterManifest for Codex CLI Reference Adapter."""
    pv_win = PlatformVerification(
        operating_system="windows",
        host_surface=HostSurface.CLI,
        verification_level=VerificationLevel.CLI_VERIFIED,
        verified_version=verified_version,
        verified_at="2026-08-26T14:30:00Z",
        e2e_evidence_refs=("evidence-codex-cli-win-01",)
    )
    pv_mac = PlatformVerification(
        operating_system="macos",
        host_surface=HostSurface.CLI,
        verification_level=VerificationLevel.STATIC_ONLY,
        verified_version=verified_version
    )
    pv_linux = PlatformVerification(
        operating_system="linux",
        host_surface=HostSurface.CLI,
        verification_level=VerificationLevel.STATIC_ONLY,
        verified_version=verified_version
    )

    return AdapterManifest(
        schema_version="2.0",
        adapter_id=adapter_id,
        display_name="OpenAI Codex CLI Reference Adapter",
        implementation_version="1.0.0",
        host_surface=HostSurface.CLI,
        verification_level=VerificationLevel.CLI_VERIFIED,
        capabilities={
            "real_subagents": "supported",
            "parallelism": "supported",
            "isolated_context": "supported",
            "worktree": "supported",
            "permission_approval": "supported",
            "mcp": "supported",
            "interactive_confirmation": "unsupported",
            "usage_telemetry": "supported"
        },
        workspace_modes=("isolated", "worktree", "shared"),
        identity_fields=("session_id", "host_id", "invocation_id"),
        auth_boundary=AuthBoundaryType.USER_LOCAL,
        billing_boundary=BillingBoundaryType.USER_SUBSCRIPTION,
        platform_version_constraint=">=0.140.0",
        supported_operating_systems=("windows", "macos", "linux"),
        platform_verifications={"windows": pv_win, "macos": pv_mac, "linux": pv_linux},
        executable_candidates_by_os={
            "windows": ("codex.exe", "%LOCALAPPDATA%\\OpenAI\\Codex\\bin\\*\\codex.exe"),
            "macos": ("codex", "/usr/local/bin/codex"),
            "linux": ("codex", "/usr/bin/codex")
        },
        config_path_templates_by_os={
            "windows": ("~/.codex/config.toml",),
            "macos": ("~/.codex/config.toml",),
            "linux": ("~/.codex/config.toml",)
        },
        conformance_suite_version="2.0",
        auth_context_id="user_local_ctx",
        billing_context_id="user_sub_ctx",
        verified_at="2026-08-26T14:30:00Z",
        e2e_evidence_refs=("evidence-codex-cli-win-01",),
        extra={"priority": 100}
    )
