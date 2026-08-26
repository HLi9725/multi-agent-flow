from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import Enum
import platform
import sys
import threading
from types import MappingProxyType
from typing import Any, Dict, FrozenSet, List, Mapping as TMapping, Optional, Sequence, Set, Tuple

from .agent_schema import CapabilitySupport, HostCapabilities
from .host_adapter import BaseHostAdapter
from .adapter_manifest import (
    AdapterManifest,
    AuthBoundaryType,
    BillingBoundaryType,
    PlatformVerification,
    VerificationLevel,
    _assert_valid_string,
    _freeze_manifest_value,
    _scan_for_sensitive_data,
)


class ResolutionStatus(str, Enum):
    SELECTED = "selected"
    UNSUPPORTED = "unsupported"
    AMBIGUOUS = "ambiguous"
    MANUAL_FALLBACK = "manual_fallback"


def _normalize_current_os() -> str:
    sys_plat = sys.platform.lower()
    if sys_plat.startswith("win"):
        return "windows"
    if sys_plat.startswith("darwin"):
        return "macos"
    if sys_plat.startswith("linux"):
        return "linux"
    return sys_plat


@dataclass(frozen=True)
class AdapterResolutionRequest:
    project_id: str
    required_capabilities: Tuple[str, ...] = field(default_factory=tuple)
    allowed_verification_levels: Tuple[VerificationLevel, ...] = field(default_factory=tuple)
    required_workspace_modes: Tuple[str, ...] = field(default_factory=tuple)
    adapter_id: Optional[str] = None
    target_os: Optional[str] = None
    allow_manual_fallback: bool = False
    selection_strategy: str = "deterministic"
    audit_context: TMapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        _assert_valid_string(self.project_id, "project_id")
        if self.adapter_id is not None:
            _assert_valid_string(self.adapter_id, "adapter_id")
        if self.target_os is not None:
            _assert_valid_string(self.target_os, "target_os")

        frozen_rc = _freeze_manifest_value(self.required_capabilities)
        if not isinstance(frozen_rc, tuple) or not all(isinstance(c, str) for c in frozen_rc):
            raise ValueError("required_capabilities must be a tuple of strings")
        object.__setattr__(self, "required_capabilities", frozen_rc)

        frozen_vl = _freeze_manifest_value(self.allowed_verification_levels)
        if not isinstance(frozen_vl, tuple) or not all(isinstance(v, VerificationLevel) for v in frozen_vl):
            raise ValueError("allowed_verification_levels must be a tuple of VerificationLevel enums")
        object.__setattr__(self, "allowed_verification_levels", frozen_vl)

        frozen_wm = _freeze_manifest_value(self.required_workspace_modes)
        if not isinstance(frozen_wm, tuple) or not all(isinstance(m, str) for m in frozen_wm):
            raise ValueError("required_workspace_modes must be a tuple of strings")
        object.__setattr__(self, "required_workspace_modes", frozen_wm)

        frozen_audit = _freeze_manifest_value(self.audit_context)
        if not isinstance(frozen_audit, Mapping):
            raise ValueError("audit_context must be a mapping")
        _scan_for_sensitive_data(frozen_audit, "audit_context")
        object.__setattr__(self, "audit_context", frozen_audit)


@dataclass(frozen=True)
class AdapterResolutionDecision:
    decision_status: ResolutionStatus
    selected_adapter_id: Optional[str]
    matched_capabilities: Tuple[str, ...]
    missing_capabilities: Tuple[str, ...] = field(default_factory=tuple)
    verification_level: Optional[VerificationLevel] = None
    reason: str = ""
    auth_boundary_summary: str = ""
    billing_boundary_summary: str = ""
    workspace_modes: Tuple[str, ...] = field(default_factory=tuple)
    is_real_host: bool = False
    audit_context: TMapping[str, Any] = field(default_factory=dict)
    manifest: Optional[AdapterManifest] = None

    def __post_init__(self) -> None:
        if not isinstance(self.decision_status, ResolutionStatus):
            raise ValueError(f"decision_status must be a ResolutionStatus enum, got {type(self.decision_status).__name__}")
        if self.verification_level is not None and not isinstance(self.verification_level, VerificationLevel):
            raise ValueError(f"verification_level must be a VerificationLevel enum, got {type(self.verification_level).__name__}")

        object.__setattr__(self, "matched_capabilities", tuple(_freeze_manifest_value(self.matched_capabilities)))
        object.__setattr__(self, "missing_capabilities", tuple(_freeze_manifest_value(self.missing_capabilities)))
        object.__setattr__(self, "workspace_modes", tuple(_freeze_manifest_value(self.workspace_modes)))
        object.__setattr__(self, "audit_context", _freeze_manifest_value(self.audit_context))


class AdapterRegistryError(Exception):
    """Base exception for AdapterRegistry operations."""
    pass


class AdapterRegistry:
    def __init__(self, context_id: str = "default"):
        _assert_valid_string(context_id, "context_id")
        self.context_id = context_id
        self._adapters: Dict[str, BaseHostAdapter] = {}
        self._manifests: Dict[str, AdapterManifest] = {}
        self._lock = threading.RLock()

    def register(self, adapter: BaseHostAdapter, manifest: AdapterManifest) -> None:
        if not isinstance(adapter, BaseHostAdapter):
            raise AdapterRegistryError(f"Adapter must be an instance of BaseHostAdapter, got {type(adapter).__name__}")
        if not isinstance(manifest, AdapterManifest):
            raise AdapterRegistryError(f"Manifest must be an instance of AdapterManifest, got {type(manifest).__name__}")

        with self._lock:
            if manifest.adapter_id in self._manifests:
                raise AdapterRegistryError(f"Adapter ID '{manifest.adapter_id}' is already registered in context '{self.context_id}'.")

            # Validate capability consistency between manifest and adapter detect_capabilities
            caps = adapter.detect_capabilities()
            if not isinstance(caps, HostCapabilities):
                raise AdapterRegistryError("adapter.detect_capabilities() must return HostCapabilities instance")

            # Check if adapter is fake / simulated
            if caps.is_real_host is False and manifest.verification_level in (
                VerificationLevel.NATIVE_VERIFIED,
                VerificationLevel.CLI_VERIFIED,
                VerificationLevel.MCP_VERIFIED
            ):
                raise AdapterRegistryError(
                    f"Adapter detect_capabilities indicates is_real_host=False, which cannot be registered as {manifest.verification_level.value}."
                )

            self._adapters[manifest.adapter_id] = adapter
            self._manifests[manifest.adapter_id] = manifest

    def get(self, adapter_id: str) -> Optional[BaseHostAdapter]:
        with self._lock:
            return self._adapters.get(adapter_id)

    def get_manifest(self, adapter_id: str) -> Optional[AdapterManifest]:
        with self._lock:
            return self._manifests.get(adapter_id)

    def list_manifests(self) -> Tuple[AdapterManifest, ...]:
        with self._lock:
            return tuple(self._manifests.values())

    def resolve(self, request: AdapterResolutionRequest) -> AdapterResolutionDecision:
        with self._lock:
            effective_os = request.target_os or _normalize_current_os()

            # 1. Exact adapter_id specified
            if request.adapter_id is not None:
                manifest = self._manifests.get(request.adapter_id)
                adapter = self._adapters.get(request.adapter_id)
                if not manifest or not adapter:
                    if request.allow_manual_fallback:
                        return AdapterResolutionDecision(
                            decision_status=ResolutionStatus.MANUAL_FALLBACK,
                            selected_adapter_id=None,
                            matched_capabilities=(),
                            missing_capabilities=request.required_capabilities,
                            reason=f"Requested adapter_id '{request.adapter_id}' is not registered; manual fallback enabled.",
                            audit_context=request.audit_context
                        )
                    return AdapterResolutionDecision(
                        decision_status=ResolutionStatus.UNSUPPORTED,
                        selected_adapter_id=None,
                        matched_capabilities=(),
                        missing_capabilities=request.required_capabilities,
                        reason=f"Requested adapter_id '{request.adapter_id}' is not registered.",
                        audit_context=request.audit_context
                    )

                # Evaluate single candidate
                can_match, missing_caps, matched_caps, plat_ver, reject_reason = self._evaluate_candidate(
                    manifest, adapter, request, effective_os
                )
                if can_match:
                    return AdapterResolutionDecision(
                        decision_status=ResolutionStatus.SELECTED,
                        selected_adapter_id=manifest.adapter_id,
                        matched_capabilities=matched_caps,
                        missing_capabilities=(),
                        verification_level=plat_ver.verification_level if plat_ver else manifest.verification_level,
                        reason=f"Exact adapter '{manifest.adapter_id}' matches all requirements.",
                        auth_boundary_summary=manifest.auth_boundary.value,
                        billing_boundary_summary=manifest.billing_boundary.value,
                        workspace_modes=manifest.workspace_modes,
                        is_real_host=adapter.detect_capabilities().is_real_host,
                        audit_context=request.audit_context,
                        manifest=manifest
                    )
                else:
                    if request.allow_manual_fallback:
                        return AdapterResolutionDecision(
                            decision_status=ResolutionStatus.MANUAL_FALLBACK,
                            selected_adapter_id=None,
                            matched_capabilities=matched_caps,
                            missing_capabilities=missing_caps,
                            reason=f"Requested adapter '{manifest.adapter_id}' does not satisfy requirements: {reject_reason}; manual fallback.",
                            audit_context=request.audit_context
                        )
                    return AdapterResolutionDecision(
                        decision_status=ResolutionStatus.UNSUPPORTED,
                        selected_adapter_id=None,
                        matched_capabilities=matched_caps,
                        missing_capabilities=missing_caps,
                        reason=f"Requested adapter '{manifest.adapter_id}' does not satisfy requirements: {reject_reason}",
                        audit_context=request.audit_context
                    )

            # 2. General capability and verification level search
            matching_candidates: List[Tuple[AdapterManifest, BaseHostAdapter, Tuple[str, ...], PlatformVerification]] = []
            rejection_reasons: List[str] = []

            # Sort manifest keys for deterministic ordering
            sorted_adapter_ids = sorted(self._manifests.keys())
            for aid in sorted_adapter_ids:
                man = self._manifests[aid]
                adp = self._adapters[aid]
                can_match, missing_caps, matched_caps, plat_ver, reject_reason = self._evaluate_candidate(
                    man, adp, request, effective_os
                )
                if can_match and plat_ver is not None:
                    matching_candidates.append((man, adp, matched_caps, plat_ver))
                else:
                    rejection_reasons.append(f"{aid}: {reject_reason}")

            if len(matching_candidates) == 1:
                sel_man, sel_adp, sel_matched, sel_pv = matching_candidates[0]
                return AdapterResolutionDecision(
                    decision_status=ResolutionStatus.SELECTED,
                    selected_adapter_id=sel_man.adapter_id,
                    matched_capabilities=sel_matched,
                    missing_capabilities=(),
                    verification_level=sel_pv.verification_level,
                    reason=f"Adapter '{sel_man.adapter_id}' uniquely satisfied all requirements.",
                    auth_boundary_summary=sel_man.auth_boundary.value,
                    billing_boundary_summary=sel_man.billing_boundary.value,
                    workspace_modes=sel_man.workspace_modes,
                    is_real_host=sel_adp.detect_capabilities().is_real_host,
                    audit_context=request.audit_context,
                    manifest=sel_man
                )
            elif len(matching_candidates) == 0:
                if request.allow_manual_fallback:
                    return AdapterResolutionDecision(
                        decision_status=ResolutionStatus.MANUAL_FALLBACK,
                        selected_adapter_id=None,
                        matched_capabilities=(),
                        missing_capabilities=request.required_capabilities,
                        reason=f"No matching adapter found ({'; '.join(rejection_reasons)}); manual fallback enabled.",
                        audit_context=request.audit_context
                    )
                return AdapterResolutionDecision(
                    decision_status=ResolutionStatus.UNSUPPORTED,
                    selected_adapter_id=None,
                    matched_capabilities=(),
                    missing_capabilities=request.required_capabilities,
                    reason=f"No adapter satisfies requirements. Candidates rejected: {'; '.join(rejection_reasons)}",
                    audit_context=request.audit_context
                )
            else:
                # Multiple candidates match -> ambiguous
                candidate_ids = [c[0].adapter_id for c in matching_candidates]
                return AdapterResolutionDecision(
                    decision_status=ResolutionStatus.AMBIGUOUS,
                    selected_adapter_id=None,
                    matched_capabilities=(),
                    missing_capabilities=(),
                    reason=f"Multiple adapters satisfy criteria without a tie-breaker: {candidate_ids}. Resolution is ambiguous.",
                    audit_context=request.audit_context
                )

    def _evaluate_candidate(
        self,
        manifest: AdapterManifest,
        adapter: BaseHostAdapter,
        request: AdapterResolutionRequest,
        effective_os: str
    ) -> Tuple[bool, Tuple[str, ...], Tuple[str, ...], Optional[PlatformVerification], str]:
        # a. OS support check
        if effective_os not in manifest.supported_operating_systems:
            return False, request.required_capabilities, (), None, f"Operating system '{effective_os}' not supported."

        # b. Platform verification level check
        pv = manifest.platform_verifications.get(effective_os)
        if not pv:
            return False, request.required_capabilities, (), None, f"No platform verification record for '{effective_os}'."

        if request.allowed_verification_levels:
            if pv.verification_level not in request.allowed_verification_levels:
                return False, request.required_capabilities, (), pv, f"Platform verification level '{pv.verification_level.value}' not in allowed levels."

        # c. Workspace modes check
        for req_wm in request.required_workspace_modes:
            if req_wm not in manifest.workspace_modes:
                return False, request.required_capabilities, (), pv, f"Required workspace mode '{req_wm}' not supported."

        # d. Capabilities check
        detected_caps = adapter.detect_capabilities()
        matched: List[str] = []
        missing: List[str] = []

        for req_cap in request.required_capabilities:
            # Check manifest declared capability
            declared_status = manifest.capabilities.get(req_cap, "unknown")
            if declared_status != "supported":
                missing.append(req_cap)
                continue

            # Check runtime detected capability if field exists on HostCapabilities
            field_name = f"supports_{req_cap}" if not req_cap.startswith("supports_") else req_cap
            if hasattr(detected_caps, field_name):
                cap_val = getattr(detected_caps, field_name)
                if cap_val != CapabilitySupport.SUPPORTED and cap_val != "supported":
                    missing.append(req_cap)
                    continue
            matched.append(req_cap)

        if missing:
            return False, tuple(missing), tuple(matched), pv, f"Missing required capabilities: {missing}."

        return True, (), tuple(matched), pv, "OK"
