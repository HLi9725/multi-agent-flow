import builtins
import os
import subprocess
import pytest

from scripts._lib.core.agent_schema import (
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
from scripts._lib.core.evidence_schema import EvidenceType
from scripts._lib.core.evidence_gate import EvidenceValidationContext
from scripts._lib.core.adapter_manifest import (
    AdapterManifest,
    AuthBoundaryType,
    BillingBoundaryType,
    ExecutionMode,
    HostSurface,
    PlatformVerification,
    VerificationLevel,
)
from scripts._lib.core.adapter_conformance import (
    ConformanceError,
    FaultyAcceptForeignHandleAdapter,
    FaultyCapabilitiesMismatchAdapter,
    FaultyForgedRealHostHandleAdapter,
    FaultySideEffectSubprocessAdapter,
    FaultySideEffectWriteFileAdapter,
    StandardTestFakeAdapter,
    assert_capabilities_conformance,
    assert_handle_conformance,
    assert_lifecycle_conformance,
    assert_manifest_conformance,
    assert_zero_side_effects,
    create_standard_fake_manifest,
    run_adapter_conformance_suite,
)


def test_standard_fake_adapter_passes_conformance():
    adp = StandardTestFakeAdapter("std_fake")
    man = create_standard_fake_manifest("std_fake")
    result = run_adapter_conformance_suite(adp, man)

    assert result["status"] == "PASSED"
    assert result["adapter_id"] == "std_fake"
    assert result["is_real_host"] is False


def test_faulty_capabilities_mismatch_intercepted():
    adp = FaultyCapabilitiesMismatchAdapter("faulty_caps")
    man = create_standard_fake_manifest("faulty_caps")

    with pytest.raises(ConformanceError, match="detect_capabilities reports"):
        assert_capabilities_conformance(adp, man)


def test_faulty_forged_real_host_handle_intercepted():
    adp = FaultyForgedRealHostHandleAdapter("faulty_forge")
    req = AgentRequest(
        session_id="s1",
        prompt="test",
        role="DEV",
        workspace_dir="."
    )
    with pytest.raises(ConformanceError, match="must produce handle with is_real_host=False"):
        assert_handle_conformance(adp, req)


def test_faulty_foreign_handle_acceptance_intercepted():
    adp = FaultyAcceptForeignHandleAdapter("faulty_foreign")
    req = AgentRequest(
        session_id="s1",
        prompt="test",
        role="DEV",
        workspace_dir="."
    )
    with pytest.raises(ConformanceError, match="accepted foreign handle"):
        assert_lifecycle_conformance(adp, req)


def test_zero_side_effects_active_interception_write_file():
    # DEF-T0049-3: Malicious adapter writing file during detect_capabilities must be intercepted
    adp = FaultySideEffectWriteFileAdapter("faulty_writer")

    with pytest.raises(ConformanceError, match="Side-effect intercepted: builtins.open write attempt"):
        assert_zero_side_effects(adp)

    # Ensure no side effect file actually persisted
    assert not os.path.exists("unauthorized_side_effect.tmp")


def test_zero_side_effects_active_interception_subprocess():
    # DEF-T0049-3: Malicious adapter running subprocess during detect_capabilities must be intercepted
    adp = FaultySideEffectSubprocessAdapter("faulty_runner")

    with pytest.raises(ConformanceError, match="Side-effect intercepted: subprocess.run attempt"):
        assert_zero_side_effects(adp)


def test_adapter_timeout_and_cancel_lifecycle():
    # DEF-T0049-7: Adapter timeout and cancellation handling
    class TimeoutAndCancelAdapter(StandardTestFakeAdapter):
        def wait_for_result(self, handle: AgentHandle, timeout_seconds=None):
            raise AgentTimeoutError(f"Session {handle.session_id} timed out")

    adp = TimeoutAndCancelAdapter("timeout_adp")
    req = AgentRequest(session_id="sess_timeout", prompt="timeout prompt", role="DEV", workspace_dir=".")
    handle = adp.dispatch_agent(req)

    with pytest.raises(AgentTimeoutError, match="timed out"):
        adp.wait_for_result(handle, timeout_seconds=0.01)

    assert adp.cancel_agent(handle) is True
    # Second cancel on removed session returns False
    assert adp.cancel_agent(handle) is False


def test_evidence_context_and_worktree_compatibility_seam():
    # DEF-T0049-7: Verify that AdapterManifest identity fields and capabilities match 2B/2C structures
    man = create_standard_fake_manifest("compat_adapter")
    adp = StandardTestFakeAdapter("compat_adapter")

    caps = adp.detect_capabilities()
    assert "worktree" in man.capabilities
    assert caps.supports_worktree == CapabilitySupport.SUPPORTED
    assert "isolated" in man.workspace_modes
    assert "worktree" in man.workspace_modes

    # Verify compatibility with EvidenceValidationContext
    req = AgentRequest(session_id="sess_evidence", prompt="prompt", role="BUILDER", workspace_dir=".")
    handle = adp.dispatch_agent(req)

    ctx = EvidenceValidationContext(
        project_id="test_project",
        task_id="T0049",
        actor_role="DEV",
        transition_from="进行中",
        transition_to="审查中",
        baseline_commit="21828a88ae0aa65b7cf84ea9b1e4244737100892",
        result_commit="956853803bf43d82de1a2e62c4f66a5efb619533",
        expected_invocation_id="inv_001",
        expected_adapter=man.adapter_id,
        expected_workspace_mode="worktree",
        expected_evidence_type=EvidenceType.TASK_TRANSITION,
        host_handle=handle,
        expected_capabilities=caps
    )
    assert ctx.expected_adapter == "compat_adapter"
    assert ctx.host_handle.host_id == "compat_adapter"
