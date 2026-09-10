# -*- coding: utf-8 -*-
"""
scripts/_lib/core/production_runner.py
Universal Production Orchestration Runner for multi-agent-flow.
Production-grade universal automatic runner for arbitrary projects and tasks.
"""
from dataclasses import dataclass, field, replace
import hashlib
import json
import os
import re
import shlex
import subprocess
import sys
import threading
import time
import uuid
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence, Set, Tuple

try:
    from ... import paths
except Exception:
    try:
        from scripts import paths
    except Exception:
        import paths
from ..boards.board_adapter_factory import get_board_adapter
from ..boards.offline_board_adapter import OfflineBoardAdapter
from .adapter_manifest import (
    AdapterManifest,
    AuthBoundaryType,
    BillingBoundaryType,
    ExecutionMode,
    HostSurface,
    PlatformVerification,
    VerificationLevel,
)
from .adapter_registry import AdapterRegistry
from .agent_schema import (
    AgentHandle,
    AgentInvalidHandleError,
    AgentNotSupportedError,
    AgentRequest,
    AgentResult,
    AgentStatus,
    AgentTimeoutError,
    CapabilitySupport,
    ConfirmationRequest,
    HostCapabilities,
)
from .evidence_gate import EvidenceGate, EvidenceValidationContext
from .evidence_schema import (
    ArtifactRecord,
    EvidenceError,
    EvidenceGateError,
    EvidenceMetadata,
    EvidenceRecord,
    EvidenceType,
)
from .evidence_store import EvidenceStore
from .orchestrator import (
    BuilderToReviewerHandover,
    DefectRejectionHandover,
    Orchestrator,
    ReviewerToQAHandover,
    TaskExecutionSession,
)
from .orchestrator_schema import (
    OrchestrationMode,
    OrchestrationRole,
    OrchestrationState,
    UserAcceptanceRequest,
)
from .runner_checkpoint_store import RunnerCheckpointStore, _validate_task_id
from .runner_schema import (
    QA_JSON_SCHEMA,
    REVIEWER_JSON_SCHEMA,
    QAStructuredOutput,
    ReviewerStructuredOutput,
    RunnerCheckpoint,
    RunnerResult,
    RunnerState,
    TaskExecutionSpec,
)
from .task_spec_loader import load_task_execution_spec, verify_optimistic_concurrency
from .worktree_manager import WorktreeManager
from .worktree_schema import WorktreeRequest
from ..hosts.antigravity_adapter import AntigravityAdapter, create_antigravity_manifest
from ..hosts.codex_cli_adapter import CodexCliAdapter, create_codex_cli_manifest


ALLOWED_TEST_BINARIES = {
    "pytest", "pytest.exe",
    "python", "python.exe", "python3", "python3.exe", "py", "py.exe",
    "npm", "npm.cmd", "npm.exe",
    "npx", "npx.cmd", "npx.exe",
    "cargo", "cargo.exe",
    "go", "go.exe",
}

FORBIDDEN_SHELL_TOKENS = {
    "|", "&", ";", ">", "<", "`", "$", "\n", "\r", "&&", "||", ";;",
}

FORBIDDEN_DANGEROUS_COMMANDS = {
    "rm", "del", "rmdir", "rd", "format", "shutdown", "curl", "wget",
    "nc", "netcat", "bash", "sh", "powershell", "powershell.exe", "cmd", "cmd.exe",
}


def _extract_capabilities_extra(caps: Any) -> Dict[str, Any]:
    if not caps:
        return {}
    extra: Dict[str, Any] = {}
    for k, v in caps.__dict__.items():
        if k != "extra":
            extra[f"capability_{k}"] = v
    return extra


def _extract_real_invocation_id(result: AgentResult, handle: AgentHandle) -> Optional[str]:
    """从 AgentResult / AgentHandle 中提取真实非空 Invocation ID (严格校验成功状态与会话一致性)"""
    if not isinstance(result, AgentResult) or not isinstance(handle, AgentHandle):
        return None
    if result.status != AgentStatus.SUCCESS:
        return None
    if result.session_id != handle.session_id:
        return None
    if not result.is_real_host or not handle.is_real_host:
        return None

    if getattr(result, "host_invocation_id", None):
        return str(result.host_invocation_id).strip()

    if getattr(result, "partial_results", None):
        for pr in result.partial_results:
            if hasattr(pr, "get"):
                inv = pr.get("invocation_id") or pr.get("host_invocation_id")
                if inv:
                    return str(inv).strip()

    return None


def _validate_qa_test_command(test_cmd: str, worktree_dir: str) -> Tuple[bool, Optional[str], List[str]]:
    """严格校验 QA 测试命令族、受控参数与路径边界（P2 门禁）"""
    if not test_cmd or not test_cmd.strip():
        return False, "QA test command cannot be empty", []

    for token in FORBIDDEN_SHELL_TOKENS:
        if token in test_cmd:
            return False, f"Dangerous shell operator '{token}' forbidden in QA test command", []

    try:
        args = shlex.split(test_cmd, posix=(sys.platform != "win32"))
    except Exception as e:
        return False, f"Failed to parse test command: {e}", []

    # Windows shlex(posix=False) preserves surrounding quotes.  Normalize only
    # balanced outer quotes; embedded content and shell operators remain subject
    # to the existing fail-closed checks above.
    args = [
        arg[1:-1] if len(arg) >= 2 and arg[0] == arg[-1] and arg[0] in {'"', "'"} else arg
        for arg in args
    ]

    if not args:
        return False, "QA test command parsed to empty argument list", []

    raw_bin = args[0]
    base_bin = os.path.basename(raw_bin).lower()

    if os.path.isabs(raw_bin):
        is_allowed_bin = bool(
            sys.executable
            and os.path.normcase(os.path.realpath(raw_bin))
            == os.path.normcase(os.path.realpath(sys.executable))
        )
    else:
        is_allowed_bin = base_bin in ALLOWED_TEST_BINARIES
    if not is_allowed_bin:
        return False, f"Binary '{raw_bin}' is not in allowed test runner whitelist ({sorted(ALLOWED_TEST_BINARIES)})", []

    if base_bin in FORBIDDEN_DANGEROUS_COMMANDS:
        return False, f"Command '{base_bin}' is strictly forbidden in QA test execution", []

    python_bins = {"python", "python.exe", "python3", "python3.exe", "py", "py.exe"}
    if sys.executable:
        python_bins.add(os.path.basename(sys.executable).lower())
    if base_bin in python_bins:
        if len(args) < 3 or args[1:3] != ["-m", "pytest"]:
            return False, "Python QA commands must use the controlled 'python -m pytest' entry point", []
        runner_args = args[3:]
    elif base_bin in {"pytest", "pytest.exe"}:
        runner_args = args[1:]
    elif base_bin in {"npm", "npm.cmd", "npm.exe"}:
        if args[1:2] == ["test"]:
            runner_args = args[2:]
        elif args[1:3] == ["run", "test"]:
            runner_args = args[3:]
        elif args[1:3] == ["run", "build"]:
            runner_args = args[3:]
        else:
            return False, "npm QA commands are limited to 'npm test', 'npm run test', or 'npm run build'", []
    elif base_bin in {"npx", "npx.cmd", "npx.exe"}:
        if len(args) < 2 or args[1].lower() not in {"jest", "vitest", "mocha"}:
            return False, "npx QA commands are limited to jest, vitest, or mocha", []
        runner_args = args[2:]
    elif base_bin in {"cargo", "cargo.exe", "go", "go.exe"}:
        if args[1:2] != ["test"]:
            return False, f"{base_bin} QA commands must use the 'test' subcommand", []
        runner_args = args[2:]
    else:
        return False, f"Unsupported QA command shape for '{base_bin}'", []

    forbidden_runner_flags = {
        "-p", "--pyargs", "-c", "--config-file", "--rootdir", "--confcutdir",
        "--override-ini", "-o", "--basetemp", "--ignore", "--ignore-glob",
    }
    for index, arg in enumerate(runner_args):
        lowered = arg.lower()
        flag_name = lowered.split("=", 1)[0]
        if flag_name in forbidden_runner_flags:
            return False, f"QA runner option '{arg}' is forbidden by the production allowlist", []
        if index > 0 and runner_args[index - 1].lower() in forbidden_runner_flags:
            return False, f"QA runner option value '{arg}' is forbidden by the production allowlist", []

    norm_worktree = os.path.normcase(os.path.realpath(worktree_dir))
    for arg in runner_args:
        if arg.startswith("-"):
            continue
        looks_like_path = (
            os.path.isabs(arg)
            or "/" in arg
            or "\\" in arg
            or arg.endswith((".py", ".js", ".ts", ".java", ".go", ".rs"))
            or os.path.exists(os.path.join(worktree_dir, arg))
        )
        if not looks_like_path:
            continue
        full_arg_path = os.path.normcase(os.path.realpath(os.path.join(worktree_dir, arg)))
        try:
            inside = os.path.commonpath([norm_worktree, full_arg_path]) == norm_worktree
        except ValueError:
            inside = False
        if not inside:
            return False, f"Argument path '{arg}' escapes worktree boundary '{worktree_dir}'", []

    return True, None, args


def _validate_reviewer_schema_builtin(data: Any) -> Tuple[bool, Optional[str]]:
    """纯内置严格 JSON Schema 验证器（零外部依赖，DEF-T0061-1）"""
    if not isinstance(data, dict):
        return False, "Root must be a JSON object"
    required_fields = [
        "task_id",
        "baseline_commit",
        "candidate_commit",
        "session_id",
        "review_request_id",
        "decision",
        "defects",
        "summary",
    ]
    for field in required_fields:
        if field not in data:
            return False, f"Missing required field '{field}'"

    if not isinstance(data["task_id"], str) or not data["task_id"].strip():
        return False, "task_id must be a non-empty string"
    if not isinstance(data["baseline_commit"], str) or not re.match(r"^[0-9a-f]{40}$", data["baseline_commit"]):
        return False, "baseline_commit must be a 40-hex character SHA"
    if not isinstance(data["candidate_commit"], str) or not re.match(r"^[0-9a-f]{40}$", data["candidate_commit"]):
        return False, "candidate_commit must be a 40-hex character SHA"
    if not isinstance(data["session_id"], str) or not data["session_id"].strip():
        return False, "session_id must be a non-empty string"
    if not isinstance(data["review_request_id"], str) or not data["review_request_id"].strip():
        return False, "review_request_id must be a non-empty string"
    if data["decision"] not in ("PASS", "REJECT"):
        return False, f"decision must be 'PASS' or 'REJECT', got '{data['decision']}'"
    if not isinstance(data["defects"], (list, tuple)):
        return False, "defects must be an array"
    for d in data["defects"]:
        if not isinstance(d, dict):
            return False, "defects items must be objects"
        if not d.get("defect_id") or not d.get("severity") or not d.get("description"):
            return False, "defect item missing defect_id, severity, or description"
        if d.get("severity") not in ("P0", "P1", "P2", "P3"):
            return False, f"defect severity must be P0, P1, P2, or P3, got '{d.get('severity')}'"
    if not isinstance(data["summary"], str):
        return False, "summary must be a string"
    if set(data) != set(required_fields):
        return False, "Reviewer output contains unknown additional properties"
    return True, None


def _validate_qa_schema_builtin(data: Any) -> Tuple[bool, Optional[str]]:
    """Strictly validate QA identity, coverage, negative cases, and command evidence."""
    if not isinstance(data, dict):
        return False, "Root must be a JSON object"
    required_fields = list(QA_JSON_SCHEMA["required"])
    actual_fields = set(data)
    if actual_fields != set(required_fields):
        missing = sorted(set(required_fields) - actual_fields)
        unknown = sorted(actual_fields - set(required_fields))
        return False, f"QA output fields mismatch; missing={missing}, unknown={unknown}"

    non_empty_strings = (
        "task_id",
        "baseline_commit",
        "candidate_commit",
        "session_id",
        "qa_request_id",
        "acceptance_criteria_hash",
        "summary",
    )
    for field_name in non_empty_strings:
        if not isinstance(data[field_name], str) or not data[field_name].strip():
            return False, f"{field_name} must be a non-empty string"
    if not re.fullmatch(r"[0-9a-f]{40}", data["baseline_commit"]):
        return False, "baseline_commit must be a 40-hex character SHA"
    if not re.fullmatch(r"[0-9a-f]{40}", data["candidate_commit"]):
        return False, "candidate_commit must be a 40-hex character SHA"
    if not re.fullmatch(r"[0-9a-f]{64}", data["acceptance_criteria_hash"]):
        return False, "acceptance_criteria_hash must be a 64-hex character SHA"
    if data["decision"] not in ("PASS", "FAIL"):
        return False, "decision must be 'PASS' or 'FAIL'"

    item_contracts = {
        "acceptance_coverage": ({"criterion_id", "status", "evidence"}, "criterion_id"),
        "test_commands": ({"command", "exit_code", "summary"}, "command"),
        "negative_scenarios": ({"name", "status", "evidence"}, "name"),
    }
    for collection_name, (allowed_fields, identity_field) in item_contracts.items():
        value = data[collection_name]
        if not isinstance(value, list):
            return False, f"{collection_name} must be an array"
        for item in value:
            if not isinstance(item, dict) or set(item) != allowed_fields:
                return False, f"{collection_name} items must contain only {sorted(allowed_fields)}"
            if not isinstance(item[identity_field], str) or not item[identity_field].strip():
                return False, f"{collection_name}.{identity_field} must be a non-empty string"
            if collection_name == "test_commands":
                if not isinstance(item["exit_code"], int) or isinstance(item["exit_code"], bool):
                    return False, "test_commands.exit_code must be an integer"
                if not isinstance(item["summary"], str) or not item["summary"].strip():
                    return False, "test_commands.summary must be a non-empty string"
            else:
                if item["status"] not in ("PASS", "FAIL"):
                    return False, f"{collection_name}.status must be PASS or FAIL"
                if not isinstance(item["evidence"], str) or not item["evidence"].strip():
                    return False, f"{collection_name}.evidence must be a non-empty string"

    if not isinstance(data["uncovered_risks"], list) or any(
        not isinstance(item, str) or not item.strip() for item in data["uncovered_risks"]
    ):
        return False, "uncovered_risks must be an array of non-empty strings"
    if not isinstance(data["defects"], list):
        return False, "defects must be an array"
    for defect in data["defects"]:
        if not isinstance(defect, dict):
            return False, "defects items must be objects"
        if not defect.get("defect_id") or not defect.get("severity") or not defect.get("description"):
            return False, "defect item missing defect_id, severity, or description"
        if defect["severity"] not in ("P0", "P1", "P2", "P3"):
            return False, "defect severity must be P0, P1, P2, or P3"
    return True, None


def create_default_registry(context_id: str = "prod_runner") -> AdapterRegistry:
    """创建并预注册默认 Codex 与 Antigravity 适配器的注册表"""
    reg = AdapterRegistry(context_id=context_id)
    try:
        codex_manifest = create_codex_cli_manifest(adapter_id="codex_cli", verified_version="0.149.0")
        codex_adapter = CodexCliAdapter(is_real_host=True, default_approval_policy="on-request")
        reg.register(codex_adapter, codex_manifest)
    except Exception:
        pass

    try:
        ag_manifest = create_antigravity_manifest(adapter_id="antigravity", verified_version="1.1.22")
        ag_adapter = AntigravityAdapter(is_real_host=True, verification_level=VerificationLevel.CLI_VERIFIED)
        reg.register(ag_adapter, ag_manifest)
    except Exception:
        pass
    return reg


def _execution_options_from_spec(spec: TaskExecutionSpec) -> Dict[str, Any]:
    """Persist effective runtime choices so resume cannot silently change hosts or gates."""
    return {
        "builder_adapter_id": spec.builder_adapter_id,
        "reviewer_adapter_id": spec.reviewer_adapter_id,
        "qa_adapter_id": spec.qa_adapter_id,
        "workspace_mode": spec.workspace_mode,
        "worktree_root": spec.worktree_root,
        "test_commands": list(spec.test_commands),
        "builder_timeout_seconds": spec.builder_timeout_seconds,
        "reviewer_timeout_seconds": spec.reviewer_timeout_seconds,
        "qa_timeout_seconds": spec.qa_timeout_seconds,
        "max_review_cycles": spec.max_review_cycles,
        "max_qa_cycles": spec.max_qa_cycles,
        "max_total_attempts": spec.max_total_attempts,
        "total_wall_clock_timeout_seconds": spec.total_wall_clock_timeout_seconds,
    }


class ProductionRunnerError(Exception):
    pass


class RunnerCancelledError(ProductionRunnerError):
    pass


class ProductionRunner:
    """通用生产级自动编排 Runner 内核"""

    def __init__(
        self,
        registry: Optional[AdapterRegistry] = None,
        evidence_store: Optional[EvidenceStore] = None,
        evidence_gate: Optional[EvidenceGate] = None,
        checkpoint_store: Optional[RunnerCheckpointStore] = None,
        worktree_manager: Optional[WorktreeManager] = None,
        progress_callback: Optional[Callable[[Mapping[str, Any]], None]] = None,
    ):
        self.registry = registry or create_default_registry()
        self.evidence_store = evidence_store
        self.evidence_gate = evidence_gate
        self.checkpoint_store = checkpoint_store or RunnerCheckpointStore()
        self.worktree_manager = worktree_manager
        self.progress_callback = progress_callback
        self._active_handles: Dict[str, AgentHandle] = {}
        self._lock = threading.Lock()

    def _emit_progress(
        self,
        *,
        task_id: str,
        state: str,
        role: str,
        event: str,
        message: str,
        candidate_commit: Optional[str] = None,
        cycle: Optional[int] = None,
    ) -> None:
        """Publish a bounded, non-secret stage event without affecting execution."""
        if self.progress_callback is None:
            return
        payload: Dict[str, Any] = {
            "event": event,
            "task_id": task_id,
            "state": state,
            "role": role,
            "message": message,
            "timestamp": time.time(),
        }
        if candidate_commit:
            payload["candidate_commit"] = candidate_commit
        if cycle is not None:
            payload["cycle"] = cycle
        try:
            self.progress_callback(payload)
        except Exception:
            # Telemetry is informative only and must never become a release gate.
            pass

    @staticmethod
    def _record_pre_granted_approval(adapter: Any, request: AgentRequest, approved: bool) -> None:
        """Record one exact outer-host approval when the CLI user explicitly granted it."""
        if not approved:
            return
        recorder = getattr(adapter, "record_out_of_band_approval", None)
        if callable(recorder):
            recorder(request)

    def _is_cancellation_requested(self, task_id: str) -> bool:
        try:
            data = self.checkpoint_store.query_status(task_id)
        except Exception:
            return False
        return bool(data and data.get("state") == RunnerState.CANCELLED.value)

    def _wait_for_result_cancellable(
        self,
        adapter: Any,
        handle: AgentHandle,
        timeout_seconds: float,
        task_id: str,
    ) -> AgentResult:
        """在原 Runner 进程中轮询跨进程取消标记，并由持有真实句柄的 Adapter 取消 Host。"""
        result_box: Dict[str, Any] = {}
        done = threading.Event()

        def _wait() -> None:
            try:
                result_box["result"] = adapter.wait_for_result(handle, timeout_seconds=timeout_seconds)
            except BaseException as exc:
                result_box["error"] = exc
            finally:
                done.set()

        timeout_seconds = max(0.001, float(timeout_seconds))
        deadline = time.monotonic() + timeout_seconds
        waiter = threading.Thread(target=_wait, name=f"yy-flow-wait-{task_id}", daemon=True)
        waiter.start()
        while not done.wait(min(0.1, max(0.001, deadline - time.monotonic()))):
            if self._is_cancellation_requested(task_id):
                try:
                    adapter.cancel_agent(handle)
                finally:
                    raise RunnerCancelledError(f"Task {task_id} was cancelled by user command.")
            if time.monotonic() >= deadline:
                try:
                    adapter.cancel_agent(handle)
                finally:
                    raise AgentTimeoutError(
                        f"Host session '{handle.session_id}' exceeded Runner deadline after {timeout_seconds}s"
                    )

        if self._is_cancellation_requested(task_id):
            try:
                adapter.cancel_agent(handle)
            finally:
                raise RunnerCancelledError(f"Task {task_id} was cancelled by user command.")
        if "error" in result_box:
            raise result_box["error"]
        result = result_box.get("result")
        if not isinstance(result, AgentResult):
            raise ProductionRunnerError("Host adapter returned an invalid AgentResult (Fail-Closed).")
        return result

    def _do_state_transition(
        self,
        authority_root: str,
        task_id: str,
        role: str,
        from_status: str,
        to_status: str,
        assignee: str,
        remarks: str,
        task_type: str = "A",
    ) -> Tuple[bool, Optional[str]]:
        """调用权威 transition_task.py 进行强门控与状态机流转"""
        transition_script = os.path.join(authority_root, "scripts", "transition_task.py")
        local_transition_script = os.path.realpath(
            os.path.join(os.path.dirname(__file__), "..", "..", "transition_task.py")
        )
        if not os.path.isfile(transition_script):
            transition_script = local_transition_script
        if not os.path.isfile(transition_script):
            try:
                transition_script = os.path.join(paths.skill_root(), "scripts", "transition_task.py")
            except Exception:
                pass
        if not os.path.isfile(transition_script):
            try:
                transition_script = os.path.join(paths.project_root(), "scripts", "transition_task.py")
            except Exception:
                pass

        if not os.path.isfile(transition_script):
            return False, "Unable to locate transition_task.py (Fail-Closed)."

        config_candidates = (
            os.path.join(authority_root, ".yy-flow", "user_data", "workflow.config.yaml"),
            os.path.join(authority_root, "user_data", "workflow.config.yaml"),
            os.path.join(authority_root, "config", "workflow.config.yaml"),
            paths.resolve_runtime_config(cwd=authority_root),
        )
        config_path = next((p for p in config_candidates if os.path.isfile(p)), None)
        if config_path is None:
            return False, (
                "Unable to locate authoritative workflow.config.yaml under "
                f"'{authority_root}' (Fail-Closed)."
            )

        env = dict(os.environ)
        env["YY_FLOW_PROJECT_ROOT"] = authority_root
        cmd = [
            sys.executable,
            transition_script,
            "--config", config_path,
            "--task-id", task_id,
            "--role", role,
            "--from-status", from_status,
            "--to-status", to_status,
            "--assignee", assignee,
            "--remarks", remarks,
            "--type", task_type,
        ]
        if to_status in ("已完成", "已验收"):
            import datetime
            cmd.extend(["--end-time", datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")])
        proc = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace", env=env)
        if proc.returncode != 0:
            return False, proc.stderr or proc.stdout
        return True, None

    def _get_git_commit(self, worktree_dir: str) -> str:
        """获取工作区当前 HEAD commit SHA"""
        return subprocess.check_output(
            ["git", "rev-parse", "HEAD"],
            cwd=worktree_dir,
            stderr=subprocess.DEVNULL,
            encoding="utf-8",
            errors="replace",
        ).strip()

    def _build_reviewer_diff_bundle(
        self,
        worktree_dir: str,
        baseline_commit: str,
        candidate_commit: str,
        max_chars: int = 180_000,
    ) -> str:
        """Build a bounded, immutable review payload without Reviewer tool access."""
        if not re.fullmatch(r"[0-9a-fA-F]{40}", baseline_commit or ""):
            raise ProductionRunnerError("Invalid baseline commit for Reviewer bundle (Fail-Closed).")
        if not re.fullmatch(r"[0-9a-fA-F]{40}", candidate_commit or ""):
            raise ProductionRunnerError("Invalid candidate commit for Reviewer bundle (Fail-Closed).")

        stat = subprocess.check_output(
            ["git", "diff", "--stat", baseline_commit, candidate_commit, "--"],
            cwd=worktree_dir,
            stderr=subprocess.STDOUT,
            encoding="utf-8",
            errors="replace",
        ).strip()
        patch = subprocess.check_output(
            ["git", "diff", "--no-ext-diff", "--unified=40", baseline_commit, candidate_commit, "--"],
            cwd=worktree_dir,
            stderr=subprocess.STDOUT,
            encoding="utf-8",
            errors="replace",
        )
        changed_output = subprocess.check_output(
            ["git", "diff", "--name-only", baseline_commit, candidate_commit, "--"],
            cwd=worktree_dir,
            stderr=subprocess.STDOUT,
            encoding="utf-8",
            errors="replace",
        )
        changed_files = tuple(line.strip() for line in changed_output.splitlines() if line.strip())

        symbols = []
        symbol_pattern = re.compile(
            r"^\+\s*(?:(?:async\s+)?def|class|function|const|let|var)\s+([A-Za-z_$][\w$]*)",
            re.MULTILINE,
        )
        for symbol in symbol_pattern.findall(patch):
            if len(symbol) >= 4 and symbol not in symbols:
                symbols.append(symbol)
            if len(symbols) >= 20:
                break

        reference_lines = []
        for symbol in symbols:
            proc = subprocess.run(
                ["git", "grep", "-n", "-F", symbol, candidate_commit, "--"],
                cwd=worktree_dir,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
            )
            if proc.returncode not in (0, 1):
                raise ProductionRunnerError(
                    f"Unable to build Reviewer repository impact index for symbol '{symbol}'."
                )
            matches = [line for line in proc.stdout.splitlines() if line.strip()][:8]
            if matches:
                reference_lines.append(f"[{symbol}]\n" + "\n".join(matches))

        surfaces = []
        lower_paths = tuple(path.lower().replace("\\", "/") for path in changed_files)
        if any("/api/" in f"/{path}" or "/schemas/" in f"/{path}" for path in lower_paths):
            surfaces.append("API/schema changed: verify runtime API, persisted contract, generated SDK, and every caller.")
        if any("/models/" in f"/{path}" or "migration" in path or "/database" in path for path in lower_paths):
            surfaces.append("Model/database changed: verify migration, existing-data compatibility, indexes, and isolation.")
        if any(path.endswith((".ts", ".tsx", ".js", ".jsx", ".vue")) for path in lower_paths):
            surfaces.append("Frontend changed: verify production build, generated clients, routing, and visible error states.")
        if any("auth" in path or "security" in path or "permission" in path for path in lower_paths):
            surfaces.append("Security boundary changed: verify unauthenticated, unauthorized, and cross-user negative paths.")
        if not surfaces:
            surfaces.append("Trace every changed public symbol to unchanged callers and relevant regression tests.")

        impact = (
            "CHANGED FILES:\n"
            + ("\n".join(changed_files) or "(none)")
            + "\n\nMANDATORY IMPACT CHECKS:\n- "
            + "\n- ".join(surfaces)
            + "\n\nREPOSITORY REFERENCE INDEX:\n"
            + ("\n\n".join(reference_lines) or "(no changed public symbols detected)")
        )
        bundle = f"DIFF STAT:\n{stat or '(no stat)'}\n\n{impact}\n\nPATCH:\n{patch}"
        if not patch.strip():
            raise ProductionRunnerError("Reviewer bundle contains no candidate diff (Fail-Closed).")
        if len(bundle) > max_chars:
            raise ProductionRunnerError(
                f"Reviewer bundle exceeds safe inline limit ({len(bundle)} > {max_chars}); user input required."
            )
        return bundle

    def _finalize_builder_candidate(self, worktree_dir: str, baseline_commit: str) -> str:
        """将真实 Builder 已完成但未提交的隔离工作区变更固化为候选提交。

        仅当真实 Builder 已成功返回、HEAD 仍等于基线且工作区确有变更时
        执行受控提交。已有候选提交却遗留脏文件、空变更、Git hook 失败
        或提交后仍不干净都会 Fail-Closed。
        """
        head = self._get_git_commit(worktree_dir)
        status = subprocess.check_output(
            ["git", "status", "--porcelain=v1", "--untracked-files=all"],
            cwd=worktree_dir,
            encoding="utf-8",
            errors="replace",
        ).strip()

        if head.lower() != baseline_commit.lower():
            if status:
                raise RuntimeError(
                    "Builder created a candidate commit but left additional uncommitted changes (Fail-Closed)."
                )
            return head

        if not status:
            raise RuntimeError(
                "Builder produced no new commits or working-tree changes (Fail-Closed)."
            )

        add_proc = subprocess.run(
            ["git", "add", "--all", "--", "."],
            cwd=worktree_dir,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
        if add_proc.returncode != 0:
            raise RuntimeError(f"Failed to stage Builder changes: {add_proc.stderr or add_proc.stdout}")

        commit_proc = subprocess.run(
            ["git", "commit", "-m", "feat(runner): 固化自动开发产物"],
            cwd=worktree_dir,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
        if commit_proc.returncode != 0:
            raise RuntimeError(f"Failed to commit Builder changes: {commit_proc.stderr or commit_proc.stdout}")

        candidate = self._get_git_commit(worktree_dir)
        if candidate.lower() == baseline_commit.lower():
            raise RuntimeError("Controlled Builder commit did not advance HEAD (Fail-Closed).")

        remaining = subprocess.check_output(
            ["git", "status", "--porcelain=v1", "--untracked-files=all"],
            cwd=worktree_dir,
            encoding="utf-8",
            errors="replace",
        ).strip()
        if remaining:
            raise RuntimeError("Builder worktree remained dirty after controlled commit (Fail-Closed).")
        return candidate

    def _verify_qa_immutability(self, worktree_dir: str, expected_candidate: str) -> Tuple[bool, Optional[str]]:
        """严格核验 QA 前后源码不可变性（P1 门禁）"""
        try:
            head_commit = subprocess.check_output(
                ["git", "rev-parse", "HEAD"],
                cwd=worktree_dir,
                stderr=subprocess.DEVNULL,
                encoding="utf-8",
                errors="replace",
            ).strip()
        except Exception as e:
            return False, f"Failed to inspect HEAD in QA worktree: {e}"

        if head_commit != expected_candidate:
            return False, f"QA modified HEAD or committed code illegally (expected {expected_candidate}, got {head_commit})! Fail-Closed."

        try:
            diff_out = subprocess.check_output(
                ["git", "diff", expected_candidate, "--", ".", ":!user_data", ":!.yy-flow"],
                cwd=worktree_dir,
                stderr=subprocess.DEVNULL,
                encoding="utf-8",
                errors="replace",
            ).strip()
            if diff_out:
                return False, f"QA modified tracked source code files relative to candidate commit! Fail-Closed. Diff: {diff_out[:200]}"
        except Exception as e:
            return False, f"Failed to check git diff in QA worktree: {e}"

        try:
            cached_diff = subprocess.check_output(
                ["git", "diff", "--cached", "--", ".", ":!user_data", ":!.yy-flow"],
                cwd=worktree_dir,
                stderr=subprocess.DEVNULL,
                encoding="utf-8",
                errors="replace",
            ).strip()
            if cached_diff:
                return False, "QA staged modifications to index! Fail-Closed."
        except Exception as e:
            return False, f"Failed to check git cached diff in QA worktree: {e}"

        ALLOWED_ROOT_PREFIXES = (
            "user_data",
            ".yy-flow",
        )
        ALLOWED_CACHE_COMPONENTS = {
            ".pytest_cache",
            "__pycache__",
            ".coverage",
            "htmlcov",
            ".tox",
            ".hypothesis",
            "test-results",
            "junit.xml",
        }
        try:
            status_out = subprocess.check_output(
                ["git", "status", "--porcelain", "--untracked-files=all"],
                cwd=worktree_dir,
                stderr=subprocess.DEVNULL,
                encoding="utf-8",
                errors="replace",
            )
            for line in status_out.splitlines():
                if not line.strip():
                    continue
                file_rel = line[3:].strip()
                if file_rel.startswith('"') and file_rel.endswith('"'):
                    file_rel = file_rel[1:-1]
                normalized_rel = file_rel.replace("\\", "/").rstrip("/")
                path_parts = tuple(part for part in normalized_rel.split("/") if part)
                is_safe_root = any(
                    normalized_rel == prefix or normalized_rel.startswith(prefix + "/")
                    for prefix in ALLOWED_ROOT_PREFIXES
                )
                is_safe_cache = (
                    is_safe_root
                    or any(part in ALLOWED_CACHE_COMPONENTS for part in path_parts)
                    or normalized_rel.endswith(".pyc")
                )
                if not is_safe_cache:
                    return False, f"QA created or modified untracked/source file '{file_rel}'! Code immutability boundary violated! Fail-Closed."
        except Exception as e:
            return False, f"Failed to check git status in QA worktree: {e}"

        return True, None

    def _parse_reviewer_structured_json(
        self,
        raw_output: str,
        task_id: str,
        baseline_commit: str,
        candidate_commit: str,
        session_id: str,
        invocation_id: str,
        review_request_id: str,
    ) -> ReviewerStructuredOutput:
        """严格解析 Reviewer 结构化 JSON 返回值并强制核验 Schema 与身份绑定（DEF-T0061-1）"""
        json_obj: Optional[Dict[str, Any]] = None
        cleaned = raw_output.strip()

        if cleaned.startswith("{") and cleaned.endswith("}"):
            try:
                json_obj = json.loads(cleaned)
            except Exception:
                json_obj = None

        if json_obj is None:
            match = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", raw_output, re.DOTALL)
            if match:
                try:
                    json_obj = json.loads(match.group(1))
                except Exception:
                    json_obj = None

        if json_obj is None:
            return ReviewerStructuredOutput(
                task_id=task_id,
                baseline_commit=baseline_commit,
                candidate_commit=candidate_commit,
                session_id=session_id,
                host_invocation_id=invocation_id,
                review_request_id=review_request_id,
                decision="REJECT",
                defects=(
                    {
                        "defect_id": f"DEF-{task_id}-SCHEMA-VIOLATION",
                        "severity": "P1",
                        "description": f"Reviewer did not return valid JSON according to schema: {raw_output[:200]}",
                    },
                ),
                summary="Reviewer response violated structured JSON schema.",
            )

        valid, err_msg = _validate_reviewer_schema_builtin(json_obj)
        if not valid:
            return ReviewerStructuredOutput(
                task_id=task_id,
                baseline_commit=baseline_commit,
                candidate_commit=candidate_commit,
                session_id=session_id,
                host_invocation_id=invocation_id,
                review_request_id=review_request_id,
                decision="REJECT",
                defects=(
                    {
                        "defect_id": f"DEF-{task_id}-SCHEMA-VIOLATION",
                        "severity": "P1",
                        "description": f"Reviewer JSON failed schema validation: {err_msg}",
                    },
                ),
                summary="Reviewer response violated structured JSON schema.",
            )

        mismatches = []
        if json_obj.get("task_id") != task_id:
            mismatches.append(f"task_id mismatch: expected '{task_id}', got '{json_obj.get('task_id')}'")
        if json_obj.get("baseline_commit") != baseline_commit:
            mismatches.append(f"baseline_commit mismatch: expected '{baseline_commit}', got '{json_obj.get('baseline_commit')}'")
        if json_obj.get("candidate_commit") != candidate_commit:
            mismatches.append(f"candidate_commit mismatch: expected '{candidate_commit}', got '{json_obj.get('candidate_commit')}'")
        if json_obj.get("session_id") != session_id:
            mismatches.append(f"session_id mismatch: expected '{session_id}', got '{json_obj.get('session_id')}'")
        if json_obj.get("review_request_id") != review_request_id:
            mismatches.append(
                "review_request_id mismatch: "
                f"expected '{review_request_id}', got '{json_obj.get('review_request_id')}'"
            )

        if mismatches:
            return ReviewerStructuredOutput(
                task_id=task_id,
                baseline_commit=baseline_commit,
                candidate_commit=candidate_commit,
                session_id=session_id,
                host_invocation_id=invocation_id,
                review_request_id=review_request_id,
                decision="REJECT",
                defects=(
                    {
                        "defect_id": f"DEF-{task_id}-IDENTITY-MISMATCH",
                        "severity": "P1",
                        "description": "; ".join(mismatches),
                    },
                ),
                summary="Identity binding mismatch in reviewer structured output",
            )

        decision = json_obj["decision"]
        raw_defects = json_obj.get("defects", [])
        if decision == "PASS" and raw_defects:
            return ReviewerStructuredOutput(
                task_id=task_id,
                baseline_commit=baseline_commit,
                candidate_commit=candidate_commit,
                session_id=session_id,
                host_invocation_id=invocation_id,
                review_request_id=review_request_id,
                decision="REJECT",
                defects=(
                    {
                        "defect_id": f"DEF-{task_id}-INVALID-PASS",
                        "severity": "P1",
                        "description": "Reviewer returned decision 'PASS' but provided non-empty defects list.",
                    },
                ),
                summary="Reviewer returned contradictory PASS decision with non-empty defects list.",
            )

        return ReviewerStructuredOutput(
            task_id=task_id,
            baseline_commit=baseline_commit,
            candidate_commit=candidate_commit,
            session_id=session_id,
            host_invocation_id=invocation_id,
            review_request_id=review_request_id,
            decision=decision,
            defects=tuple(raw_defects),
            summary=json_obj.get("summary", ""),
        )

    def _parse_qa_structured_json(
        self,
        raw_output: str,
        task_id: str,
        baseline_commit: str,
        candidate_commit: str,
        session_id: str,
        invocation_id: str,
        qa_request_id: str,
        acceptance_criteria_hash: str,
        expected_criterion_ids: Sequence[str],
        expected_test_commands: Sequence[str],
    ) -> QAStructuredOutput:
        """Parse QA output and fail closed on identity, coverage, or adversarial-test gaps."""

        def failed(defect_suffix: str, description: str) -> QAStructuredOutput:
            return QAStructuredOutput(
                task_id=task_id,
                baseline_commit=baseline_commit,
                candidate_commit=candidate_commit,
                session_id=session_id,
                host_invocation_id=invocation_id,
                qa_request_id=qa_request_id,
                acceptance_criteria_hash=acceptance_criteria_hash,
                decision="FAIL",
                uncovered_risks=(description,),
                defects=({
                    "defect_id": f"DEF-{task_id}-{defect_suffix}",
                    "severity": "P1",
                    "description": description,
                },),
                summary=description,
            )

        json_obj: Optional[Dict[str, Any]] = None
        cleaned = raw_output.strip()
        if cleaned.startswith("{") and cleaned.endswith("}"):
            try:
                json_obj = json.loads(cleaned)
            except Exception:
                json_obj = None
        if json_obj is None:
            match = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", raw_output, re.DOTALL)
            if match:
                try:
                    json_obj = json.loads(match.group(1))
                except Exception:
                    json_obj = None
        if json_obj is None:
            return failed("QA-SCHEMA-VIOLATION", "QA did not return a valid structured JSON object.")

        valid, error = _validate_qa_schema_builtin(json_obj)
        if not valid:
            return failed("QA-SCHEMA-VIOLATION", f"QA JSON failed schema validation: {error}")

        expected_identity = {
            "task_id": task_id,
            "baseline_commit": baseline_commit,
            "candidate_commit": candidate_commit,
            "session_id": session_id,
            "qa_request_id": qa_request_id,
            "acceptance_criteria_hash": acceptance_criteria_hash,
        }
        mismatches = [
            f"{name} mismatch: expected '{expected}', got '{json_obj.get(name)}'"
            for name, expected in expected_identity.items()
            if json_obj.get(name) != expected
        ]
        if mismatches:
            return failed("QA-IDENTITY-MISMATCH", "; ".join(mismatches))

        decision = json_obj["decision"]
        defects = tuple(json_obj["defects"])
        uncovered_risks = tuple(json_obj["uncovered_risks"])
        coverage = tuple(json_obj["acceptance_coverage"])
        command_reports = tuple(json_obj["test_commands"])
        negative_scenarios = tuple(json_obj["negative_scenarios"])

        if decision == "PASS":
            expected_ids = tuple(expected_criterion_ids)
            actual_ids = tuple(item["criterion_id"] for item in coverage)
            if len(actual_ids) != len(set(actual_ids)) or set(actual_ids) != set(expected_ids):
                return failed(
                    "QA-COVERAGE-GAP",
                    f"QA acceptance coverage must match every criterion exactly once; expected={list(expected_ids)}, actual={list(actual_ids)}",
                )
            if any(item["status"] != "PASS" for item in coverage):
                return failed("QA-COVERAGE-FAIL", "QA cannot PASS while an acceptance criterion is not PASS.")

            reported_commands = tuple(item["command"] for item in command_reports)
            reports_by_command = {item["command"]: item for item in command_reports}
            missing_commands = [command for command in expected_test_commands if command not in reports_by_command]
            unexpected_commands = [command for command in reported_commands if command not in expected_test_commands]
            duplicate_commands = len(reported_commands) != len(set(reported_commands))
            failed_commands = [
                command for command in expected_test_commands
                if command in reports_by_command and reports_by_command[command]["exit_code"] != 0
            ]
            if missing_commands or unexpected_commands or duplicate_commands or failed_commands:
                return failed(
                    "QA-COMMAND-GAP",
                    "QA command evidence must match every required command exactly once; "
                    f"missing={missing_commands}, unexpected={unexpected_commands}, "
                    f"duplicates={duplicate_commands}, failed={failed_commands}",
                )
            if not negative_scenarios:
                return failed("QA-NEGATIVE-GAP", "QA PASS requires at least one independently verified negative scenario.")
            if any(item["status"] != "PASS" for item in negative_scenarios):
                return failed("QA-NEGATIVE-FAIL", "QA cannot PASS while a negative scenario is not PASS.")
            if uncovered_risks:
                return failed("QA-UNCOVERED-RISK", "QA cannot PASS with uncovered risks: " + "; ".join(uncovered_risks))
            if defects:
                return failed("QA-INVALID-PASS", "QA returned PASS with non-empty defects.")
        elif not defects:
            return failed("QA-INVALID-FAIL", "QA returned FAIL without structured defects.")

        return QAStructuredOutput(
            task_id=task_id,
            baseline_commit=baseline_commit,
            candidate_commit=candidate_commit,
            session_id=session_id,
            host_invocation_id=invocation_id,
            qa_request_id=qa_request_id,
            acceptance_criteria_hash=acceptance_criteria_hash,
            decision=decision,
            acceptance_coverage=coverage,
            test_commands=command_reports,
            negative_scenarios=negative_scenarios,
            uncovered_risks=uncovered_risks,
            defects=defects,
            summary=json_obj["summary"],
        )

    def start(
        self,
        spec: TaskExecutionSpec,
        interactive_approval_cb: Optional[Callable[[str, Dict[str, Any]], bool]] = None,
        pre_granted_approval: bool = False,
    ) -> RunnerResult:
        """启动新任务的自动编排"""
        _validate_task_id(spec.task_id)
        if not verify_optimistic_concurrency(spec):
            return RunnerResult(
                success=False,
                state=RunnerState.FAILED.value,
                task_id=spec.task_id,
                message="Optimistic concurrency verification failed: task is in terminal status or modified concurrently.",
            )

        lock_handle, lock_file = self.checkpoint_store.acquire_runner_lock(spec.task_id)
        if lock_handle is None:
            return RunnerResult(
                success=False,
                state=RunnerState.FAILED.value,
                task_id=spec.task_id,
                message=f"Task {spec.task_id} is already locked by another running process.",
            )

        try:
            return self._execute_loop(
                spec,
                lock_tuple=(lock_handle, lock_file),
                interactive_approval_cb=interactive_approval_cb,
                existing_checkpoint=None,
                pre_granted_approval=pre_granted_approval,
            )
        finally:
            self.checkpoint_store.release_runner_lock((lock_handle, lock_file))

    def _execute_loop(
        self,
        spec: TaskExecutionSpec,
        lock_tuple: Tuple[Any, Optional[str]],
        interactive_approval_cb: Optional[Callable[[str, Dict[str, Any]], bool]] = None,
        existing_checkpoint: Optional[RunnerCheckpoint] = None,
        pre_granted_approval: bool = False,
    ) -> RunnerResult:
        start_wall_clock = time.time()
        project_root = spec.project_root
        task_id = spec.task_id
        current_board_status = spec.status_at_read

        # Fresh starts only accept the initial states. The actual claim is delayed
        # until the worktree and all adapters are ready, avoiding a dangling
        # 进行中 card when local orchestration prerequisites are missing.
        if existing_checkpoint is None:
            if current_board_status not in ("待开始", "进行中"):
                return RunnerResult(
                    success=False,
                    state=RunnerState.FAILED.value,
                    task_id=task_id,
                    message=(
                        f"Fresh start requires task status 待开始 or 进行中; got "
                        f"'{current_board_status}'. Use resume for a checkpointed task."
                    ),
                )

        if self.evidence_store is None:
            evidence_dir = os.path.join(project_root, "user_data", "runner_evidence")
            os.makedirs(evidence_dir, exist_ok=True)
            self.evidence_store = EvidenceStore(root_dir=evidence_dir)

        if self.evidence_gate is None:
            self.evidence_gate = EvidenceGate(store=self.evidence_store, project_root=project_root)

        orchestrator = Orchestrator(
            registry=self.registry,
            evidence_store=self.evidence_store,
            evidence_gate=self.evidence_gate,
            project_root=project_root,
        )

        worktree_dir = project_root
        worktree_branch = spec.baseline_branch
        if spec.workspace_mode == "branch":
            if self.worktree_manager is None:
                controlled_root = spec.worktree_root or os.path.join(paths.resolve_data_root(cwd=project_root), "worktrees")
                self.worktree_manager = WorktreeManager(controlled_root=controlled_root, target_repo_path=project_root)

            req = WorktreeRequest(
                project_id=spec.project_id,
                task_id=task_id,
                actor_role="BUILDER",
                host_session_id=f"sess_wt_{task_id}_{int(time.time())}",
                baseline_commit=spec.baseline_commit or "HEAD",
            )
            existing_desc = None
            try:
                for wt in self.worktree_manager.list_worktrees():
                    if getattr(wt, "request", None) and wt.request.task_id == task_id:
                        existing_desc = wt
                        break
            except Exception:
                existing_desc = None

            if existing_desc:
                worktree_dir = existing_desc.absolute_path
                worktree_branch = existing_desc.branch_name
            else:
                desc = self.worktree_manager.create_worktree(req)
                worktree_dir = desc.absolute_path
                worktree_branch = desc.branch_name

        checkpoint = existing_checkpoint or RunnerCheckpoint(
            task_id=task_id,
            project_id=spec.project_id,
            state=RunnerState.WORKTREE_READY.value,
            current_role="BUILDER",
            candidate_commit=None,
            candidate_generation=0,
            review_cycle=0,
            qa_cycle=0,
            total_attempts=0,
            worktree_path=worktree_dir,
            worktree_branch=worktree_branch,
            execution_options=_execution_options_from_spec(spec),
        )
        self.checkpoint_store.save_checkpoint(checkpoint)
        self._emit_progress(
            task_id=task_id,
            state=RunnerState.WORKTREE_READY.value,
            role="RUNNER",
            event="worktree_ready",
            message=f"Workspace ready on {worktree_branch}",
        )

        builder_adapter = self.registry.get(spec.builder_adapter_id)
        reviewer_adapter = self.registry.get(spec.reviewer_adapter_id)
        qa_adapter = self.registry.get(spec.qa_adapter_id)

        if not builder_adapter or not reviewer_adapter or not qa_adapter:
            return RunnerResult(
                success=False,
                state=RunnerState.FAILED.value,
                task_id=task_id,
                message=f"Missing adapter: builder={spec.builder_adapter_id}, reviewer={spec.reviewer_adapter_id}, qa={spec.qa_adapter_id}",
            )

        # PM creates/assigns the A-class card; once all local prerequisites are
        # ready, Runner legally claims it as DEV immediately before host dispatch.
        if existing_checkpoint is None and current_board_status == "待开始":
            claimed, claim_error = self._do_state_transition(
                spec.authority_root,
                task_id=task_id,
                role="DEV",
                from_status="待开始",
                to_status="进行中",
                assignee="李开发",
                remarks="Production Runner 启动并由 Builder 合法领取任务",
                task_type=spec.task_type,
            )
            if not claimed:
                return RunnerResult(
                    success=False,
                    state=RunnerState.FAILED.value,
                    task_id=task_id,
                    message=f"Failed to claim waiting task before Builder dispatch: {claim_error}",
                )
            current_board_status = "进行中"

        candidate_commit = checkpoint.candidate_commit
        candidate_generation = checkpoint.candidate_generation
        review_cycle = checkpoint.review_cycle
        qa_cycle = checkpoint.qa_cycle
        total_attempts = checkpoint.total_attempts
        evidence_ids: List[str] = list(checkpoint.evidence_ids)
        defects_history: List[Dict[str, Any]] = list(checkpoint.defects_history)
        start_role = checkpoint.current_role if existing_checkpoint else "BUILDER"
        skip_builder = (start_role in ("REVIEWER", "QA") and candidate_commit is not None)
        skip_reviewer = (start_role == "QA" and candidate_commit is not None)

        while True:
            total_attempts += 1
            if total_attempts > spec.max_total_attempts:
                return RunnerResult(
                    success=False,
                    state=RunnerState.NEEDS_USER_INPUT.value,
                    task_id=task_id,
                    candidate_commit=candidate_commit,
                    candidate_generation=candidate_generation,
                    evidence_ids=tuple(evidence_ids),
                    message=f"Task exceeded max total attempts ({spec.max_total_attempts}). Paused at NEEDS_USER_INPUT.",
                    diagnostics={"total_attempts": total_attempts, "defects": defects_history},
                )

            if time.time() - start_wall_clock > spec.total_wall_clock_timeout_seconds:
                return RunnerResult(
                    success=False,
                    state=RunnerState.NEEDS_USER_INPUT.value,
                    task_id=task_id,
                    candidate_commit=candidate_commit,
                    candidate_generation=candidate_generation,
                    evidence_ids=tuple(evidence_ids),
                    message=f"Task exceeded wall clock timeout ({spec.total_wall_clock_timeout_seconds}s). Paused at NEEDS_USER_INPUT.",
                    diagnostics={"elapsed": time.time() - start_wall_clock},
                )

            # ==========================================
            # STAGE 1: CODEX BUILDER
            # ==========================================
            if not skip_builder:
                sess_builder = f"sess_builder_runner_{task_id.lower()}_{int(time.time()*1000)}"
                candidate_generation += 1

                checkpoint = RunnerCheckpoint(
                    task_id=task_id,
                    project_id=spec.project_id,
                    state=RunnerState.BUILDING.value,
                    current_role="BUILDER",
                    candidate_commit=candidate_commit,
                    candidate_generation=candidate_generation,
                    review_cycle=review_cycle,
                    qa_cycle=qa_cycle,
                    total_attempts=total_attempts,
                    worktree_path=worktree_dir,
                    worktree_branch=worktree_branch,
                    builder_session_id=sess_builder,
                    evidence_ids=tuple(evidence_ids),
                    execution_options=_execution_options_from_spec(spec),
                    defects_history=tuple(defects_history),
                )
                self.checkpoint_store.save_checkpoint(checkpoint)

                if defects_history:
                    last_defect = defects_history[-1]
                    builder_prompt = (
                        f"Task {task_id}: Previous attempt was rejected by {last_defect.get('role', 'REVIEWER')}.\n"
                        f"Defects: {json.dumps(last_defect.get('defects', []), ensure_ascii=False)}\n"
                        f"Summary: {last_defect.get('summary', '')}\n"
                        f"Please fix the defects in {worktree_dir}, verify your changes, and make a git commit."
                    )
                else:
                    custom_prompts = getattr(spec, "custom_prompts", {}) or {}
                    builder_prompt = (
                        custom_prompts.get("BUILDER")
                        or f"Task {task_id}: {spec.task_name}\n"
                        f"Requirements:\n{spec.requirement_text}\n"
                        f"Acceptance Criteria:\n{spec.acceptance_criteria}\n"
                        f"Please implement the requirements in {worktree_dir}, write unit tests, verify your implementation, and make a git commit."
                    )

                builder_request = AgentRequest(
                    session_id=sess_builder,
                    prompt=builder_prompt,
                    role="BUILDER",
                    workspace_dir=worktree_dir,
                    timeout_seconds=float(spec.builder_timeout_seconds),
                    extra_context={
                        "sandbox": True,
                        "sandbox_mode": "workspace-write",
                        "permission_boundary": "workspace_write",
                        "operation_intent": "Implement workspace-local changes and create one candidate git commit",
                        "worktree_dir": worktree_dir,
                        "project_id": spec.project_id,
                        "pre_granted_approval": pre_granted_approval,
                        "approve_for_me": pre_granted_approval,
                    },
                )
                self._emit_progress(
                    task_id=task_id,
                    state=RunnerState.BUILDING.value,
                    role="BUILDER",
                    event="stage_started",
                    message=f"Dispatching Builder via {spec.builder_adapter_id}",
                    cycle=candidate_generation,
                )

                try:
                    self._record_pre_granted_approval(
                        builder_adapter,
                        builder_request,
                        pre_granted_approval,
                    )
                    builder_handle = builder_adapter.dispatch_agent(builder_request)
                    with self._lock:
                        self._active_handles[task_id] = builder_handle

                    builder_result = self._wait_for_result_cancellable(
                        builder_adapter,
                        builder_handle,
                        timeout_seconds=float(spec.builder_timeout_seconds),
                        task_id=task_id,
                    )
                except RunnerCancelledError as ce:
                    return RunnerResult(
                        success=True,
                        state=RunnerState.CANCELLED.value,
                        task_id=task_id,
                        candidate_commit=candidate_commit,
                        candidate_generation=candidate_generation,
                        evidence_ids=tuple(evidence_ids),
                        message=str(ce),
                    )
                except AgentNotSupportedError as se:
                    with self._lock:
                        self._active_handles.pop(task_id, None)
                    checkpoint = RunnerCheckpoint(
                        task_id=task_id,
                        project_id=spec.project_id,
                        state=RunnerState.APPROVAL_REQUIRED.value,
                        current_role="BUILDER",
                        candidate_commit=candidate_commit,
                        candidate_generation=candidate_generation,
                        review_cycle=review_cycle,
                        qa_cycle=qa_cycle,
                        total_attempts=total_attempts,
                        worktree_path=worktree_dir,
                        worktree_branch=worktree_branch,
                        builder_session_id=sess_builder,
                        evidence_ids=tuple(evidence_ids),
                        execution_options=_execution_options_from_spec(spec),
                        defects_history=tuple(defects_history),
                        approval_reason=str(se),
                        last_error="Permission approval required",
                    )
                    self.checkpoint_store.save_checkpoint(checkpoint)
                    return RunnerResult(
                        success=False,
                        state=RunnerState.APPROVAL_REQUIRED.value,
                        task_id=task_id,
                        candidate_commit=candidate_commit,
                        candidate_generation=candidate_generation,
                        evidence_ids=tuple(evidence_ids),
                        message=f"Execution paused at APPROVAL_REQUIRED: {se}",
                        diagnostics={"approval_reason": str(se)},
                    )
                except Exception as e:
                    with self._lock:
                        self._active_handles.pop(task_id, None)
                    return RunnerResult(
                        success=False,
                        state=RunnerState.FAILED.value,
                        task_id=task_id,
                        candidate_commit=candidate_commit,
                        candidate_generation=candidate_generation,
                        message=f"Builder dispatch/wait failed: {e}",
                        diagnostics={"error": str(e)},
                    )
                finally:
                    with self._lock:
                        self._active_handles.pop(task_id, None)

                if builder_result.status != AgentStatus.SUCCESS:
                    return RunnerResult(
                        success=False,
                        state=RunnerState.FAILED.value,
                        task_id=task_id,
                        message=f"Builder execution failed with status: {builder_result.status.value}",
                    )
                if builder_result.session_id != builder_handle.session_id:
                    return RunnerResult(
                        success=False,
                        state=RunnerState.FAILED.value,
                        task_id=task_id,
                        message="Builder session ID mismatch (Fail-Closed).",
                    )
                if not builder_result.is_real_host:
                    return RunnerResult(
                        success=False,
                        state=RunnerState.FAILED.value,
                        task_id=task_id,
                        message="Builder result is not from a real host (Fail-Closed).",
                    )

                inv_builder = _extract_real_invocation_id(builder_result, builder_handle)
                if not inv_builder:
                    return RunnerResult(
                        success=False,
                        state=RunnerState.FAILED.value,
                        task_id=task_id,
                        message="Builder host did not produce a valid canonical invocation identity (Fail-Closed).",
                    )

                try:
                    candidate_commit = self._finalize_builder_candidate(
                        worktree_dir,
                        spec.baseline_commit,
                    )
                except Exception as e:
                    return RunnerResult(
                        success=False,
                        state=RunnerState.FAILED.value,
                        task_id=task_id,
                        message=f"Failed to finalize candidate commit from Builder worktree: {e}",
                    )

                if not re.match(r"^[0-9a-f]{40}$", candidate_commit):
                    return RunnerResult(
                        success=False,
                        state=RunnerState.FAILED.value,
                        task_id=task_id,
                        message=f"Invalid candidate commit SHA: '{candidate_commit}' (Fail-Closed).",
                    )

                if spec.baseline_commit and candidate_commit.lower() == spec.baseline_commit.lower():
                    return RunnerResult(
                        success=False,
                        state=RunnerState.FAILED.value,
                        task_id=task_id,
                        message="Builder produced no new commits on top of baseline commit (Candidate == Baseline). Fail-Closed.",
                    )
                self._emit_progress(
                    task_id=task_id,
                    state=RunnerState.BUILDING.value,
                    role="BUILDER",
                    event="stage_completed",
                    message="Builder produced a clean candidate commit",
                    candidate_commit=candidate_commit,
                    cycle=candidate_generation,
                )

                builder_evidence_id = f"evi_builder_{task_id.lower()}_{int(time.time()*1000)}"
                builder_caps = builder_adapter.detect_capabilities()
                b_meta = EvidenceMetadata(
                    project_id=spec.project_id,
                    task_id=task_id,
                    actor_role="BUILDER",
                    host_id=spec.builder_adapter_id,
                    adapter=spec.builder_adapter_id,
                    host_session_id=sess_builder,
                    host_invocation_id=inv_builder,
                    is_real_host=builder_result.is_real_host,
                    workspace_mode="workspace_write",
                    transition_from="BUILDING",
                    transition_to="REVIEWING",
                    created_at=time.time(),
                    extra=_extract_capabilities_extra(builder_caps),
                )
                b_record = EvidenceRecord(
                    evidence_id=builder_evidence_id,
                    evidence_type=EvidenceType.TASK_TRANSITION,
                    baseline_commit=spec.baseline_commit,
                    result_commit=candidate_commit,
                    artifacts=(),
                    metadata=b_meta,
                )
                self.evidence_store.append(b_record)
                try:
                    self.evidence_gate.validate_evidence(
                        builder_evidence_id,
                        EvidenceValidationContext(
                            project_id=spec.project_id,
                            task_id=task_id,
                            actor_role="BUILDER",
                            transition_from="BUILDING",
                            transition_to="REVIEWING",
                            baseline_commit=spec.baseline_commit,
                            result_commit=candidate_commit,
                            expected_invocation_id=inv_builder,
                            expected_adapter=spec.builder_adapter_id,
                            expected_workspace_mode="workspace_write",
                            expected_evidence_type=EvidenceType.TASK_TRANSITION,
                            host_handle=builder_handle,
                            expected_capabilities=builder_caps,
                            agent_result=builder_result,
                        ),
                    )
                except Exception as gate_error:
                    return RunnerResult(
                        success=False,
                        state=RunnerState.FAILED.value,
                        task_id=task_id,
                        candidate_commit=candidate_commit,
                        message=f"Builder EvidenceGate validation failed: {gate_error}",
                    )
                evidence_ids.append(builder_evidence_id)

                # 状态机推进: 进行中 -> 审查中 (DEV)
                if current_board_status == "进行中":
                    ok_trans, err_trans = self._do_state_transition(
                        spec.authority_root, task_id, "DEV", "进行中", "审查中", "周审查",
                        f"Codex Builder 完成开发，候选提交: {candidate_commit}", spec.task_type
                    )
                    if not ok_trans:
                        return RunnerResult(
                            success=False, state=RunnerState.FAILED.value, task_id=task_id,
                            message=f"Failed to transition state to 审查中: {err_trans}"
                        )
                    current_board_status = "审查中"
            else:
                skip_builder = False

            # ==========================================
            # STAGE 2: ANTIGRAVITY REVIEWER
            # ==========================================
            if not skip_reviewer:
                if not verify_optimistic_concurrency(
                    spec,
                    enforce_status=False,
                    enforce_version=False,
                ):
                    return RunnerResult(
                        success=False,
                        state=RunnerState.NEEDS_USER_INPUT.value,
                        task_id=task_id,
                        candidate_commit=candidate_commit,
                        evidence_ids=tuple(evidence_ids),
                        message="Task requirements or acceptance criteria changed before Reviewer dispatch.",
                    )
                review_cycle += 1
                sess_reviewer = f"sess_reviewer_runner_{task_id.lower()}_{int(time.time()*1000)}"
                review_request_id = f"rev_req_{uuid.uuid4().hex}"

                checkpoint = RunnerCheckpoint(
                    task_id=task_id,
                    project_id=spec.project_id,
                    state=RunnerState.REVIEWING.value,
                    current_role="REVIEWER",
                    candidate_commit=candidate_commit,
                    candidate_generation=candidate_generation,
                    review_cycle=review_cycle,
                    qa_cycle=qa_cycle,
                    total_attempts=total_attempts,
                    worktree_path=worktree_dir,
                    worktree_branch=worktree_branch,
                    builder_session_id=sess_builder if "sess_builder" in locals() else None,
                    reviewer_session_id=sess_reviewer,
                    evidence_ids=tuple(evidence_ids),
                    execution_options=_execution_options_from_spec(spec),
                    defects_history=tuple(defects_history),
                )
                self.checkpoint_store.save_checkpoint(checkpoint)

                try:
                    reviewer_diff_bundle = self._build_reviewer_diff_bundle(
                        worktree_dir,
                        spec.baseline_commit,
                        candidate_commit,
                    )
                except Exception as exc:
                    return RunnerResult(
                        success=False,
                        state=RunnerState.NEEDS_USER_INPUT.value,
                        task_id=task_id,
                        candidate_commit=candidate_commit,
                        evidence_ids=tuple(evidence_ids),
                        message=f"Unable to create bounded Reviewer payload: {exc}",
                    )

                reviewer_prompt = (
                    (getattr(spec, 'custom_prompts', {}) or {}).get("REVIEWER")
                    or f"You are Antigravity Reviewer for Task {task_id}.\n"
                    f"Requirements: {spec.requirement_text}\n"
                    f"Acceptance Criteria: {spec.acceptance_criteria}\n"
                    f"Baseline Commit: {spec.baseline_commit}\n"
                    f"Candidate Commit: {candidate_commit}\n\n"
                    "Review ONLY the immutable diff embedded below. Do not invoke tools, commands, "
                    "browsers, file-system access, subagents, or permission prompts.\n\n"
                    f"{reviewer_diff_bundle}\n\n"
                    f"You MUST return ONLY a JSON object strictly matching this schema:\n"
                    f"```json\n"
                    f"{json.dumps(REVIEWER_JSON_SCHEMA, ensure_ascii=False, sort_keys=True)}\n"
                    f"```\n"
                    f"Required fields:\n"
                    f"- task_id: '{task_id}'\n"
                    f"- baseline_commit: '{spec.baseline_commit}'\n"
                    f"- candidate_commit: '{candidate_commit}'\n"
                    f"- session_id: '{sess_reviewer}'\n"
                    f"- review_request_id: '{review_request_id}'\n"
                    f"- decision: 'PASS' or 'REJECT'\n"
                    f"- defects: [ {{\"defect_id\": \"...\", \"severity\": \"P1\", \"description\": \"...\"}} ]\n"
                    f"- summary: 'summary text'\n"
                )

                reviewer_request = AgentRequest(
                    session_id=sess_reviewer,
                    prompt=reviewer_prompt,
                    role="REVIEWER",
                    workspace_dir=worktree_dir,
                    timeout_seconds=float(spec.reviewer_timeout_seconds),
                    extra_context={
                        "sandbox": True,
                        "permission_boundary": "workspace_read",
                        "operation_intent": (
                            f"git diff --no-ext-diff --unified=40 {spec.baseline_commit} "
                            f"{candidate_commit} --"
                        ),
                        "json_schema": REVIEWER_JSON_SCHEMA,
                        "worktree_dir": worktree_dir,
                        "project_id": spec.project_id,
                        "review_request_id": review_request_id,
                        "pre_granted_approval": pre_granted_approval,
                    },
                )
                self._emit_progress(
                    task_id=task_id,
                    state=RunnerState.REVIEWING.value,
                    role="REVIEWER",
                    event="stage_started",
                    message=f"Dispatching independent Reviewer via {spec.reviewer_adapter_id}",
                    candidate_commit=candidate_commit,
                    cycle=review_cycle,
                )

                try:
                    self._record_pre_granted_approval(
                        reviewer_adapter,
                        reviewer_request,
                        pre_granted_approval,
                    )
                    reviewer_handle = reviewer_adapter.dispatch_agent(reviewer_request)
                    with self._lock:
                        self._active_handles[task_id] = reviewer_handle

                    reviewer_result = self._wait_for_result_cancellable(
                        reviewer_adapter,
                        reviewer_handle,
                        timeout_seconds=float(spec.reviewer_timeout_seconds),
                        task_id=task_id,
                    )
                except RunnerCancelledError as ce:
                    return RunnerResult(
                        success=True,
                        state=RunnerState.CANCELLED.value,
                        task_id=task_id,
                        candidate_commit=candidate_commit,
                        candidate_generation=candidate_generation,
                        evidence_ids=tuple(evidence_ids),
                        message=str(ce),
                    )
                except AgentNotSupportedError as se:
                    with self._lock:
                        self._active_handles.pop(task_id, None)
                    checkpoint = RunnerCheckpoint(
                        task_id=task_id,
                        project_id=spec.project_id,
                        state=RunnerState.APPROVAL_REQUIRED.value,
                        current_role="REVIEWER",
                        candidate_commit=candidate_commit,
                        candidate_generation=candidate_generation,
                        review_cycle=review_cycle,
                        qa_cycle=qa_cycle,
                        total_attempts=total_attempts,
                        worktree_path=worktree_dir,
                        worktree_branch=worktree_branch,
                        reviewer_session_id=sess_reviewer,
                        evidence_ids=tuple(evidence_ids),
                        execution_options=_execution_options_from_spec(spec),
                        defects_history=tuple(defects_history),
                        approval_reason=str(se),
                        last_error="Permission approval required",
                    )
                    self.checkpoint_store.save_checkpoint(checkpoint)
                    return RunnerResult(
                        success=False,
                        state=RunnerState.APPROVAL_REQUIRED.value,
                        task_id=task_id,
                        candidate_commit=candidate_commit,
                        candidate_generation=candidate_generation,
                        evidence_ids=tuple(evidence_ids),
                        message=f"Execution paused at APPROVAL_REQUIRED: {se}",
                        diagnostics={"approval_reason": str(se)},
                    )
                except Exception as e:
                    with self._lock:
                        self._active_handles.pop(task_id, None)
                    return RunnerResult(
                        success=False,
                        state=RunnerState.FAILED.value,
                        task_id=task_id,
                        candidate_commit=candidate_commit,
                        candidate_generation=candidate_generation,
                        message=f"Reviewer dispatch/wait failed: {e}",
                        diagnostics={"error": str(e)},
                    )
                finally:
                    with self._lock:
                        self._active_handles.pop(task_id, None)

                if reviewer_result.status != AgentStatus.SUCCESS:
                    return RunnerResult(
                        success=False,
                        state=RunnerState.FAILED.value,
                        task_id=task_id,
                        message=f"Reviewer execution failed with status: {reviewer_result.status.value}",
                    )
                if reviewer_result.session_id != reviewer_handle.session_id:
                    return RunnerResult(
                        success=False,
                        state=RunnerState.FAILED.value,
                        task_id=task_id,
                        message="Reviewer session ID mismatch (Fail-Closed).",
                    )
                if not reviewer_result.is_real_host:
                    return RunnerResult(
                        success=False,
                        state=RunnerState.FAILED.value,
                        task_id=task_id,
                        message="Reviewer result is not from a real host (Fail-Closed).",
                    )

                inv_reviewer = _extract_real_invocation_id(reviewer_result, reviewer_handle)
                if not inv_reviewer:
                    return RunnerResult(
                        success=False,
                        state=RunnerState.FAILED.value,
                        task_id=task_id,
                        message="Reviewer host did not produce a valid canonical invocation identity (Fail-Closed).",
                    )

                review_output = self._parse_reviewer_structured_json(
                    raw_output=reviewer_result.output,
                    task_id=task_id,
                    baseline_commit=spec.baseline_commit,
                    candidate_commit=candidate_commit,
                    session_id=sess_reviewer,
                    invocation_id=inv_reviewer,
                    review_request_id=review_request_id,
                )
                self._emit_progress(
                    task_id=task_id,
                    state=RunnerState.REVIEWING.value,
                    role="REVIEWER",
                    event="stage_completed",
                    message=f"Reviewer decision: {review_output.decision}",
                    candidate_commit=candidate_commit,
                    cycle=review_cycle,
                )

                reviewer_evidence_id = f"evi_reviewer_{task_id.lower()}_{int(time.time()*1000)}"
                reviewer_caps = reviewer_adapter.detect_capabilities()
                r_meta = EvidenceMetadata(
                    project_id=spec.project_id,
                    task_id=task_id,
                    actor_role="REVIEWER",
                    host_id=spec.reviewer_adapter_id,
                    adapter=spec.reviewer_adapter_id,
                    host_session_id=sess_reviewer,
                    host_invocation_id=inv_reviewer,
                    is_real_host=reviewer_result.is_real_host,
                    workspace_mode="workspace_read",
                    transition_from="REVIEWING",
                    transition_to="TESTING" if review_output.decision == "PASS" else "BUILDING",
                    created_at=time.time(),
                    extra=_extract_capabilities_extra(reviewer_caps),
                )
                r_record = EvidenceRecord(
                    evidence_id=reviewer_evidence_id,
                    evidence_type=EvidenceType.TASK_TRANSITION,
                    baseline_commit=spec.baseline_commit,
                    result_commit=candidate_commit,
                    artifacts=(),
                    metadata=r_meta,
                )
                self.evidence_store.append(r_record)
                try:
                    self.evidence_gate.validate_evidence(
                        reviewer_evidence_id,
                        EvidenceValidationContext(
                            project_id=spec.project_id,
                            task_id=task_id,
                            actor_role="REVIEWER",
                            transition_from="REVIEWING",
                            transition_to="TESTING" if review_output.decision == "PASS" else "BUILDING",
                            baseline_commit=spec.baseline_commit,
                            result_commit=candidate_commit,
                            expected_invocation_id=inv_reviewer,
                            expected_adapter=spec.reviewer_adapter_id,
                            expected_workspace_mode="workspace_read",
                            expected_evidence_type=EvidenceType.TASK_TRANSITION,
                            host_handle=reviewer_handle,
                            expected_capabilities=reviewer_caps,
                            agent_result=reviewer_result,
                        ),
                    )
                except Exception as gate_error:
                    return RunnerResult(
                        success=False,
                        state=RunnerState.FAILED.value,
                        task_id=task_id,
                        candidate_commit=candidate_commit,
                        message=f"Reviewer EvidenceGate validation failed: {gate_error}",
                    )
                evidence_ids.append(reviewer_evidence_id)

                if review_output.decision == "PASS":
                    # 状态机推进: 审查中 -> 测试中 (REVIEWER)
                    if current_board_status == "审查中":
                        ok_trans, err_trans = self._do_state_transition(
                            spec.authority_root, task_id, "REVIEWER", "审查中", "测试中", "章测试",
                            f"Antigravity Reviewer 审核通过，候选提交: {candidate_commit}", spec.task_type
                        )
                        if not ok_trans:
                            return RunnerResult(
                                success=False, state=RunnerState.FAILED.value, task_id=task_id,
                                message=f"Failed to transition state to 测试中: {err_trans}"
                            )
                        current_board_status = "测试中"
                else:
                    # 状态机回退: 审查中 -> 已退回 -> 进行中 (REVIEWER & DEV)
                    review_exhausted = review_cycle >= spec.max_review_cycles
                    if current_board_status == "审查中":
                        ok_return, err_return = self._do_state_transition(
                            spec.authority_root, task_id, "REVIEWER", "审查中", "已退回", "李开发",
                            f"Antigravity Reviewer 审核退回: {review_output.summary}", spec.task_type
                        )
                        if not ok_return:
                            return RunnerResult(
                                success=False, state=RunnerState.FAILED.value, task_id=task_id,
                                message=f"Failed to return rejected review to 已退回: {err_return}",
                            )
                        current_board_status = "已退回"
                        if not review_exhausted:
                            ok_reclaim, err_reclaim = self._do_state_transition(
                                spec.authority_root, task_id, "DEV", "已退回", "进行中", "李开发",
                                "Builder 重新认领任务进行缺陷修复", spec.task_type
                            )
                            if not ok_reclaim:
                                return RunnerResult(
                                    success=False, state=RunnerState.FAILED.value, task_id=task_id,
                                    message=f"Failed to reclaim rejected review for Builder: {err_reclaim}",
                                )
                            current_board_status = "进行中"

                    defects_history.append({
                        "cycle": review_cycle,
                        "role": "REVIEWER",
                        "defects": list(review_output.defects),
                        "summary": review_output.summary,
                    })
                    if review_exhausted:
                        return RunnerResult(
                            success=False,
                            state=RunnerState.NEEDS_USER_INPUT.value,
                            task_id=task_id,
                            candidate_commit=candidate_commit,
                            candidate_generation=candidate_generation,
                            evidence_ids=tuple(evidence_ids),
                            message=f"Reviewer rejected candidate and exceeded max review cycles ({spec.max_review_cycles}). Paused at NEEDS_USER_INPUT.",
                            diagnostics={"defects": defects_history},
                        )
                    continue
            else:
                skip_reviewer = False

            # ==========================================
            # STAGE 3: CODEX QA
            # ==========================================
            if not verify_optimistic_concurrency(
                spec,
                enforce_status=False,
                enforce_version=False,
            ):
                return RunnerResult(
                    success=False,
                    state=RunnerState.NEEDS_USER_INPUT.value,
                    task_id=task_id,
                    candidate_commit=candidate_commit,
                    evidence_ids=tuple(evidence_ids),
                    message="Task requirements or acceptance criteria changed before QA dispatch.",
                )
            qa_cycle += 1
            sess_qa = f"sess_qa_runner_{task_id.lower()}_{int(time.time()*1000)}"

            checkpoint = RunnerCheckpoint(
                task_id=task_id,
                project_id=spec.project_id,
                state=RunnerState.QA_TESTING.value,
                current_role="QA",
                candidate_commit=candidate_commit,
                candidate_generation=candidate_generation,
                review_cycle=review_cycle,
                qa_cycle=qa_cycle,
                total_attempts=total_attempts,
                worktree_path=worktree_dir,
                worktree_branch=worktree_branch,
                builder_session_id=sess_builder if "sess_builder" in locals() else None,
                reviewer_session_id=sess_reviewer if "sess_reviewer" in locals() else None,
                qa_session_id=sess_qa,
                evidence_ids=tuple(evidence_ids),
                execution_options=_execution_options_from_spec(spec),
                defects_history=tuple(defects_history),
            )
            self.checkpoint_store.save_checkpoint(checkpoint)

            # QA 前核验 candidate_commit 与 HEAD 一致
            try:
                head_before_qa = self._get_git_commit(worktree_dir)
            except Exception as e:
                return RunnerResult(
                    success=False,
                    state=RunnerState.FAILED.value,
                    task_id=task_id,
                    message=f"Failed to inspect HEAD before QA: {e}",
                )

            if head_before_qa != candidate_commit:
                return RunnerResult(
                    success=False,
                    state=RunnerState.FAILED.value,
                    task_id=task_id,
                    candidate_commit=candidate_commit,
                    message=f"Worktree HEAD ({head_before_qa}) does not match candidate commit ({candidate_commit}) before QA! Fail-Closed.",
                )

            try:
                qa_diff_bundle = self._build_reviewer_diff_bundle(
                    worktree_dir,
                    spec.baseline_commit,
                    candidate_commit,
                )
            except Exception as exc:
                return RunnerResult(
                    success=False,
                    state=RunnerState.NEEDS_USER_INPUT.value,
                    task_id=task_id,
                    candidate_commit=candidate_commit,
                    evidence_ids=tuple(evidence_ids),
                    message=f"Unable to create bounded QA candidate payload: {exc}",
                )

            test_commands = spec.test_commands or (spec.test_command or "python -m pytest -q",)
            validated_commands = []
            for test_cmd in test_commands:
                valid_cmd, cmd_err, cmd_args = _validate_qa_test_command(test_cmd, worktree_dir)
                if not valid_cmd:
                    return RunnerResult(
                        success=False,
                        state=RunnerState.FAILED.value,
                        task_id=task_id,
                        candidate_commit=candidate_commit,
                        message=f"QA test command security validation failed for '{test_cmd}': {cmd_err}",
                    )
                validated_commands.append((test_cmd, cmd_args))

            # The Runner, not the headless QA agent, owns command execution. This
            # avoids duplicate test runs and prevents Antigravity CLI from trying
            # to open an interactive permission prompt in stream-json mode. QA
            # receives bounded, masked executable evidence and remains read-only.
            command_results = []
            qa_command_evidence = []
            for command, command_args in validated_commands:
                try:
                    test_proc = subprocess.run(
                        command_args,
                        cwd=worktree_dir,
                        capture_output=True,
                        text=True,
                        encoding="utf-8",
                        errors="replace",
                        timeout=spec.qa_timeout_seconds,
                    )
                    exit_code = test_proc.returncode
                    output_material = (test_proc.stdout or "") + "\n" + (test_proc.stderr or "")
                except Exception as exc:
                    exit_code = 1
                    output_material = f"Runner command exception: {type(exc).__name__}: {exc}"
                masked_output = self.evidence_store._mask_text(output_material)
                command_results.append({
                    "command": command,
                    "exit_code": exit_code,
                    "output_hash": hashlib.sha256(output_material.encode("utf-8", errors="replace")).hexdigest(),
                })
                qa_command_evidence.append({
                    "command": command,
                    "exit_code": exit_code,
                    "output_excerpt": masked_output[-4000:],
                })

            qa_request_id = f"qa_req_{uuid.uuid4().hex}"
            criteria_payload = [item.to_dict() for item in spec.acceptance_criteria_items]
            qa_prompt = (
                f"You are the independent QA gate for Task {task_id}.\n"
                f"Requirements:\n{spec.requirement_text}\n\n"
                f"Acceptance criteria hash: {spec.acceptance_criteria_hash}\n"
                f"Acceptance criteria (cover every ID exactly once):\n"
                f"{json.dumps(criteria_payload, ensure_ascii=False, indent=2)}\n\n"
                f"Baseline Commit: {spec.baseline_commit}\n"
                f"Candidate Commit: {candidate_commit}\n"
                f"Reviewer PASS summary: {review_output.summary if 'review_output' in locals() else 'validated reviewer evidence'}\n\n"
                f"Immutable candidate diff:\n{qa_diff_bundle}\n\n"
                "Perform a semantic QA assessment from the immutable candidate context and the Runner-produced "
                "test evidence below. Do not invoke tools, commands, browsers, file-system access, subagents, or "
                "permission prompts. Trace changed behavior through every API, background worker, database, "
                "authorization, contract/SDK, and frontend boundary that applies. Verify at least one negative or "
                "adversarial scenario represented by the acceptance criteria, review context, or test evidence. "
                "If any criterion or risk is not verifiable, return FAIL; never infer PASS from a green regression "
                "suite alone. In test_commands, copy each Runner command and its recorded exit_code exactly once; "
                "do not claim to have executed it yourself.\n\n"
                f"Runner-produced test evidence:\n"
                f"{json.dumps(qa_command_evidence, ensure_ascii=False, indent=2)}\n\n"
                "Return ONLY a JSON object matching this schema:\n"
                f"{json.dumps(QA_JSON_SCHEMA, ensure_ascii=False, sort_keys=True)}\n"
                f"Identity bindings: task_id={task_id}, baseline_commit={spec.baseline_commit}, "
                f"candidate_commit={candidate_commit}, session_id={sess_qa}, qa_request_id={qa_request_id}, "
                f"acceptance_criteria_hash={spec.acceptance_criteria_hash}."
            )

            qa_request = AgentRequest(
                session_id=sess_qa,
                prompt=qa_prompt,
                role="QA",
                workspace_dir=worktree_dir,
                timeout_seconds=float(spec.qa_timeout_seconds),
                extra_context={
                    "sandbox": True,
                    "permission_boundary": "workspace_read",
                    "operation_intent": "read-only semantic assessment of inline Runner test evidence",
                    "json_schema": QA_JSON_SCHEMA,
                    "worktree_dir": worktree_dir,
                    "project_id": spec.project_id,
                    "qa_request_id": qa_request_id,
                    "acceptance_criteria_hash": spec.acceptance_criteria_hash,
                    "pre_granted_approval": pre_granted_approval,
                },
            )
            self._emit_progress(
                task_id=task_id,
                state=RunnerState.QA_TESTING.value,
                role="QA",
                event="stage_started",
                message=f"Dispatching independent QA via {spec.qa_adapter_id}",
                candidate_commit=candidate_commit,
                cycle=qa_cycle,
            )

            try:
                self._record_pre_granted_approval(
                    qa_adapter,
                    qa_request,
                    pre_granted_approval,
                )
                qa_handle = qa_adapter.dispatch_agent(qa_request)
                with self._lock:
                    self._active_handles[task_id] = qa_handle

                qa_result = self._wait_for_result_cancellable(
                    qa_adapter,
                    qa_handle,
                    timeout_seconds=float(spec.qa_timeout_seconds),
                    task_id=task_id,
                )
            except RunnerCancelledError as ce:
                return RunnerResult(
                    success=True,
                    state=RunnerState.CANCELLED.value,
                    task_id=task_id,
                    candidate_commit=candidate_commit,
                    candidate_generation=candidate_generation,
                    evidence_ids=tuple(evidence_ids),
                    message=str(ce),
                )
            except AgentNotSupportedError as se:
                with self._lock:
                    self._active_handles.pop(task_id, None)
                checkpoint = RunnerCheckpoint(
                    task_id=task_id,
                    project_id=spec.project_id,
                    state=RunnerState.APPROVAL_REQUIRED.value,
                    current_role="QA",
                    candidate_commit=candidate_commit,
                    candidate_generation=candidate_generation,
                    review_cycle=review_cycle,
                    qa_cycle=qa_cycle,
                    total_attempts=total_attempts,
                    worktree_path=worktree_dir,
                    worktree_branch=worktree_branch,
                    qa_session_id=sess_qa,
                    evidence_ids=tuple(evidence_ids),
                    execution_options=_execution_options_from_spec(spec),
                    defects_history=tuple(defects_history),
                    approval_reason=str(se),
                    last_error="Permission approval required",
                )
                self.checkpoint_store.save_checkpoint(checkpoint)
                return RunnerResult(
                    success=False,
                    state=RunnerState.APPROVAL_REQUIRED.value,
                    task_id=task_id,
                    candidate_commit=candidate_commit,
                    candidate_generation=candidate_generation,
                    evidence_ids=tuple(evidence_ids),
                    message=f"Execution paused at APPROVAL_REQUIRED: {se}",
                    diagnostics={"approval_reason": str(se)},
                )
            except Exception as e:
                with self._lock:
                    self._active_handles.pop(task_id, None)
                return RunnerResult(
                    success=False,
                    state=RunnerState.FAILED.value,
                    task_id=task_id,
                    message=f"QA dispatch/wait failed: {e}",
                    diagnostics={"error": str(e)},
                )
            finally:
                with self._lock:
                    self._active_handles.pop(task_id, None)

            if qa_result.status != AgentStatus.SUCCESS:
                return RunnerResult(
                    success=False,
                    state=RunnerState.FAILED.value,
                    task_id=task_id,
                    message=f"QA execution failed with status: {qa_result.status.value}",
                )
            if qa_result.session_id != qa_handle.session_id:
                return RunnerResult(
                    success=False,
                    state=RunnerState.FAILED.value,
                    task_id=task_id,
                    message="QA session ID mismatch (Fail-Closed).",
                )
            if not qa_result.is_real_host:
                return RunnerResult(
                    success=False,
                    state=RunnerState.FAILED.value,
                    task_id=task_id,
                    message="QA result is not from a real host (Fail-Closed).",
                )

            inv_qa = _extract_real_invocation_id(qa_result, qa_handle)
            if not inv_qa:
                return RunnerResult(
                    success=False,
                    state=RunnerState.FAILED.value,
                    task_id=task_id,
                    message="QA host did not produce a valid canonical invocation identity (Fail-Closed).",
                )

            qa_output = self._parse_qa_structured_json(
                raw_output=qa_result.output,
                task_id=task_id,
                baseline_commit=spec.baseline_commit,
                candidate_commit=candidate_commit,
                session_id=sess_qa,
                invocation_id=inv_qa,
                qa_request_id=qa_request_id,
                acceptance_criteria_hash=spec.acceptance_criteria_hash,
                expected_criterion_ids=[item.criterion_id for item in spec.acceptance_criteria_items],
                expected_test_commands=test_commands,
            )

            test_exit_codes = tuple(item["exit_code"] for item in command_results)
            commands_passed = bool(test_exit_codes) and all(code == 0 for code in test_exit_codes)
            qa_passed = qa_output.decision == "PASS" and commands_passed
            self._emit_progress(
                task_id=task_id,
                state=RunnerState.QA_TESTING.value,
                role="QA",
                event="stage_completed",
                message=f"QA decision: {'PASS' if qa_passed else 'FAIL'}",
                candidate_commit=candidate_commit,
                cycle=qa_cycle,
            )

            # 严格核验 QA 前后源码不可变性（P1 门禁：HEAD、diff、cached diff、status）
            immutability_ok, immutability_err = self._verify_qa_immutability(worktree_dir, candidate_commit)
            if not immutability_ok:
                return RunnerResult(
                    success=False,
                    state=RunnerState.FAILED.value,
                    task_id=task_id,
                    candidate_commit=candidate_commit,
                    message=f"QA violated code immutability boundary: {immutability_err}",
                )

            if not verify_optimistic_concurrency(
                spec,
                enforce_status=False,
                enforce_version=False,
            ):
                return RunnerResult(
                    success=False,
                    state=RunnerState.NEEDS_USER_INPUT.value,
                    task_id=task_id,
                    candidate_commit=candidate_commit,
                    evidence_ids=tuple(evidence_ids),
                    message="Task requirements or acceptance criteria changed during QA execution.",
                )

            qa_evidence_id = f"evi_qa_{task_id.lower()}_{int(time.time()*1000)}"
            qa_caps = qa_adapter.detect_capabilities()
            qa_report = qa_output.to_dict()
            qa_report_hash = hashlib.sha256(
                json.dumps(qa_report, ensure_ascii=False, sort_keys=True).encode("utf-8")
            ).hexdigest()
            test_command_hash = hashlib.sha256(
                json.dumps(list(test_commands), ensure_ascii=False, sort_keys=True).encode("utf-8")
            ).hexdigest()
            test_output_hash = hashlib.sha256(
                json.dumps(command_results, ensure_ascii=False, sort_keys=True).encode("utf-8")
            ).hexdigest()
            covered_criterion_ids = tuple(
                item["criterion_id"] for item in qa_output.acceptance_coverage if item.get("status") == "PASS"
            )
            qa_semantic_metadata = {
                "qa_decision": "PASS" if qa_passed else "FAIL",
                "qa_request_id": qa_request_id,
                "qa_report_hash": qa_report_hash,
                "acceptance_criteria_hash": spec.acceptance_criteria_hash,
                "covered_criterion_ids": covered_criterion_ids,
                "required_test_command_count": len(test_commands),
                "negative_scenario_count": len(qa_output.negative_scenarios),
                "uncovered_risk_count": len(qa_output.uncovered_risks),
                "defect_count": len(qa_output.defects) + (0 if commands_passed else 1),
                "test_command_hash": test_command_hash,
                "test_output_hash": test_output_hash,
                "test_exit_codes": test_exit_codes,
                "qa_report": qa_report,
                "required_test_commands": list(test_commands),
                "runner_test_results": command_results,
            }
            qa_extra = _extract_capabilities_extra(qa_caps)
            qa_extra.update(qa_semantic_metadata)
            qa_meta = EvidenceMetadata(
                project_id=spec.project_id,
                task_id=task_id,
                actor_role="QA",
                host_id=spec.qa_adapter_id,
                adapter=spec.qa_adapter_id,
                host_session_id=sess_qa,
                host_invocation_id=inv_qa,
                is_real_host=qa_result.is_real_host,
                workspace_mode="workspace_read",
                transition_from="TESTING",
                transition_to="PENDING_USER_ACCEPTANCE" if qa_passed else "BUILDING",
                created_at=time.time(),
                extra=qa_extra,
            )
            qa_record = EvidenceRecord(
                evidence_id=qa_evidence_id,
                evidence_type=EvidenceType.TASK_TRANSITION,
                baseline_commit=spec.baseline_commit,
                result_commit=candidate_commit,
                artifacts=(),
                metadata=qa_meta,
            )
            self.evidence_store.append(qa_record)
            try:
                self.evidence_gate.validate_evidence(
                    qa_evidence_id,
                    EvidenceValidationContext(
                        project_id=spec.project_id,
                        task_id=task_id,
                        actor_role="QA",
                        transition_from="TESTING",
                        transition_to="PENDING_USER_ACCEPTANCE" if qa_passed else "BUILDING",
                        baseline_commit=spec.baseline_commit,
                        result_commit=candidate_commit,
                        expected_invocation_id=inv_qa,
                        expected_adapter=spec.qa_adapter_id,
                        expected_workspace_mode="workspace_read",
                        expected_evidence_type=EvidenceType.TASK_TRANSITION,
                        host_handle=qa_handle,
                        expected_capabilities=qa_caps,
                        agent_result=qa_result,
                        expected_metadata=qa_semantic_metadata,
                        require_qa_semantics=True,
                    ),
                )
            except Exception as gate_error:
                return RunnerResult(
                    success=False,
                    state=RunnerState.FAILED.value,
                    task_id=task_id,
                    candidate_commit=candidate_commit,
                    message=f"QA EvidenceGate validation failed: {gate_error}",
                )
            evidence_ids.append(qa_evidence_id)

            if not qa_passed:
                qa_defects = [dict(item) for item in qa_output.defects]
                if not commands_passed:
                    qa_defects.append({
                        "defect_id": f"DEF-{task_id}-QA-COMMAND-FAIL",
                        "severity": "P1",
                        "description": f"Runner-controlled QA commands failed with exit codes {list(test_exit_codes)}.",
                    })
                qa_exhausted = qa_cycle >= spec.max_qa_cycles
                if current_board_status == "测试中":
                    ok_return, err_return = self._do_state_transition(
                        spec.authority_root, task_id, "QA", "测试中", "已退回", "李开发",
                        f"QA 未通过语义覆盖门禁：{qa_output.summary}；命令退出码: {list(test_exit_codes)}", spec.task_type,
                    )
                    if not ok_return:
                        return RunnerResult(
                            success=False, state=RunnerState.FAILED.value, task_id=task_id,
                            candidate_commit=candidate_commit,
                            message=f"Failed to return QA failure to 已退回: {err_return}",
                        )
                    current_board_status = "已退回"
                    if not qa_exhausted:
                        ok_reclaim, err_reclaim = self._do_state_transition(
                            spec.authority_root, task_id, "DEV", "已退回", "进行中", "李开发",
                            "Builder 重新认领任务修复 QA 缺陷", spec.task_type,
                        )
                        if not ok_reclaim:
                            return RunnerResult(
                                success=False, state=RunnerState.FAILED.value, task_id=task_id,
                                candidate_commit=candidate_commit,
                                message=f"Failed to reclaim QA failure for Builder: {err_reclaim}",
                            )
                        current_board_status = "进行中"
                defects_history.append({
                    "cycle": qa_cycle,
                    "role": "QA",
                    "defects": qa_defects,
                    "summary": qa_output.summary,
                })
                if qa_exhausted:
                    return RunnerResult(
                        success=False,
                        state=RunnerState.NEEDS_USER_INPUT.value,
                        task_id=task_id,
                        candidate_commit=candidate_commit,
                        candidate_generation=candidate_generation,
                        evidence_ids=tuple(evidence_ids),
                        message=f"QA semantic or command gate failed and exceeded max QA cycles ({spec.max_qa_cycles}). Paused at NEEDS_USER_INPUT.",
                        diagnostics={"defects": defects_history},
                    )
                continue

            # ==========================================
            # STAGE 4: USER ACCEPTANCE & COMPLETION
            # ==========================================
            confirmation_req_id = f"conf_req_{task_id.lower()}_{int(time.time()*1000)}"

            checkpoint = RunnerCheckpoint(
                task_id=task_id,
                project_id=spec.project_id,
                state=RunnerState.PENDING_USER_ACCEPTANCE.value,
                current_role="QA",
                candidate_commit=candidate_commit,
                candidate_generation=candidate_generation,
                review_cycle=review_cycle,
                qa_cycle=qa_cycle,
                total_attempts=total_attempts,
                worktree_path=worktree_dir,
                worktree_branch=worktree_branch,
                evidence_ids=tuple(evidence_ids),
                execution_options=_execution_options_from_spec(spec),
                confirmation_request_id=confirmation_req_id,
                defects_history=tuple(defects_history),
            )
            self.checkpoint_store.save_checkpoint(checkpoint)

            # 通过合法状态机 transition_task.py 将任务流转至 已完成 (DEF-T0061-3)
            ok_trans, err_trans = self._do_state_transition(
                spec.authority_root,
                task_id=task_id,
                role="QA",
                from_status=current_board_status,
                to_status="已完成",
                assignee="严经理",
                remarks=f"2F-PROD通用自动编排Runner完成全流水线验证，候选提交: {candidate_commit}，停在已完成待用户验收",
                task_type=spec.task_type,
            )
            if not ok_trans:
                return RunnerResult(
                    success=False,
                    state=RunnerState.FAILED.value,
                    task_id=task_id,
                    candidate_commit=candidate_commit,
                    candidate_generation=candidate_generation,
                    evidence_ids=tuple(evidence_ids),
                    message=f"Failed to execute legal state transition to '已完成': {err_trans}",
                )

            self._emit_progress(
                task_id=task_id,
                state=RunnerState.PENDING_USER_ACCEPTANCE.value,
                role="RUNNER",
                event="pending_user_acceptance",
                message="EvidenceGate passed; board is 已完成 and waiting for explicit user acceptance",
                candidate_commit=candidate_commit,
            )

            return RunnerResult(
                success=True,
                state=RunnerState.PENDING_USER_ACCEPTANCE.value,
                task_id=task_id,
                candidate_commit=candidate_commit,
                candidate_generation=candidate_generation,
                evidence_ids=tuple(evidence_ids),
                confirmation_request_id=confirmation_req_id,
                message=f"Production orchestration pipeline succeeded for {task_id}. State transitioned to 已完成 (PENDING_USER_ACCEPTANCE). Awaiting explicit user acceptance.",
                diagnostics={
                    "total_attempts": total_attempts,
                    "review_cycles": review_cycle,
                    "qa_cycles": qa_cycle,
                    "candidate_commit": candidate_commit,
                },
            )

    def get_status(self, project_root: str, task_id: str, authority_root: Optional[str] = None) -> Dict[str, Any]:
        """纯只读查询当前任务与 Checkpoint 状态（零写入、零锁、零 Host 调用）"""
        _validate_task_id(task_id)
        ckpt = self.checkpoint_store.load_checkpoint(task_id)
        if ckpt:
            return {
                "task_id": task_id,
                "project_id": ckpt.project_id,
                "has_checkpoint": True,
                "state": ckpt.state,
                "current_role": ckpt.current_role,
                "candidate_commit": ckpt.candidate_commit,
                "candidate_generation": ckpt.candidate_generation,
                "review_cycle": ckpt.review_cycle,
                "qa_cycle": ckpt.qa_cycle,
                "evidence_ids": list(ckpt.evidence_ids),
                "confirmation_request_id": ckpt.confirmation_request_id,
                "last_error": ckpt.last_error,
                "approval_reason": ckpt.approval_reason,
                "execution_options": dict(ckpt.execution_options),
            }
        return {
            "task_id": task_id,
            "has_checkpoint": False,
            "state": "NOT_STARTED",
            "message": "No active checkpoint found for this task.",
        }

    def resume(
        self,
        project_root: str,
        task_id: str,
        authority_root: Optional[str] = None,
        interactive_approval_cb: Optional[Callable[[str, Dict[str, Any]], bool]] = None,
        pre_granted_approval: bool = False,
        overrides: Optional[Dict[str, Any]] = None,
    ) -> RunnerResult:
        """从 Checkpoint 恢复执行 (支持断点恢复、完整复核 HEAD、Evidence 与权威状态)"""
        _validate_task_id(task_id)
        ckpt = self.checkpoint_store.load_checkpoint(task_id)
        if not ckpt:
            return RunnerResult(
                success=False,
                state=RunnerState.FAILED.value,
                task_id=task_id,
                message=f"Cannot resume: No checkpoint found for task {task_id}.",
            )

        effective_overrides = dict(ckpt.execution_options)
        effective_overrides.update(overrides or {})
        spec = load_task_execution_spec(
            project_root=project_root,
            task_id=task_id,
            authority_root=authority_root,
            overrides=effective_overrides,
        )

        # 1. 严格乐观并发校验权威看板状态
        if not verify_optimistic_concurrency(spec):
            return RunnerResult(
                success=False,
                state=RunnerState.FAILED.value,
                task_id=task_id,
                message="Optimistic concurrency verification failed: task is in terminal status or modified in authoritative board.",
            )

        if ckpt.state in (RunnerState.CANCELLED.value, RunnerState.PENDING_USER_ACCEPTANCE.value):
            return RunnerResult(
                success=False,
                state=ckpt.state,
                task_id=task_id,
                candidate_commit=ckpt.candidate_commit,
                message=f"Checkpoint state '{ckpt.state}' cannot be resumed.",
            )

        if ckpt.state == RunnerState.APPROVAL_REQUIRED.value and not pre_granted_approval:
            return RunnerResult(
                success=False,
                state=RunnerState.APPROVAL_REQUIRED.value,
                task_id=task_id,
                message="Explicit --approve is required to resume this checkpoint.",
            )

        # 2. 严格核验 Worktree 路径与 Git 仓库完整性
        resume_root = os.path.realpath(ckpt.worktree_path) if ckpt.worktree_path else os.path.realpath(project_root)
        if not os.path.isdir(resume_root):
            return RunnerResult(
                success=False,
                state=RunnerState.FAILED.value,
                task_id=task_id,
                message=f"Checkpoint worktree no longer exists: {resume_root}",
            )

        # 3. 严格核验 Candidate Commit 是否在 Git 仓库中有效存在
        if ckpt.candidate_commit:
            try:
                subprocess.run(
                    ["git", "cat-file", "-e", f"{ckpt.candidate_commit}^{{commit}}"],
                    cwd=resume_root,
                    check=True,
                    capture_output=True,
                )
            except Exception:
                return RunnerResult(
                    success=False,
                    state=RunnerState.FAILED.value,
                    task_id=task_id,
                    message=f"Candidate commit '{ckpt.candidate_commit}' referenced in checkpoint does not exist in worktree Git history.",
                )

        # 4. 严格核验 Checkpoint 中所有 Evidence 是否在 EvidenceStore 中有效存在
        if ckpt.evidence_ids:
            if self.evidence_store is None:
                evidence_dir = os.path.join(project_root, "user_data", "runner_evidence")
                self.evidence_store = EvidenceStore(root_dir=evidence_dir)
            for evi_id in ckpt.evidence_ids:
                try:
                    self.evidence_store.read(evi_id)
                except Exception:
                    return RunnerResult(
                        success=False,
                        state=RunnerState.FAILED.value,
                        task_id=task_id,
                        message=f"Evidence record '{evi_id}' referenced in checkpoint not found in EvidenceStore.",
                    )

        resumed_baseline = spec.baseline_commit
        if ckpt.evidence_ids and self.evidence_store is not None:
            try:
                first_evi = self.evidence_store.read(ckpt.evidence_ids[0])
                if first_evi.baseline_commit and re.match(r"^[0-9a-f]{40}$", first_evi.baseline_commit):
                    resumed_baseline = first_evi.baseline_commit
            except Exception:
                pass

        spec = replace(
            spec,
            project_id=ckpt.project_id,
            project_root=resume_root,
            baseline_commit=resumed_baseline,
            workspace_mode="inherit",
        )

        lock_handle, lock_file = self.checkpoint_store.acquire_runner_lock(task_id)
        if lock_handle is None:
            return RunnerResult(
                success=False,
                state=RunnerState.FAILED.value,
                task_id=task_id,
                message=f"Task {task_id} is already locked by another running process.",
            )

        try:
            return self._execute_loop(
                spec,
                lock_tuple=(lock_handle, lock_file),
                interactive_approval_cb=interactive_approval_cb,
                existing_checkpoint=ckpt,
                pre_granted_approval=pre_granted_approval,
            )
        finally:
            self.checkpoint_store.release_runner_lock((lock_handle, lock_file))

    def cancel(self, project_root: str, task_id: str, authority_root: Optional[str] = None) -> RunnerResult:
        """安全取消任务：向所有正在运行的 Host 发送 cancel 请求，不删除 Worktree，不清除 Evidence"""
        _validate_task_id(task_id)

        ckpt = self.checkpoint_store.load_checkpoint(task_id)
        if not ckpt:
            return RunnerResult(
                success=False,
                state=RunnerState.FAILED.value,
                task_id=task_id,
                message=f"Cannot cancel: No checkpoint found for task {task_id}.",
            )
        if ckpt.state in (RunnerState.CANCELLED.value, RunnerState.PENDING_USER_ACCEPTANCE.value):
            return RunnerResult(
                success=False,
                state=ckpt.state,
                task_id=task_id,
                candidate_commit=ckpt.candidate_commit,
                message=f"Checkpoint state '{ckpt.state}' cannot be cancelled.",
            )

        # 1. 取消活动的代理句柄与 Host 进程
        active_handle = None
        with self._lock:
            active_handle = self._active_handles.pop(task_id, None)

        if active_handle and self.registry:
            try:
                adapter = self.registry.get(active_handle.host_id)
                if adapter and hasattr(adapter, "cancel_agent"):
                    adapter.cancel_agent(active_handle)
            except Exception:
                pass

        # 遍历注册表中的适配器，取消与该 task 关联的正在运行会话
        if self.registry and getattr(self.registry, "_adapters", None):
            for adapter in self.registry._adapters.values():
                try:
                    if hasattr(adapter, "_running_sessions") and hasattr(adapter, "cancel_agent"):
                        with getattr(adapter, "_lock", threading.Lock()):
                            sessions_to_cancel = [
                                s_data.get("handle")
                                for s_data in adapter._running_sessions.values()
                                if s_data.get("handle") and (task_id in s_data.get("handle").session_id or task_id.lower() in s_data.get("handle").session_id)
                            ]
                        for h in sessions_to_cancel:
                            if h:
                                adapter.cancel_agent(h)
                except Exception:
                    pass

        # 2. 更新 Checkpoint 状态为 CANCELLED
        if ckpt:
            updated = RunnerCheckpoint(
                task_id=task_id,
                project_id=ckpt.project_id,
                state=RunnerState.CANCELLED.value,
                current_role=ckpt.current_role,
                candidate_commit=ckpt.candidate_commit,
                candidate_generation=ckpt.candidate_generation,
                review_cycle=ckpt.review_cycle,
                qa_cycle=ckpt.qa_cycle,
                total_attempts=ckpt.total_attempts,
                worktree_path=ckpt.worktree_path,
                worktree_branch=ckpt.worktree_branch,
                builder_session_id=ckpt.builder_session_id,
                reviewer_session_id=ckpt.reviewer_session_id,
                qa_session_id=ckpt.qa_session_id,
                evidence_ids=ckpt.evidence_ids,
                execution_options=ckpt.execution_options,
                confirmation_request_id=ckpt.confirmation_request_id,
                defects_history=ckpt.defects_history,
                last_error="Cancelled by user command.",
            )
            self.checkpoint_store.save_checkpoint(updated)

        return RunnerResult(
            success=True,
            state=RunnerState.CANCELLED.value,
            task_id=task_id,
            message=f"Task {task_id} successfully cancelled. Running host sessions cancelled, checkpoint updated, worktree and evidence preserved.",
        )
