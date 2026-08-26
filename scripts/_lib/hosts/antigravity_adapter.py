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

# Whitelist of permissible execution modes and sandbox policies
ALLOWED_EXECUTION_MODES: Set[str] = {"accept-edits", "plan"}
ALLOWED_APPROVAL_POLICIES: Set[str] = {"request-review", "strict", "proceed-in-sandbox", "never", "on-request"}

# Mapping from project roles to specialized Antigravity subagents
ROLE_AGENT_MAP: Dict[str, str] = {
    "DEV": "flow-dev",
    "BUILDER": "flow-dev",
    "REVIEWER": "flow-reviewer",
    "QA": "flow-qa",
    "ARCHITECT": "flow-architect",
    "PM": "flow-pm",
    "DOCS": "flow-docs",
    "DEVOPS": "flow-devops",
}

# 5-Tier Permission Categories
SAFE_LOCAL_GIT_CMDS: Set[str] = {"status", "diff", "log", "show", "rev-parse", "branch", "worktree"}
SAFE_LOCAL_SCRIPT_PREFIXES: Tuple[str, ...] = (
    "scripts/heartbeat.py",
    "scripts/quick_task.py",
    "scripts/transition_task.py",
    "scripts/check_stage_gate.py",
)

RISK_LEVEL_PRIORITY: Dict[str, int] = {
    "destructive": 5,
    "billing": 4,
    "acceptance": 3,
    "controlled_external": 2,
    "safe_local": 1,
}

FORBIDDEN_PYTEST_FLAGS: Tuple[str, ...] = (
    "-p", "--plugin", "--pyargs", "-c", "--config-file", "--ini",
    "-o", "--override-ini", "--import-mode", "--assert", "--basetemp",
    "--doctest-modules", "--cov", "--cov-config", "--trace", "--pdb",
    "-w", "--warnings"
)


def _evaluate_single_command_risk(cmd_line: str, workspace_dir: Optional[str] = None) -> str:
    """Evaluate risk level for a single unchained command."""
    cmd_norm = cmd_line.strip().replace("\\", "/")
    cmd_lower = cmd_norm.lower()

    if not cmd_norm:
        return "safe_local"

    # 1. Acceptance operations
    if any(p in cmd_lower for p in ("git push", "git merge", "release", "publish")):
        return "acceptance"

    # 2. Destructive operations
    if any(p in cmd_lower for p in (
        "git reset", "git clean", "git rebase", "git checkout -f",
        "rm -rf", "del /f", "del /s", "remove-item -recurse"
    )):
        return "destructive"

    # 3. Billing operations
    if any(p in cmd_lower for p in ("api_key", "billing", "purchase", "subscription_upgrade")):
        return "billing"

    # 4. Controlled external operations (Network, package managers, external paths)
    if any(p in cmd_lower for p in (
        "pip install", "npm install", "curl ", "wget ", "git clone", "git fetch", "http://", "https://"
    )):
        return "controlled_external"

    # Reject inline python execution / subshell from safe_local
    if "python -c" in cmd_lower or "python -" in cmd_lower.split() or "eval(" in cmd_lower or "exec(" in cmd_lower:
        return "controlled_external"

    # 5. Check safe_local candidates
    parts = cmd_norm.split()
    if not parts:
        return "safe_local"

    # A. Git read-only commands
    if parts[0].lower() == "git" and len(parts) >= 2:
        git_sub = parts[1].lower()
        if git_sub in ("status", "diff", "log", "show", "rev-parse"):
            if not any(f in cmd_lower for f in ("-f", "--force", "--hard", "--delete", "-d")):
                return "safe_local"
        elif git_sub == "branch":
            # Safe read-only branch listing vs destructive branch creation/deletion
            safe_branch_flags = {
                "-l", "--list", "-a", "--all", "-r", "--remotes",
                "--show-current", "--contains", "--no-contains", "-v", "-vv",
                "--merged", "--no-merged", "-i", "--ignore-case", "--column", "--no-column"
            }
            branch_args = parts[2:]
            if not branch_args or all(arg.lower() in safe_branch_flags for arg in branch_args):
                return "safe_local"
            return "destructive"
        elif git_sub == "worktree":
            # Safe read-only worktree listing vs destructive worktree add/remove/prune
            if len(parts) >= 3 and parts[2].lower() == "list":
                safe_wt_flags = {"--porcelain", "-v", "--verbose", "-z"}
                wt_args = parts[3:]
                if all(arg.lower() in safe_wt_flags for arg in wt_args):
                    return "safe_local"
            return "destructive"

    # B. Pytest commands with strict flag validation (DEF-T0052-3)
    if cmd_lower.startswith("python -m pytest") or cmd_lower.startswith("pytest"):
        pytest_idx = 1 if parts[0].lower() == "pytest" else 3
        pytest_args = parts[pytest_idx:]

        for arg in pytest_args:
            arg_lower = arg.lower()
            # If argument matches or starts with any forbidden flag
            for f in FORBIDDEN_PYTEST_FLAGS:
                if arg_lower == f or arg_lower.startswith(f + "="):
                    return "controlled_external"
            # Disallow .ini, .cfg, or arbitrary config files passed as positional arguments
            if arg_lower.endswith(".ini") or arg_lower.endswith(".cfg"):
                return "controlled_external"
            # Disallow shell metacharacters in arguments
            if any(ch in arg for ch in ("`", "$", ">", "<", "|", "&", ";")):
                return "controlled_external"
            # Disallow non-test python scripts executed via pytest positional args
            if arg_lower.endswith(".py") and not (
                "test" in os.path.basename(arg_lower) or arg_lower.startswith("tests/")
            ):
                return "controlled_external"

        return "safe_local"

    # C. Safe local scripts (Must match exact script path as first argument)
    if parts[0].lower() in ("python", "python3", "python.exe"):
        if len(parts) >= 2:
            script_arg = parts[1].replace("\\", "/")
            if script_arg in SAFE_LOCAL_SCRIPT_PREFIXES:
                if not any(ch in cmd_lower for ch in ("eval(", "exec(", "os.system", "`", "$", ";", "|", "&")):
                    return "safe_local"
    elif cmd_lower.startswith("powershell"):
        file_idx = -1
        for i, p in enumerate(parts):
            if p.lower() == "-file" and i + 1 < len(parts):
                file_idx = i + 1
                break
        if file_idx > 0:
            ps_script = parts[file_idx].replace("\\", "/")
    # If it starts with an executable or command that did not pass safe whitelist
    first_token = parts[0].lower() if parts else ""
    if first_token in (
        "pip", "npm", "curl", "wget", "sh", "bash", "cmd", "cmd.exe",
        "node", "ruby", "perl", "sudo", "apt", "brew", "yum", "cargo", "go", "make",
        "python", "python3", "python.exe", "powershell", "powershell.exe", "git", "pytest"
    ) or any(first_token.endswith(ext) for ext in (".exe", ".bat", ".cmd", ".ps1", ".sh", ".py")):
        return "controlled_external"

    # Standard natural language task prompt within workspace sandbox
    return "safe_local"


def evaluate_command_risk(cmd_line: str, workspace_dir: Optional[str] = None) -> str:
    """
    Evaluate command risk according to §3.1 5-tier permission hierarchy with command chaining support:
    - safe_local: Project-level Allow, can be executed without repeated prompts once approved.
    - controlled_external: Network, dependencies, external paths -> Ask.
    - destructive: Clean, reset, rebase, branch -D, deletion -> Deny/Ask.
    - billing: API Key, paid endpoints -> Explicit prompt.
    - acceptance: Push, merge main, release -> Explicit user confirmation.
    - Note: Command chains (;, &&, ||, |, &) evaluate all sub-commands and return the highest risk level.
    """
    tokens = re.split(r"(?:;|&&|\|\||\||&|\r?\n)", cmd_line)
    max_risk = "safe_local"
    max_score = 0

    for token in tokens:
        sub_cmd = token.strip()
        if not sub_cmd:
            continue
        risk = _evaluate_single_command_risk(sub_cmd, workspace_dir)
        score = RISK_LEVEL_PRIORITY.get(risk, 2)
        if score > max_score:
            max_score = score
            max_risk = risk

    return max_risk


def _find_default_antigravity_executable() -> Optional[str]:
    """Search for the standard Antigravity CLI (agy) executable across environment and default paths."""
    for name in ("agy", "agy.exe", "antigravity", "antigravity.exe"):
        which_path = shutil.which(name)
        if which_path and os.path.isfile(which_path):
            return which_path

    if sys.platform.startswith("win"):
        local_app_data = os.environ.get("LOCALAPPDATA", "")
        if local_app_data:
            candidate = os.path.join(local_app_data, "agy", "bin", "agy.exe")
            if os.path.isfile(candidate):
                return candidate
            pattern = os.path.join(local_app_data, "Programs", "Antigravity", "*.exe")
            matches = glob.glob(pattern)
            if matches:
                return matches[0]

        user_profile = os.environ.get("USERPROFILE", "")
        if user_profile:
            candidate = os.path.join(user_profile, ".gemini", "antigravity", "bin", "agy.exe")
            if os.path.isfile(candidate):
                return candidate

    return None


def _is_git_repository(path: str) -> bool:
    """Check if the given directory is inside or is a Git repository/worktree."""
    if not os.path.isdir(path):
        return False
    git_entry = os.path.join(path, ".git")
    if os.path.exists(git_entry):
        return True
    cur = os.path.abspath(path)
    while True:
        if os.path.exists(os.path.join(cur, ".git")):
            return True
        parent = os.path.dirname(cur)
        if parent == cur:
            break
        cur = parent
    return False


class AntigravityAdapter(BaseHostAdapter):
    """
    Reference Host Adapter for Google Antigravity (AGY) CLI / IDE.
    Enforces five-tier permission boundaries, role-based subagent routing,
    fail-closed canonical session/step binding, and handle ownership security.
    """
    def __init__(
        self,
        adapter_id: str = "antigravity",
        executable_path: Optional[str] = None,
        is_real_host: bool = True,
        default_sandbox_mode: bool = True,
        default_approval_policy: str = "request-review",
        default_timeout_seconds: float = 60.0
    ):
        self.adapter_id = adapter_id
        self._instance_id = f"agy-inst:{uuid.uuid4().hex[:8]}"
        self._is_real_host = is_real_host
        self._executable_path = executable_path or _find_default_antigravity_executable()
        self._default_sandbox_mode = default_sandbox_mode
        self._default_approval_policy = default_approval_policy
        self._default_timeout_seconds = default_timeout_seconds
        self._running_sessions: Dict[str, Dict[str, Any]] = {}
        self._session_history: Dict[str, Dict[str, Any]] = {}
        self._permission_cache: Dict[Tuple[str, str, str, str, str, str, str], bool] = {}
        self._lock = threading.RLock()

        # Fixed static capabilities definition (Zero side-effects on detection)
        self._capabilities = HostCapabilities(
            is_real_host=self._is_real_host,
            supports_real_subagents=CapabilitySupport.SUPPORTED,
            supports_parallelism=CapabilitySupport.SUPPORTED,
            supports_isolated_context=CapabilitySupport.SUPPORTED,
            supports_worktree=CapabilitySupport.SUPPORTED,
            supports_permission_approval=CapabilitySupport.UNSUPPORTED,
            supports_mcp=CapabilitySupport.SUPPORTED,
            supports_interactive_confirmation=CapabilitySupport.UNSUPPORTED,
            supports_usage_telemetry=CapabilitySupport.SUPPORTED,
            max_concurrent_agents=8,
            extra=MappingProxyType({
                "host_surface": "cli",
                "cli_binary": os.path.basename(self._executable_path) if self._executable_path else "agy",
                "sandbox_policy": "enabled" if self._default_sandbox_mode else "disabled",
                "approval_policy": self._default_approval_policy,
                "supported_roles": tuple(ROLE_AGENT_MAP.keys()),
            })
        )

    def detect_capabilities(self) -> HostCapabilities:
        """Pure memory / zero side-effects capability detection."""
        return self._capabilities

    def get_conformance_probe_spec(self) -> Tuple[Tuple[Any, ...], Dict[str, Any]]:
        """Return spawn-safe constructor inputs for isolated capability probing."""
        return (), {"adapter_id": self.adapter_id, "is_real_host": self._is_real_host}

    def validate_workspace_roots(self, request: AgentRequest) -> None:
        """
        Validate primary workspace and all secondary project folders (DEF-T0052-4).
        All roots must be absolute paths, exist, and be inside trusted Git repositories.
        """
        if not request.workspace_dir or not os.path.isabs(request.workspace_dir):
            raise ValueError(f"request.workspace_dir must be an absolute path, got '{request.workspace_dir}'")

        if not _is_git_repository(request.workspace_dir):
            raise AgentNotSupportedError(
                f"Workspace '{request.workspace_dir}' is not inside a trusted Git repository."
            )

        # Check secondary project folders / kanban_dir in extra_context
        if isinstance(request.extra_context, Mapping):
            folders = request.extra_context.get("project_folders")
            if folders:
                if isinstance(folders, str):
                    folders = [folders]
                for folder in folders:
                    folder_path = str(folder).strip()
                    if not folder_path or not os.path.isabs(folder_path) or not _is_git_repository(folder_path):
                        raise AgentNotSupportedError(
                            f"Project folder '{folder_path}' is not inside a trusted Git repository (Dual-root Fail-Closed)."
                        )

            kanban_dir = request.extra_context.get("kanban_dir")
            if kanban_dir:
                kanban_path = str(kanban_dir).strip()
                if not kanban_path or not os.path.isabs(kanban_path) or not _is_git_repository(kanban_path):
                    raise AgentNotSupportedError(
                        f"Kanban folder '{kanban_path}' is not inside a trusted Git repository (Dual-root Fail-Closed)."
                    )

    def build_antigravity_exec_command(self, request: AgentRequest) -> List[str]:
        """
        Build the exact argument list for `agy` execution.
        Validates roles, sandbox flags, subagents, and output format without bypassing permissions.
        All option flags must precede `--print` in agy CLI syntax.
        """
        role = (request.role or "").upper().strip()
        requested_mode = None
        if isinstance(request.extra_context, Mapping):
            requested_mode = request.extra_context.get("mode")

        if requested_mode is not None:
            if requested_mode not in ALLOWED_EXECUTION_MODES:
                raise AgentNotSupportedError(
                    f"Unauthorized execution mode '{requested_mode}'. Allowed whitelist: {sorted(ALLOWED_EXECUTION_MODES)}"
                )
            if role == "REVIEWER" and requested_mode != "plan":
                raise AgentNotSupportedError(
                    f"Role '{role}' is strictly read-only; cannot request writable execution mode '{requested_mode}'"
                )
            exec_mode = requested_mode
        else:
            if role in ("REVIEWER", "QA"):
                exec_mode = "plan"
            else:
                exec_mode = "accept-edits"

        # Determine target subagent
        agent_name = ROLE_AGENT_MAP.get(role, "flow-dev" if role in ("DEV", "BUILDER") else "self")

        cmd = [self._executable_path or "agy"]

        if request.workspace_dir:
            cmd.extend(["--add-dir", request.workspace_dir])
        if agent_name:
            cmd.extend(["--agent", agent_name])
        if exec_mode:
            cmd.extend(["--mode", exec_mode])

        # Sandbox protection
        use_sandbox = self._default_sandbox_mode
        if isinstance(request.extra_context, Mapping) and "sandbox" in request.extra_context:
            use_sandbox = bool(request.extra_context["sandbox"])
        if use_sandbox:
            cmd.append("--sandbox")

        # Project ID binding if available
        if isinstance(request.extra_context, Mapping) and request.extra_context.get("project_id"):
            cmd.extend(["--project", str(request.extra_context["project_id"])])

        cmd.extend(["--output-format", "stream-json"])
        cmd.extend(["--print", request.prompt])
        return cmd

    def dispatch_agent(self, request: AgentRequest) -> AgentHandle:
        if not isinstance(request, AgentRequest):
            raise TypeError(f"request must be an AgentRequest instance, got {type(request).__name__}")
        if not request.session_id or not request.session_id.strip():
            raise ValueError("request.session_id cannot be empty")

        # DEF-T0052-4: Dual-root and workspace validation
        self.validate_workspace_roots(request)

        session_id = request.session_id.strip()
        invocation_id = f"inv-{uuid.uuid4().hex[:12]}"
        invocation_token = secrets.token_urlsafe(32)

        # 5-Tier Permission check & 7-tuple cache integration
        risk = evaluate_command_risk(request.prompt, request.workspace_dir)
        project_id = "default_project"
        auth_context = "user_local_ctx"
        permission_boundary = "workspace_read" if risk == "safe_local" else "workspace_write"
        if isinstance(request.extra_context, Mapping):
            project_id = str(request.extra_context.get("project_id", project_id))
            auth_context = str(request.extra_context.get("auth_context", auth_context))
            permission_boundary = str(request.extra_context.get("permission_boundary", permission_boundary))

        command_family = f"{risk}:{request.role or 'default'}"
        has_approval = self.has_permission_approval(
            project_id=project_id,
            auth_context=auth_context,
            session_id=session_id,
            workspace_dir=request.workspace_dir,
            command_family=command_family,
            permission_boundary=permission_boundary,
        )

        if risk == "safe_local":
            if not has_approval:
                self.record_permission_approval(
                    project_id=project_id,
                    auth_context=auth_context,
                    session_id=session_id,
                    workspace_dir=request.workspace_dir,
                    command_family=command_family,
                    permission_boundary=permission_boundary,
                )
        else:
            explicit_approved = False
            if isinstance(request.extra_context, Mapping):
                explicit_approved = bool(
                    request.extra_context.get("approved")
                    or request.extra_context.get("user_confirmed")
                    or request.extra_context.get("approval_token")
                )
            if not has_approval and not explicit_approved:
                raise AgentNotSupportedError(
                    f"Operation classified as '{risk}' requires explicit user permission approval for session '{session_id}'."
                )
            elif explicit_approved and not has_approval:
                self.record_permission_approval(
                    project_id=project_id,
                    auth_context=auth_context,
                    session_id=session_id,
                    workspace_dir=request.workspace_dir,
                    command_family=command_family,
                    permission_boundary=permission_boundary,
                )

        cmd = self.build_antigravity_exec_command(request)

        if self._is_real_host and (not self._executable_path or not os.path.exists(self._executable_path)):
            raise AgentNotSupportedError(f"Antigravity CLI executable not found at '{self._executable_path}'.")

        creationflags = 0
        if sys.platform.startswith("win"):
            creationflags = subprocess.CREATE_NEW_PROCESS_GROUP

        handle = AgentHandle(
            session_id=session_id,
            host_id=self.adapter_id,
            status="running",
            is_real_host=self._is_real_host,
            adapter_instance_id=self._instance_id,
            invocation_token=invocation_token,
        )

        # Atomic reservation before process spawn
        with self._lock:
            if session_id in self._running_sessions or session_id in self._session_history:
                raise AgentInvalidHandleError(f"Session '{session_id}' already exists in this adapter instance")
            self._running_sessions[session_id] = {
                "handle": handle,
                "request": request,
                "invocation_id": invocation_id,
                "invocation_token": invocation_token,
                "conversation_id": None,
                "usage": {},
                "process": None,
                "start_time": time.time(),
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
                    stdin=subprocess.PIPE,
                    cwd=request.workspace_dir,  # Project directory isolation
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
                raise RuntimeError(f"Failed to launch Antigravity CLI process: {e}") from e

            with self._lock:
                current = self._running_sessions.get(session_id)
                if not current or current.get("handle") != handle:
                    self._terminate_process_tree(process)
                    raise AgentInvalidHandleError(f"Session reservation for '{session_id}' was lost")
                current["process"] = process

        return handle

    def wait_for_result(self, handle: AgentHandle, timeout_seconds: Optional[float] = None) -> AgentResult:
        if not isinstance(handle, AgentHandle):
            raise AgentInvalidHandleError("handle must be an AgentHandle instance")
        if handle.adapter_instance_id != self._instance_id:
            raise AgentInvalidHandleError(f"Handle belongs to foreign adapter instance '{handle.adapter_instance_id}'")
        if handle.host_id != self.adapter_id:
            raise AgentInvalidHandleError(f"Handle host_id '{handle.host_id}' does not match adapter '{self.adapter_id}'")
        if handle.is_real_host != self._is_real_host:
            raise AgentInvalidHandleError(f"Handle is_real_host '{handle.is_real_host}' does not match adapter '{self._is_real_host}'")

        with self._lock:
            session_data = self._running_sessions.get(handle.session_id)
            if not session_data:
                if handle.session_id in self._session_history:
                    return self._session_history[handle.session_id]["result"]
                raise AgentInvalidHandleError(f"Session '{handle.session_id}' not found in active sessions")

            if handle != session_data.get("handle"):
                raise AgentInvalidHandleError("Handle attributes mismatch with registered session")

        timeout = timeout_seconds if timeout_seconds is not None else self._default_timeout_seconds
        process: Optional[subprocess.Popen] = session_data.get("process")

        # Fake/simulated fallback handling
        if not self._is_real_host or process is None:
            sim_conv_id = f"ag-conv-{uuid.uuid4().hex[:12]}"
            sim_inv_id = f"{sim_conv_id}:step_1"
            sim_result = AgentResult(
                session_id=handle.session_id,
                status=AgentStatus.SUCCESS,
                output="Antigravity simulated execution output",
                partial_results=({"conversation_id": sim_conv_id, "invocation_id": sim_inv_id},),
                is_real_host=False
            )
            with self._lock:
                session_data["completed"] = True
                session_data["conversation_id"] = sim_conv_id
                session_data["invocation_id"] = sim_inv_id
                session_data["result"] = sim_result
                self._session_history[handle.session_id] = session_data
                self._running_sessions.pop(handle.session_id, None)
            return sim_result

        # Real process execution and parsing
        try:
            stdout_data, stderr_data = process.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            self._terminate_process_tree(process)
            with self._lock:
                self._running_sessions.pop(handle.session_id, None)
            raise AgentTimeoutError(f"Antigravity session '{handle.session_id}' timed out after {timeout}s")

        exit_code = process.returncode
        output_text, events, error_msg, detected_conv_id, detected_inv_id, detected_usage = self._parse_antigravity_output(stdout_data, stderr_data)

        # Fail-Closed on missing canonical identity
        if exit_code == 0 and not error_msg:
            if not detected_conv_id or not detected_inv_id:
                status = AgentStatus.FAILED
                error_msg = "Missing canonical host conversation/invocation identity from Antigravity stream (Fail-Closed)"
                final_invocation_id = None
            else:
                status = AgentStatus.SUCCESS
                final_invocation_id = detected_inv_id
        else:
            status = AgentStatus.FAILED
            final_invocation_id = detected_inv_id

        final_output = output_text if output_text else (stderr_data or "No output returned")

        meta_event = {
            "conversation_id": detected_conv_id if status == AgentStatus.SUCCESS else None,
            "invocation_id": final_invocation_id,
            "usage": detected_usage,
            "exit_code": exit_code,
            "host_identity_source": "antigravity_host_conversation_id" if status == AgentStatus.SUCCESS else None,
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
            session_data["conversation_id"] = detected_conv_id if status == AgentStatus.SUCCESS else None
            session_data["invocation_id"] = final_invocation_id
            session_data["usage"] = detected_usage
            session_data["result"] = result
            self._session_history[handle.session_id] = session_data
            self._running_sessions.pop(handle.session_id, None)

        return result

    def cancel_agent(self, handle: AgentHandle) -> bool:
        if not isinstance(handle, AgentHandle):
            raise AgentInvalidHandleError("handle must be an AgentHandle instance")
        if handle.adapter_instance_id != self._instance_id:
            raise AgentInvalidHandleError(f"Handle belongs to foreign adapter instance '{handle.adapter_instance_id}'")
        if handle.host_id != self.adapter_id:
            raise AgentInvalidHandleError(f"Handle host_id '{handle.host_id}' does not match adapter '{self.adapter_id}'")
        if handle.is_real_host != self._is_real_host:
            raise AgentInvalidHandleError(f"Handle is_real_host '{handle.is_real_host}' does not match adapter '{self._is_real_host}'")

        with self._lock:
            session_data = self._running_sessions.get(handle.session_id)
            if not session_data:
                return False

            if handle != session_data.get("handle"):
                raise AgentInvalidHandleError("Handle attributes mismatch with registered session")

            process: Optional[subprocess.Popen] = session_data.get("process")
            if process is not None and process.poll() is None:
                self._terminate_process_tree(process)

            session_data["completed"] = True
            session_data["result"] = AgentResult(
                session_id=handle.session_id,
                status=AgentStatus.CANCELLED,
                output="Antigravity CLI execution cancelled",
                is_real_host=self._is_real_host,
            )
            self._session_history[handle.session_id] = session_data
            self._running_sessions.pop(handle.session_id, None)
            return True

    def record_permission_approval(
        self,
        project_id: str,
        auth_context: str,
        session_id: str,
        workspace_dir: str,
        command_family: str,
        permission_boundary: str,
    ) -> None:
        """
        Cache verified permission approval strictly bound to 7-tuple.
        Key: (project_id, auth_context, adapter_instance_id, session_id, workspace_dir, command_family, permission_boundary).
        """
        cache_key = (
            project_id,
            auth_context,
            self._instance_id,
            session_id,
            workspace_dir,
            command_family,
            permission_boundary,
        )
        with self._lock:
            self._permission_cache[cache_key] = True

    def has_permission_approval(
        self,
        project_id: str,
        auth_context: str,
        session_id: str,
        workspace_dir: str,
        command_family: str,
        permission_boundary: str,
    ) -> bool:
        """Check whether exact 7-tuple has verified permission approval."""
        cache_key = (
            project_id,
            auth_context,
            self._instance_id,
            session_id,
            workspace_dir,
            command_family,
            permission_boundary,
        )
        with self._lock:
            return bool(self._permission_cache.get(cache_key, False))

    def request_confirmation(self, req: ConfirmationRequest) -> ConfirmationResult:
        """
        The selected non-interactive CLI surface cannot prove a human choice.
        A trusted outer host must collect and persist user confirmation as evidence.
        """
        if not isinstance(req, ConfirmationRequest):
            raise TypeError("req must be a ConfirmationRequest instance")
        if not req.request_id or not req.request_id.strip():
            raise ValueError("ConfirmationRequest.request_id cannot be empty")

        raise AgentNotSupportedError(
            "Antigravity CLI print mode is non-interactive; confirmation must be collected "
            "and verified by a trusted outer host."
        )

        return ConfirmationResult(
            request_id=req.request_id,
            selected_option=selected,
            is_confirmed=is_confirmed,
            is_real_host=self._is_real_host
        )

    def get_session_thread_id(self, session_id: str) -> Optional[str]:
        """Retrieve captured real conversation/thread ID."""
        with self._lock:
            data = self._session_history.get(session_id) or self._running_sessions.get(session_id)
            if data:
                return data.get("conversation_id")
        return None

    def get_session_invocation_id(self, session_id: str) -> Optional[str]:
        """Retrieve captured real invocation ID (<conversation_id>:<step_id>)."""
        with self._lock:
            data = self._session_history.get(session_id) or self._running_sessions.get(session_id)
            if data:
                return data.get("invocation_id")
        return None

    def get_session_usage(self, session_id: str) -> Dict[str, Any]:
        """Retrieve captured token usage."""
        with self._lock:
            data = self._session_history.get(session_id) or self._running_sessions.get(session_id)
            if data:
                return dict(data.get("usage", {}))
        return {}

    def _terminate_process_tree(self, process: subprocess.Popen) -> None:
        """Safely terminate child process and its tree."""
        if process is None or process.poll() is not None:
            return

        try:
            if sys.platform.startswith("win"):
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

    def _parse_antigravity_output(
        self,
        stdout: str,
        stderr: str
    ) -> Tuple[str, List[Dict[str, Any]], Optional[str], Optional[str], Optional[str], Dict[str, Any]]:
        """
        Parse JSON / stream-json events emitted by `agy`.
        Extracts messages, events, errors, conversation_id, step/item invocation ID, and token usage.
        """
        events: List[Dict[str, Any]] = []
        messages: List[str] = []
        error_msg: Optional[str] = None
        detected_conv_id: Optional[str] = None
        detected_step_id: Optional[str] = None
        detected_usage: Dict[str, Any] = {}

        if stdout:
            for line in stdout.splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    ev = json.loads(line)
                    if isinstance(ev, dict):
                        events.append(ev)

                        # Extract conversation / session ID
                        c_id = None
                        if isinstance(ev.get("conversation_id"), str) and ev["conversation_id"].strip():
                            c_id = ev["conversation_id"].strip()
                        elif isinstance(ev.get("conversationId"), str) and ev["conversationId"].strip():
                            c_id = ev["conversationId"].strip()
                        elif isinstance(ev.get("session_id"), str) and ev["session_id"].strip():
                            c_id = ev["session_id"].strip()
                        elif ev.get("type") in ("conversation.started", "session.started") and isinstance(ev.get("id"), str):
                            c_id = ev["id"].strip()

                        if c_id and not detected_conv_id:
                            detected_conv_id = c_id

                        # Extract step / item / invocation ID
                        s_id = None
                        if isinstance(ev.get("step_id"), str) and ev["step_id"].strip():
                            s_id = ev["step_id"].strip()
                        elif isinstance(ev.get("step_index"), int):
                            s_id = f"step_{ev['step_index']}"
                        elif isinstance(ev.get("item_id"), str) and ev["item_id"].strip():
                            s_id = ev["item_id"].strip()
                        elif isinstance(ev.get("id"), str) and any(ev["id"].startswith(pfx) for pfx in ("step_", "item_", "turn_", "inv_")):
                            s_id = ev["id"].strip()

                        if s_id and not detected_step_id:
                            detected_step_id = s_id

                        # Extract token usage
                        if "usage" in ev and isinstance(ev["usage"], dict):
                            detected_usage.update(ev["usage"])
                        elif "token_usage" in ev and isinstance(ev["token_usage"], dict):
                            detected_usage.update(ev["token_usage"])

                        # Extract content
                        ev_type = ev.get("type", "")
                        if ev_type in ("message", "assistant_message", "output", "text", "PLANNER_RESPONSE"):
                            content = ev.get("content") or ev.get("text") or ev.get("message")
                            if isinstance(content, str):
                                messages.append(content)
                        elif ev_type == "error":
                            error_msg = ev.get("message") or ev.get("error") or str(ev)
                except Exception:
                    # Non-JSON stdout line
                    m_conv = re.search(r'"conversation_id"\s*:\s*"([^"]+)"', line)
                    if m_conv and not detected_conv_id:
                        detected_conv_id = m_conv.group(1)
                    m_step = re.search(r'"(?:step_id|item_id)"\s*:\s*"([^"]+)"', line)
                    if m_step and not detected_step_id:
                        detected_step_id = m_step.group(1)
                    messages.append(line)

        detected_invocation_id = None
        if detected_conv_id and detected_step_id:
            detected_invocation_id = f"{detected_conv_id}:{detected_step_id}"

        output_text = "\n".join(messages).strip()
        if not output_text and stderr:
            output_text = stderr.strip()

        return output_text, events, error_msg, detected_conv_id, detected_invocation_id, detected_usage


def create_antigravity_manifest(
    adapter_id: str = "antigravity",
    verified_version: str = "1.1.21"
) -> AdapterManifest:
    """Create the official AdapterManifest for Google Antigravity Reference Adapter."""
    pv_win = PlatformVerification(
        operating_system="windows",
        host_surface=HostSurface.CLI,
        verification_level=VerificationLevel.CLI_VERIFIED,
        verified_version=verified_version,
        verified_at="2026-08-26T17:00:00Z",
        e2e_evidence_refs=("evidence-antigravity-cli-win-01",)
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
        display_name="Google Antigravity Reference Adapter",
        implementation_version="1.0.0",
        host_surface=HostSurface.CLI,
        verification_level=VerificationLevel.CLI_VERIFIED,
        capabilities={
            "real_subagents": "supported",
            "parallelism": "supported",
            "isolated_context": "supported",
            "worktree": "supported",
            "permission_approval": "unsupported",
            "mcp": "supported",
            "interactive_confirmation": "unsupported",
            "usage_telemetry": "supported"
        },
        workspace_modes=("isolated", "worktree", "shared"),
        identity_fields=("session_id", "host_id", "invocation_id"),
        auth_boundary=AuthBoundaryType.USER_LOCAL,
        billing_boundary=BillingBoundaryType.USER_SUBSCRIPTION,
        platform_version_constraint=">=1.0.0",
        supported_operating_systems=("windows", "macos", "linux"),
        platform_verifications={"windows": pv_win, "macos": pv_mac, "linux": pv_linux},
        executable_candidates_by_os={
            "windows": ("agy.exe", "%LOCALAPPDATA%\\agy\\bin\\agy.exe", "%USERPROFILE%\\.gemini\\antigravity\\bin\\agy.exe"),
            "macos": ("agy", "/usr/local/bin/agy", "~/.gemini/antigravity/bin/agy"),
            "linux": ("agy", "/usr/local/bin/agy", "~/.gemini/antigravity/bin/agy")
        },
        config_path_templates_by_os={
            "windows": ("~/.gemini/antigravity-cli/settings.json",),
            "macos": ("~/.gemini/antigravity-cli/settings.json",),
            "linux": ("~/.gemini/antigravity-cli/settings.json",)
        },
        conformance_suite_version="2.0",
        auth_context_id="user_local_ctx",
        billing_context_id="user_sub_ctx",
        verified_at="2026-08-26T17:00:00Z",
        e2e_evidence_refs=("evidence-antigravity-cli-win-01",),
        extra={"priority": 90}
    )
