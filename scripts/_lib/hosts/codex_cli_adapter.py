from collections.abc import Mapping
import glob
import json
import os
import platform
import re
import secrets
import shutil
import subprocess
import sys
import threading
import time
from types import MappingProxyType
from typing import Any, Dict, List, Optional, Set, Tuple
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

# Whitelist of permissible sandbox modes and approval policies (DEF-T0050-1 & DEF-T0050-2)
ALLOWED_SANDBOX_MODES: Set[str] = {"read-only", "workspace-write"}
ALLOWED_APPROVAL_POLICIES: Set[str] = {"on-request", "never"}
ALLOWED_WINDOWS_SANDBOX_IMPLEMENTATIONS: Set[str] = {"elevated", "unelevated"}


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


def _is_git_repository(path: str) -> bool:
    """Check if the given directory is inside or is a Git repository/worktree (DEF-T0050-5)."""
    if not os.path.isdir(path):
        return False
    git_entry = os.path.join(path, ".git")
    if os.path.exists(git_entry):
        return True
    # Walk up parent directories to check for repository root / worktrees
    cur = os.path.abspath(path)
    while True:
        if os.path.exists(os.path.join(cur, ".git")):
            return True
        parent = os.path.dirname(cur)
        if parent == cur:
            break
        cur = parent
    return False


def _resolve_git_common_dir(workspace_dir: str) -> Optional[str]:
    """解析当前仓库的 Git 公共元数据目录，供隔离 Worktree 的 Builder 精确写入提交对象。"""
    try:
        raw = subprocess.check_output(
            ["git", "rev-parse", "--git-common-dir"],
            cwd=workspace_dir,
            stderr=subprocess.DEVNULL,
            text=True,
            encoding="utf-8",
            errors="replace",
        ).strip()
    except Exception:
        return None
    if not raw:
        return None
    resolved = os.path.realpath(raw if os.path.isabs(raw) else os.path.join(workspace_dir, raw))
    return resolved if os.path.isdir(resolved) else None


class CodexCliAdapter(BaseHostAdapter):
    """
    Reference Host Adapter for OpenAI Codex CLI.
    Enforces role-based sandboxing, permission approval contracts (§7.1),
    real thread/invocation binding, and clean Windows process tree management.
    """
    def __init__(
        self,
        adapter_id: str = "codex_cli",
        executable_path: Optional[str] = None,
        is_real_host: bool = True,
        default_sandbox_mode: str = "workspace-write",
        default_approval_policy: str = "on-request",
        windows_sandbox_implementation: str = "unelevated",
        default_timeout_seconds: float = 60.0
    ):
        if windows_sandbox_implementation not in ALLOWED_WINDOWS_SANDBOX_IMPLEMENTATIONS:
            raise ValueError(
                "windows_sandbox_implementation must be one of "
                f"{sorted(ALLOWED_WINDOWS_SANDBOX_IMPLEMENTATIONS)}"
            )
        self.adapter_id = adapter_id
        self._instance_id = f"codex-cli-inst:{uuid.uuid4().hex[:8]}"
        self._is_real_host = is_real_host
        self._executable_path = executable_path or _find_default_codex_executable()
        self._default_sandbox_mode = default_sandbox_mode
        self._default_approval_policy = default_approval_policy
        # Production automation must not repeatedly invoke the administrator-only
        # elevated sandbox bootstrap.  The official unelevated implementation
        # remains sandboxed and is the supported fallback when elevated setup is
        # unavailable.  Callers may opt in to elevated only after provisioning it.
        self._windows_sandbox_implementation = windows_sandbox_implementation
        self._default_timeout_seconds = default_timeout_seconds
        self._running_sessions: Dict[str, Dict[str, Any]] = {}
        self._session_history: Dict[str, Dict[str, Any]] = {}
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
            # `codex exec` is non-interactive. User confirmation must be
            # supplied by a trusted outer host, never inferred from options.
            supports_interactive_confirmation=CapabilitySupport.UNSUPPORTED,
            supports_usage_telemetry=CapabilitySupport.SUPPORTED,
            max_concurrent_agents=4,
            extra=MappingProxyType({
                "host_surface": "cli",
                "cli_binary": os.path.basename(self._executable_path) if self._executable_path else "codex",
                "sandbox_policy": self._default_sandbox_mode,
                "approval_policy": self._default_approval_policy,
                "windows_sandbox_implementation": self._windows_sandbox_implementation,
            })
        )

    def detect_capabilities(self) -> HostCapabilities:
        """Pure memory / zero side-effects capability detection."""
        return self._capabilities

    def get_conformance_probe_spec(self) -> Tuple[Tuple[Any, ...], Dict[str, Any]]:
        """Return spawn-safe constructor inputs for isolated capability probing."""
        return (), {"adapter_id": self.adapter_id, "is_real_host": self._is_real_host}

    def build_codex_exec_command(self, request: AgentRequest) -> List[str]:
        """
        Build the exact argument list for `codex exec`.
        Validates sandbox modes, approval flags, workspace path, and mutual exclusivity (DEF-T0050-6, DEF-T0050-7).
        """
        role = (request.role or "").upper().strip()
        requested_sandbox = None
        if isinstance(request.extra_context, Mapping):
            requested_sandbox = request.extra_context.get("sandbox_mode") or request.extra_context.get("sandbox")

        if requested_sandbox is not None:
            if requested_sandbox not in ALLOWED_SANDBOX_MODES:
                raise AgentNotSupportedError(
                    f"Unauthorized or dangerous sandbox mode '{requested_sandbox}'. "
                    f"Allowed whitelist: {sorted(ALLOWED_SANDBOX_MODES)}"
                )
            if role == "REVIEWER" and requested_sandbox != "read-only":
                raise AgentNotSupportedError(
                    f"Role '{role}' is strictly read-only; cannot request writable sandbox '{requested_sandbox}'"
                )
            sandbox_mode = requested_sandbox
        else:
            if role in ("REVIEWER", "QA"):
                sandbox_mode = "read-only"
            else:
                sandbox_mode = self._default_sandbox_mode

        approval_policy = self._default_approval_policy
        is_auto_approval = False
        if isinstance(request.extra_context, Mapping):
            custom_policy = request.extra_context.get("approval_policy")
            if custom_policy:
                if custom_policy not in ALLOWED_APPROVAL_POLICIES and custom_policy not in ("auto", "approve-for-me"):
                    raise ValueError(f"Invalid approval_policy '{custom_policy}'. Allowed: {ALLOWED_APPROVAL_POLICIES}")
                approval_policy = custom_policy
            if custom_policy in ("auto", "approve-for-me") or request.extra_context.get("approve_for_me"):
                is_auto_approval = True

        git_common_dir = None
        if role not in ("REVIEWER", "QA") and sandbox_mode == "workspace-write":
            git_common_dir = _resolve_git_common_dir(request.workspace_dir)

        if is_auto_approval:
            # DEF-T0050-7: REVIEWER cannot use --approve-for-me because it implies workspace-write
            if role == "REVIEWER":
                raise AgentNotSupportedError(
                    f"Role '{role}' is strictly read-only and cannot use '--approve-for-me' (which forces workspace-write)"
                )
            # DEF-T0050-7: --approve-for-me is mutually exclusive with -s / --sandbox in Codex CLI
            cmd = [
                self._executable_path or "codex",
                "exec",
                "--json",
                "-C", request.workspace_dir,
                "--approve-for-me",
            ]
        else:
            cmd = [
                self._executable_path or "codex",
                "exec",
                "--json",
                "-C", request.workspace_dir,
                "-s", sandbox_mode,
            ]

        if sys.platform.startswith("win"):
            # Per-invocation override: do not mutate the user's global Codex
            # configuration. This prevents an unprovisioned elevated sandbox
            # from opening a UAC installer for every Builder/QA subprocess.
            cmd[2:2] = [
                "-c",
                f'windows.sandbox="{self._windows_sandbox_implementation}"',
            ]

        if git_common_dir and os.path.commonpath([
            os.path.normcase(os.path.realpath(request.workspace_dir)),
            os.path.normcase(git_common_dir),
        ]) != os.path.normcase(os.path.realpath(request.workspace_dir)):
            cmd.extend(["--add-dir", git_common_dir])
        cmd.append(request.prompt)

        return cmd

    def dispatch_agent(self, request: AgentRequest) -> AgentHandle:
        if not isinstance(request, AgentRequest):
            raise TypeError(f"request must be an AgentRequest instance, got {type(request).__name__}")
        if not request.session_id or not request.session_id.strip():
            raise ValueError("request.session_id cannot be empty")
        if not request.workspace_dir or not os.path.isabs(request.workspace_dir):
            raise ValueError(f"request.workspace_dir must be an absolute path, got '{request.workspace_dir}'")

        # DEF-T0050-5: Git repository boundary check
        if not _is_git_repository(request.workspace_dir):
            raise AgentNotSupportedError(
                f"Workspace '{request.workspace_dir}' is not inside a trusted Git repository."
            )

        session_id = request.session_id.strip()
        invocation_id = f"inv-{uuid.uuid4().hex[:12]}"
        invocation_token = secrets.token_urlsafe(32)

        # Build and validate command (DEF-T0050-1, DEF-T0050-2, DEF-T0050-6, DEF-T0050-7)
        cmd = self.build_codex_exec_command(request)
        if "--approve-for-me" in cmd:
            sandbox_mode = "workspace-write"
            approval_policy = "approve-for-me"
        else:
            sandbox_mode = cmd[cmd.index("-s") + 1]
            approval_policy = self._default_approval_policy

        # If real host execution is requested but executable is missing
        if self._is_real_host and (not self._executable_path or not os.path.exists(self._executable_path)):
            raise AgentNotSupportedError(f"Codex CLI executable not found at '{self._executable_path}'.")

        creationflags = 0
        if sys.platform.startswith("win"):
            # CREATE_NEW_PROCESS_GROUP for clean process tree termination
            creationflags = subprocess.CREATE_NEW_PROCESS_GROUP

        handle = AgentHandle(
            session_id=session_id,
            host_id=self.adapter_id,
            status="running",
            is_real_host=self._is_real_host,
            adapter_instance_id=self._instance_id,
            invocation_token=invocation_token,
        )

        # Reserve the session before launching the subprocess. This makes the
        # duplicate check atomic and prevents an overwritten entry from
        # orphaning a previously launched process.
        with self._lock:
            if session_id in self._running_sessions or session_id in self._session_history:
                raise AgentInvalidHandleError(f"Session '{session_id}' already exists in this adapter instance")
            self._running_sessions[session_id] = {
                "handle": handle,
                "request": request,
                "invocation_id": invocation_id,
                "thread_id": None,
                "usage": {},
                "process": None,
                "start_time": time.time(),
                "sandbox_mode": sandbox_mode,
                "approval_policy": approval_policy,
                "workspace_dir": request.workspace_dir,
                "completed": False,
                "result": None,
            }

        if self._is_real_host:
            try:
                process = subprocess.Popen(
                    cmd,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    stdin=subprocess.DEVNULL,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    creationflags=creationflags
                )
            except Exception as e:
                with self._lock:
                    current = self._running_sessions.get(session_id)
                    if current and current.get("handle") == handle:
                        self._running_sessions.pop(session_id, None)
                raise RuntimeError(f"Failed to launch Codex CLI process: {e}") from e

            with self._lock:
                current = self._running_sessions.get(session_id)
                if not current or current.get("handle") != handle:
                    self._terminate_process_tree(process)
                    raise AgentInvalidHandleError(f"Session reservation for '{session_id}' was lost")
                current["process"] = process

        return handle

    def _validate_handle(self, handle: AgentHandle, *, include_history: bool = False) -> Dict[str, Any]:
        """Validate handle ownership and its unguessable invocation token."""
        if not isinstance(handle, AgentHandle):
            raise AgentInvalidHandleError("handle must be an AgentHandle instance")
        if handle.adapter_instance_id != self._instance_id:
            raise AgentInvalidHandleError(f"Handle belongs to foreign adapter instance '{handle.adapter_instance_id}'")
        if handle.host_id != self.adapter_id:
            raise AgentInvalidHandleError(f"Handle host_id '{handle.host_id}' does not match adapter '{self.adapter_id}'")
        if handle.is_real_host != self._is_real_host:
            raise AgentInvalidHandleError(f"Handle is_real_host '{handle.is_real_host}' does not match adapter '{self._is_real_host}'")
        if not handle.invocation_token:
            raise AgentInvalidHandleError("Handle invocation_token is missing")

        with self._lock:
            session_data = self._running_sessions.get(handle.session_id)
            if session_data is None and include_history:
                session_data = self._session_history.get(handle.session_id)
            if session_data is None:
                raise AgentInvalidHandleError(f"Session '{handle.session_id}' not found in this adapter instance")
            stored_handle = session_data.get("handle")
            if not isinstance(stored_handle, AgentHandle):
                raise AgentInvalidHandleError(f"Session '{handle.session_id}' has no valid owned handle")
            if not secrets.compare_digest(handle.invocation_token, stored_handle.invocation_token):
                raise AgentInvalidHandleError("Handle invocation_token does not match the owned session")
            if handle != stored_handle:
                raise AgentInvalidHandleError("Handle fields do not match the owned session handle")
            return session_data

    def wait_for_result(self, handle: AgentHandle, timeout_seconds: Optional[float] = None) -> AgentResult:
        session_data = self._validate_handle(handle, include_history=True)
        if session_data.get("completed") and session_data.get("result") is not None:
            return session_data["result"]

        timeout = timeout_seconds if timeout_seconds is not None else self._default_timeout_seconds
        process: Optional[subprocess.Popen] = session_data.get("process")

        # Fake/simulated fallback handling
        if not self._is_real_host or process is None:
            sim_thread_id = f"sim-thread-{uuid.uuid4().hex[:12]}"
            sim_inv_id = f"{sim_thread_id}:item_0"
            sim_result = AgentResult(
                session_id=handle.session_id,
                status=AgentStatus.SUCCESS,
                output="Codex CLI simulated execution output",
                partial_results=({"thread_id": sim_thread_id, "invocation_id": sim_inv_id},),
                is_real_host=False
            )
            with self._lock:
                session_data["completed"] = True
                session_data["thread_id"] = sim_thread_id
                session_data["invocation_id"] = sim_inv_id
                session_data["result"] = sim_result
                self._session_history[handle.session_id] = session_data
                self._running_sessions.pop(handle.session_id, None)
            return sim_result

        # Real process wait and stream processing
        try:
            stdout_data, stderr_data = process.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            self._terminate_process_tree(process)
            with self._lock:
                self._running_sessions.pop(handle.session_id, None)
            raise AgentTimeoutError(f"Codex CLI session '{handle.session_id}' timed out after {timeout}s")

        exit_code = process.returncode
        output_text, events, error_msg, detected_thread_id, detected_invocation_id, detected_usage = self._parse_jsonl_output(stdout_data, stderr_data)

        # DEF-T0050-11: Fail-Closed on missing canonical identity
        if exit_code == 0 and not error_msg:
            if not detected_thread_id or not detected_invocation_id:
                status = AgentStatus.FAILED
                error_msg = "Missing canonical host thread/invocation identity from Codex JSONL stream (Fail-Closed)"
                final_invocation_id = None
            else:
                status = AgentStatus.SUCCESS
                final_invocation_id = detected_invocation_id
        else:
            status = AgentStatus.FAILED
            final_invocation_id = detected_invocation_id

        final_output = output_text if output_text else (stderr_data or "No output returned")

        # DEF-T0050-3, DEF-T0050-8, DEF-T0050-10: Bind canonical thread_id:item_id composite invocation identity
        meta_event = {
            "thread_id": detected_thread_id if status == AgentStatus.SUCCESS else None,
            "invocation_id": final_invocation_id,
            "sandbox_mode": session_data.get("sandbox_mode"),
            "approval_policy": session_data.get("approval_policy"),
            "usage": detected_usage,
            "exit_code": exit_code,
            "host_identity_source": "openai_codex_host_thread_id" if status == AgentStatus.SUCCESS else None,
        }

        result = AgentResult(
            session_id=handle.session_id,
            status=status,
            output=final_output,
            partial_results=tuple([meta_event] + events),
            error_message=error_msg,
            is_real_host=True
        )

        with self._lock:
            session_data["completed"] = True
            session_data["thread_id"] = detected_thread_id if status == AgentStatus.SUCCESS else None
            session_data["invocation_id"] = final_invocation_id
            session_data["usage"] = detected_usage
            session_data["result"] = result
            self._session_history[handle.session_id] = session_data
            self._running_sessions.pop(handle.session_id, None)

        return result

    def cancel_agent(self, handle: AgentHandle) -> bool:
        # DEF-T0050-12/17: validate both adapter ownership and the exact
        # unguessable token issued for this invocation.
        session_data = self._validate_handle(handle, include_history=True)

        with self._lock:
            if handle.session_id not in self._running_sessions:
                return False
            process: Optional[subprocess.Popen] = session_data.get("process")
            if process is not None and process.poll() is None:
                self._terminate_process_tree(process)

            session_data["completed"] = True
            session_data["result"] = AgentResult(
                session_id=handle.session_id,
                status=AgentStatus.CANCELLED,
                output="Codex CLI execution cancelled",
                is_real_host=self._is_real_host,
            )
            self._session_history[handle.session_id] = session_data
            self._running_sessions.pop(handle.session_id, None)
            return True

    def request_confirmation(self, req: ConfirmationRequest) -> ConfirmationResult:
        """
        The selected non-interactive CLI surface cannot prove a human choice.

        A trusted outer host must collect and persist user confirmation as
        evidence. Strings supplied in ``options`` are choices, not proof.
        """
        if not isinstance(req, ConfirmationRequest):
            raise TypeError("req must be a ConfirmationRequest instance")
        if not req.request_id or not req.request_id.strip():
            raise ValueError("ConfirmationRequest.request_id cannot be empty")

        raise AgentNotSupportedError(
            "Codex CLI exec is non-interactive; confirmation must be collected "
            "and verified by a trusted outer host."
        )

    def get_session_thread_id(self, session_id: str) -> Optional[str]:
        """Retrieve the captured real thread_id for a given session."""
        with self._lock:
            data = self._session_history.get(session_id) or self._running_sessions.get(session_id)
            if data:
                return data.get("thread_id")
        return None

    def get_session_invocation_id(self, session_id: str) -> Optional[str]:
        """Retrieve the captured real invocation/turn ID for a given session (DEF-T0050-8)."""
        with self._lock:
            data = self._session_history.get(session_id) or self._running_sessions.get(session_id)
            if data:
                return data.get("invocation_id")
        return None

    def get_session_usage(self, session_id: str) -> Dict[str, Any]:
        """Retrieve captured usage telemetry for a given session."""
        with self._lock:
            data = self._session_history.get(session_id) or self._running_sessions.get(session_id)
            if data:
                return dict(data.get("usage", {}))
        return {}

    def _terminate_process_tree(self, process: subprocess.Popen) -> None:
        """Safely terminate child process and its process tree on Windows/POSIX."""
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

    def _parse_jsonl_output(
        self,
        stdout: str,
        stderr: str
    ) -> Tuple[str, List[Dict[str, Any]], Optional[str], Optional[str], Optional[str], Dict[str, Any]]:
        """
        Parse JSONL events emitted by `codex exec --json`.
        Extracts messages, events, errors (DEF-T0050-5), real host thread_id (DEF-T0050-3),
        real host item/turn/invocation_id (DEF-T0050-8, DEF-T0050-9), and usage telemetry.
        """
        events: List[Dict[str, Any]] = []
        messages: List[str] = []
        error_msg: Optional[str] = None
        detected_thread_id: Optional[str] = None
        detected_item_id: Optional[str] = None
        detected_invocation_id: Optional[str] = None
        detected_usage: Dict[str, Any] = {}

        if stdout:
            for line in stdout.splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    ev = json.loads(line)
                    if isinstance(ev, dict):
                        # DEF-T0050-5: All valid JSON events including error events MUST be appended!
                        events.append(ev)

                        # DEF-T0050-3 & DEF-T0050-9: Check for real host thread_id from thread.started / session_start
                        t_id = None
                        if isinstance(ev.get("thread_id"), str) and ev["thread_id"].strip():
                            t_id = ev["thread_id"].strip()
                        elif isinstance(ev.get("thread"), dict) and isinstance(ev["thread"].get("id"), str):
                            t_id = ev["thread"]["id"].strip()
                        elif isinstance(ev.get("data"), dict) and isinstance(ev["data"].get("thread_id"), str):
                            t_id = ev["data"]["thread_id"].strip()
                        elif ev.get("type") == "thread.started" and isinstance(ev.get("id"), str):
                            t_id = ev["id"].strip()
                        elif isinstance(ev.get("session_id"), str) and ("-" in ev["session_id"] or len(ev["session_id"]) > 16):
                            t_id = ev["session_id"].strip()

                        if t_id and not detected_thread_id:
                            detected_thread_id = t_id

                        # DEF-T0050-8 & DEF-T0050-9: Check for real host item.id (item.completed) or turn/invocation_id
                        i_id = None
                        if isinstance(ev.get("item"), dict) and isinstance(ev["item"].get("id"), str):
                            detected_item_id = ev["item"]["id"].strip()
                            i_id = detected_item_id
                        elif isinstance(ev.get("item_id"), str) and ev["item_id"].strip():
                            detected_item_id = ev["item_id"].strip()
                            i_id = detected_item_id
                        elif isinstance(ev.get("turn_id"), str) and ev["turn_id"].strip():
                            i_id = ev["turn_id"].strip()
                        elif isinstance(ev.get("turn"), dict) and isinstance(ev["turn"].get("id"), str):
                            i_id = ev["turn"]["id"].strip()
                        elif isinstance(ev.get("invocation_id"), str) and ev["invocation_id"].strip():
                            i_id = ev["invocation_id"].strip()
                        elif isinstance(ev.get("id"), str) and any(ev["id"].startswith(pfx) for pfx in ("item_", "turn-", "turn_", "inv-", "inv_", "msg_")):
                            i_id = ev["id"].strip()

                        if i_id and not detected_invocation_id:
                            detected_invocation_id = i_id

                        # Check for usage / tokens from turn.completed / usage event
                        if "usage" in ev and isinstance(ev["usage"], dict):
                            detected_usage.update(ev["usage"])
                        elif "token_usage" in ev and isinstance(ev["token_usage"], dict):
                            detected_usage.update(ev["token_usage"])

                        ev_type = ev.get("type", "")
                        if ev_type in ("message", "assistant_message", "output", "text"):
                            content = ev.get("content") or ev.get("text") or ev.get("message")
                            if isinstance(content, str):
                                messages.append(content)
                        elif ev_type in ("item.completed", "item.created") and isinstance(ev.get("item"), dict):
                            item_content = ev["item"].get("content") or ev["item"].get("text")
                            if isinstance(item_content, str) and item_content.strip():
                                messages.append(item_content)
                        elif ev_type == "error":
                            error_msg = ev.get("message") or ev.get("error") or str(ev)
                except Exception:
                    # Non-JSON line from stdout
                    m_th = re.search(r'"thread_id"\s*:\s*"([^"]+)"', line)
                    if m_th and not detected_thread_id:
                        detected_thread_id = m_th.group(1)
                    m_inv = re.search(r'"(?:item_id|turn_id|invocation_id)"\s*:\s*"([^"]+)"', line)
                    if m_inv and not detected_invocation_id:
                        detected_invocation_id = m_inv.group(1)
                    messages.append(line)

        # DEF-T0050-10/14: both sides must come from the host event stream.
        # A thread alone is a session identity, not an invocation identity.
        identity_part = detected_item_id or detected_invocation_id
        if detected_thread_id and identity_part:
            prefix = f"{detected_thread_id}:"
            if identity_part == detected_thread_id:
                detected_invocation_id = None
            elif identity_part.startswith(prefix):
                detected_invocation_id = identity_part
            else:
                detected_invocation_id = f"{prefix}{identity_part}"
        else:
            detected_invocation_id = None

        output_text = "\n".join(messages).strip()
        if not output_text and stderr:
            output_text = stderr.strip()

        return output_text, events, error_msg, detected_thread_id, detected_invocation_id, detected_usage


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
