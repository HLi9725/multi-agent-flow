import os
import pytest
import threading

from scripts._lib.core.adapter_manifest import (
    AdapterManifest,
    AuthBoundaryType,
    BillingBoundaryType,
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


def test_registry_context_isolation():
    reg_a = AdapterRegistry(context_id="project_A")
    reg_b = AdapterRegistry(context_id="project_B")

    adp_a = StandardTestFakeAdapter(adapter_id="adp_a")
    man_a = create_standard_fake_manifest("adp_a")
    reg_a.register(adp_a, man_a)

    assert reg_a.get("adp_a") is not None
    assert reg_b.get("adp_a") is None
    assert len(reg_a.list_manifests()) == 1
    assert len(reg_b.list_manifests()) == 0


def test_registry_exact_id_resolution():
    registry = AdapterRegistry()
    adp = StandardTestFakeAdapter(adapter_id="my_exact_adapter")
    man = create_standard_fake_manifest("my_exact_adapter")
    registry.register(adp, man)

    # 1. Exact match found
    req_success = AdapterResolutionRequest(
        project_id="p1",
        adapter_id="my_exact_adapter",
        required_capabilities=("worktree",),
        allowed_verification_levels=(VerificationLevel.STATIC_ONLY,)
    )
    decision = registry.resolve(req_success)
    assert decision.decision_status == ResolutionStatus.SELECTED
    assert decision.selected_adapter_id == "my_exact_adapter"

    # 2. Exact match missing -> unsupported
    req_missing = AdapterResolutionRequest(
        project_id="p1",
        adapter_id="unregistered_adapter",
        allow_manual_fallback=False
    )
    decision_missing = registry.resolve(req_missing)
    assert decision_missing.decision_status == ResolutionStatus.UNSUPPORTED
    assert decision_missing.selected_adapter_id is None

    # 3. Exact match missing with manual fallback
    req_fallback = AdapterResolutionRequest(
        project_id="p1",
        adapter_id="unregistered_adapter",
        allow_manual_fallback=True
    )
    decision_fallback = registry.resolve(req_fallback)
    assert decision_fallback.decision_status == ResolutionStatus.MANUAL_FALLBACK


def test_registry_capability_and_unknown_handling():
    registry = AdapterRegistry()
    adp = StandardTestFakeAdapter(adapter_id="cap_adapter")
    man = create_standard_fake_manifest("cap_adapter")
    registry.register(adp, man)

    # Required supported capability -> SELECTED
    req1 = AdapterResolutionRequest(
        project_id="p1",
        required_capabilities=("parallelism", "worktree"),
        allowed_verification_levels=(VerificationLevel.STATIC_ONLY,)
    )
    d1 = registry.resolve(req1)
    assert d1.decision_status == ResolutionStatus.SELECTED
    assert "parallelism" in d1.matched_capabilities

    # Required unsupported capability -> UNSUPPORTED
    req2 = AdapterResolutionRequest(
        project_id="p1",
        required_capabilities=("real_subagents",),  # marked unsupported in standard fake
        allowed_verification_levels=(VerificationLevel.STATIC_ONLY,)
    )
    d2 = registry.resolve(req2)
    assert d2.decision_status == ResolutionStatus.UNSUPPORTED
    assert "real_subagents" in d2.missing_capabilities

    # Required undeclared / unknown capability -> UNSUPPORTED (UNKNOWN != SUPPORTED)
    req3 = AdapterResolutionRequest(
        project_id="p1",
        required_capabilities=("completely_unknown_capability",),
        allowed_verification_levels=(VerificationLevel.STATIC_ONLY,)
    )
    d3 = registry.resolve(req3)
    assert d3.decision_status == ResolutionStatus.UNSUPPORTED
    assert "completely_unknown_capability" in d3.missing_capabilities


def test_registry_cross_platform_resolution():
    registry = AdapterRegistry()

    pv_win = PlatformVerification(
        operating_system="windows",
        host_surface=HostSurface.SIMULATED,
        verification_level=VerificationLevel.STATIC_ONLY,
        verified_version="1.0.0"
    )
    pv_mac = PlatformVerification(
        operating_system="macos",
        host_surface=HostSurface.SIMULATED,
        verification_level=VerificationLevel.STATIC_ONLY,
        verified_version="1.0.0"
    )
    pv_linux = PlatformVerification(
        operating_system="linux",
        host_surface=HostSurface.SIMULATED,
        verification_level=VerificationLevel.UNSUPPORTED,
        verified_version="1.0.0"
    )

    man = AdapterManifest(
        schema_version="2.0",
        adapter_id="cross_plat_adapter",
        display_name="Cross Platform Test",
        implementation_version="1.0.0",
        host_surface=HostSurface.SIMULATED,
        verification_level=VerificationLevel.STATIC_ONLY,
        capabilities={"worktree": "supported"},
        workspace_modes=("worktree",),
        identity_fields=("id",),
        auth_boundary=AuthBoundaryType.NONE,
        billing_boundary=BillingBoundaryType.UNMETERED,
        platform_version_constraint=">=1.0.0",
        supported_operating_systems=("windows", "macos", "linux"),
        platform_verifications={"windows": pv_win, "macos": pv_mac, "linux": pv_linux},
        executable_candidates_by_os={"windows": (), "macos": (), "linux": ()},
        config_path_templates_by_os={"windows": (), "macos": (), "linux": ()},
        conformance_suite_version="2.0"
    )
    adp = StandardTestFakeAdapter(adapter_id="cross_plat_adapter")
    registry.register(adp, man)

    # Windows request requesting STATIC_ONLY -> SELECTED
    req_win = AdapterResolutionRequest(
        project_id="p1",
        target_os="windows",
        allowed_verification_levels=(VerificationLevel.STATIC_ONLY,)
    )
    assert registry.resolve(req_win).decision_status == ResolutionStatus.SELECTED

    # Linux request requesting STATIC_ONLY -> UNSUPPORTED (because Linux is declared UNSUPPORTED)
    req_linux = AdapterResolutionRequest(
        project_id="p1",
        target_os="linux",
        allowed_verification_levels=(VerificationLevel.STATIC_ONLY,)
    )
    assert registry.resolve(req_linux).decision_status == ResolutionStatus.UNSUPPORTED


def test_registry_multi_candidate_ambiguity():
    registry = AdapterRegistry()
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
    registry = AdapterRegistry()
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
