from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import Enum
import re
from types import MappingProxyType
from typing import Any, Dict, FrozenSet, List, Mapping as TMapping, Optional, Sequence, Set, Tuple


def _freeze_manifest_value(value: Any) -> Any:
    """Recursively copy mutable containers into deeply immutable equivalents."""
    if value is None or isinstance(value, (int, float, str, bool, bytes, Enum)):
        return value
    if isinstance(value, PlatformVerification):
        return value
    if isinstance(value, Mapping):
        res = {}
        for k, v in value.items():
            if not isinstance(k, str):
                raise TypeError(f"Mapping key must be string, got {type(k).__name__}")
            _assert_valid_string(k, "mapping_key")
            res[k] = _freeze_manifest_value(v)
        return MappingProxyType(res)
    if isinstance(value, (list, tuple)):
        return tuple(_freeze_manifest_value(item) for item in value)
    if isinstance(value, (set, frozenset)):
        return frozenset(_freeze_manifest_value(item) for item in value)
    raise TypeError(f"Unsupported mutable type for Manifest: {type(value)}")


def _assert_valid_string(val: Any, field_name: str, allow_empty: bool = False) -> str:
    if not isinstance(val, str):
        raise ValueError(f"Invalid {field_name}: must be a string, got {type(val).__name__}")
    if not allow_empty and not val:
        raise ValueError(f"Invalid {field_name}: cannot be empty")
    if not allow_empty and not val.strip():
        raise ValueError(f"Invalid {field_name}: cannot be whitespace-only")
    if re.search(r'[\x00-\x1f\x7f]', val):
        raise ValueError(f"Invalid {field_name}: contains forbidden control characters")
    return val


def _scan_for_sensitive_data(val: Any, path: str = "manifest") -> None:
    """Scan data structures to reject secrets, tokens, API keys, and authorization headers."""
    if isinstance(val, str):
        lower = val.lower()
        forbidden_keywords = [
            "bearer ", "sk-", "ghp_", "aws_access_key", "password=", "secret=",
            "authorization:", "session_token", "cookie:"
        ]
        for kw in forbidden_keywords:
            if kw in lower:
                raise ValueError(f"Sensitive credential pattern '{kw}' detected at {path}")
    elif isinstance(val, Mapping):
        for k, v in val.items():
            k_lower = str(k).lower()
            if any(s in k_lower for s in ("token", "secret", "password", "api_key", "auth_header", "private_key")):
                raise ValueError(f"Sensitive key name '{k}' forbidden at {path}")
            _scan_for_sensitive_data(v, f"{path}.{k}")
    elif isinstance(val, (list, tuple, set, frozenset)):
        for idx, item in enumerate(val):
            _scan_for_sensitive_data(item, f"{path}[{idx}]")


class VerificationLevel(str, Enum):
    NATIVE_VERIFIED = "native_verified"
    CLI_VERIFIED = "cli_verified"
    MCP_VERIFIED = "mcp_verified"
    STATIC_ONLY = "static_only"
    UNSUPPORTED = "unsupported"


class ExecutionMode(str, Enum):
    MANUAL = "manual"
    ASSISTED = "assisted"
    VERIFIED_AUTOMATIC = "verified_automatic"


class HostSurface(str, Enum):
    NATIVE = "native"
    CLI = "cli"
    MCP = "mcp"
    STATIC = "static"
    SIMULATED = "simulated"


class AuthBoundaryType(str, Enum):
    NONE = "none"
    USER_LOCAL = "user_local"
    HOST_MANAGED = "host_managed"
    ENVIRONMENT = "environment"
    EXPLICIT_DELEGATION = "explicit_delegation"


class BillingBoundaryType(str, Enum):
    NONE = "none"
    USER_SUBSCRIPTION = "user_subscription"
    API_KEY = "api_key"
    HOST_INCLUDED = "host_included"
    UNMETERED = "unmetered"


ALLOWED_OPERATING_SYSTEMS: FrozenSet[str] = frozenset({"windows", "macos", "linux"})


@dataclass(frozen=True)
class PlatformVerification:
    operating_system: str
    host_surface: HostSurface
    verification_level: VerificationLevel
    verified_version: str
    verified_at: Optional[str] = None
    e2e_evidence_refs: Tuple[str, ...] = field(default_factory=tuple)

    def __post_init__(self) -> None:
        _assert_valid_string(self.operating_system, "operating_system")
        if self.operating_system not in ALLOWED_OPERATING_SYSTEMS:
            raise ValueError(f"Invalid operating_system '{self.operating_system}'. Allowed: {sorted(ALLOWED_OPERATING_SYSTEMS)}")
        if not isinstance(self.host_surface, HostSurface):
            raise ValueError(f"host_surface must be a HostSurface enum, got {type(self.host_surface).__name__}")
        if not isinstance(self.verification_level, VerificationLevel):
            raise ValueError(f"verification_level must be a VerificationLevel enum, got {type(self.verification_level).__name__}")
        _assert_valid_string(self.verified_version, "verified_version")

        if self.verified_at is not None:
            _assert_valid_string(self.verified_at, "verified_at")

        frozen_refs = _freeze_manifest_value(self.e2e_evidence_refs)
        if not isinstance(frozen_refs, tuple) or not all(isinstance(r, str) for r in frozen_refs):
            raise ValueError("e2e_evidence_refs must be a tuple of strings")
        for r in frozen_refs:
            _assert_valid_string(r, "e2e_evidence_ref")
        object.__setattr__(self, "e2e_evidence_refs", frozen_refs)

        # Verification anti-forgery rules
        if self.verification_level == VerificationLevel.NATIVE_VERIFIED and self.host_surface != HostSurface.NATIVE:
            raise ValueError("native_verified requires NATIVE surface")
        if self.verification_level == VerificationLevel.CLI_VERIFIED and self.host_surface != HostSurface.CLI:
            raise ValueError("cli_verified requires CLI surface")
        if self.verification_level == VerificationLevel.MCP_VERIFIED and self.host_surface != HostSurface.MCP:
            raise ValueError("mcp_verified requires MCP surface")
        if self.host_surface == HostSurface.SIMULATED and self.verification_level in (VerificationLevel.NATIVE_VERIFIED, VerificationLevel.CLI_VERIFIED, VerificationLevel.MCP_VERIFIED):
            raise ValueError("simulated surface cannot be verified")
        if self.verification_level in (VerificationLevel.NATIVE_VERIFIED, VerificationLevel.CLI_VERIFIED, VerificationLevel.MCP_VERIFIED):
            if not self.e2e_evidence_refs:
                raise ValueError(f"VerificationLevel '{self.verification_level.value}' requires non-empty e2e_evidence_refs.")
            if not self.verified_at:
                raise ValueError(f"VerificationLevel '{self.verification_level.value}' requires non-empty verified_at timestamp.")


@dataclass(frozen=True)
class AdapterManifest:
    schema_version: str
    adapter_id: str
    display_name: str
    implementation_version: str
    host_surface: HostSurface
    verification_level: VerificationLevel
    capabilities: TMapping[str, str]
    workspace_modes: Tuple[str, ...]
    identity_fields: Tuple[str, ...]
    auth_boundary: AuthBoundaryType
    billing_boundary: BillingBoundaryType
    platform_version_constraint: str
    supported_operating_systems: Tuple[str, ...]
    platform_verifications: TMapping[str, PlatformVerification]
    executable_candidates_by_os: TMapping[str, Tuple[str, ...]]
    config_path_templates_by_os: TMapping[str, Tuple[str, ...]]
    conformance_suite_version: str
    auth_context_id: Optional[str] = None
    billing_context_id: Optional[str] = None
    verified_at: Optional[str] = None
    e2e_evidence_refs: Tuple[str, ...] = field(default_factory=tuple)
    extra: TMapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        _assert_valid_string(self.schema_version, "schema_version")
        _assert_valid_string(self.adapter_id, "adapter_id")
        # DEF-T0049-6: strictly lowercase normalized format
        if not re.fullmatch(r'^[a-z0-9_][a-z0-9_\-\.]{1,63}$', self.adapter_id):
            raise ValueError(f"Invalid adapter_id format '{self.adapter_id}'. Must match ^[a-z0-9_][a-z0-9_\\-\\.]{{1,63}}$ (strictly lowercase).")

        _assert_valid_string(self.display_name, "display_name")
        _assert_valid_string(self.implementation_version, "implementation_version")
        if not isinstance(self.host_surface, HostSurface):
            raise ValueError(f"host_surface must be a HostSurface enum, got {type(self.host_surface).__name__}")
        if not isinstance(self.verification_level, VerificationLevel):
            raise ValueError(f"verification_level must be a VerificationLevel enum, got {type(self.verification_level).__name__}")
        if not isinstance(self.auth_boundary, AuthBoundaryType):
            raise ValueError(f"auth_boundary must be an AuthBoundaryType enum, got {type(self.auth_boundary).__name__}")
        if not isinstance(self.billing_boundary, BillingBoundaryType):
            raise ValueError(f"billing_boundary must be a BillingBoundaryType enum, got {type(self.billing_boundary).__name__}")

        if self.auth_context_id is not None:
            _assert_valid_string(self.auth_context_id, "auth_context_id")
            _scan_for_sensitive_data(self.auth_context_id, "auth_context_id")
        if self.billing_context_id is not None:
            _assert_valid_string(self.billing_context_id, "billing_context_id")
            _scan_for_sensitive_data(self.billing_context_id, "billing_context_id")
        if self.auth_boundary == AuthBoundaryType.NONE and self.auth_context_id is not None:
            raise ValueError("auth_context_id must be None when auth_boundary is NONE")
        if self.auth_boundary != AuthBoundaryType.NONE and self.auth_context_id is None:
            raise ValueError("Non-NONE auth_boundary requires a non-secret auth_context_id")
        if self.billing_boundary in (BillingBoundaryType.NONE, BillingBoundaryType.UNMETERED):
            if self.billing_context_id is not None:
                raise ValueError("billing_context_id must be None for NONE or UNMETERED billing boundaries")
        elif self.billing_context_id is None:
            raise ValueError("Metered or host-bound billing_boundary requires a non-secret billing_context_id")
        _assert_valid_string(self.platform_version_constraint, "platform_version_constraint")
        _assert_valid_string(self.conformance_suite_version, "conformance_suite_version")

        # Freeze and validate supported operating systems
        frozen_os = _freeze_manifest_value(self.supported_operating_systems)
        if not isinstance(frozen_os, tuple) or not all(isinstance(os_name, str) for os_name in frozen_os):
            raise ValueError("supported_operating_systems must be a tuple of strings")
        for os_name in frozen_os:
            _assert_valid_string(os_name, "supported_operating_system")
            if os_name not in ALLOWED_OPERATING_SYSTEMS:
                raise ValueError(f"Invalid operating system '{os_name}'. Allowed: {sorted(ALLOWED_OPERATING_SYSTEMS)}")
        object.__setattr__(self, "supported_operating_systems", frozen_os)

        # Freeze and validate capabilities
        frozen_caps = _freeze_manifest_value(self.capabilities)
        if not isinstance(frozen_caps, Mapping):
            raise ValueError("capabilities must be a mapping of capability name to support level")
        for cap_k, cap_v in frozen_caps.items():
            _assert_valid_string(cap_k, "capability_key")
            _assert_valid_string(cap_v, "capability_value")
            if cap_v not in ("supported", "unsupported", "unknown"):
                raise ValueError(f"Capability '{cap_k}' has invalid support status '{cap_v}'. Must be supported, unsupported, or unknown.")
        object.__setattr__(self, "capabilities", frozen_caps)

        # Freeze workspace_modes and identity_fields
        frozen_wm = _freeze_manifest_value(self.workspace_modes)
        if not isinstance(frozen_wm, tuple) or not all(isinstance(m, str) for m in frozen_wm):
            raise ValueError("workspace_modes must be a tuple of strings")
        for wm in frozen_wm:
            _assert_valid_string(wm, "workspace_mode")
        object.__setattr__(self, "workspace_modes", frozen_wm)

        frozen_id_fields = _freeze_manifest_value(self.identity_fields)
        if not isinstance(frozen_id_fields, tuple) or not all(isinstance(f, str) for f in frozen_id_fields):
            raise ValueError("identity_fields must be a tuple of strings")
        for idf in frozen_id_fields:
            _assert_valid_string(idf, "identity_field")
        object.__setattr__(self, "identity_fields", frozen_id_fields)

        # Freeze platform verifications
        frozen_pv = _freeze_manifest_value(self.platform_verifications)
        if not isinstance(frozen_pv, Mapping):
            raise ValueError("platform_verifications must be a mapping of os_name to PlatformVerification")
        for pv_os, pv_val in frozen_pv.items():
            _assert_valid_string(pv_os, "platform_verification_key")
            if pv_os not in self.supported_operating_systems:
                raise ValueError(f"platform_verifications contains os '{pv_os}' not in supported_operating_systems")
            if not isinstance(pv_val, PlatformVerification):
                raise ValueError(f"platform_verifications['{pv_os}'] must be PlatformVerification instance")
            if pv_val.host_surface != self.host_surface:
                raise ValueError(f"Mismatch between PlatformVerification.host_surface '{pv_val.host_surface.value}' and Manifest.host_surface '{self.host_surface.value}'")
            if pv_val.operating_system != pv_os:
                raise ValueError(f"Mismatch between key '{pv_os}' and PlatformVerification.operating_system '{pv_val.operating_system}'")
        object.__setattr__(self, "platform_verifications", frozen_pv)

        # Freeze OS candidates and templates
        frozen_exec = _freeze_manifest_value(self.executable_candidates_by_os)
        if not isinstance(frozen_exec, Mapping):
            raise ValueError("executable_candidates_by_os must be a mapping")
        for os_k, cands in frozen_exec.items():
            _assert_valid_string(os_k, "os_name in executable_candidates_by_os")
            if not isinstance(cands, (list, tuple)):
                raise ValueError(f"executable candidates for {os_k} must be tuple/list")
            for cand in cands:
                _assert_valid_string(cand, f"executable_candidate for {os_k}")
        object.__setattr__(self, "executable_candidates_by_os", frozen_exec)

        frozen_tmpl = _freeze_manifest_value(self.config_path_templates_by_os)
        if not isinstance(frozen_tmpl, Mapping):
            raise ValueError("config_path_templates_by_os must be a mapping")
        for os_k, tmpls in frozen_tmpl.items():
            _assert_valid_string(os_k, "os_name in config_path_templates_by_os")
            if not isinstance(tmpls, (list, tuple)):
                raise ValueError(f"config templates for {os_k} must be tuple/list")
            for tmpl in tmpls:
                _assert_valid_string(tmpl, f"config_path_template for {os_k}")
        object.__setattr__(self, "config_path_templates_by_os", frozen_tmpl)

        # Freeze e2e_evidence_refs and extra
        frozen_refs = _freeze_manifest_value(self.e2e_evidence_refs)
        if not isinstance(frozen_refs, tuple) or not all(isinstance(r, str) for r in frozen_refs):
            raise ValueError("e2e_evidence_refs must be a tuple of strings")
        for r in frozen_refs:
            _assert_valid_string(r, "e2e_evidence_ref")
        object.__setattr__(self, "e2e_evidence_refs", frozen_refs)

        frozen_extra = _freeze_manifest_value(self.extra)
        if not isinstance(frozen_extra, Mapping):
            raise ValueError("extra must be a mapping")
        object.__setattr__(self, "extra", frozen_extra)

        if self.verified_at is not None:
            _assert_valid_string(self.verified_at, "verified_at")

        # Scan for sensitive credentials across entire manifest
        _scan_for_sensitive_data(self.extra, "extra")
        _scan_for_sensitive_data(self.config_path_templates_by_os, "config_path_templates_by_os")

        # Top-level verification anti-forgery check
        if self.verification_level == VerificationLevel.NATIVE_VERIFIED and self.host_surface != HostSurface.NATIVE:
            raise ValueError("native_verified requires NATIVE surface")
        if self.verification_level == VerificationLevel.CLI_VERIFIED and self.host_surface != HostSurface.CLI:
            raise ValueError("cli_verified requires CLI surface")
        if self.verification_level == VerificationLevel.MCP_VERIFIED and self.host_surface != HostSurface.MCP:
            raise ValueError("mcp_verified requires MCP surface")
        if self.host_surface == HostSurface.SIMULATED and self.verification_level in (VerificationLevel.NATIVE_VERIFIED, VerificationLevel.CLI_VERIFIED, VerificationLevel.MCP_VERIFIED):
            raise ValueError("simulated surface cannot be verified")
        if self.verification_level in (VerificationLevel.NATIVE_VERIFIED, VerificationLevel.CLI_VERIFIED, VerificationLevel.MCP_VERIFIED):
            if not self.e2e_evidence_refs:
                raise ValueError(f"Top-level verification_level '{self.verification_level.value}' requires e2e_evidence_refs.")
            if not self.verified_at:
                raise ValueError(f"Top-level verification_level '{self.verification_level.value}' requires verified_at timestamp.")
