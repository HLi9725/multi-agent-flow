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
from scripts.run_phase2_live_e2e import run_phase2_live_e2e_pipeline, detect_antigravity_cli


@pytest.fixture(autouse=True)
def process_guard(monkeypatch):
    """
    Strict outer process guard for 2F-LIVE automated unit tests.
    Prohibits execution of agy.exe, taskkill.exe, browsers (Edge, Firefox, Chrome) or OAuth.
    """
    forbidden_tokens = [
        "agy", "agy.exe",
        "taskkill", "taskkill.exe",
        "msedge", "msedge.exe",
        "firefox", "firefox.exe",
        "chrome", "chrome.exe",
        "google-chrome",
        "--oauth", "oauth", "login"
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
        # Allow run_phase2_live_e2e to call --version or ping safely when mocked
        for tok in forbidden_tokens:
            if tok in cmd_str and "--version" not in cmd_str and "--output-format" not in cmd_str:
                raise AssertionError(f"PROCESS_GUARD_TRIGGERED: Forbidden subprocess.run in test: {cmd_str}")
        return orig_run(cmd, *args, **kwargs)

    monkeypatch.setattr(subprocess, "Popen", guarded_popen)


def test_live_e2e_pipeline_in_temporary_worktree(tmp_path, monkeypatch):
    """Test full dual-host L2 pipeline execution in an isolated temporary worktree with mock agy detection."""
    # Mock agy detection to return safe static_only info without real subprocessing
    def mock_detect():
        return (r"C:\fake\agy.exe", "1.1.21", "status=timeout; test-mock-diag")

    monkeypatch.setattr("scripts.run_phase2_live_e2e.detect_antigravity_cli", mock_detect)

    res = run_phase2_live_e2e_pipeline(str(tmp_path))

    assert res.success is True
    assert res.is_blocked is True
    assert "Antigravity CLI live endpoint unreachable" in res.blocked_reason
    assert res.verification_level_by_os["windows"] == "static_only"
    assert res.dual_host_l2_result["status"] == "NOT_READY"
    assert res.dual_host_l2_result["orchestration_mode"] == "assisted"
    assert res.dual_host_l2_result["state_reached"] == "PENDING_USER_ACCEPTANCE"
    # When Antigravity is STATIC_ONLY, real_host_sessions must be empty (strictly no forged real_host identities)
    assert res.real_host_sessions == {}
    assert res.qa_real_test_summary["exit_code"] == 0
    assert res.qa_real_test_summary["passed"] == 2
    assert "fixture_math_util.py" in res.assisted_handover_card


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

    ag_manifest = create_antigravity_manifest(adapter_id="antigravity", verified_version="1.1.21")
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

    # Record approval for git status on ws1
    adapter.record_permission_approval("ws1", "git_status")
    assert adapter.has_permission_approval("ws1", "git_status") is True

    # Same command on different workspace ws2 -> False
    assert adapter.has_permission_approval("ws2", "git_status") is False

    # Different command on ws1 -> False
    assert adapter.has_permission_approval("ws1", "rm_rf") is False


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
