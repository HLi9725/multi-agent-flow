import os
import pytest
import threading

from scripts._lib.core.agent_schema import CapabilitySupport, HostCapabilities
from scripts._lib.core.adapter_manifest import (
    AdapterManifest,
    AuthBoundaryType,
    BillingBoundaryType,
    ExecutionMode,
    HostSurface,
    PlatformVerification,
    VerificationLevel,
)
from scripts._lib.core.adapter_registry import (
    AdapterRegistry,
    AdapterRegistryError,
    AdapterResolutionDecision,
    AdapterResolutionRequest,
    ResolutionStatus,
)
from scripts._lib.core.adapter_conformance import (
    StandardTestFakeAdapter,
    create_standard_fake_manifest,
)


def test_registry_registration_and_get():
    registry = AdapterRegistry(context_id="ctx_1")
    adapter = StandardTestFakeAdapter(adapter_id="fake_1")
    manifest = create_standard_fake_manifest("fake_1")

    registry.register(adapter, manifest)
    assert registry.get("fake_1") is adapter
    assert registry.get_manifest("fake_1") is manifest
    assert len(registry.list_manifests()) == 1

    # Duplicate registration rejected
    with pytest.raises(AdapterRegistryError, match="already registered"):
        registry.register(adapter, manifest)


def test_registry_adapter_id_mismatch_rejected():
    # DEF-T0049-4: Adapter implementation ID must match Manifest ID
    registry = AdapterRegistry(context_id="ctx_id_check")
    adapter = StandardTestFakeAdapter(adapter_id="adapter_impl_id")
    manifest = create_standard_fake_manifest("manifest_id_differs")

    with pytest.raises(AdapterRegistryError, match="does not match Manifest adapter_id"):
        registry.register(adapter, manifest)

    # Ensure no partial state remained
    assert registry.get("manifest_id_differs") is None
    assert len(registry.list_manifests()) == 0


def test_registry_fake_adapter_cannot_claim_verified():
    registry = AdapterRegistry(context_id="ctx_2")
    adapter = StandardTestFakeAdapter(adapter_id="fake_2")

    pv_win = PlatformVerification(
        operating_system="windows",
        host_surface=HostSurface.NATIVE,
        verification_level=VerificationLevel.NATIVE_VERIFIED,
        verified_version="1.0.0",
        verified_at="2026-08-26T10:00:00Z",
        e2e_evidence_refs=("fake-e2e-ref",)
    )
    manifest = AdapterManifest(
        schema_version="2.0",
        adapter_id="fake_2",
        display_name="Fake Claiming Verified",
        implementation_version="1.0.0",
        host_surface=HostSurface.NATIVE,
        verification_level=VerificationLevel.NATIVE_VERIFIED,
        capabilities={},
        workspace_modes=("isolated",),
        identity_fields=("id",),
        auth_boundary=AuthBoundaryType.NONE,
        billing_boundary=BillingBoundaryType.UNMETERED,
        platform_version_constraint=">=1.0.0",
        supported_operating_systems=("windows",),
        platform_verifications={"windows": pv_win},
        executable_candidates_by_os={"windows": ()},
        config_path_templates_by_os={"windows": ()},
        conformance_suite_version="2.0",
        verified_at="2026-08-26T10:00:00Z",
        e2e_evidence_refs=("fake-e2e-ref",)
    )

    with pytest.raises(AdapterRegistryError, match="cannot be registered as native_verified"):
        registry.register(adapter, manifest)


def test_registry_context_and_project_id_binding():
    # DEF-T0049-5: Registry context_id bound to request.project_id
    reg_a = AdapterRegistry(context_id="project_A")
    reg_b = AdapterRegistry(context_id="project_B")

    adp_a = StandardTestFakeAdapter(adapter_id="adp_a")
    man_a = create_standard_fake_manifest("adp_a")
    reg_a.register(adp_a, man_a)

    assert reg_a.get("adp_a") is not None
    assert reg_b.get("adp_a") is None

    # Request matching project_A succeeds
    req_match = AdapterResolutionRequest(
        project_id="project_A",
        allowed_verification_levels=(VerificationLevel.STATIC_ONLY,)
    )
    assert reg_a.resolve(req_match).decision_status == ResolutionStatus.SELECTED

    # Request with mismatched project_B on reg_a fails-closed
    req_mismatch = AdapterResolutionRequest(
        project_id="project_B",
        allowed_verification_levels=(VerificationLevel.STATIC_ONLY,)
    )
    d_mismatch = reg_a.resolve(req_mismatch)
    assert d_mismatch.decision_status == ResolutionStatus.UNSUPPORTED
    assert "Project ID mismatch" in d_mismatch.reason


def test_registry_verification_level_fail_closed_and_empty_set():
    # DEF-T0049-1: Empty allowed_verification_levels must raise ValueError
    with pytest.raises(ValueError, match="non-empty tuple of VerificationLevel"):
        AdapterResolutionRequest(
            project_id="p1",
            allowed_verification_levels=()  # Empty!
        )


def test_registry_unsupported_level_never_selected():
    # DEF-T0049-1: UNSUPPORTED verification level can NEVER be selected
    registry = AdapterRegistry(context_id="p1")

    pv_win = PlatformVerification(
        operating_system="windows",
        host_surface=HostSurface.SIMULATED,
        verification_level=VerificationLevel.UNSUPPORTED,
        verified_version="1.0.0"
    )
    man = AdapterManifest(
        schema_version="2.0",
        adapter_id="unsupported_adp",
        display_name="Unsupported",
        implementation_version="1.0.0",
        host_surface=HostSurface.SIMULATED,
        verification_level=VerificationLevel.UNSUPPORTED,
        capabilities={},
        workspace_modes=("isolated",),
        identity_fields=("id",),
        auth_boundary=AuthBoundaryType.NONE,
        billing_boundary=BillingBoundaryType.UNMETERED,
        platform_version_constraint=">=1.0.0",
        supported_operating_systems=("windows",),
        platform_verifications={"windows": pv_win},
        executable_candidates_by_os={"windows": ()},
        config_path_templates_by_os={"windows": ()},
        conformance_suite_version="2.0"
    )
    adp = StandardTestFakeAdapter(adapter_id="unsupported_adp")
    registry.register(adp, man)

    # Even if allowed_verification_levels contains UNSUPPORTED, it must be rejected!
    req = AdapterResolutionRequest(
        project_id="p1",
        allowed_verification_levels=(VerificationLevel.UNSUPPORTED, VerificationLevel.STATIC_ONLY)
    )
    decision = registry.resolve(req)
    assert decision.decision_status == ResolutionStatus.UNSUPPORTED
    assert "UNSUPPORTED can never be selected" in decision.reason


def test_registry_static_only_rejected_for_verified_automatic():
    # DEF-T0049-1: STATIC_ONLY cannot enter verified_automatic execution mode
    registry = AdapterRegistry(context_id="p1")
    adp = StandardTestFakeAdapter(adapter_id="static_adp")
    man = create_standard_fake_manifest("static_adp")
    registry.register(adp, man)

    req_auto = AdapterResolutionRequest(
        project_id="p1",
        execution_mode=ExecutionMode.VERIFIED_AUTOMATIC,
        allowed_verification_levels=(VerificationLevel.STATIC_ONLY,)
    )
    decision = registry.resolve(req_auto)
    assert decision.decision_status == ResolutionStatus.UNSUPPORTED
    assert "STATIC_ONLY" in decision.reason


def test_registry_ghost_capability_rejected():
    # DEF-T0049-2: Manifest claims ghost capability not present in HostCapabilities
    registry = AdapterRegistry(context_id="p1")

    pv_win = PlatformVerification(
        operating_system="windows",
        host_surface=HostSurface.SIMULATED,
        verification_level=VerificationLevel.STATIC_ONLY,
        verified_version="1.0.0"
    )
    man = AdapterManifest(
        schema_version="2.0",
        adapter_id="ghost_adp",
        display_name="Ghost Capability Adapter",
        implementation_version="1.0.0",
        host_surface=HostSurface.SIMULATED,
        verification_level=VerificationLevel.STATIC_ONLY,
        capabilities={"ghost_capability_x": "supported"},  # Ghost capability!
        workspace_modes=("isolated",),
        identity_fields=("id",),
        auth_boundary=AuthBoundaryType.NONE,
        billing_boundary=BillingBoundaryType.UNMETERED,
        platform_version_constraint=">=1.0.0",
        supported_operating_systems=("windows",),
        platform_verifications={"windows": pv_win},
        executable_candidates_by_os={"windows": ()},
        config_path_templates_by_os={"windows": ()},
        conformance_suite_version="2.0"
    )
    adp = StandardTestFakeAdapter(adapter_id="ghost_adp")
    registry.register(adp, man)

    # Resolution requesting ghost_capability_x must fail-closed
    req = AdapterResolutionRequest(
        project_id="p1",
        required_capabilities=("ghost_capability_x",),
        allowed_verification_levels=(VerificationLevel.STATIC_ONLY,)
    )
    decision = registry.resolve(req)
    assert decision.decision_status == ResolutionStatus.UNSUPPORTED
    assert "ghost_capability_x" in decision.missing_capabilities


def test_registry_exact_id_no_manual_fallback():
    # DEF-T0049-7: Exact adapter_id specified cannot use manual fallback
    registry = AdapterRegistry(context_id="p1")
    adp = StandardTestFakeAdapter(adapter_id="my_exact_adapter")
    man = create_standard_fake_manifest("my_exact_adapter")
    registry.register(adp, man)

    # Exact match missing with allow_manual_fallback=True -> must still be UNSUPPORTED!
    req_missing = AdapterResolutionRequest(
        project_id="p1",
        adapter_id="unregistered_adapter",
        allowed_verification_levels=(VerificationLevel.STATIC_ONLY,),
        allow_manual_fallback=True
    )
    decision_missing = registry.resolve(req_missing)
    assert decision_missing.decision_status == ResolutionStatus.UNSUPPORTED


def test_registry_selection_strategy_validation():
    # DEF-T0049-7: Invalid selection_strategy must raise ValueError
    with pytest.raises(ValueError, match="Invalid selection_strategy"):
        AdapterResolutionRequest(
            project_id="p1",
            allowed_verification_levels=(VerificationLevel.STATIC_ONLY,),
            selection_strategy="invalid_random_strategy"
        )


def test_registry_multi_candidate_ambiguity():
    registry = AdapterRegistry(context_id="p1")
    adp1 = StandardTestFakeAdapter(adapter_id="candidate_1")
    man1 = create_standard_fake_manifest("candidate_1")
    adp2 = StandardTestFakeAdapter(adapter_id="candidate_2")
    man2 = create_standard_fake_manifest("candidate_2")

    registry.register(adp1, man1)
    registry.register(adp2, man2)

    # Both match identical criteria without exact ID -> AMBIGUOUS
    req = AdapterResolutionRequest(
        project_id="p1",
        required_capabilities=("worktree",),
        allowed_verification_levels=(VerificationLevel.STATIC_ONLY,)
    )
    decision = registry.resolve(req)
    assert decision.decision_status == ResolutionStatus.AMBIGUOUS
    assert decision.selected_adapter_id is None
    assert "candidate_1" in decision.reason
    assert "candidate_2" in decision.reason


def test_registry_thread_safety():
    registry = AdapterRegistry(context_id="p_thread")
    errors = []

    def worker(idx):
        try:
            aid = f"thread_adapter_{idx}"
            adp = StandardTestFakeAdapter(adapter_id=aid)
            man = create_standard_fake_manifest(aid)
            registry.register(adp, man)
            got = registry.get(aid)
            assert got is adp
        except Exception as e:
            errors.append(e)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(10)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert not errors
    assert len(registry.list_manifests()) == 10
