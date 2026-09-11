from collections.abc import Mapping
import glob
import json
import math
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
    AgentPermissionRequiredError,
    AgentRequest,
    AgentResult,
    AgentStatus,
    AgentTimeoutError,
    AgentUnsafeHostConfigError,
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

FORBIDDEN_HOST_PERMISSION_RULES: Set[str] = {
    "command(*)",
    "command(regex:.*)",
    "unsandboxed(*)",
    "unsandboxed(regex:.*)",
}


def _antigravity_config_path() -> str:
    return os.path.realpath(os.path.expanduser("~/.gemini/antigravity-cli/settings.json"))


def _validate_antigravity_host_config() -> None:
    """Fail closed on global wildcard/bypass rules; never rewrite host config."""
    config_path = _antigravity_config_path()
    if not os.path.isfile(config_path):
        return
    try:
        with open(config_path, "r", encoding="utf-8") as handle:
            config = json.load(handle)
    except Exception as exc:
        raise AgentUnsafeHostConfigError(
            f"Antigravity host config is unreadable or invalid JSON at '{config_path}': {exc}. "
            "Runner will not repair global settings automatically."
        ) from exc

    serialized = json.dumps(config, ensure_ascii=False, sort_keys=True).lower()
    permissions = config.get("permissions", {}) if isinstance(config, Mapping) else {}
    allow_rules = permissions.get("allow", ()) if isinstance(permissions, Mapping) else ()
    normalized_rules = {
        re.sub(r"\s+", "", str(rule)).lower()
        for rule in (allow_rules if isinstance(allow_rules, (list, tuple)) else ())
    }
    forbidden = sorted(normalized_rules.intersection(FORBIDDEN_HOST_PERMISSION_RULES))
    bypass_tokens = [
        token for token in ("dangerously-skip-permissions", "skip_permission_checks")
        if token in serialized
    ]
    if forbidden or bypass_tokens:
        details = ", ".join(forbidden + bypass_tokens)
        raise AgentUnsafeHostConfigError(
            f"Unsafe Antigravity global permission configuration detected at '{config_path}': {details}. "
            "Remove the wildcard/bypass rules manually; --approve cannot override this safety gate."
        )


def _is_host_permission_denial(*parts: Optional[str]) -> bool:
    material = "\n".join(str(part) for part in parts if part).lower()
    return any(marker in material for marker in (
        "permission_denied", "permission denied", "permission was denied",
        "auto-denied", "auto denied", "requires approval", "approval required",
        "not permitted", "sandbox denied", "access is denied", "access denied",
    ))

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

    # 1. Acceptance operations (Chinese + English)
    if any(p in cmd_lower for p in (
        "git push", "git merge", "release", "publish", "发布", "推送", "合流", "上线"
    )):
        return "acceptance"

    # 2. Destructive operations (Command syntax + Natural language deletion + Chinese keywords)
    if any(p in cmd_lower for p in (
        "git reset", "git clean", "git rebase", "git checkout -f",
        "rm -rf", "del /f", "del /s", "remove-item -recurse", "format",
        "删除", "清理", "销毁", "重置", "覆盖", "卸载", "清空", "丢弃", "废弃", "回收站", "撤销", "抹掉",
        "drop database", "drop table", "truncate table"
    )) or bool(re.search(r"\b(delete|remove|erase|destroy|drop|truncate|purge|overwrite|wipe|unlink|rmdir|clean|reset|empty|discard|recycle|trash|wastebasket|uncommit|revert)\b", cmd_lower)):
        return "destructive"

    # 3. Billing operations
    if any(p in cmd_lower for p in ("api_key", "billing", "purchase", "subscription", "充值", "账单", "购买")):
        return "billing"

    # 4. Controlled external operations (Network, package managers, external downloads)
    if any(p in cmd_lower for p in (
        "pip install", "npm install", "curl ", "wget ", "git clone", "git fetch",
        "http://", "https://", "download", "fetch", "联网", "外网", "下载"
    )):
        return "controlled_external"

    # Reject inline python execution / subshell from safe_local
    if "python -c" in cmd_lower or "python -" in cmd_lower.split() or "eval(" in cmd_lower or "exec(" in cmd_lower or "os.system" in cmd_lower:
        return "controlled_external"

    # 5. Check safe_local candidates
    parts = cmd_norm.split()
    if not parts:
        return "safe_local"

    first_token = parts[0].lower()

    # A. Git read-only commands
    if first_token == "git" and len(parts) >= 2:
        git_sub = parts[1].lower()

        # Reject any git command that writes output to arbitrary file (e.g. git diff --output=...)
        for arg in parts[2:]:
            arg_l = arg.lower()
            if arg_l.startswith("--output") or arg_l.startswith("-o") or arg_l.startswith("--file") or ">" in arg_l:
                return "destructive"

        if git_sub in ("status", "diff", "log", "show", "rev-parse"):
            dangerous_flags = {"-f", "--force", "--hard", "--delete", "-d"}
            if not any(arg.lower() in dangerous_flags for arg in parts[2:]):
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

        return "controlled_external"

    # B. Pytest commands with strict flag, path & junction/traversal validation
    if cmd_lower.startswith("python -m pytest") or cmd_lower.startswith("pytest"):
        pytest_idx = 1 if first_token == "pytest" else 3
        pytest_args = parts[pytest_idx:]

        ws_real = os.path.realpath(os.path.abspath(workspace_dir or "."))

        for arg in pytest_args:
            arg_lower = arg.lower()
            # Forbidden flags
            for f in FORBIDDEN_PYTEST_FLAGS:
                if arg_lower == f or arg_lower.startswith(f + "="):
                    return "controlled_external"
            # Disallow .ini, .cfg config files
            if arg_lower.endswith(".ini") or arg_lower.endswith(".cfg"):
                return "controlled_external"
            # Disallow shell metacharacters
            if any(ch in arg for ch in ("`", "$", ">", "<", "|", "&", ";")):
                return "controlled_external"

            # Check if arg is a path
            if not arg.startswith("-"):
                # Path safety: Disallow directory traversal (..) or absolute paths pointing outside workspace
                if ".." in arg_lower or arg_lower.startswith("/") or re.match(r"^[a-zA-Z]:", arg):
                    return "controlled_external"
                # Resolve real path to detect Windows Junction / Symlink pointing outside workspace
                try:
                    candidate_path = os.path.join(ws_real, arg) if not os.path.isabs(arg) else arg
                    if os.path.exists(candidate_path):
                        real_p = os.path.realpath(os.path.abspath(candidate_path))
                        if os.path.commonpath([ws_real, real_p]) != ws_real:
                            return "controlled_external"
                except Exception:
                    return "controlled_external"

                # Disallow non-test python files executed via pytest
                if arg_lower.endswith(".py") and not (
                    "test" in os.path.basename(arg_lower) or arg_lower.startswith("tests/")
                ):
                    return "controlled_external"

        return "safe_local"

    # C. Safe local scripts (Must match exact script path as first argument)
    if first_token in ("python", "python3", "python.exe"):
        if len(parts) >= 2:
            script_arg = parts[1].replace("\\", "/")
            if script_arg in SAFE_LOCAL_SCRIPT_PREFIXES:
                if not any(ch in cmd_lower for ch in ("eval(", "exec(", "os.system", "`", "$", ";", "|", "&", ">", "<")):
                    # DEF-T0052-19: Check transition_task / quick_task destination status
                    if "transition_task" in script_arg or "quick_task" in script_arg:
                        destructive_status_keywords = (
                            "已取消", "已废弃", "已退回", "已阻塞",
                            "cancel", "cancelled", "reject", "rejected", "abandon", "blocked"
                        )
                        if any(k in cmd_lower for k in destructive_status_keywords):
                            return "destructive"
                    return "safe_local"
        return "controlled_external"

    elif first_token.startswith("powershell"):
        file_idx = -1
        for i, p in enumerate(parts):
            if p.lower() == "-file" and i + 1 < len(parts):
                file_idx = i + 1
                break
        if file_idx > 0:
            ps_script = parts[file_idx].replace("\\", "/")
            if ps_script in SAFE_LOCAL_SCRIPT_PREFIXES:
                return "safe_local"
        return "controlled_external"

    # If it starts with an executable or command that did not pass safe whitelist
    if first_token in (
        "pip", "npm", "curl", "wget", "sh", "bash", "cmd", "cmd.exe",
        "node", "ruby", "perl", "sudo", "apt", "brew", "yum", "cargo", "go", "make"
    ) or any(first_token.endswith(ext) for ext in (".exe", ".bat", ".cmd", ".ps1", ".sh", ".py")):
        return "controlled_external"

    # Standard benign natural language task prompt within workspace sandbox
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


def _evaluate_request_risk(request: AgentRequest) -> str:
    """Classify the operation plan, never arbitrary source text embedded in a prompt.

    Production Runner prompts legitimately contain patches, requirements, and defect
    descriptions with words such as ``delete`` or ``drop``.  Treating that material as
    an executable command produced false destructive classifications.  A trusted
    orchestrator may therefore provide a narrow ``operation_intent`` describing the
    actual host operation.  Direct callers that omit it retain the legacy fail-closed
    prompt classification.
    """
    operation_intent = None
    if isinstance(request.extra_context, Mapping):
        operation_intent = request.extra_context.get("operation_intent")
    material = str(operation_intent).strip() if operation_intent is not None else ""
    if not material:
        material = request.prompt
    return evaluate_command_risk(material, request.workspace_dir)


def _format_print_timeout(timeout_seconds: float) -> str:
    """Return a valid agy duration while keeping Runner and CLI deadlines aligned."""
    return f"{max(1, int(math.ceil(float(timeout_seconds))))}s"


def _build_stream_input(prompt: str) -> str:
    """Encode one Antigravity user turn as documented NDJSON for stdin transport."""
    return json.dumps(
        {"event": "user", "message": {"content": prompt}},
        ensure_ascii=False,
        separators=(",", ":"),
    ) + "\n"


def _json_compatible(value: Any) -> Any:
    """Thaw immutable AgentRequest context into JSON-compatible containers."""
    if isinstance(value, Mapping):
        return {str(key): _json_compatible(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [_json_compatible(item) for item in value]
    return value


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


def _find_git_common_dir(path: str) -> Optional[str]:
    """Find the common .git directory for this repository or linked worktree."""
    if not path or not os.path.exists(path):
        return None
    cur = os.path.realpath(os.path.abspath(path))
    while True:
        git_entry = os.path.join(cur, ".git")
        if os.path.exists(git_entry):
            if os.path.isdir(git_entry):
                return os.path.realpath(git_entry)
            elif os.path.isfile(git_entry):
                try:
                    with open(git_entry, "r", encoding="utf-8") as f:
                        line = f.read().strip()
                    if line.startswith("gitdir:"):
                        gitdir = line[len("gitdir:"):].strip()
                        if not os.path.isabs(gitdir):
                            gitdir = os.path.join(cur, gitdir)
                        gitdir = os.path.realpath(gitdir)
                        commondir_file = os.path.join(gitdir, "commondir")
                        if os.path.isfile(commondir_file):
                            with open(commondir_file, "r", encoding="utf-8") as f:
                                cdir = f.read().strip()
                            if not os.path.isabs(cdir):
                                cdir = os.path.join(gitdir, cdir)
                            return os.path.realpath(cdir)
                        elif os.path.basename(os.path.dirname(gitdir)) == "worktrees":
                            return os.path.realpath(os.path.dirname(os.path.dirname(gitdir)))
                        return gitdir
                except Exception:
                    pass
                return os.path.realpath(cur)
        parent = os.path.dirname(cur)
        if parent == cur:
            break
        cur = parent
    return None


def _find_git_root(path: str) -> Optional[str]:
    """Find the root directory of the git repository or worktree containing path."""
    if not path or not os.path.exists(path):
        return None
    cur = os.path.realpath(os.path.abspath(path))
    while True:
        if os.path.exists(os.path.join(cur, ".git")):
            return cur
        parent = os.path.dirname(cur)
        if parent == cur:
            break
        cur = parent
    return None


def _is_git_repository(path: str) -> bool:
    """Check if the given directory is inside or is a Git repository/worktree."""
    return _find_git_root(path) is not None


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
        is_real_host: bool = False,
        default_sandbox_mode: bool = True,
        default_approval_policy: str = "request-review",
        default_timeout_seconds: float = 60.0,
        allowed_project_roots: Optional[Tuple[str, ...]] = None,
        verification_level: VerificationLevel = VerificationLevel.STATIC_ONLY,
    ):
        self.adapter_id = adapter_id
        self._instance_id = f"agy-inst:{uuid.uuid4().hex[:8]}"
        self._is_real_host = is_real_host
        self._verification_level = verification_level
        self._executable_path = executable_path or _find_default_antigravity_executable()
        self._default_sandbox_mode = default_sandbox_mode
        self._default_approval_policy = default_approval_policy
        self._default_timeout_seconds = default_timeout_seconds
        self._allowed_project_roots = allowed_project_roots
        self._running_sessions: Dict[str, Dict[str, Any]] = {}
        self._session_history: Dict[str, Dict[str, Any]] = {}
        self._permission_cache: Dict[Tuple[str, str, str, str, str, str, str], bool] = {}
        self._verification_probe_ctx = threading.local()
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
        Validate primary workspace and all secondary project folders (DEF-T0052-4, DEF-T0052-20).
        All roots must be absolute paths, exist, and belong to the same Git repository / worktree cluster or authorized project boundary.
        """
        if not request.workspace_dir or not os.path.isabs(request.workspace_dir):
            raise ValueError(f"request.workspace_dir must be an absolute path, got '{request.workspace_dir}'")

        ws_real = os.path.realpath(os.path.abspath(request.workspace_dir))
        ws_common = _find_git_common_dir(ws_real)
        ws_root = _find_git_root(ws_real)
        if not ws_common or not ws_root:
            raise AgentNotSupportedError(
                f"Workspace '{request.workspace_dir}' is not inside a trusted Git repository."
            )

        # Build set of authorized project root boundaries
        trusted_common_dirs: Set[str] = {ws_common}
        trusted_roots: Set[str] = {ws_root}
        if self._allowed_project_roots:
            for r in self._allowed_project_roots:
                r_real = os.path.realpath(os.path.abspath(r))
                trusted_roots.add(r_real)
                r_common = _find_git_common_dir(r_real)
                if r_common:
                    trusted_common_dirs.add(r_common)

        # Helper to validate a secondary folder
        def _check_folder(folder_val: Any, field_name: str) -> None:
            folder_str = str(folder_val).strip()
            if not folder_str or not os.path.isabs(folder_str) or not os.path.exists(folder_str):
                raise AgentNotSupportedError(
                    f"{field_name} '{folder_str}' must be an existing absolute path (Dual-root Fail-Closed)."
                )
            f_real = os.path.realpath(os.path.abspath(folder_str))
            f_common = _find_git_common_dir(f_real)
            f_root = _find_git_root(f_real)
            if not f_common or not f_root:
                raise AgentNotSupportedError(
                    f"{field_name} '{folder_str}' is not inside a Git repository (Dual-root Fail-Closed)."
                )
            # Accept if:
            # 1. Shares the same git-common-dir (e.g. main repo & linked worktree), OR
            # 2. In trusted roots / directory subtrees
            in_same_repo = f_common in trusted_common_dirs
            in_trusted_tree = any(
                f_real == tr or f_real.startswith(tr + os.sep) or tr.startswith(f_real + os.sep)
                for tr in trusted_roots
            )
            if not in_same_repo and not in_trusted_tree:
                raise AgentNotSupportedError(
                    f"{field_name} '{folder_str}' is outside authorized project boundary (Cross-Project Isolation Violation)."
                )

        if isinstance(request.extra_context, Mapping):
            folders = request.extra_context.get("project_folders")
            if folders:
                if isinstance(folders, str):
                    folders = [folders]
                for folder in folders:
                    _check_folder(folder, "Project folder")

            kanban_dir = request.extra_context.get("kanban_dir")
            if kanban_dir:
                _check_folder(kanban_dir, "Kanban folder")

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

        # A one-shot verification probe reviews an inline payload and must not inherit
        # a global Reviewer agent profile that may require command permissions.
        verification_probe_authorized = (
            getattr(self._verification_probe_ctx, "session_id", None) == request.session_id.strip()
        )
        agent_name = (
            "self"
            if verification_probe_authorized
            else ROLE_AGENT_MAP.get(role, "flow-dev" if role in ("DEV", "BUILDER") else "self")
        )

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

        # Prompts travel over stdin rather than argv.  Windows CreateProcess limits
        # the complete command line to roughly 32 KiB, while Reviewer prompts may
        # contain a complete candidate diff.  Antigravity's documented stream-json
        # input mode removes that platform limit without truncating review context.
        cmd.extend(["--input-format", "stream-json"])
        cmd.extend(["--output-format", "stream-json"])

        if isinstance(request.extra_context, Mapping):
            output_schema = request.extra_context.get("json_schema")
            if output_schema is not None:
                if not isinstance(output_schema, Mapping):
                    raise AgentNotSupportedError("json_schema must be a mapping")
                cmd.extend([
                    "--json-schema",
                    json.dumps(_json_compatible(output_schema), ensure_ascii=False, separators=(",", ":")),
                ])

        cmd.extend(["--print-timeout", _format_print_timeout(request.timeout_seconds)])
        return cmd

    def dispatch_agent(self, request: AgentRequest) -> AgentHandle:
        if not isinstance(request, AgentRequest):
            raise TypeError(f"request must be an AgentRequest instance, got {type(request).__name__}")
        if not request.session_id or not request.session_id.strip():
            raise ValueError("request.session_id cannot be empty")

        if self._is_real_host and isinstance(request.extra_context, Mapping) and request.extra_context.get(
            "enforce_host_config_safety"
        ):
            _validate_antigravity_host_config()

        # DEF-T0052-4: Dual-root and workspace validation
        self.validate_workspace_roots(request)

        # Build and validate command syntax, role routing and execution mode whitelist
        cmd = self.build_antigravity_exec_command(request)

        session_id = request.session_id.strip()
        invocation_id = f"inv-{uuid.uuid4().hex[:12]}"
        invocation_token = secrets.token_urlsafe(32)

        # 5-Tier Permission check & 6-tuple cache integration
        risk = _evaluate_request_risk(request)
        project_id = "default_project"
        auth_context = "user_local_ctx"
        permission_boundary = "workspace_read" if risk == "safe_local" else "workspace_write"
        if isinstance(request.extra_context, Mapping):
            project_id = str(request.extra_context.get("project_id", project_id))
            auth_context = str(request.extra_context.get("auth_context", auth_context))
            permission_boundary = str(request.extra_context.get("permission_boundary", permission_boundary))

        command_family = f"{risk}:{request.role or 'default'}"

        if risk in {"destructive", "billing", "acceptance"}:
            # An out-of-band approval may authorize controlled external reads, but it
            # must never authorize destructive, billing, or acceptance operations.
            raise AgentNotSupportedError(
                f"Operation classified as '{risk}' is strictly forbidden on non-interactive Antigravity CLI surface (Fail-Closed)."
            )

        # safe_local / controlled_external: require an approval recorded by the
        # trusted outer host.  Request fields themselves can never self-authorize.
        has_approval = self.has_permission_approval(
            project_id=project_id,
            auth_context=auth_context,
            session_id=session_id,
            workspace_dir=request.workspace_dir,
            command_family=command_family,
            permission_boundary=permission_boundary,
        )
        if not has_approval:
            raise AgentPermissionRequiredError(
                f"Operation classified as '{risk}' requires explicit user permission approval for command family "
                f"'{command_family}' in project '{project_id}' before execution."
            )

        # DEF-T0052-21: Block direct unverified automated real process spawning when STATIC_ONLY (Zero caller bypass)
        verification_probe_authorized = (
            getattr(self._verification_probe_ctx, "session_id", None) == session_id
        )
        if self._is_real_host and self._verification_level == VerificationLevel.STATIC_ONLY and not verification_probe_authorized:
            raise AgentNotSupportedError(
                "Antigravity Adapter is declared STATIC_ONLY; automated real host CLI execution "
                "is disabled until host surface is CLI_VERIFIED with human OAuth authorization."
            )

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

    def dispatch_verification_probe(self, request: AgentRequest) -> AgentHandle:
        """Run one externally approved, read-only probe without pre-promoting STATIC_ONLY."""
        if not isinstance(request, AgentRequest):
            raise TypeError("request must be an AgentRequest instance")
        if request.role.strip().upper() != "REVIEWER":
            raise AgentNotSupportedError("Verification probes are restricted to the read-only REVIEWER role")
        if str(request.extra_context.get("permission_boundary", "")) != "workspace_read":
            raise AgentNotSupportedError("Verification probes require workspace_read permission boundary")
        if str(request.extra_context.get("mode", "")) != "plan":
            raise AgentNotSupportedError("Verification probes require plan mode")
        session_id = request.session_id.strip()
        if getattr(self._verification_probe_ctx, "session_id", None) is not None:
            raise AgentNotSupportedError("Nested verification probes are not allowed")
        self._verification_probe_ctx.session_id = session_id
        try:
            return self.dispatch_agent(request)
        finally:
            self._verification_probe_ctx.session_id = None

    def promote_after_verified_evidence(
        self,
        evidence_ref: str,
        *,
        host_session_id: str,
        host_invocation_id: str,
        store: Optional[Any] = None,
        gate: Optional[Any] = None,
        validation_context: Optional[Any] = None,
    ) -> None:
        """Promote only when evidence exists in store, is verified, and names one completed canonical real session."""
        if not isinstance(evidence_ref, str) or not evidence_ref.strip():
            raise ValueError("evidence_ref must be non-empty")
        if not isinstance(host_session_id, str) or not host_session_id.strip():
            raise ValueError("host_session_id must be non-empty")
        if not isinstance(host_invocation_id, str) or not host_invocation_id.strip():
            raise ValueError("host_invocation_id must be non-empty")

        if store is None or gate is None or validation_context is None:
            raise AgentNotSupportedError(
                "Evidence promotion requires store, gate, and an explicit validation context"
            )
        try:
            record = store.read(evidence_ref.strip())
            if not record or not record.metadata:
                raise AgentNotSupportedError(f"Evidence '{evidence_ref}' not found in store")
            if record.metadata.host_session_id != host_session_id.strip():
                raise AgentNotSupportedError(f"Evidence '{evidence_ref}' session mismatch")
            if record.metadata.host_invocation_id != host_invocation_id.strip():
                raise AgentNotSupportedError(f"Evidence '{evidence_ref}' invocation mismatch")
            if gate.store is not store:
                raise AgentNotSupportedError("Evidence gate is not bound to the supplied store")
            if gate.validate_evidence(evidence_ref.strip(), validation_context) is not True:
                raise AgentNotSupportedError(f"Evidence gate did not validate '{evidence_ref}'")
        except Exception as e:
            raise AgentNotSupportedError(f"Evidence store validation failed for '{evidence_ref}': {e}") from e

        with self._lock:
            data = self._session_history.get(host_session_id.strip())
            has_identity_chain = bool(
                data
                and data.get("completed")
                and data.get("conversation_id")
                and data.get("invocation_id") == host_invocation_id.strip()
                and isinstance(data.get("result"), AgentResult)
                and data["result"].is_real_host is True
            )
            if not has_identity_chain:
                raise AgentNotSupportedError(
                    "Cannot promote without an evidence-bound completed real canonical identity chain"
                )
            self._verification_level = VerificationLevel.CLI_VERIFIED

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
            stdout_data, stderr_data = process.communicate(
                input=_build_stream_input(session_data["request"].prompt),
                timeout=timeout,
            )
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
            "prompt_transport": "stdin_stream_json",
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

        if status == AgentStatus.FAILED and _is_host_permission_denial(
            error_msg, final_output, stderr_data
        ):
            raise AgentPermissionRequiredError(
                "Antigravity host denied the requested operation and requires explicit user approval. "
                f"Host detail: {(error_msg or final_output)[:500]}"
            )

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
        session_id: Optional[str] = None,
        workspace_dir: str = "",
        command_family: str = "",
        permission_boundary: str = "",
    ) -> None:
        """
        Cache verified permission approval bound to 7-tuple (or 6-tuple when session_id is None).
        Key: (project_id, auth_context, adapter_instance_id, session_id or "*", workspace_dir, command_family, permission_boundary).
        """
        cache_key = (
            project_id,
            auth_context,
            self._instance_id,
            session_id or "*",
            workspace_dir,
            command_family,
            permission_boundary,
        )
        with self._lock:
            self._permission_cache[cache_key] = True

    def record_out_of_band_approval(self, request: AgentRequest) -> str:
        """Record explicit outer-host approval for one exact request identity.

        This method is intentionally separate from ``dispatch_agent`` so an
        untrusted request cannot approve itself.  Only safe-local and
        controlled-external requests are eligible; destructive, billing and
        acceptance operations remain fail-closed even after user approval.
        """
        if not isinstance(request, AgentRequest):
            raise TypeError("request must be an AgentRequest instance")
        risk = _evaluate_request_risk(request)
        if risk not in {"safe_local", "controlled_external"}:
            raise AgentNotSupportedError(
                f"Operation classified as '{risk}' cannot receive out-of-band approval (Fail-Closed)."
            )

        project_id = "default_project"
        auth_context = "user_local_ctx"
        permission_boundary = "workspace_read" if risk == "safe_local" else "workspace_write"
        if isinstance(request.extra_context, Mapping):
            project_id = str(request.extra_context.get("project_id", project_id))
            auth_context = str(request.extra_context.get("auth_context", auth_context))
            permission_boundary = str(request.extra_context.get("permission_boundary", permission_boundary))

        self.record_permission_approval(
            project_id=project_id,
            auth_context=auth_context,
            session_id=request.session_id.strip(),
            workspace_dir=request.workspace_dir,
            command_family=f"{risk}:{request.role or 'default'}",
            permission_boundary=permission_boundary,
        )
        return risk

    def has_permission_approval(
        self,
        project_id: str,
        auth_context: str,
        session_id: Optional[str] = None,
        workspace_dir: str = "",
        command_family: str = "",
        permission_boundary: str = "",
    ) -> bool:
        """Check whether exact 7-tuple (or project-wide wildcard) has verified permission approval."""
        with self._lock:
            if session_id:
                specific_key = (
                    project_id,
                    auth_context,
                    self._instance_id,
                    session_id,
                    workspace_dir,
                    command_family,
                    permission_boundary,
                )
                if self._permission_cache.get(specific_key, False):
                    return True
            wildcard_key = (
                project_id,
                auth_context,
                self._instance_id,
                "*",
                workspace_dir,
                command_family,
                permission_boundary,
            )
            return bool(self._permission_cache.get(wildcard_key, False))

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
        structured_outputs: List[str] = []
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

                        # Some CLI builds place the schema result directly on
                        # the stream event instead of nesting it under result.
                        top_status = str(ev.get("status") or "").strip().upper()
                        if ev.get("structured_output") is not None and top_status in ("", "SUCCESS"):
                            structured_value = ev["structured_output"]
                            if isinstance(structured_value, str):
                                structured_outputs.append(structured_value.strip())
                            else:
                                structured_outputs.append(json.dumps(structured_value, ensure_ascii=False))

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
                        elif isinstance(ev.get("init"), dict) and isinstance(ev["init"].get("conversation_id"), str):
                            c_id = ev["init"]["conversation_id"].strip()
                        elif isinstance(ev.get("step_update"), dict) and isinstance(ev["step_update"].get("conversation_id"), str):
                            c_id = ev["step_update"]["conversation_id"].strip()
                        elif isinstance(ev.get("result"), dict) and isinstance(ev["result"].get("conversation_id"), str):
                            c_id = ev["result"]["conversation_id"].strip()

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
                        elif isinstance(ev.get("step_update"), dict):
                            su = ev["step_update"]
                            if isinstance(su.get("step_id"), str) and su["step_id"].strip():
                                s_id = su["step_id"].strip()
                            elif isinstance(su.get("step_index"), int):
                                s_id = f"step_{su['step_index']}"

                        if s_id:
                            detected_step_id = s_id

                        # Extract token usage
                        if "usage" in ev and isinstance(ev["usage"], dict):
                            detected_usage.update(ev["usage"])
                        elif "token_usage" in ev and isinstance(ev["token_usage"], dict):
                            detected_usage.update(ev["token_usage"])
                        elif isinstance(ev.get("result"), dict) and isinstance(ev["result"].get("usage"), dict):
                            detected_usage.update(ev["result"]["usage"])

                        # Extract content / response. Agy may emit planner/progress
                        # messages before the final schema-constrained result. The
                        # structured result is authoritative and must not be joined
                        # with those messages.
                        ev_type = ev.get("type", "")
                        if ev_type in ("message", "assistant_message", "output", "text", "PLANNER_RESPONSE"):
                            content = ev.get("content") or ev.get("text") or ev.get("message")
                            if isinstance(content, str):
                                messages.append(content)
                        elif ev_type == "error":
                            error_msg = ev.get("message") or ev.get("error") or str(ev)
                        elif isinstance(ev.get("result"), dict):
                            res_obj = ev["result"]
                            result_status = str(res_obj.get("status") or "").strip().upper()
                            if result_status == "SUCCESS" and res_obj.get("structured_output") is not None:
                                structured_value = res_obj["structured_output"]
                                if isinstance(structured_value, str):
                                    structured_outputs.append(structured_value.strip())
                                else:
                                    structured_outputs.append(json.dumps(structured_value, ensure_ascii=False))
                            elif result_status == "SUCCESS" and isinstance(res_obj.get("response"), str):
                                messages.append(res_obj["response"])
                            elif result_status and result_status != "SUCCESS":
                                error_msg = (
                                    res_obj.get("error")
                                    or f"Antigravity CLI returned non-success status: {result_status}"
                                )
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

        # Prefer the last successful structured result. Earlier stream events are
        # telemetry, not part of the Agent's schema-bound response.
        output_text = structured_outputs[-1] if structured_outputs else "\n".join(messages).strip()
        if not output_text and stderr:
            output_text = stderr.strip()

        return output_text, events, error_msg, detected_conv_id, detected_invocation_id, detected_usage


def create_antigravity_manifest(
    adapter_id: str = "antigravity",
    verified_version: str = "1.1.21",
    verification_level_windows: VerificationLevel = VerificationLevel.STATIC_ONLY,
    e2e_evidence_refs_windows: Tuple[str, ...] = (),
    verified_at_windows: Optional[str] = None,
) -> AdapterManifest:
    """Create the official AdapterManifest for Google Antigravity Reference Adapter."""
    if verification_level_windows == VerificationLevel.CLI_VERIFIED:
        if not verified_at_windows or not verified_at_windows.strip():
            raise ValueError("CLI_VERIFIED requires an explicit verified_at_windows from completed E2E evidence")
        if not e2e_evidence_refs_windows or any(
            not isinstance(ref, str) or not ref.strip() for ref in e2e_evidence_refs_windows
        ):
            raise ValueError("CLI_VERIFIED requires non-empty e2e_evidence_refs_windows")
        v_ts = verified_at_windows.strip()
        v_refs = tuple(ref.strip() for ref in e2e_evidence_refs_windows)
        pv_win = PlatformVerification(
            operating_system="windows",
            host_surface=HostSurface.CLI,
            verification_level=VerificationLevel.CLI_VERIFIED,
            verified_version=verified_version,
            verified_at=v_ts,
            e2e_evidence_refs=v_refs,
        )
        manifest_ver_level = VerificationLevel.CLI_VERIFIED
        manifest_ver_at = v_ts
        manifest_refs = v_refs
    else:
        pv_win = PlatformVerification(
            operating_system="windows",
            host_surface=HostSurface.CLI,
            verification_level=VerificationLevel.STATIC_ONLY,
            verified_version=verified_version,
        )
        manifest_ver_level = VerificationLevel.STATIC_ONLY
        manifest_ver_at = None
        manifest_refs = ()

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
        verification_level=manifest_ver_level,
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
        verified_at=manifest_ver_at,
        e2e_evidence_refs=manifest_refs,
        extra={"priority": 90}
    )
