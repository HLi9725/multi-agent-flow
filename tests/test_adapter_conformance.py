import builtins
import os
import subprocess
import pytest

from scripts._lib.core.agent_schema import (
    AgentHandle,
    AgentRequest,
    AgentResult,
    AgentStatus,
    CapabilitySupport,
    HostCapabilities,
)
from scripts._lib.core.adapter_manifest import (
    AdapterManifest,
    AuthBoundaryType,
    BillingBoundaryType,
    HostSurface,
    PlatformVerification,
    VerificationLevel,
)
from scripts._lib.core.adapter_conformance import (
    ConformanceError,
    FaultyAcceptForeignHandleAdapter,
    FaultyCapabilitiesMismatchAdapter,
    FaultyForgedRealHostHandleAdapter,
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


def test_zero_side_effects_guarantee(monkeypatch):
    adp = StandardTestFakeAdapter("side_effect_checker")

    def forbidden_call(*args, **kwargs):
        pytest.fail(f"Side-effect call attempted with args: {args} kwargs: {kwargs}")

    # Guard file writes, subprocesses, and directory creations during detect_capabilities
    monkeypatch.setattr(os, "mkdir", forbidden_call)
    monkeypatch.setattr(os, "makedirs", forbidden_call)
    monkeypatch.setattr(subprocess, "run", forbidden_call)
    monkeypatch.setattr(subprocess, "Popen", forbidden_call)

    # Calling detect_capabilities must succeed with zero side-effects
    assert_zero_side_effects(adp)
    caps = adp.detect_capabilities()
    assert isinstance(caps, HostCapabilities)


def test_evidence_context_and_worktree_compatibility_seam():
    # Verify that AdapterManifest identity fields and capabilities match 2B/2C structures
    man = create_standard_fake_manifest("compat_adapter")
    adp = StandardTestFakeAdapter("compat_adapter")

    caps = adp.detect_capabilities()
    assert "worktree" in man.capabilities
    assert caps.supports_worktree == CapabilitySupport.SUPPORTED
    assert "isolated" in man.workspace_modes
    assert "worktree" in man.workspace_modes
