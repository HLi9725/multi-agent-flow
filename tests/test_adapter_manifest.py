import pytest
from types import MappingProxyType

from scripts._lib.core.adapter_manifest import (
    AdapterManifest,
    AuthBoundaryType,
    BillingBoundaryType,
    HostSurface,
    PlatformVerification,
    VerificationLevel,
)
from scripts._lib.core.adapter_conformance import create_standard_fake_manifest


def test_valid_manifest_creation_and_immutability():
    manifest = create_standard_fake_manifest("my_adapter")
    assert manifest.adapter_id == "my_adapter"
    assert manifest.verification_level == VerificationLevel.STATIC_ONLY
    assert isinstance(manifest.capabilities, MappingProxyType)
    assert isinstance(manifest.workspace_modes, tuple)
    assert isinstance(manifest.supported_operating_systems, tuple)
    assert "windows" in manifest.supported_operating_systems
    assert "macos" in manifest.supported_operating_systems
    assert "linux" in manifest.supported_operating_systems

    # Immutability test: cannot mutate capabilities
    with pytest.raises((TypeError, AttributeError)):
        manifest.capabilities["new_cap"] = "supported"  # type: ignore

    # Cannot mutate workspace_modes
    with pytest.raises((TypeError, AttributeError)):
        manifest.workspace_modes.append("extra")  # type: ignore


def test_manifest_rejects_uppercase_adapter_id():
    # DEF-T0049-6: adapter_id must be strictly lowercase format
    pv = PlatformVerification(
        operating_system="windows",
        host_surface=HostSurface.SIMULATED,
        verification_level=VerificationLevel.STATIC_ONLY,
        verified_version="1.0.0"
    )
    with pytest.raises(ValueError, match="strictly lowercase"):
        AdapterManifest(
            schema_version="2.0",
            adapter_id="My_Adapter_Uppercase",
            display_name="Test",
            implementation_version="1.0",
            host_surface=HostSurface.SIMULATED,
            verification_level=VerificationLevel.STATIC_ONLY,
            capabilities={},
            workspace_modes=("isolated",),
            identity_fields=("id",),
            auth_boundary=AuthBoundaryType.NONE,
            billing_boundary=BillingBoundaryType.UNMETERED,
            platform_version_constraint=">=1.0",
            supported_operating_systems=("windows",),
            platform_verifications={"windows": pv},
            executable_candidates_by_os={},
            config_path_templates_by_os={},
            conformance_suite_version="2.0"
        )


def test_manifest_rejects_non_string_mapping_keys():
    # DEF-T0049-6: No implicit str(k) conversion; non-string keys must raise TypeError
    pv = PlatformVerification(
        operating_system="windows",
        host_surface=HostSurface.SIMULATED,
        verification_level=VerificationLevel.STATIC_ONLY,
        verified_version="1.0.0"
    )
    with pytest.raises(TypeError, match="Mapping key must be string"):
        AdapterManifest(
            schema_version="2.0",
            adapter_id="valid_id",
            display_name="Test",
            implementation_version="1.0",
            host_surface=HostSurface.SIMULATED,
            verification_level=VerificationLevel.STATIC_ONLY,
            capabilities={123: "supported"},  # Non-string key!
            workspace_modes=("isolated",),
            identity_fields=("id",),
            auth_boundary=AuthBoundaryType.NONE,
            billing_boundary=BillingBoundaryType.UNMETERED,
            platform_version_constraint=">=1.0",
            supported_operating_systems=("windows",),
            platform_verifications={"windows": pv},
            executable_candidates_by_os={},
            config_path_templates_by_os={},
            conformance_suite_version="2.0"
        )


def test_manifest_rejects_empty_and_control_chars():
    pv = PlatformVerification(
        operating_system="windows",
        host_surface=HostSurface.SIMULATED,
        verification_level=VerificationLevel.STATIC_ONLY,
        verified_version="1.0.0"
    )

    # Empty adapter_id
    with pytest.raises(ValueError, match="cannot be empty"):
        AdapterManifest(
            schema_version="2.0",
            adapter_id="",
            display_name="Test",
            implementation_version="1.0",
            host_surface=HostSurface.SIMULATED,
            verification_level=VerificationLevel.STATIC_ONLY,
            capabilities={},
            workspace_modes=("isolated",),
            identity_fields=("id",),
            auth_boundary=AuthBoundaryType.NONE,
            billing_boundary=BillingBoundaryType.UNMETERED,
            platform_version_constraint=">=1.0",
            supported_operating_systems=("windows",),
            platform_verifications={"windows": pv},
            executable_candidates_by_os={},
            config_path_templates_by_os={},
            conformance_suite_version="2.0"
        )

    # Whitespace only
    with pytest.raises(ValueError, match="cannot be whitespace-only"):
        AdapterManifest(
            schema_version="2.0",
            adapter_id="   ",
            display_name="Test",
            implementation_version="1.0",
            host_surface=HostSurface.SIMULATED,
            verification_level=VerificationLevel.STATIC_ONLY,
            capabilities={},
            workspace_modes=("isolated",),
            identity_fields=("id",),
            auth_boundary=AuthBoundaryType.NONE,
            billing_boundary=BillingBoundaryType.UNMETERED,
            platform_version_constraint=">=1.0",
            supported_operating_systems=("windows",),
            platform_verifications={"windows": pv},
            executable_candidates_by_os={},
            config_path_templates_by_os={},
            conformance_suite_version="2.0"
        )

    # Control chars in display_name
    with pytest.raises(ValueError, match="contains forbidden control characters"):
        AdapterManifest(
            schema_version="2.0",
            adapter_id="valid_id",
            display_name="Test\nName",
            implementation_version="1.0",
            host_surface=HostSurface.SIMULATED,
            verification_level=VerificationLevel.STATIC_ONLY,
            capabilities={},
            workspace_modes=("isolated",),
            identity_fields=("id",),
            auth_boundary=AuthBoundaryType.NONE,
            billing_boundary=BillingBoundaryType.UNMETERED,
            platform_version_constraint=">=1.0",
            supported_operating_systems=("windows",),
            platform_verifications={"windows": pv},
            executable_candidates_by_os={},
            config_path_templates_by_os={},
            conformance_suite_version="2.0"
        )


def test_manifest_credential_and_secret_rejection():
    pv = PlatformVerification(
        operating_system="windows",
        host_surface=HostSurface.SIMULATED,
        verification_level=VerificationLevel.STATIC_ONLY,
        verified_version="1.0.0"
    )

    # Secret in extra
    with pytest.raises(ValueError, match="Sensitive"):
        AdapterManifest(
            schema_version="2.0",
            adapter_id="valid_id",
            display_name="Test",
            implementation_version="1.0",
            host_surface=HostSurface.SIMULATED,
            verification_level=VerificationLevel.STATIC_ONLY,
            capabilities={},
            workspace_modes=("isolated",),
            identity_fields=("id",),
            auth_boundary=AuthBoundaryType.NONE,
            billing_boundary=BillingBoundaryType.UNMETERED,
            platform_version_constraint=">=1.0",
            supported_operating_systems=("windows",),
            platform_verifications={"windows": pv},
            executable_candidates_by_os={},
            config_path_templates_by_os={},
            conformance_suite_version="2.0",
            extra={"api_key": "sk-1234567890"}
        )

    # Bearer token string
    with pytest.raises(ValueError, match="Sensitive"):
        AdapterManifest(
            schema_version="2.0",
            adapter_id="valid_id",
            display_name="Test",
            implementation_version="1.0",
            host_surface=HostSurface.SIMULATED,
            verification_level=VerificationLevel.STATIC_ONLY,
            capabilities={},
            workspace_modes=("isolated",),
            identity_fields=("id",),
            auth_boundary=AuthBoundaryType.NONE,
            billing_boundary=BillingBoundaryType.UNMETERED,
            platform_version_constraint=">=1.0",
            supported_operating_systems=("windows",),
            platform_verifications={"windows": pv},
            executable_candidates_by_os={},
            config_path_templates_by_os={"windows": ("Bearer my_secret_token",)},
            conformance_suite_version="2.0"
        )


def test_verification_level_anti_forgery():
    # Attempting to declare native_verified without e2e_evidence_refs
    with pytest.raises(ValueError, match="requires non-empty e2e_evidence_refs"):
        PlatformVerification(
            operating_system="windows",
            host_surface=HostSurface.NATIVE,
            verification_level=VerificationLevel.NATIVE_VERIFIED,
            verified_version="1.0.0",
            verified_at="2026-08-26T10:00:00Z",
            e2e_evidence_refs=()  # Empty!
        )

    # Attempting to declare native_verified without verified_at
    with pytest.raises(ValueError, match="requires non-empty verified_at"):
        PlatformVerification(
            operating_system="windows",
            host_surface=HostSurface.NATIVE,
            verification_level=VerificationLevel.NATIVE_VERIFIED,
            verified_version="1.0.0",
            verified_at=None,
            e2e_evidence_refs=("evidence-123",)
        )


def test_cross_platform_verification_isolation():
    # Windows verified does NOT automatically verify macOS or Linux
    pv_win = PlatformVerification(
        operating_system="windows",
        host_surface=HostSurface.NATIVE,
        verification_level=VerificationLevel.NATIVE_VERIFIED,
        verified_version="1.0.0",
        verified_at="2026-08-26T10:00:00Z",
        e2e_evidence_refs=("win-e2e-01",)
    )
    pv_mac = PlatformVerification(
        operating_system="macos",
        host_surface=HostSurface.NATIVE,
        verification_level=VerificationLevel.STATIC_ONLY,
        verified_version="1.0.0"
    )
    pv_linux = PlatformVerification(
        operating_system="linux",
        host_surface=HostSurface.NATIVE,
        verification_level=VerificationLevel.UNSUPPORTED,
        verified_version="1.0.0"
    )

    man = AdapterManifest(
        schema_version="2.0",
        adapter_id="multi_os_adapter",
        display_name="Multi OS Adapter",
        implementation_version="1.0.0",
        host_surface=HostSurface.NATIVE,
        verification_level=VerificationLevel.NATIVE_VERIFIED,
        capabilities={"worktree": "supported"},
        workspace_modes=("isolated",),
        identity_fields=("id",),
        auth_boundary=AuthBoundaryType.HOST_MANAGED,
        billing_boundary=BillingBoundaryType.HOST_INCLUDED,
        platform_version_constraint=">=1.0.0",
        supported_operating_systems=("windows", "macos", "linux"),
        platform_verifications={"windows": pv_win, "macos": pv_mac, "linux": pv_linux},
        executable_candidates_by_os={"windows": ("cmd.exe",), "macos": ("/bin/sh",), "linux": ("/bin/sh",)},
        config_path_templates_by_os={"windows": (), "macos": (), "linux": ()},
        conformance_suite_version="2.0",
        verified_at="2026-08-26T10:00:00Z",
        e2e_evidence_refs=("win-e2e-01",)
    )

    assert man.platform_verifications["windows"].verification_level == VerificationLevel.NATIVE_VERIFIED
    assert man.platform_verifications["macos"].verification_level == VerificationLevel.STATIC_ONLY
    assert man.platform_verifications["linux"].verification_level == VerificationLevel.UNSUPPORTED
