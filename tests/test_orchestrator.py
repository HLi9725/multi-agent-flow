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


@pytest.fixture(autouse=True)
def process_guard(monkeypatch):
    """
    Strict outer process guard for 2F tests.
    Any attempt to execute agy.exe, taskkill.exe, browsers (Edge, Firefox, Chrome) or OAuth immediately fails.
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
        for tok in forbidden_tokens:
            if tok in cmd_str:
                raise AssertionError(f"PROCESS_GUARD_TRIGGERED: Forbidden subprocess.run in test: {cmd_str}")
        return orig_run(cmd, *args, **kwargs)

    monkeypatch.setattr(subprocess, "Popen", guarded_popen)
    monkeypatch.setattr(subprocess, "run", guarded_run)


@pytest.fixture
def mock_evidence_env(tmp_path):
    """Create isolated EvidenceStore and EvidenceGate for testing."""
    store_dir = str(tmp_path / "evidence_store")
    os.makedirs(store_dir, exist_ok=True)
    store = EvidenceStore(root_dir=store_dir)
    gate = EvidenceGate(store=store, project_root=str(tmp_path))
    return store, gate, str(tmp_path)


@pytest.fixture
def mock_dual_registry():
    """Create AdapterRegistry with both Codex and Antigravity registered."""
    registry = AdapterRegistry(context_id="test_project_alpha")

    # Codex CLI Adapter
    codex_manifest = create_codex_cli_manifest(adapter_id="codex_cli", verified_version="0.149.0")
    codex_adapter = CodexCliAdapter(is_real_host=True)
    registry.register(codex_adapter, codex_manifest)

    # Antigravity Adapter (STATIC_ONLY on all OS)
    ag_manifest = create_antigravity_manifest(adapter_id="antigravity", verified_version="1.1.21")
    ag_adapter = AntigravityAdapter(is_real_host=True, verification_level=VerificationLevel.STATIC_ONLY)
    registry.register(ag_adapter, ag_manifest)

    return registry


def _create_helper_evidence(
    store: EvidenceStore,
    evidence_id: str,
    evidence_type: EvidenceType,
    project_id: str,
    task_id: str,
    actor_role: str,
    transition_from: str,
    transition_to: str,
    baseline_commit: str,
    result_commit: str,
    host_id: str,
    adapter: str,
    host_session_id: str,
    host_invocation_id: str,
    workspace_mode: str,
    capabilities: HostCapabilities,
    extra_meta: Optional[Dict[str, Any]] = None,
) -> EvidenceRecord:
    """Helper to store a fully compliant EvidenceRecord."""
    extra: Dict[str, Any] = {}
    for k, v in capabilities.__dict__.items():
        if k == "extra":
            continue
        extra[f"capability_{k}"] = v
    if extra_meta:
        extra.update(extra_meta)

    meta = EvidenceMetadata(
        project_id=project_id,
        task_id=task_id,
        actor_role=actor_role,
        host_id=host_id,
        adapter=adapter,
        host_session_id=host_session_id,
        host_invocation_id=host_invocation_id,
        is_real_host=True,
        workspace_mode=workspace_mode,
        transition_from=transition_from,
        transition_to=transition_to,
        created_at=time.time(),
        extra=extra,
    )
    rec = EvidenceRecord(
        evidence_id=evidence_id,
        evidence_type=evidence_type,
        baseline_commit=baseline_commit,
        result_commit=result_commit,
        artifacts=(),
        metadata=meta,
    )
    store.append(rec)
    return rec


def test_dual_adapter_registry_registration_and_isolation(mock_dual_registry):
    """Verify Codex and Antigravity can be registered to same registry without conflict."""
    registry = mock_dual_registry

    codex_manifest = registry.get_manifest("codex_cli")
    ag_manifest = registry.get_manifest("antigravity")

    assert codex_manifest is not None
    assert ag_manifest is not None
    assert codex_manifest.adapter_id == "codex_cli"
    assert ag_manifest.adapter_id == "antigravity"

    # Codex platform verification
    assert codex_manifest.platform_verifications["windows"].verification_level == VerificationLevel.CLI_VERIFIED
    # Antigravity platform verification is STATIC_ONLY
    assert ag_manifest.platform_verifications["windows"].verification_level == VerificationLevel.STATIC_ONLY

    # Resolution test for exact adapter_id
    req_codex = AdapterResolutionRequest(
        project_id="test_project_alpha",
        allowed_verification_levels=(VerificationLevel.CLI_VERIFIED,),
        adapter_id="codex_cli",
        target_os="windows",
        execution_mode=ExecutionMode.ASSISTED,
        auth_context_id="user_local_ctx",
        billing_context_id="user_sub_ctx",
    )
    decision_codex = registry.resolve(req_codex)
    assert decision_codex.decision_status == ResolutionStatus.SELECTED
    assert decision_codex.selected_adapter_id == "codex_cli"

    req_ag = AdapterResolutionRequest(
        project_id="test_project_alpha",
        allowed_verification_levels=(VerificationLevel.STATIC_ONLY,),
        adapter_id="antigravity",
        target_os="windows",
        execution_mode=ExecutionMode.MANUAL,
        auth_context_id="user_local_ctx",
        billing_context_id="user_sub_ctx",
    )
    decision_ag = registry.resolve(req_ag)
    assert decision_ag.decision_status == ResolutionStatus.SELECTED
    assert decision_ag.selected_adapter_id == "antigravity"


def test_orchestrator_cross_host_handle_isolation(mock_dual_registry, mock_evidence_env):
    """Verify Codex handle cannot be used with Antigravity and vice versa."""
    store, gate, proj_root = mock_evidence_env
    orchestrator = Orchestrator(registry=mock_dual_registry, evidence_store=store, evidence_gate=gate, project_root=proj_root)

    codex_handle = AgentHandle(
        session_id="sess_codex_01",
        host_id="codex_cli",
        status=AgentStatus.UNKNOWN,
        is_real_host=True,
        adapter_instance_id="inst_codex_01",
        invocation_token="tok_codex_01"
    )

    ag_handle = AgentHandle(
        session_id="sess_ag_01",
        host_id="antigravity",
        status=AgentStatus.UNKNOWN,
        is_real_host=True,
        adapter_instance_id="inst_ag_01",
        invocation_token="tok_ag_01"
    )

    # Valid binding
    orchestrator.validate_handle_adapter_binding(codex_handle, "codex_cli")
    orchestrator.validate_handle_adapter_binding(ag_handle, "antigravity")

    # Cross-host mismatch raises OrchestrationSessionIsolationError
    with pytest.raises(OrchestrationSessionIsolationError, match="Cross-host handle exchange is strictly forbidden"):
        orchestrator.validate_handle_adapter_binding(codex_handle, "antigravity")

    with pytest.raises(OrchestrationSessionIsolationError, match="Cross-host handle exchange is strictly forbidden"):
        orchestrator.validate_handle_adapter_binding(ag_handle, "codex_cli")


def test_orchestrator_cross_project_and_auth_isolation(mock_dual_registry, mock_evidence_env):
    """Verify boundaries prevent cross-project and cross-auth leakage."""
    store, gate, proj_root = mock_evidence_env
    orchestrator = Orchestrator(registry=mock_dual_registry, evidence_store=store, evidence_gate=gate, project_root=proj_root)

    # Start builder in proj_alpha
    session = orchestrator.start_builder(
        task_id="T0100",
        project_id="proj_alpha",
        branch="feature/alpha",
        baseline_commit="commit_base_111",
        assignee="李开发",
        workspace_dir="/ws/alpha",
        worktree_dir="/wt/alpha",
        auth_context="auth_user_1",
        billing_context="billing_team_1",
        builder_session_id="sess_b_1",
        builder_invocation_id="sess_b_1:step_1",
        builder_adapter_id="codex_cli"
    )
    assert session.state == OrchestrationState.BUILDING

    # Attempt submit with wrong project_id
    bad_handover_proj = BuilderToReviewerHandover(
        task_id="T0100",
        project_id="proj_beta",  # Mismatch!
        branch="feature/alpha",
        baseline_commit="commit_base_111",
        candidate_commit="commit_cand_222",
        modified_files=("src/main.py",),
        diff_stat={"insertions": 10},
        test_summary={"passed": 5},
        workspace_dir="/ws/alpha",
        worktree_dir="/wt/alpha",
        builder_session_id="sess_b_1",
        builder_invocation_id="sess_b_1:step_1",
        auth_context="auth_user_1",
        billing_context="billing_team_1"
    )
    with pytest.raises(OrchestrationSecurityError, match="Cross-project boundary violation"):
        orchestrator.submit_to_reviewer(bad_handover_proj)

    # Attempt submit with wrong auth_context
    bad_handover_auth = BuilderToReviewerHandover(
        task_id="T0100",
        project_id="proj_alpha",
        branch="feature/alpha",
        baseline_commit="commit_base_111",
        candidate_commit="commit_cand_222",
        modified_files=("src/main.py",),
        diff_stat={"insertions": 10},
        test_summary={"passed": 5},
        workspace_dir="/ws/alpha",
        worktree_dir="/wt/alpha",
        builder_session_id="sess_b_1",
        builder_invocation_id="sess_b_1:step_1",
        auth_context="auth_user_2",  # Mismatch!
        billing_context="billing_team_1"
    )
    with pytest.raises(OrchestrationSecurityError, match="Cross-auth-context boundary violation"):
        orchestrator.submit_to_reviewer(bad_handover_auth)


def test_orchestrator_full_deterministic_lifecycle_with_real_gate(mock_dual_registry, mock_evidence_env):
    """Test full happy path with real EvidenceGate verification at every gate."""
    store, gate, proj_root = mock_evidence_env
    orchestrator = Orchestrator(registry=mock_dual_registry, evidence_store=store, evidence_gate=gate, project_root=proj_root)

    task_id = "T0200"
    project_id = "proj_alpha"
    branch = "feature/f1"
    base_commit = "base_commit_sha"
    cand_commit = "candidate_commit_sha"

    # Handles
    builder_handle = AgentHandle(
        session_id="sess_builder_001",
        host_id="codex_cli",
        status=AgentStatus.UNKNOWN,
        is_real_host=True,
        adapter_instance_id="inst_b1",
        invocation_token="tok_b1",
    )
    reviewer_handle = AgentHandle(
        session_id="sess_reviewer_002",
        host_id="codex_cli",
        status=AgentStatus.UNKNOWN,
        is_real_host=True,
        adapter_instance_id="inst_r2",
        invocation_token="tok_r2",
    )
    qa_handle = AgentHandle(
        session_id="sess_qa_003",
        host_id="codex_cli",
        status=AgentStatus.UNKNOWN,
        is_real_host=True,
        adapter_instance_id="inst_q3",
        invocation_token="tok_q3",
    )
    user_handle = AgentHandle(
        session_id="sess_user_004",
        host_id="user_console",
        status=AgentStatus.UNKNOWN,
        is_real_host=True,
        adapter_instance_id="inst_u4",
        invocation_token="tok_u4",
    )

    codex_caps = mock_dual_registry.get("codex_cli").detect_capabilities()

    # 1. Builder starts
    session = orchestrator.start_builder(
        task_id=task_id,
        project_id=project_id,
        branch=branch,
        baseline_commit=base_commit,
        assignee="李开发",
        workspace_dir=proj_root,
        worktree_dir=proj_root,
        auth_context="auth_ctx_main",
        billing_context="billing_ctx_main",
        builder_session_id="sess_builder_001",
        builder_invocation_id="sess_builder_001:step_1",
        builder_adapter_id="codex_cli"
    )
    assert session.state == OrchestrationState.BUILDING
    assert session.current_role == OrchestrationRole.BUILDER

    # Prepare real Evidence for Builder -> Reviewer
    evi_b_id = "evi_trans_builder_to_reviewer_001"
    _create_helper_evidence(
        store=store,
        evidence_id=evi_b_id,
        evidence_type=EvidenceType.TASK_TRANSITION,
        project_id=project_id,
        task_id=task_id,
        actor_role="BUILDER",
        transition_from="BUILDING",
        transition_to="REVIEWING",
        baseline_commit=base_commit,
        result_commit=cand_commit,
        host_id="codex_cli",
        adapter="codex_cli",
        host_session_id="sess_builder_001",
        host_invocation_id="sess_builder_001:step_1",
        workspace_mode="workspace_write",
        capabilities=codex_caps,
    )

    # 2. Builder submits to Reviewer with real evidence & handle
    b_handover = BuilderToReviewerHandover(
        task_id=task_id,
        project_id=project_id,
        branch=branch,
        baseline_commit=base_commit,
        candidate_commit=cand_commit,
        modified_files=("scripts/core.py",),
        diff_stat={"insertions": 50},
        test_summary={"passed": 10},
        workspace_dir=proj_root,
        worktree_dir=proj_root,
        builder_session_id="sess_builder_001",
        builder_invocation_id="sess_builder_001:step_1",
        auth_context="auth_ctx_main",
        billing_context="billing_ctx_main"
    )
    session = orchestrator.submit_to_reviewer(b_handover, evidence_id=evi_b_id, host_handle=builder_handle)
    assert session.state == OrchestrationState.REVIEWING
    assert session.current_role == OrchestrationRole.REVIEWER
    assert session.last_evidence_id == evi_b_id

    # Prepare real Evidence for Reviewer -> QA
    evi_r_id = "evi_trans_reviewer_to_qa_002"
    _create_helper_evidence(
        store=store,
        evidence_id=evi_r_id,
        evidence_type=EvidenceType.TASK_TRANSITION,
        project_id=project_id,
        task_id=task_id,
        actor_role="REVIEWER",
        transition_from="REVIEWING",
        transition_to="TESTING",
        baseline_commit=base_commit,
        result_commit=cand_commit,
        host_id="codex_cli",
        adapter="codex_cli",
        host_session_id="sess_reviewer_002",
        host_invocation_id="sess_reviewer_002:step_1",
        workspace_mode="workspace_read",
        capabilities=codex_caps,
    )

    # 3. Reviewer approves -> transitions to QA with real evidence
    r_handover = ReviewerToQAHandover(
        task_id=task_id,
        project_id=project_id,
        branch=branch,
        candidate_commit=cand_commit,
        reviewer_decision="PASS",
        review_comments="Code looks good, all constraints respected.",
        review_evidence_id=evi_r_id,
        reviewer_session_id="sess_reviewer_002",
        reviewer_invocation_id="sess_reviewer_002:step_1",
        workspace_dir=proj_root,
        worktree_dir=proj_root,
        auth_context="auth_ctx_main",
        billing_context="billing_ctx_main"
    )
    session = orchestrator.pass_reviewer_to_qa(r_handover, evidence_id=evi_r_id, host_handle=reviewer_handle)
    assert session.state == OrchestrationState.TESTING
    assert session.current_role == OrchestrationRole.QA
    assert session.last_evidence_id == evi_r_id

    # Prepare real Evidence for QA -> User Acceptance
    evi_q_id = "evi_trans_qa_to_user_003"
    _create_helper_evidence(
        store=store,
        evidence_id=evi_q_id,
        evidence_type=EvidenceType.TASK_TRANSITION,
        project_id=project_id,
        task_id=task_id,
        actor_role="QA",
        transition_from="TESTING",
        transition_to="PENDING_USER_ACCEPTANCE",
        baseline_commit=base_commit,
        result_commit=cand_commit,
        host_id="codex_cli",
        adapter="codex_cli",
        host_session_id="sess_qa_003",
        host_invocation_id="sess_qa_003:step_1",
        workspace_mode="workspace_read",
        capabilities=codex_caps,
    )

    # 4. QA passes -> advances to User Acceptance with real evidence
    qa_req = UserAcceptanceRequest(
        task_id=task_id,
        project_id=project_id,
        branch=branch,
        candidate_commit=cand_commit,
        qa_report={"total": 20, "passed": 20, "failed": 0},
        reviewer_report={"decision": "PASS"},
        artifacts=(),
        user_confirmation_prompt="Please review candidate candidate_commit_sha and confirm acceptance.",
        auth_context="auth_ctx_main"
    )
    session = orchestrator.pass_qa_to_user_acceptance(
        request=qa_req,
        qa_session_id="sess_qa_003",
        qa_invocation_id="sess_qa_003:step_1",
        evidence_id=evi_q_id,
        host_handle=qa_handle,
    )
    assert session.state == OrchestrationState.PENDING_USER_ACCEPTANCE
    assert session.current_role == OrchestrationRole.USER
    assert session.last_evidence_id == evi_q_id

    # Prepare real Evidence & ConfirmationResult for User Acceptance
    confirm_res = ConfirmationResult(
        request_id="req_conf_001",
        selected_option="approve",
        is_confirmed=True,
        is_real_host=True,
    )
    user_caps = HostCapabilities(
        is_real_host=True,
        supports_interactive_confirmation=CapabilitySupport.SUPPORTED,
    )
    evi_u_id = "evi_user_confirmation_004"
    _create_helper_evidence(
        store=store,
        evidence_id=evi_u_id,
        evidence_type=EvidenceType.USER_CONFIRMATION,
        project_id=project_id,
        task_id=task_id,
        actor_role="USER",
        transition_from="PENDING_USER_ACCEPTANCE",
        transition_to="ACCEPTED",
        baseline_commit=base_commit,
        result_commit=cand_commit,
        host_id="user_console",
        adapter="user_console",
        host_session_id="sess_user_004",
        host_invocation_id="user_confirmation",
        workspace_mode="workspace_read",
        capabilities=user_caps,
        extra_meta={
            "confirmation_id": "req_conf_001",
            "user_source": "explicit_user",
            "confirmed_at": time.time(),
        },
    )

    # 5. User accepts explicitly with valid ConfirmationResult and evidence
    user_dec = UserAcceptanceDecision(
        task_id=task_id,
        user_source="explicit_user",
        is_accepted=True,
        remarks="Approved by user after review",
        user_signature="user_sig_abc"
    )
    session = orchestrator.confirm_user_acceptance(
        decision=user_dec,
        evidence_id=evi_u_id,
        confirmation_result=confirm_res,
        host_handle=user_handle,
    )
    assert session.state == OrchestrationState.ACCEPTED
    assert session.current_role == OrchestrationRole.USER


def test_orchestrator_ghost_evidence_fail_closed(mock_dual_registry, mock_evidence_env):
    """DEF-T0053-2: Verify missing or ghost evidence strictly fails closed."""
    store, gate, proj_root = mock_evidence_env
    orchestrator = Orchestrator(registry=mock_dual_registry, evidence_store=store, evidence_gate=gate, project_root=proj_root)

    orchestrator.start_builder(
        task_id="T0250",
        project_id="proj_alpha",
        branch="feature/f1",
        baseline_commit="base_sha",
        assignee="李开发",
        workspace_dir=proj_root,
        worktree_dir=proj_root,
        auth_context="auth_ctx",
        billing_context="billing_ctx",
        builder_session_id="sess_b",
        builder_invocation_id="sess_b:1",
        builder_adapter_id="codex_cli"
    )

    b_handover = BuilderToReviewerHandover(
        task_id="T0250",
        project_id="proj_alpha",
        branch="feature/f1",
        baseline_commit="base_sha",
        candidate_commit="cand_sha",
        modified_files=("a.py",),
        diff_stat={},
        test_summary={},
        workspace_dir=proj_root,
        worktree_dir=proj_root,
        builder_session_id="sess_b",
        builder_invocation_id="sess_b:1",
        auth_context="auth_ctx",
        billing_context="billing_ctx"
    )

    builder_handle = AgentHandle(
        session_id="sess_b",
        host_id="codex_cli",
        status=AgentStatus.UNKNOWN,
        is_real_host=True,
        adapter_instance_id="inst_b",
        invocation_token="tok_b"
    )

    # Passing a non-existent ghost evidence ID must Fail-Closed
    with pytest.raises(OrchestrationGateError, match="Evidence validation failed"):
        orchestrator.submit_to_reviewer(b_handover, evidence_id="ghost_evidence_123", host_handle=builder_handle)

    # Submitting to an orchestrator with no evidence infrastructure must Fail-Closed
    bare_orchestrator = Orchestrator(registry=mock_dual_registry, evidence_store=None, evidence_gate=None, project_root=proj_root)
    bare_orchestrator.start_builder(
        task_id="T0250",
        project_id="proj_alpha",
        branch="feature/f1",
        baseline_commit="base_sha",
        assignee="李开发",
        workspace_dir=proj_root,
        worktree_dir=proj_root,
        auth_context="auth_ctx",
        billing_context="billing_ctx",
        builder_session_id="sess_b",
        builder_invocation_id="sess_b:1",
        builder_adapter_id="codex_cli"
    )
    with pytest.raises(OrchestrationGateError, match="EvidenceGate/EvidenceStore is not configured"):
        bare_orchestrator.submit_to_reviewer(b_handover, evidence_id="ghost_evidence_123", host_handle=builder_handle)


def test_orchestrator_user_acceptance_credential_verification(mock_dual_registry, mock_evidence_env):
    """DEF-T0053-3: Verify user acceptance strictly requires ConfirmationResult with is_real_host=True."""
    store, gate, proj_root = mock_evidence_env
    orchestrator = Orchestrator(registry=mock_dual_registry, evidence_store=store, evidence_gate=gate, project_root=proj_root)

    orchestrator.start_builder(
        task_id="T0260",
        project_id="proj_alpha",
        branch="feature/f1",
        baseline_commit="base_sha",
        assignee="李开发",
        workspace_dir=proj_root,
        worktree_dir=proj_root,
        auth_context="auth_ctx",
        billing_context="billing_ctx",
        builder_session_id="sess_b",
        builder_invocation_id="sess_b:1",
        builder_adapter_id="codex_cli"
    )

    b_handover = BuilderToReviewerHandover(
        task_id="T0260",
        project_id="proj_alpha",
        branch="feature/f1",
        baseline_commit="base_sha",
        candidate_commit="cand_sha",
        modified_files=("a.py",),
        diff_stat={},
        test_summary={},
        workspace_dir=proj_root,
        worktree_dir=proj_root,
        builder_session_id="sess_b",
        builder_invocation_id="sess_b:1",
        auth_context="auth_ctx",
        billing_context="billing_ctx"
    )
    orchestrator.submit_to_reviewer(b_handover)

    # Set up session in PENDING_USER_ACCEPTANCE
    session = orchestrator.get_session("T0260")
    session.state = OrchestrationState.PENDING_USER_ACCEPTANCE
    session.candidate_commit = "cand_sha"

    # 1. Acceptance without confirmation_result -> Rejected
    user_dec = UserAcceptanceDecision(
        task_id="T0260",
        user_source="explicit_user",
        is_accepted=True,
        remarks="Self-accepted without credential"
    )
    with pytest.raises(OrchestrationSecurityError, match="strictly requires a ConfirmationResult credential"):
        orchestrator.confirm_user_acceptance(user_dec, confirmation_result=None)

    # 2. Acceptance with fake is_real_host=False -> Rejected
    fake_res = ConfirmationResult(
        request_id="req_1",
        selected_option="approve",
        is_confirmed=True,
        is_real_host=False,
    )
    with pytest.raises(OrchestrationSecurityError, match="ConfirmationResult must have is_real_host=True"):
        orchestrator.confirm_user_acceptance(user_dec, confirmation_result=fake_res)

    # 3. Acceptance with is_confirmed=False -> Rejected
    rejected_res = ConfirmationResult(
        request_id="req_1",
        selected_option="deny",
        is_confirmed=False,
        is_real_host=True,
    )
    with pytest.raises(OrchestrationSecurityError, match="ConfirmationResult is_confirmed is False"):
        orchestrator.confirm_user_acceptance(user_dec, confirmation_result=rejected_res)

    # 4. Valid acceptance with real ConfirmationResult -> Accepted
    valid_res = ConfirmationResult(
        request_id="req_1",
        selected_option="approve",
        is_confirmed=True,
        is_real_host=True,
    )
    session_after = orchestrator.confirm_user_acceptance(user_dec, confirmation_result=valid_res)
    assert session_after.state == OrchestrationState.ACCEPTED


def test_orchestrator_state_jumping_rejection(mock_dual_registry, mock_evidence_env):
    """Verify state machine strictly rejects illegal jumping transitions."""
    store, gate, proj_root = mock_evidence_env
    orchestrator = Orchestrator(registry=mock_dual_registry, evidence_store=store, evidence_gate=gate, project_root=proj_root)

    orchestrator.start_builder(
        task_id="T0300",
        project_id="proj_alpha",
        branch="feature/f1",
        baseline_commit="base_sha",
        assignee="李开发",
        workspace_dir=proj_root,
        worktree_dir=proj_root,
        auth_context="auth_ctx",
        billing_context="billing_ctx",
        builder_session_id="sess_b",
        builder_invocation_id="sess_b:1",
        builder_adapter_id="codex_cli"
    )

    # In BUILDING, cannot call pass_reviewer_to_qa
    r_handover = ReviewerToQAHandover(
        task_id="T0300",
        project_id="proj_alpha",
        branch="feature/f1",
        candidate_commit="cand_sha",
        reviewer_decision="PASS",
        review_comments="Skipped review",
        review_evidence_id="evi_fake",
        reviewer_session_id="sess_r",
        reviewer_invocation_id="sess_r:1",
        workspace_dir=proj_root,
        worktree_dir=proj_root,
        auth_context="auth_ctx",
        billing_context="billing_ctx"
    )
    with pytest.raises(OrchestrationStateError, match="expected REVIEWING"):
        orchestrator.pass_reviewer_to_qa(r_handover, evidence_id="evi_fake")

    # In BUILDING, cannot confirm user acceptance
    user_dec = UserAcceptanceDecision(
        task_id="T0300",
        user_source="explicit_user",
        is_accepted=True,
        remarks="Self-accept"
    )
    with pytest.raises(OrchestrationStateError, match="expected PENDING_USER_ACCEPTANCE"):
        orchestrator.confirm_user_acceptance(user_dec)


def test_orchestrator_role_session_isolation(mock_dual_registry, mock_evidence_env):
    """Verify distinct roles cannot reuse the same session_id."""
    store, gate, proj_root = mock_evidence_env
    orchestrator = Orchestrator(registry=mock_dual_registry, evidence_store=store, evidence_gate=gate, project_root=proj_root)

    # Builder starts with session 'sess_shared'
    orchestrator.start_builder(
        task_id="T0400",
        project_id="proj_alpha",
        branch="feature/f1",
        baseline_commit="base_sha",
        assignee="李开发",
        workspace_dir=proj_root,
        worktree_dir=proj_root,
        auth_context="auth_ctx",
        billing_context="billing_ctx",
        builder_session_id="sess_shared",
        builder_invocation_id="sess_shared:1",
        builder_adapter_id="codex_cli"
    )

    b_handover = BuilderToReviewerHandover(
        task_id="T0400",
        project_id="proj_alpha",
        branch="feature/f1",
        baseline_commit="base_sha",
        candidate_commit="cand_sha",
        modified_files=("a.py",),
        diff_stat={},
        test_summary={},
        workspace_dir=proj_root,
        worktree_dir=proj_root,
        builder_session_id="sess_shared",
        builder_invocation_id="sess_shared:1",
        auth_context="auth_ctx",
        billing_context="billing_ctx"
    )
    orchestrator.submit_to_reviewer(b_handover)

    # Reviewer attempts to use builder's session 'sess_shared' -> REJECTED
    r_handover_reused = ReviewerToQAHandover(
        task_id="T0400",
        project_id="proj_alpha",
        branch="feature/f1",
        candidate_commit="cand_sha",
        reviewer_decision="PASS",
        review_comments="Same session",
        review_evidence_id="evi_r",
        reviewer_session_id="sess_shared",  # Violates isolation!
        reviewer_invocation_id="sess_shared:2",
        workspace_dir=proj_root,
        worktree_dir=proj_root,
        auth_context="auth_ctx",
        billing_context="billing_ctx"
    )
    with pytest.raises(OrchestrationSessionIsolationError, match="cannot be identical to Builder session"):
        orchestrator.pass_reviewer_to_qa(r_handover_reused, evidence_id="evi_r")


def test_orchestrator_reviewer_rejection_and_rework_loop(mock_dual_registry, mock_evidence_env):
    """Test Reviewer rejection loops back to Builder on the same task ID."""
    store, gate, proj_root = mock_evidence_env
    orchestrator = Orchestrator(registry=mock_dual_registry, evidence_store=store, evidence_gate=gate, project_root=proj_root)

    orchestrator.start_builder(
        task_id="T0500",
        project_id="proj_alpha",
        branch="feature/f1",
        baseline_commit="base_sha",
        assignee="李开发",
        workspace_dir=proj_root,
        worktree_dir=proj_root,
        auth_context="auth_ctx",
        billing_context="billing_ctx",
        builder_session_id="sess_builder_1",
        builder_invocation_id="sess_builder_1:1",
        builder_adapter_id="codex_cli"
    )

    b_handover = BuilderToReviewerHandover(
        task_id="T0500",
        project_id="proj_alpha",
        branch="feature/f1",
        baseline_commit="base_sha",
        candidate_commit="cand_sha_v1",
        modified_files=("a.py",),
        diff_stat={},
        test_summary={},
        workspace_dir=proj_root,
        worktree_dir=proj_root,
        builder_session_id="sess_builder_1",
        builder_invocation_id="sess_builder_1:1",
        auth_context="auth_ctx",
        billing_context="billing_ctx"
    )
    orchestrator.submit_to_reviewer(b_handover)

    # Reviewer rejects
    reject_handover = DefectRejectionHandover(
        task_id="T0500",
        project_id="proj_alpha",
        source_role=OrchestrationRole.REVIEWER,
        defect_list=("DEF-1: Missing bounds check", "DEF-2: Incomplete docstring"),
        comments="Please fix defects DEF-1 and DEF-2",
        candidate_commit="cand_sha_v1",
        source_session_id="sess_reviewer_1",
        source_invocation_id="sess_reviewer_1:1",
        target_builder_role=OrchestrationRole.BUILDER,
        target_builder_assignee="李开发"
    )
    session = orchestrator.reject_by_reviewer(reject_handover)
    assert session.state == OrchestrationState.REJECTED_BY_REVIEWER
    assert session.current_role == OrchestrationRole.BUILDER
    assert session.current_assignee == "李开发"

    # Builder re-starts rework on same task
    session = orchestrator.start_builder(
        task_id="T0500",
        project_id="proj_alpha",
        branch="feature/f1",
        baseline_commit="base_sha",
        assignee="李开发",
        workspace_dir=proj_root,
        worktree_dir=proj_root,
        auth_context="auth_ctx",
        billing_context="billing_ctx",
        builder_session_id="sess_builder_rework",
        builder_invocation_id="sess_builder_rework:1",
        builder_adapter_id="codex_cli"
    )
    assert session.state == OrchestrationState.BUILDING

    # Builder re-submits fixed candidate
    b_handover_v2 = BuilderToReviewerHandover(
        task_id="T0500",
        project_id="proj_alpha",
        branch="feature/f1",
        baseline_commit="base_sha",
        candidate_commit="cand_sha_v2",
        modified_files=("a.py",),
        diff_stat={},
        test_summary={},
        workspace_dir=proj_root,
        worktree_dir=proj_root,
        builder_session_id="sess_builder_rework",
        builder_invocation_id="sess_builder_rework:1",
        auth_context="auth_ctx",
        billing_context="billing_ctx"
    )
    session = orchestrator.submit_to_reviewer(b_handover_v2)
    assert session.state == OrchestrationState.REVIEWING


def test_orchestrator_qa_rejection_and_rework_loop(mock_dual_registry, mock_evidence_env):
    """Test QA rejection loops back to Builder on the same task ID."""
    store, gate, proj_root = mock_evidence_env
    orchestrator = Orchestrator(registry=mock_dual_registry, evidence_store=store, evidence_gate=gate, project_root=proj_root)

    orchestrator.start_builder(
        task_id="T0600",
        project_id="proj_alpha",
        branch="feature/f1",
        baseline_commit="base_sha",
        assignee="李开发",
        workspace_dir=proj_root,
        worktree_dir=proj_root,
        auth_context="auth_ctx",
        billing_context="billing_ctx",
        builder_session_id="sess_b",
        builder_invocation_id="sess_b:1",
        builder_adapter_id="codex_cli"
    )
    b_handover = BuilderToReviewerHandover(
        task_id="T0600",
        project_id="proj_alpha",
        branch="feature/f1",
        baseline_commit="base_sha",
        candidate_commit="cand_sha_v1",
        modified_files=("a.py",),
        diff_stat={},
        test_summary={},
        workspace_dir=proj_root,
        worktree_dir=proj_root,
        builder_session_id="sess_b",
        builder_invocation_id="sess_b:1",
        auth_context="auth_ctx",
        billing_context="billing_ctx"
    )
    orchestrator.submit_to_reviewer(b_handover)

    # Set up session in TESTING
    session = orchestrator.get_session("T0600")
    session.state = OrchestrationState.TESTING
    session.reviewer_session_id = "sess_r"
    session.candidate_commit = "cand_sha_v1"

    # QA rejects
    qa_reject = DefectRejectionHandover(
        task_id="T0600",
        project_id="proj_alpha",
        source_role=OrchestrationRole.QA,
        defect_list=("TEST-FAIL: test_timeout failed",),
        comments="Regression test failed in QA suite",
        candidate_commit="cand_sha_v1",
        source_session_id="sess_qa_1",
        source_invocation_id="sess_qa_1:1",
        target_builder_role=OrchestrationRole.BUILDER,
        target_builder_assignee="李开发"
    )
    session = orchestrator.reject_by_qa(qa_reject)
    assert session.state == OrchestrationState.REJECTED_BY_QA
    assert session.current_role == OrchestrationRole.BUILDER
    assert session.current_assignee == "李开发"


def test_orchestrator_modes_and_static_only_guard(mock_dual_registry, mock_evidence_env):
    """Verify evaluation of manual/assisted/verified_automatic modes and Antigravity STATIC_ONLY guard."""
    store, gate, proj_root = mock_evidence_env
    orchestrator = Orchestrator(registry=mock_dual_registry, evidence_store=store, evidence_gate=gate, project_root=proj_root)

    # Antigravity is STATIC_ONLY on windows -> Mode is ASSISTED / MANUAL
    ag_mode = orchestrator.evaluate_orchestration_mode("antigravity", target_os="windows")
    assert ag_mode == OrchestrationMode.ASSISTED

    # Codex is CLI_VERIFIED on windows -> Mode is VERIFIED_AUTOMATIC
    codex_mode = orchestrator.evaluate_orchestration_mode("codex_cli", target_os="windows")
    assert codex_mode == OrchestrationMode.VERIFIED_AUTOMATIC

    # Assisted card generation produces valid structured YAML
    handover = BuilderToReviewerHandover(
        task_id="T0800",
        project_id="proj_alpha",
        branch="feature/ag",
        baseline_commit="base_sha",
        candidate_commit="cand_sha",
        modified_files=("app.py",),
        diff_stat={"insertions": 10},
        test_summary={"passed": 2},
        workspace_dir=proj_root,
        worktree_dir=proj_root,
        builder_session_id="sess_b",
        builder_invocation_id="sess_b:1",
        auth_context="auth_ctx",
        billing_context="billing_ctx"
    )
    card = orchestrator.generate_assisted_handover_card(handover)
    assert "handover_type: BUILDER_TO_REVIEWER" in card
    assert "task_id: T0800" in card
    assert "instruction:" in card

    # Dual-host verification evaluation
    dual_eval = orchestrator.evaluate_dual_host_verification("codex_cli", "antigravity", target_os="windows")
    assert dual_eval.status == DualHostVerificationStatus.NOT_READY
    assert dual_eval.is_dual_host_verified is False
    assert "static_only" in dual_eval.reason.lower()


def test_orchestrator_checkpoint_recovery_non_destructive(mock_dual_registry, mock_evidence_env):
    """Test checkpoint serialization and restoration without data loss or git modifications."""
    store, gate, proj_root = mock_evidence_env
    orchestrator1 = Orchestrator(registry=mock_dual_registry, evidence_store=store, evidence_gate=gate, project_root=proj_root)

    orchestrator1.start_builder(
        task_id="T0900",
        project_id="proj_alpha",
        branch="feature/f1",
        baseline_commit="base_sha",
        assignee="李开发",
        workspace_dir=proj_root,
        worktree_dir=proj_root,
        auth_context="auth_ctx",
        billing_context="billing_ctx",
        builder_session_id="sess_b",
        builder_invocation_id="sess_b:1",
        builder_adapter_id="codex_cli"
    )
    b_handover = BuilderToReviewerHandover(
        task_id="T0900",
        project_id="proj_alpha",
        branch="feature/f1",
        baseline_commit="base_sha",
        candidate_commit="cand_sha",
        modified_files=("a.py",),
        diff_stat={},
        test_summary={},
        workspace_dir=proj_root,
        worktree_dir=proj_root,
        builder_session_id="sess_b",
        builder_invocation_id="sess_b:1",
        auth_context="auth_ctx",
        billing_context="billing_ctx"
    )
    orchestrator1.submit_to_reviewer(b_handover)

    # Export checkpoint
    ckpt = orchestrator1.export_checkpoint("T0900")
    assert ckpt["task_id"] == "T0900"
    assert ckpt["state"] == "REVIEWING"
    assert ckpt["candidate_commit"] == "cand_sha"

    # Restore in new orchestrator instance
    orchestrator2 = Orchestrator(registry=mock_dual_registry, evidence_store=store, evidence_gate=gate, project_root=proj_root)
    restored_session = orchestrator2.import_checkpoint(ckpt)
    assert restored_session.task_id == "T0900"
    assert restored_session.state == OrchestrationState.REVIEWING
    assert restored_session.candidate_commit == "cand_sha"

    # Prepare evidence & handle for pass_reviewer_to_qa
    r_handle = AgentHandle(
        session_id="sess_r",
        host_id="codex_cli",
        status=AgentStatus.UNKNOWN,
        is_real_host=True,
        adapter_instance_id="inst_r",
        invocation_token="tok_r"
    )
    codex_caps = mock_dual_registry.get("codex_cli").detect_capabilities()
    _create_helper_evidence(
        store=store,
        evidence_id="evi_r_ckpt",
        evidence_type=EvidenceType.TASK_TRANSITION,
        project_id="proj_alpha",
        task_id="T0900",
        actor_role="REVIEWER",
        transition_from="REVIEWING",
        transition_to="TESTING",
        baseline_commit="base_sha",
        result_commit="cand_sha",
        host_id="codex_cli",
        adapter="codex_cli",
        host_session_id="sess_r",
        host_invocation_id="sess_r:1",
        workspace_mode="workspace_read",
        capabilities=codex_caps,
    )

    # Resume from checkpoint
    r_handover = ReviewerToQAHandover(
        task_id="T0900",
        project_id="proj_alpha",
        branch="feature/f1",
        candidate_commit="cand_sha",
        reviewer_decision="PASS",
        review_comments="Reviewed after restore",
        review_evidence_id="evi_r_ckpt",
        reviewer_session_id="sess_r",
        reviewer_invocation_id="sess_r:1",
        workspace_dir=proj_root,
        worktree_dir=proj_root,
        auth_context="auth_ctx",
        billing_context="billing_ctx"
    )
    session_after = orchestrator2.pass_reviewer_to_qa(r_handover, evidence_id="evi_r_ckpt", host_handle=r_handle)
    assert session_after.state == OrchestrationState.TESTING
