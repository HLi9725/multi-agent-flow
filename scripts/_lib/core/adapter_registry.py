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
    ExecutionMode,
    HostSurface,
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


ALLOWED_SELECTION_STRATEGIES: FrozenSet[str] = frozenset({"deterministic", "priority", "first_match"})

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
    allowed_verification_levels: Tuple[VerificationLevel, ...]
    required_capabilities: Tuple[str, ...] = field(default_factory=tuple)
    required_workspace_modes: Tuple[str, ...] = field(default_factory=tuple)
    adapter_id: Optional[str] = None
    target_os: Optional[str] = None
    execution_mode: ExecutionMode = ExecutionMode.MANUAL
    allow_manual_fallback: bool = False
    selection_strategy: str = "deterministic"
    allowed_auth_boundaries: Optional[Tuple[AuthBoundaryType, ...]] = None
    allowed_billing_boundaries: Optional[Tuple[BillingBoundaryType, ...]] = None
    auth_context_id: Optional[str] = None
    billing_context_id: Optional[str] = None
    audit_context: TMapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        _assert_valid_string(self.project_id, "project_id")
        if self.adapter_id is not None:
            _assert_valid_string(self.adapter_id, "adapter_id")
        if self.target_os is not None:
            _assert_valid_string(self.target_os, "target_os")
        if self.auth_context_id is not None:
            _assert_valid_string(self.auth_context_id, "auth_context_id")
            _scan_for_sensitive_data(self.auth_context_id, "auth_context_id")
        if self.billing_context_id is not None:
            _assert_valid_string(self.billing_context_id, "billing_context_id")
            _scan_for_sensitive_data(self.billing_context_id, "billing_context_id")

        if not isinstance(self.execution_mode, ExecutionMode):
            raise ValueError(f"execution_mode must be an ExecutionMode enum, got {type(self.execution_mode).__name__}")

        _assert_valid_string(self.selection_strategy, "selection_strategy")
        if self.selection_strategy not in ALLOWED_SELECTION_STRATEGIES:
            raise ValueError(f"Invalid selection_strategy '{self.selection_strategy}'. Allowed: {sorted(ALLOWED_SELECTION_STRATEGIES)}")

        # DEF-T0049-1: allowed_verification_levels must be non-empty
        frozen_vl = _freeze_manifest_value(self.allowed_verification_levels)
        if not isinstance(frozen_vl, tuple) or not frozen_vl or not all(isinstance(v, VerificationLevel) for v in frozen_vl):
            raise ValueError("allowed_verification_levels must be a non-empty tuple of VerificationLevel enums")
        object.__setattr__(self, "allowed_verification_levels", frozen_vl)

        frozen_rc = _freeze_manifest_value(self.required_capabilities)
        if not isinstance(frozen_rc, tuple) or not all(isinstance(c, str) for c in frozen_rc):
            raise ValueError("required_capabilities must be a tuple of strings")
        for c in frozen_rc:
            _assert_valid_string(c, "required_capability")
        object.__setattr__(self, "required_capabilities", frozen_rc)

        frozen_wm = _freeze_manifest_value(self.required_workspace_modes)
        if not isinstance(frozen_wm, tuple) or not all(isinstance(m, str) for m in frozen_wm):
            raise ValueError("required_workspace_modes must be a tuple of strings")
        for m in frozen_wm:
            _assert_valid_string(m, "required_workspace_mode")
        object.__setattr__(self, "required_workspace_modes", frozen_wm)

        # DEF-T0049-11: Auth and Billing boundaries validation
        if self.allowed_auth_boundaries is not None:
            frozen_ab = _freeze_manifest_value(self.allowed_auth_boundaries)
            if not isinstance(frozen_ab, tuple) or not frozen_ab or not all(isinstance(b, AuthBoundaryType) for b in frozen_ab):
                raise ValueError("allowed_auth_boundaries must be a non-empty tuple of AuthBoundaryType enums")
            object.__setattr__(self, "allowed_auth_boundaries", frozen_ab)

        if self.allowed_billing_boundaries is not None:
            frozen_bb = _freeze_manifest_value(self.allowed_billing_boundaries)
            if not isinstance(frozen_bb, tuple) or not frozen_bb or not all(isinstance(b, BillingBoundaryType) for b in frozen_bb):
                raise ValueError("allowed_billing_boundaries must be a non-empty tuple of BillingBoundaryType enums")
            object.__setattr__(self, "allowed_billing_boundaries", frozen_bb)

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
    execution_mode: ExecutionMode = ExecutionMode.MANUAL
    is_real_host: bool = False
    audit_context: TMapping[str, Any] = field(default_factory=dict)
    manifest: Optional[AdapterManifest] = None

    def __post_init__(self) -> None:
        if not isinstance(self.decision_status, ResolutionStatus):
            raise ValueError(f"decision_status must be a ResolutionStatus enum, got {type(self.decision_status).__name__}")
        if self.verification_level is not None and not isinstance(self.verification_level, VerificationLevel):
            raise ValueError(f"verification_level must be a VerificationLevel enum, got {type(self.verification_level).__name__}")
        if not isinstance(self.execution_mode, ExecutionMode):
            raise ValueError(f"execution_mode must be an ExecutionMode enum, got {type(self.execution_mode).__name__}")

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

        # DEF-T0049-4: Adapter implementation ID must match Manifest ID exactly
        adapter_impl_id = getattr(adapter, "adapter_id", None)
        if not isinstance(adapter_impl_id, str) or not adapter_impl_id:
            raise AdapterRegistryError(f"Adapter must declare non-empty string attribute 'adapter_id', got {repr(adapter_impl_id)}")
        if adapter_impl_id != manifest.adapter_id:
            raise AdapterRegistryError(f"Adapter implementation ID '{adapter_impl_id}' does not match Manifest adapter_id '{manifest.adapter_id}'.")

        with self._lock:
            if manifest.adapter_id in self._manifests:
                raise AdapterRegistryError(f"Adapter ID '{manifest.adapter_id}' is already registered in context '{self.context_id}'.")

            # Validate capability consistency between manifest and adapter detect_capabilities
            caps = adapter.detect_capabilities()
            if not isinstance(caps, HostCapabilities):
                raise AdapterRegistryError("adapter.detect_capabilities() must return HostCapabilities instance")

            # DEF-T0049-8: Exhaustive fake / simulated adapter verification check
            if caps.is_real_host is False:
                # 1. Top level verification level
                if manifest.verification_level in (
                    VerificationLevel.NATIVE_VERIFIED,
                    VerificationLevel.CLI_VERIFIED,
                    VerificationLevel.MCP_VERIFIED
                ):
                    raise AdapterRegistryError(
                        f"Adapter detect_capabilities indicates is_real_host=False, which cannot be registered as {manifest.verification_level.value}."
                    )
                # 2. Platform level verification levels
                for pv_os, pv in manifest.platform_verifications.items():
                    if pv.verification_level in (
                        VerificationLevel.NATIVE_VERIFIED,
                        VerificationLevel.CLI_VERIFIED,
                        VerificationLevel.MCP_VERIFIED
                    ):
                        raise AdapterRegistryError(
                            f"Adapter detect_capabilities indicates is_real_host=False, which cannot have platform verification for '{pv_os}' claiming {pv.verification_level.value}."
                        )
                # 3. Manifest surface
                if manifest.host_surface in (HostSurface.NATIVE, HostSurface.CLI, HostSurface.MCP):
                    raise AdapterRegistryError(
                        f"Adapter detect_capabilities indicates is_real_host=False, which cannot declare host_surface '{manifest.host_surface.value}'."
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
            # DEF-T0049-5: context_id binding with request.project_id
            if request.project_id != self.context_id:
                return AdapterResolutionDecision(
                    decision_status=ResolutionStatus.UNSUPPORTED,
                    selected_adapter_id=None,
                    matched_capabilities=(),
                    missing_capabilities=request.required_capabilities,
                    reason=f"Project ID mismatch: request project_id '{request.project_id}' does not match registry context '{self.context_id}'.",
                    execution_mode=request.execution_mode,
                    audit_context=request.audit_context
                )

            effective_os = request.target_os or _normalize_current_os()

            # 1. Exact adapter_id specified (DEF-T0049-7: manual fallback is forbidden for exact ID!)
            if request.adapter_id is not None:
                manifest = self._manifests.get(request.adapter_id)
                adapter = self._adapters.get(request.adapter_id)
                if not manifest or not adapter:
                    return AdapterResolutionDecision(
                        decision_status=ResolutionStatus.UNSUPPORTED,
                        selected_adapter_id=None,
                        matched_capabilities=(),
                        missing_capabilities=request.required_capabilities,
                        reason=f"Requested exact adapter_id '{request.adapter_id}' is not registered in context '{self.context_id}'.",
                        execution_mode=request.execution_mode,
                        audit_context=request.audit_context
                    )

                can_match, missing_caps, matched_caps, plat_ver, reject_reason = self._evaluate_candidate(
                    manifest, adapter, request, effective_os
                )
                if can_match and plat_ver is not None:
                    return AdapterResolutionDecision(
                        decision_status=ResolutionStatus.SELECTED,
                        selected_adapter_id=manifest.adapter_id,
                        matched_capabilities=matched_caps,
                        missing_capabilities=(),
                        verification_level=plat_ver.verification_level,
                        reason=f"Exact adapter '{manifest.adapter_id}' matches all requirements.",
                        auth_boundary_summary=manifest.auth_boundary.value,
                        billing_boundary_summary=manifest.billing_boundary.value,
                        workspace_modes=manifest.workspace_modes,
                        execution_mode=request.execution_mode,
                        is_real_host=adapter.detect_capabilities().is_real_host,
                        audit_context=request.audit_context,
                        manifest=manifest
                    )
                else:
                    return AdapterResolutionDecision(
                        decision_status=ResolutionStatus.UNSUPPORTED,
                        selected_adapter_id=None,
                        matched_capabilities=matched_caps,
                        missing_capabilities=missing_caps,
                        reason=f"Requested exact adapter '{manifest.adapter_id}' does not satisfy requirements: {reject_reason}",
                        execution_mode=request.execution_mode,
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
                    execution_mode=request.execution_mode,
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
                        execution_mode=ExecutionMode.MANUAL,
                        audit_context=request.audit_context
                    )
                return AdapterResolutionDecision(
                    decision_status=ResolutionStatus.UNSUPPORTED,
                    selected_adapter_id=None,
                    matched_capabilities=(),
                    missing_capabilities=request.required_capabilities,
                    reason=f"No adapter satisfies requirements. Candidates rejected: {'; '.join(rejection_reasons)}",
                    execution_mode=request.execution_mode,
                    audit_context=request.audit_context
                )
            else:
                # DEF-T0049-10: Multi-candidate resolution based on selection_strategy
                if request.selection_strategy == "first_match":
                    matching_candidates.sort(key=lambda c: c[0].adapter_id)
                    sel_man, sel_adp, sel_matched, sel_pv = matching_candidates[0]
                    return AdapterResolutionDecision(
                        decision_status=ResolutionStatus.SELECTED,
                        selected_adapter_id=sel_man.adapter_id,
                        matched_capabilities=sel_matched,
                        missing_capabilities=(),
                        verification_level=sel_pv.verification_level,
                        reason=f"Selected '{sel_man.adapter_id}' via first_match strategy among {len(matching_candidates)} candidates.",
                        auth_boundary_summary=sel_man.auth_boundary.value,
                        billing_boundary_summary=sel_man.billing_boundary.value,
                        workspace_modes=sel_man.workspace_modes,
                        execution_mode=request.execution_mode,
                        is_real_host=sel_adp.detect_capabilities().is_real_host,
                        audit_context=request.audit_context,
                        manifest=sel_man
                    )
                elif request.selection_strategy == "priority":
                    def priority_key(cand):
                        man, _adp, _matched, _pv = cand
                        prio = 0
                        if isinstance(man.extra, Mapping):
                            raw_p = man.extra.get("priority", 0)
                            if isinstance(raw_p, (int, float)) and not isinstance(raw_p, bool):
                                prio = raw_p
                        return prio

                    sorted_by_prio = sorted(matching_candidates, key=priority_key, reverse=True)
                    top_cand = sorted_by_prio[0]
                    second_cand = sorted_by_prio[1]

                    if priority_key(top_cand) > priority_key(second_cand):
                        sel_man, sel_adp, sel_matched, sel_pv = top_cand
                        return AdapterResolutionDecision(
                            decision_status=ResolutionStatus.SELECTED,
                            selected_adapter_id=sel_man.adapter_id,
                            matched_capabilities=sel_matched,
                            missing_capabilities=(),
                            verification_level=sel_pv.verification_level,
                            reason=f"Selected '{sel_man.adapter_id}' via explicit priority strategy with priority {priority_key(top_cand)}.",
                            auth_boundary_summary=sel_man.auth_boundary.value,
                            billing_boundary_summary=sel_man.billing_boundary.value,
                            workspace_modes=sel_man.workspace_modes,
                            execution_mode=request.execution_mode,
                            is_real_host=sel_adp.detect_capabilities().is_real_host,
                            audit_context=request.audit_context,
                            manifest=sel_man
                        )
                    else:
                        candidate_ids = [c[0].adapter_id for c in matching_candidates]
                        return AdapterResolutionDecision(
                            decision_status=ResolutionStatus.AMBIGUOUS,
                            selected_adapter_id=None,
                            matched_capabilities=(),
                            missing_capabilities=(),
                            reason=f"Priority tie between candidates: {candidate_ids}. Resolution is ambiguous.",
                            execution_mode=request.execution_mode,
                            audit_context=request.audit_context
                        )
                else:  # deterministic: no implicit ranking policy
                    candidate_ids = [c[0].adapter_id for c in matching_candidates]
                    return AdapterResolutionDecision(
                        decision_status=ResolutionStatus.AMBIGUOUS,
                        selected_adapter_id=None,
                        matched_capabilities=(),
                        missing_capabilities=(),
                        reason=f"Multiple adapters satisfy criteria: {candidate_ids}. No explicit selection policy was requested.",
                        execution_mode=request.execution_mode,
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

        # DEF-T0049-1: UNSUPPORTED verification level must NEVER be selected!
        if pv.verification_level == VerificationLevel.UNSUPPORTED:
            return False, request.required_capabilities, (), pv, "VerificationLevel.UNSUPPORTED can never be selected."

        if pv.verification_level not in request.allowed_verification_levels:
            return False, request.required_capabilities, (), pv, f"Platform verification level '{pv.verification_level.value}' not in allowed levels."

        # DEF-T0049-8: Non-real host safety
        is_real = adapter.detect_capabilities().is_real_host
        if is_real is False:
            if request.execution_mode == ExecutionMode.VERIFIED_AUTOMATIC:
                return False, request.required_capabilities, (), pv, "Non-real host adapter (is_real_host=False) cannot be selected for verified_automatic execution mode."
            if pv.verification_level in (VerificationLevel.NATIVE_VERIFIED, VerificationLevel.CLI_VERIFIED, VerificationLevel.MCP_VERIFIED):
                return False, request.required_capabilities, (), pv, "Non-real host adapter (is_real_host=False) cannot provide verified execution level."

        # DEF-T0049-1: STATIC_ONLY cannot be selected for VERIFIED_AUTOMATIC execution mode
        if request.execution_mode == ExecutionMode.VERIFIED_AUTOMATIC and pv.verification_level == VerificationLevel.STATIC_ONLY:
            return False, request.required_capabilities, (), pv, "STATIC_ONLY verification cannot be used for verified_automatic execution mode."

        # DEF-T0049-11: Auth boundary filtering
        if request.allowed_auth_boundaries is not None:
            if manifest.auth_boundary not in request.allowed_auth_boundaries:
                return False, request.required_capabilities, (), pv, f"Auth boundary '{manifest.auth_boundary.value}' not in allowed auth boundaries."

        # DEF-T0049-11: Billing boundary filtering
        if request.allowed_billing_boundaries is not None:
            if manifest.billing_boundary not in request.allowed_billing_boundaries:
                return False, request.required_capabilities, (), pv, f"Billing boundary '{manifest.billing_boundary.value}' not in allowed billing boundaries."

        # DEF-T0049-14: account and billing identities are exact-match boundaries.
        if manifest.auth_context_id != request.auth_context_id:
            return False, request.required_capabilities, (), pv, "Auth context ID does not match the registered adapter context."
        if manifest.billing_context_id != request.billing_context_id:
            return False, request.required_capabilities, (), pv, "Billing context ID does not match the registered adapter context."

        # c. Workspace modes check
        for req_wm in request.required_workspace_modes:
            if req_wm not in manifest.workspace_modes:
                return False, request.required_capabilities, (), pv, f"Required workspace mode '{req_wm}' not supported."

        # d. Capabilities check (DEF-T0049-2: UNKNOWN != SUPPORTED, runtime check required)
        detected_caps = adapter.detect_capabilities()
        matched: List[str] = []
        missing: List[str] = []

        for req_cap in request.required_capabilities:
            # Check manifest declared capability
            declared_status = manifest.capabilities.get(req_cap, "unknown")
            if declared_status != "supported":
                missing.append(req_cap)
                continue

            # DEF-T0049-2: Must check runtime HostCapabilities
            field_name = f"supports_{req_cap}" if not req_cap.startswith("supports_") else req_cap
            has_cap = False
            if hasattr(detected_caps, field_name):
                cap_val = getattr(detected_caps, field_name)
                if cap_val == CapabilitySupport.SUPPORTED or cap_val == "supported":
                    has_cap = True
            elif isinstance(detected_caps.extra, Mapping):
                extra_val = detected_caps.extra.get(req_cap, detected_caps.extra.get(field_name))
                if extra_val == CapabilitySupport.SUPPORTED or extra_val == "supported":
                    has_cap = True

            if not has_cap:
                # Ghost/unknown capability -> rejected!
                missing.append(req_cap)
            else:
                matched.append(req_cap)

        if missing:
            return False, tuple(missing), tuple(matched), pv, f"Missing required capabilities: {missing}."

        return True, (), tuple(matched), pv, "OK"
