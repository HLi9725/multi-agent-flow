import os
import shutil
import subprocess
import sys
import tempfile
import time
from typing import Any, Dict, List, Optional, Tuple
import pytest

from scripts._lib.core.adapter_manifest import (
    AdapterManifest,
    AuthBoundaryType,
    BillingBoundaryType,
    ExecutionMode,
    HostSurface,
    PlatformVerification,
    VerificationLevel,
)
from scripts._lib.core.adapter_registry import AdapterRegistry, AdapterResolutionRequest, ResolutionStatus
from scripts._lib.core.agent_schema import (
    AgentHandle,
    AgentInvalidHandleError,
    AgentNotSupportedError,
    AgentRequest,
    AgentResult,
    AgentStatus,
    CapabilitySupport,
    ConfirmationRequest,
    ConfirmationResult,
    HostCapabilities,
)
from scripts._lib.core.evidence_gate import EvidenceGate, EvidenceValidationContext
from scripts._lib.core.evidence_schema import (
    ArtifactRecord,
    EvidenceError,
    EvidenceGateError,
    EvidenceMetadata,
    EvidenceRecord,
    EvidenceType,
)
from scripts._lib.core.evidence_store import EvidenceStore
from scripts._lib.core.orchestrator import Orchestrator, TaskExecutionSession
from scripts._lib.core.orchestrator_schema import (
    BuilderToReviewerHandover,
    DefectRejectionHandover,
    DualHostVerificationResult,
    DualHostVerificationStatus,
    OrchestrationError,
    OrchestrationGateError,
    OrchestrationMode,
    OrchestrationRole,
    OrchestrationSecurityError,
    OrchestrationSessionIsolationError,
    OrchestrationState,
    OrchestrationStateError,
    ReviewerToQAHandover,
    UserAcceptanceDecision,
    UserAcceptanceRequest,
)
from scripts._lib.hosts.antigravity_adapter import AntigravityAdapter, create_antigravity_manifest
from scripts._lib.hosts.codex_cli_adapter import CodexCliAdapter, create_codex_cli_manifest
import scripts.run_phase2_live_e2e as live_e2e_module
from scripts.run_phase2_live_e2e import run_phase2_live_e2e_pipeline, detect_antigravity_cli

_REAL_RESOLVE_GIT_CONTEXT = live_e2e_module._resolve_authoritative_git_context


@pytest.fixture(autouse=True)
def authoritative_git_context(monkeypatch, tmp_path):
    """Unit tests inject deterministic Git context; production uses real Git validation."""
    def resolve(_root, candidate, baseline, project_id):
        return (
            candidate or "b" * 40,
            baseline or "a" * 40,
            project_id or "test-project",
        )

    monkeypatch.setattr(live_e2e_module, "_resolve_authoritative_git_context", resolve)
    monkeypatch.setattr(live_e2e_module, "_assert_tracked_candidate_unchanged", lambda *_args: None)

    fixture_dir = tmp_path / "tests" / "fixtures"
    fixture_dir.mkdir(parents=True, exist_ok=True)
    (fixture_dir / "fixture_math_util.py").write_text(
        "def pure_add(a: int, b: int) -> int:\n    return a + b\n",
        encoding="utf-8",
    )
    (fixture_dir / "test_fixture_math_util.py").write_text(
        "from tests.fixtures.fixture_math_util import pure_add\n\n"
        "def test_pure_add():\n"
        "    assert pure_add(2, 3) == 5\n"
        "    assert pure_add(-1, 1) == 0\n",
        encoding="utf-8",
    )


@pytest.fixture(autouse=True)
def process_guard(monkeypatch):
    """
    Strict outer process guard for 2F-LIVE automated unit tests.
    Prohibits execution of taskkill.exe, browsers (Edge, Firefox, Chrome) or interactive OAuth.
    """
    forbidden_tokens = [
        "taskkill", "taskkill.exe",
        "msedge", "msedge.exe",
        "firefox", "firefox.exe",
        "chrome", "chrome.exe",
        "google-chrome",
        "--oauth", "login"
    ]

    orig_popen = subprocess.Popen
    orig_run = subprocess.run

    def guarded_popen(cmd, *args, **kwargs):
        cmd_str = str(cmd).lower()
        for tok in forbidden_tokens:
            if tok in cmd_str:
                raise AssertionError(f"PROCESS_GUARD_TRIGGERED: Forbidden process spawn in test: {cmd_str}")
        return orig_popen(cmd, *args, **kwargs)

    def guarded_run(cmd, *args, **kwargs):
        cmd_str = str(cmd).lower()
        for tok in forbidden_tokens:
            if tok in cmd_str:
                raise AssertionError(f"PROCESS_GUARD_TRIGGERED: Forbidden subprocess.run in test: {cmd_str}")
        return orig_run(cmd, *args, **kwargs)

    monkeypatch.setattr(subprocess, "Popen", guarded_popen)
    monkeypatch.setattr(subprocess, "run", guarded_run)


def test_live_e2e_blocked_when_unreachable(tmp_path, monkeypatch):
    """When Antigravity is unreachable (timeout/EOF), pipeline must fail closed as BLOCKED and remain STATIC_ONLY."""
    def mock_detect():
        return (r"C:\fake\agy.exe", "1.1.22", "status=timeout; Endpoint eligibility check failed (EOF / network barrier). Kept STATIC_ONLY.")

    monkeypatch.setattr("scripts.run_phase2_live_e2e.detect_antigravity_cli", mock_detect)

    res = run_phase2_live_e2e_pipeline(str(tmp_path))

    assert res.success is False
    assert res.is_blocked is True
    assert "Antigravity CLI live authentication failed" in res.blocked_reason
    assert res.verification_level_by_os["windows"] == "static_only"
    assert res.dual_host_l2_result["status"] == "NOT_READY"
    assert res.real_host_sessions == {}
    assert res.real_host_invocations == {}


def test_live_e2e_dispatch_flow_and_upgrade(tmp_path, monkeypatch):
    """When Antigravity is authenticated and dispatches successfully, pipeline upgrades to CLI_VERIFIED and halts at PENDING_USER_ACCEPTANCE."""
    def mock_detect():
        return (r"C:\fake\agy.exe", "1.1.22", "status=authenticated; Live session active and responsive (status=SUCCESS).")

    monkeypatch.setattr("scripts.run_phase2_live_e2e.detect_antigravity_cli", mock_detect)

    # Mock dispatch_agent and wait_for_result on AntigravityAdapter to avoid external network dependencies in unit tests
    def mock_dispatch(self, req):
        handle = AgentHandle(
            session_id=req.session_id,
            host_id="antigravity",
            status="running",
            is_real_host=True,
            adapter_instance_id=self._instance_id,
            invocation_token="tok_ag_live_mock_123",
        )
        with self._lock:
            self._running_sessions[req.session_id] = {
                "handle": handle,
                "request": req,
                "invocation_id": "inv_reviewer_exec_1",
                "conversation_id": "conv_9d8755b1_live",
                "completed": False,
            }
        return handle

    def mock_wait(self, handle, timeout_seconds=None):
        with self._lock:
            data = self._running_sessions.pop(handle.session_id, {})
            data["conversation_id"] = "conv_9d8755b1_live"
            data["invocation_id"] = "conv_9d8755b1_live:step_1"
            data["completed"] = True
            result = AgentResult(
                session_id=handle.session_id,
                status=AgentStatus.SUCCESS,
                output="PASS: pure_add is verified as a pure function.",
                is_real_host=True,
            )
            data["result"] = result
            self._session_history[handle.session_id] = data
        return result

    monkeypatch.setattr(AntigravityAdapter, "dispatch_agent", mock_dispatch)
    monkeypatch.setattr(AntigravityAdapter, "wait_for_result", mock_wait)

    def mock_codex_dispatch(self, req):
        handle = AgentHandle(
            session_id=req.session_id,
            host_id="codex_cli",
            status="running",
            is_real_host=True,
            adapter_instance_id=self._instance_id,
            invocation_token=f"tok-{req.role.lower()}",
        )
        with self._lock:
            self._running_sessions[req.session_id] = {
                "handle": handle,
                "request": req,
                "thread_id": None,
                "invocation_id": None,
                "completed": False,
            }
        return handle

    def mock_codex_wait(self, handle, timeout_seconds=None):
        with self._lock:
            data = self._running_sessions.pop(handle.session_id)
            role = data["request"].role.lower()
            data["thread_id"] = f"thread-{role}-real"
            data["invocation_id"] = f"item-{role}-real"
            data["completed"] = True
            data["result"] = AgentResult(
                session_id=handle.session_id,
                status=AgentStatus.SUCCESS,
                output="BUILDER_READY" if role == "builder" else f"{role} completed",
                is_real_host=True,
            )
            self._session_history[handle.session_id] = data
            return data["result"]

    monkeypatch.setattr(CodexCliAdapter, "dispatch_agent", mock_codex_dispatch)
    monkeypatch.setattr(CodexCliAdapter, "wait_for_result", mock_codex_wait)

    def approve(adapter, request):
        adapter.record_permission_approval(
            project_id="test-project",
            auth_context="auth_ctx_live_builder",
            session_id=request.session_id,
            workspace_dir=request.workspace_dir,
            command_family="safe_local:REVIEWER",
            permission_boundary="workspace_read",
        )

    res = run_phase2_live_e2e_pipeline(
        str(tmp_path),
        permission_approval_hook=approve,
        project_id="test-project",
        baseline_commit="a" * 40,
        candidate_commit="b" * 40,
    )

    assert res.success is True
    assert res.is_blocked is False
    assert res.verification_level_by_os["windows"] == "cli_verified"
    assert res.dual_host_l2_result["status"] == "READY"
    assert res.dual_host_l2_result["state_reached"] == "PENDING_USER_ACCEPTANCE"
    assert "builder" in res.real_host_sessions
    assert "reviewer" in res.real_host_sessions
    assert "qa" in res.real_host_sessions
    assert len(res.evidence_ids) == 3


def test_live_e2e_anti_crossover_isolation(tmp_path):
    """Verify that sessions across different projects, billing contexts, and handles are strictly blocked."""
    store_dir = str(tmp_path / "evi_store")
    os.makedirs(store_dir, exist_ok=True)
    store = EvidenceStore(root_dir=store_dir)
    gate = EvidenceGate(store=store, project_root=str(tmp_path))

    registry = AdapterRegistry(context_id="test_ctx")
    codex_manifest = create_codex_cli_manifest(adapter_id="codex_cli", verified_version="0.149.0")
    codex_adapter = CodexCliAdapter(is_real_host=True)
    registry.register(codex_adapter, codex_manifest)

    ag_manifest = create_antigravity_manifest(adapter_id="antigravity", verified_version="1.1.22")
    ag_adapter = AntigravityAdapter(is_real_host=True, verification_level=VerificationLevel.STATIC_ONLY)
    registry.register(ag_adapter, ag_manifest)

    orchestrator = Orchestrator(registry=registry, evidence_store=store, evidence_gate=gate, project_root=str(tmp_path))

    # Start builder with tenant A
    session = orchestrator.start_builder(
        task_id="T_ISO_001",
        project_id="proj_tenant_a",
        branch="feature/iso",
        baseline_commit="sha_base",
        assignee="李开发",
        workspace_dir=str(tmp_path),
        worktree_dir=str(tmp_path),
        auth_context="auth_tenant_a",
        billing_context="billing_tenant_a",
        builder_session_id="sess_b_iso",
        builder_invocation_id="sess_b_iso:1",
        builder_adapter_id="codex_cli",
    )

    # Attempt submit with crossed billing context -> Security error
    bad_handover_billing = BuilderToReviewerHandover(
        task_id="T_ISO_001",
        project_id="proj_tenant_a",
        branch="feature/iso",
        baseline_commit="sha_base",
        candidate_commit="sha_cand",
        modified_files=("a.py",),
        diff_stat={},
        test_summary={},
        workspace_dir=str(tmp_path),
        worktree_dir=str(tmp_path),
        builder_session_id="sess_b_iso",
        builder_invocation_id="sess_b_iso:1",
        auth_context="auth_tenant_a",
        billing_context="billing_tenant_B_CROSSED",
    )
    with pytest.raises(OrchestrationSecurityError, match="Cross-billing-context boundary violation"):
        orchestrator.submit_to_reviewer(bad_handover_billing)


def test_permission_cache_boundary_rules():
    """
    Verify Phase 2 permission caching rules:
    - safe_local approved command can be reused within same task/workspace/adapter boundary
    - cross-workspace or cross-adapter reuse is rejected
    - destructive/controlled_external commands require per-call approval
    """
    adapter = AntigravityAdapter(is_real_host=True)

    # Record approval for safe_local:REVIEWER on ws1
    adapter.record_permission_approval(
        project_id="proj_1",
        auth_context="auth_1",
        session_id="sess_1",
        workspace_dir="ws1",
        command_family="safe_local:REVIEWER",
        permission_boundary="workspace_read",
    )
    assert adapter.has_permission_approval(
        project_id="proj_1",
        auth_context="auth_1",
        session_id="sess_1",
        workspace_dir="ws1",
        command_family="safe_local:REVIEWER",
        permission_boundary="workspace_read",
    ) is True

    # Same command on different workspace ws2 -> False
    assert adapter.has_permission_approval(
        project_id="proj_1",
        auth_context="auth_1",
        session_id="sess_1",
        workspace_dir="ws2",
        command_family="safe_local:REVIEWER",
        permission_boundary="workspace_read",
    ) is False

    # Different command on ws1 -> False
    assert adapter.has_permission_approval(
        project_id="proj_1",
        auth_context="auth_1",
        session_id="sess_1",
        workspace_dir="ws1",
        command_family="destructive:clean",
        permission_boundary="workspace_write",
    ) is False


def test_user_acceptance_cannot_auto_complete(tmp_path):
    """Verify that reaching PENDING_USER_ACCEPTANCE never auto-transitions to ACCEPTED without explicit user confirmation."""
    store_dir = str(tmp_path / "evi_store")
    os.makedirs(store_dir, exist_ok=True)
    store = EvidenceStore(root_dir=store_dir)
    gate = EvidenceGate(store=store, project_root=str(tmp_path))

    registry = AdapterRegistry(context_id="test_ctx")
    codex_manifest = create_codex_cli_manifest(adapter_id="codex_cli", verified_version="0.149.0")
    codex_adapter = CodexCliAdapter(is_real_host=True)
    registry.register(codex_adapter, codex_manifest)

    orchestrator = Orchestrator(registry=registry, evidence_store=store, evidence_gate=gate, project_root=str(tmp_path))

    session = orchestrator.start_builder(
        task_id="T_NO_AUTO_001",
        project_id="proj_x",
        branch="feature/x",
        baseline_commit="sha_base",
        assignee="李开发",
        workspace_dir=str(tmp_path),
        worktree_dir=str(tmp_path),
        auth_context="auth_x",
        billing_context="billing_x",
        builder_session_id="sess_b",
        builder_invocation_id="sess_b:1",
        builder_adapter_id="codex_cli",
    )
    b_handover = BuilderToReviewerHandover(
        task_id="T_NO_AUTO_001",
        project_id="proj_x",
        branch="feature/x",
        baseline_commit="sha_base",
        candidate_commit="sha_cand",
        modified_files=("a.py",),
        diff_stat={},
        test_summary={},
        workspace_dir=str(tmp_path),
        worktree_dir=str(tmp_path),
        builder_session_id="sess_b",
        builder_invocation_id="sess_b:1",
        auth_context="auth_x",
        billing_context="billing_x",
    )
    orchestrator.submit_to_reviewer(b_handover)

    # Rejection of automated PM agent confirmation at Schema level
    with pytest.raises(ValueError, match="user_source must be 'explicit_user'"):
        UserAcceptanceDecision(
            task_id="T_NO_AUTO_001",
            user_source="pm_automated_agent",  # Forbidden!
            is_accepted=True,
            remarks="PM auto accepted",
        )

    # Valid decision but session still in REVIEWING (not PENDING_USER_ACCEPTANCE) -> State error
    valid_dec = UserAcceptanceDecision(
        task_id="T_NO_AUTO_001",
        user_source="explicit_user",
        is_accepted=True,
        remarks="Explicit user approve",
    )
    with pytest.raises(OrchestrationStateError, match="expected PENDING_USER_ACCEPTANCE"):
        orchestrator.confirm_user_acceptance(valid_dec)


def test_reviewer_reject_reverts_to_building_without_qa_or_acceptance(tmp_path, monkeypatch):
    """Adversarial test: when Reviewer detects defects or rejects, pipeline must transition to BUILDING and not advance to QA or acceptance."""
    def mock_detect():
        return (r"C:\fake\agy.exe", "1.1.22", "status=authenticated; Live session active and responsive (status=SUCCESS).")

    monkeypatch.setattr("scripts.run_phase2_live_e2e.detect_antigravity_cli", mock_detect)

    def mock_dispatch(self, req):
        handle = AgentHandle(
            session_id=req.session_id,
            host_id="antigravity",
            status="running",
            is_real_host=True,
            adapter_instance_id=self._instance_id,
            invocation_token="tok_ag_live_mock_123",
        )
        with self._lock:
            self._running_sessions[req.session_id] = {
                "handle": handle,
                "request": req,
                "invocation_id": "inv_reviewer_exec_1",
                "conversation_id": "conv_9d8755b1_live",
                "completed": False,
            }
        return handle

    def mock_wait(self, handle, timeout_seconds=None):
        with self._lock:
            data = self._running_sessions.pop(handle.session_id, {})
            data["conversation_id"] = "conv_9d8755b1_live"
            data["invocation_id"] = "conv_9d8755b1_live:step_1"
            data["completed"] = True
            result = AgentResult(
                session_id=handle.session_id,
                status=AgentStatus.SUCCESS,
                output="REJECT: Defect detected in pure_add boundary checking.",
                is_real_host=True,
            )
            data["result"] = result
            self._session_history[handle.session_id] = data
        return result

    monkeypatch.setattr(AntigravityAdapter, "dispatch_agent", mock_dispatch)
    monkeypatch.setattr(AntigravityAdapter, "wait_for_result", mock_wait)

    def mock_codex_dispatch(self, req):
        handle = AgentHandle(
            session_id=req.session_id,
            host_id="codex_cli",
            status="running",
            is_real_host=True,
            adapter_instance_id=self._instance_id,
            invocation_token=f"tok-{req.role.lower()}",
        )
        with self._lock:
            self._running_sessions[req.session_id] = {
                "handle": handle,
                "request": req,
                "thread_id": None,
                "invocation_id": None,
                "completed": False,
            }
        return handle

    def mock_codex_wait(self, handle, timeout_seconds=None):
        with self._lock:
            data = self._running_sessions.pop(handle.session_id)
            role = data["request"].role.lower()
            data["thread_id"] = f"thread-{role}-real"
            data["invocation_id"] = f"item-{role}-real"
            data["completed"] = True
            data["result"] = AgentResult(
                session_id=handle.session_id,
                status=AgentStatus.SUCCESS,
                output="BUILDER_READY" if role == "builder" else f"{role} completed",
                is_real_host=True,
            )
            self._session_history[handle.session_id] = data
            return data["result"]

    monkeypatch.setattr(CodexCliAdapter, "dispatch_agent", mock_codex_dispatch)
    monkeypatch.setattr(CodexCliAdapter, "wait_for_result", mock_codex_wait)

    def approve(adapter, request):
        adapter.record_permission_approval(
            project_id="test-project",
            auth_context="auth_ctx_live_builder",
            session_id=request.session_id,
            workspace_dir=request.workspace_dir,
            command_family="safe_local:REVIEWER",
            permission_boundary="workspace_read",
        )

    res = run_phase2_live_e2e_pipeline(
        str(tmp_path),
        permission_approval_hook=approve,
        project_id="test-project",
        baseline_commit="a" * 40,
        candidate_commit="b" * 40,
    )

    assert res.success is False
    assert res.is_blocked is False
    assert res.verification_level_by_os["windows"] == "static_only"
    assert res.dual_host_l2_result["status"] == "NOT_READY"
    assert res.dual_host_l2_result["state_reached"] == "REJECTED_BY_REVIEWER"
    assert "Reviewer rejected candidate" in res.diagnostics


def test_pytest_failure_rejects_to_building_without_user_acceptance(tmp_path, monkeypatch):
    """Adversarial test: when pytest fails in QA, pipeline must reject to BUILDING and not advance to user acceptance."""
    def mock_detect():
        return (r"C:\fake\agy.exe", "1.1.22", "status=authenticated; Live session active and responsive (status=SUCCESS).")

    monkeypatch.setattr("scripts.run_phase2_live_e2e.detect_antigravity_cli", mock_detect)

    def mock_dispatch(self, req):
        handle = AgentHandle(
            session_id=req.session_id,
            host_id="antigravity",
            status="running",
            is_real_host=True,
            adapter_instance_id=self._instance_id,
            invocation_token="tok_ag_live_mock_123",
        )
        with self._lock:
            self._running_sessions[req.session_id] = {
                "handle": handle,
                "request": req,
                "invocation_id": "inv_reviewer_exec_1",
                "conversation_id": "conv_9d8755b1_live",
                "completed": False,
            }
        return handle

    def mock_wait(self, handle, timeout_seconds=None):
        with self._lock:
            data = self._running_sessions.pop(handle.session_id, {})
            data["conversation_id"] = "conv_9d8755b1_live"
            data["invocation_id"] = "conv_9d8755b1_live:step_1"
            data["completed"] = True
            result = AgentResult(
                session_id=handle.session_id,
                status=AgentStatus.SUCCESS,
                output="PASS: pure_add is verified as a pure function.",
                is_real_host=True,
            )
            data["result"] = result
            self._session_history[handle.session_id] = data
        return result

    monkeypatch.setattr(AntigravityAdapter, "dispatch_agent", mock_dispatch)
    monkeypatch.setattr(AntigravityAdapter, "wait_for_result", mock_wait)

    def mock_codex_dispatch(self, req):
        handle = AgentHandle(
            session_id=req.session_id,
            host_id="codex_cli",
            status="running",
            is_real_host=True,
            adapter_instance_id=self._instance_id,
            invocation_token=f"tok-{req.role.lower()}",
        )
        with self._lock:
            self._running_sessions[req.session_id] = {
                "handle": handle,
                "request": req,
                "thread_id": None,
                "invocation_id": None,
                "completed": False,
            }
        return handle

    def mock_codex_wait(self, handle, timeout_seconds=None):
        with self._lock:
            data = self._running_sessions.pop(handle.session_id)
            role = data["request"].role.lower()
            data["thread_id"] = f"thread-{role}-real"
            data["invocation_id"] = f"item-{role}-real"
            data["completed"] = True
            data["result"] = AgentResult(
                session_id=handle.session_id,
                status=AgentStatus.SUCCESS,
                output="BUILDER_READY" if role == "builder" else f"{role} completed",
                is_real_host=True,
            )
            self._session_history[handle.session_id] = data
            return data["result"]

    monkeypatch.setattr(CodexCliAdapter, "dispatch_agent", mock_codex_dispatch)
    monkeypatch.setattr(CodexCliAdapter, "wait_for_result", mock_codex_wait)

    # Mock subprocess.run for pytest to simulate test failure
    orig_run = subprocess.run
    def mock_failing_pytest(cmd, *args, **kwargs):
        if isinstance(cmd, list) and "pytest" in cmd:
            class MockProc:
                returncode = 1
                stdout = "FAILED tests/fixtures/test_fixture_math_util.py::test_pure_add"
                stderr = ""
            return MockProc()
        return orig_run(cmd, *args, **kwargs)

    monkeypatch.setattr(subprocess, "run", mock_failing_pytest)

    def approve(adapter, request):
        adapter.record_permission_approval(
            project_id="test-project",
            auth_context="auth_ctx_live_builder",
            session_id=request.session_id,
            workspace_dir=request.workspace_dir,
            command_family="safe_local:REVIEWER",
            permission_boundary="workspace_read",
        )

    res = run_phase2_live_e2e_pipeline(
        str(tmp_path),
        permission_approval_hook=approve,
        project_id="test-project",
        baseline_commit="a" * 40,
        candidate_commit="b" * 40,
    )

    assert res.success is False
    assert res.is_blocked is False
    assert res.dual_host_l2_result["status"] == "NOT_READY"
    assert res.dual_host_l2_result["state_reached"] == "REJECTED_BY_QA"
    assert res.qa_real_test_summary["exit_code"] == 1
    assert res.qa_real_test_summary["failed"] == 1
    assert "QA pytest failed" in res.diagnostics


def test_fake_or_unverified_evidence_rejected_on_promotion(tmp_path):
    """Adversarial test: non-existent or forged evidence must be rejected on promote_after_verified_evidence."""
    store_dir = str(tmp_path / "evi_store")
    os.makedirs(store_dir, exist_ok=True)
    store = EvidenceStore(root_dir=store_dir)
    gate = EvidenceGate(store=store, project_root=str(tmp_path))

    adapter = AntigravityAdapter(is_real_host=True, verification_level=VerificationLevel.STATIC_ONLY)

    # 1. Non-existent evidence ID rejected
    with pytest.raises(AgentNotSupportedError, match="requires store, gate, and an explicit validation context"):
        adapter.promote_after_verified_evidence(
            "evi_non_existent_999",
            host_session_id="sess_fake",
            host_invocation_id="sess_fake:step_1",
            store=store,
            gate=gate,
        )

    # 2. Empty evidence ref rejected
    with pytest.raises(ValueError, match="evidence_ref must be non-empty"):
        adapter.promote_after_verified_evidence(
            "",
            host_session_id="sess_1",
            host_invocation_id="sess_1:step_1",
            store=store,
            gate=gate,
        )


def test_builder_missing_artifacts_fails_closed(tmp_path, monkeypatch):
    """Adversarial test: if Builder dispatch fails to create or verify valid fixture files, fail-closed occurs."""
    def mock_detect():
        return (r"C:\fake\agy.exe", "1.1.22", "status=authenticated; Live session active and responsive (status=SUCCESS).")

    monkeypatch.setattr("scripts.run_phase2_live_e2e.detect_antigravity_cli", mock_detect)

    shutil.rmtree(tmp_path / "tests" / "fixtures")

    with pytest.raises(RuntimeError, match="Committed Builder artifacts are missing"):
        run_phase2_live_e2e_pipeline(
            str(tmp_path),
            project_id="test-project",
            baseline_commit="a" * 40,
            candidate_commit="b" * 40,
        )


def test_authoritative_git_context_rejects_candidate_mismatch(monkeypatch, tmp_path):
    """Evidence generation must never accept a caller-supplied SHA that is not HEAD."""
    head = "b" * 40

    def fake_git(_root, *args):
        key = tuple(args)
        values = {
            ("rev-parse", "HEAD"): (0, head + "\n"),
            ("status", "--porcelain", "--untracked-files=no"): (0, ""),
        }
        code, stdout = values.get(key, (1, ""))
        return subprocess.CompletedProcess(args=["git", *args], returncode=code, stdout=stdout, stderr="")

    monkeypatch.setattr(live_e2e_module, "_run_git", fake_git)
    with pytest.raises(RuntimeError, match="does not match committed HEAD"):
        _REAL_RESOLVE_GIT_CONTEXT(str(tmp_path), "c" * 40, "a" * 40, "project")


def test_authoritative_git_context_rejects_dirty_tracked_worktree(monkeypatch, tmp_path):
    """Tracked mutations must block Evidence generation before either real host starts."""

    def fake_git(_root, *args):
        key = tuple(args)
        values = {
            ("rev-parse", "HEAD"): (0, "b" * 40 + "\n"),
            ("status", "--porcelain", "--untracked-files=no"): (0, " M tracked.py\n"),
        }
        code, stdout = values.get(key, (1, ""))
        return subprocess.CompletedProcess(args=["git", *args], returncode=code, stdout=stdout, stderr="")

    monkeypatch.setattr(live_e2e_module, "_run_git", fake_git)
    with pytest.raises(RuntimeError, match="clean tracked worktree"):
        _REAL_RESOLVE_GIT_CONTEXT(str(tmp_path), "b" * 40, "a" * 40, "project")


@pytest.mark.parametrize(
    ("output", "expected"),
    [
        ("PASS: verified; no defects found", "PASS"),
        ("No defects found", "REJECT"),
        ("Review completed", "REJECT"),
        ("REJECT: defect found", "REJECT"),
    ],
)
def test_reviewer_decision_requires_explicit_pass_marker(output, expected):
    assert live_e2e_module._classify_reviewer_decision(output, AgentStatus.SUCCESS) == expected
    assert live_e2e_module._classify_reviewer_decision(output, AgentStatus.FAILED) == "REJECT"
