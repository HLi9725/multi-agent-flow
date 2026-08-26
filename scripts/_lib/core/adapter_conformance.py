from collections.abc import Mapping
import io
import os
import pathlib
import subprocess
import sys
import threading
import uuid
from typing import Any, Dict, List, Optional, Tuple

from .agent_schema import (
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
from .host_adapter import BaseHostAdapter
from .adapter_manifest import (
    AdapterManifest,
    AuthBoundaryType,
    BillingBoundaryType,
    HostSurface,
    PlatformVerification,
    VerificationLevel,
)


class ConformanceError(Exception):
    """Raised when an adapter or manifest fails the generic conformance suite."""
    pass


_AUDIT_CONTEXT = threading.local()
_AUDIT_INSTALL_LOCK = threading.Lock()
_AUDIT_HOOK_INSTALLED = False


def _zero_side_effect_audit_hook(event: str, args: Tuple[Any, ...]) -> None:
    """Fail closed only for the thread currently running a capability probe."""
    intercepted_calls = getattr(_AUDIT_CONTEXT, "intercepted_calls", None)
    if intercepted_calls is None:
        return

    description: Optional[str] = None
    if event == "open":
        path = args[0] if args else "<unknown>"
        mode = args[1] if len(args) > 1 else None
        flags = args[2] if len(args) > 2 else 0
        write_flags = os.O_WRONLY | os.O_RDWR | os.O_CREAT | getattr(os, "O_APPEND", 0) | getattr(os, "O_TRUNC", 0)
        if (isinstance(mode, str) and any(marker in mode for marker in ("w", "a", "x", "+"))) or (
            isinstance(flags, int) and bool(flags & write_flags)
        ):
            description = f"file write attempt on {path}"
    elif event in {"os.mkdir", "os.remove", "os.rmdir", "os.rename", "os.chmod", "os.truncate"}:
        description = f"filesystem mutation via {event}"
    elif event == "subprocess.Popen":
        description = "subprocess execution attempt"
    elif event in {"socket.connect", "socket.bind", "socket.getaddrinfo"}:
        description = f"network operation via {event}"

    if description is not None:
        intercepted_calls.append(f"{event}: {description}")
        raise ConformanceError(f"Side-effect intercepted: {description}")


def _install_zero_side_effect_audit_hook() -> None:
    global _AUDIT_HOOK_INSTALLED
    if _AUDIT_HOOK_INSTALLED:
        return
    with _AUDIT_INSTALL_LOCK:
        if not _AUDIT_HOOK_INSTALLED:
            sys.addaudithook(_zero_side_effect_audit_hook)
            _AUDIT_HOOK_INSTALLED = True


def assert_manifest_conformance(manifest: AdapterManifest) -> None:
    if not isinstance(manifest, AdapterManifest):
        raise ConformanceError(f"Manifest must be an instance of AdapterManifest, got {type(manifest).__name__}")

    # Check that manifest does not claim verified without e2e refs
    if manifest.verification_level in (VerificationLevel.NATIVE_VERIFIED, VerificationLevel.CLI_VERIFIED, VerificationLevel.MCP_VERIFIED):
        if not manifest.e2e_evidence_refs:
            raise ConformanceError("Verified manifest must supply non-empty e2e_evidence_refs")
        if not manifest.verified_at:
            raise ConformanceError("Verified manifest must supply verified_at timestamp")

    # Check that every platform verification is valid
    for os_name, pv in manifest.platform_verifications.items():
        if os_name not in manifest.supported_operating_systems:
            raise ConformanceError(f"Platform verification contains OS '{os_name}' not in supported_operating_systems")
        if pv.verification_level in (VerificationLevel.NATIVE_VERIFIED, VerificationLevel.CLI_VERIFIED, VerificationLevel.MCP_VERIFIED):
            if not pv.e2e_evidence_refs:
                raise ConformanceError(f"Platform verification for '{os_name}' claims verified but lacks e2e_evidence_refs")


def assert_capabilities_conformance(adapter: BaseHostAdapter, manifest: AdapterManifest) -> None:
    caps = adapter.detect_capabilities()
    if not isinstance(caps, HostCapabilities):
        raise ConformanceError("adapter.detect_capabilities() must return HostCapabilities instance")

    # Cross check is_real_host against manifest verification_level
    if caps.is_real_host is False:
        if manifest.verification_level in (VerificationLevel.NATIVE_VERIFIED, VerificationLevel.CLI_VERIFIED, VerificationLevel.MCP_VERIFIED):
            raise ConformanceError("Fake/non-real host adapter cannot claim native/cli/mcp verified level in manifest.")
        for pv_os, pv in manifest.platform_verifications.items():
            if pv.verification_level in (VerificationLevel.NATIVE_VERIFIED, VerificationLevel.CLI_VERIFIED, VerificationLevel.MCP_VERIFIED):
                raise ConformanceError(f"Fake/non-real host adapter cannot claim {pv.verification_level.value} on platform '{pv_os}'.")

    # Cross check individual capabilities
    for cap_k, cap_v in manifest.capabilities.items():
        field_name = f"supports_{cap_k}" if not cap_k.startswith("supports_") else cap_k
        val = None
        if hasattr(caps, field_name):
            val = getattr(caps, field_name)
        elif isinstance(caps.extra, Mapping):
            val = caps.extra.get(cap_k, caps.extra.get(field_name))

        if cap_v == "supported":
            if val != CapabilitySupport.SUPPORTED and val != "supported":
                raise ConformanceError(f"Manifest claims '{cap_k}' is supported, but detect_capabilities reports '{val}'")
        elif cap_v == "unsupported":
            if val == CapabilitySupport.SUPPORTED or val == "supported":
                raise ConformanceError(f"Manifest claims '{cap_k}' is unsupported, but detect_capabilities reports SUPPORTED")


def assert_zero_side_effects(adapter: BaseHostAdapter) -> None:
    """
    Intercept writes, process creation, and network activity during detect_capabilities().

    The audit hook is process-global but inert by default. Its enforcement context is
    thread-local, so unrelated threads retain their normal filesystem/network behavior.
    This avoids rebinding builtins/os/pathlib functions across the whole process.
    """
    _install_zero_side_effect_audit_hook()
    intercepted_calls: List[str] = []
    previous_context = getattr(_AUDIT_CONTEXT, "intercepted_calls", None)
    try:
        _AUDIT_CONTEXT.intercepted_calls = intercepted_calls
        caps = adapter.detect_capabilities()
        if not isinstance(caps, HostCapabilities):
            raise ConformanceError("detect_capabilities did not return HostCapabilities instance")
    finally:
        if previous_context is None:
            try:
                del _AUDIT_CONTEXT.intercepted_calls
            except AttributeError:
                pass
        else:
            _AUDIT_CONTEXT.intercepted_calls = previous_context

    if intercepted_calls:
        raise ConformanceError(f"Zero side-effect violation: intercepted {intercepted_calls}")


def assert_handle_conformance(adapter: BaseHostAdapter, req: AgentRequest) -> AgentHandle:
    handle = adapter.dispatch_agent(req)
    if not isinstance(handle, AgentHandle):
        raise ConformanceError(f"dispatch_agent must return AgentHandle, got {type(handle).__name__}")

    if not handle.session_id:
        raise ConformanceError("AgentHandle.session_id cannot be empty")
    if not handle.host_id:
        raise ConformanceError("AgentHandle.host_id cannot be empty")

    caps = adapter.detect_capabilities()
    if caps.is_real_host is False and handle.is_real_host is not False:
        raise ConformanceError("Fake adapter must produce handle with is_real_host=False")

    return handle


def assert_lifecycle_conformance(adapter: BaseHostAdapter, req: AgentRequest) -> None:
    handle = assert_handle_conformance(adapter, req)

    # Rejection of foreign or invalid handle
    foreign_handle = AgentHandle(
        session_id=str(uuid.uuid4()),
        host_id="foreign_host_id",
        status="running",
        is_real_host=False,
        adapter_instance_id="foreign_instance"
    )
    try:
        adapter.wait_for_result(foreign_handle, timeout_seconds=0.1)
        raise ConformanceError("Adapter accepted foreign handle in wait_for_result without error.")
    except AgentInvalidHandleError:
        pass  # Expected behavior
    except ConformanceError:
        raise
    except Exception as e:
        # If adapter raises specific handle error, acceptable
        if "handle" not in str(e).lower():
            raise ConformanceError(f"Unexpected error on foreign handle: {e}")

    # Wait for result on valid handle
    result = adapter.wait_for_result(handle, timeout_seconds=1.0)
    if not isinstance(result, AgentResult):
        raise ConformanceError(f"wait_for_result must return AgentResult, got {type(result).__name__}")

    if result.session_id != req.session_id:
        raise ConformanceError(f"Result session_id '{result.session_id}' does not match request session_id '{req.session_id}'")


def run_adapter_conformance_suite(adapter: BaseHostAdapter, manifest: AdapterManifest) -> Dict[str, Any]:
    assert_manifest_conformance(manifest)
    assert_capabilities_conformance(adapter, manifest)
    assert_zero_side_effects(adapter)

    sample_req = AgentRequest(
        session_id=f"conformance-test-{uuid.uuid4().hex[:8]}",
        prompt="conformance verification prompt",
        role="BUILDER",
        workspace_dir=os.path.abspath("."),
        timeout_seconds=5.0
    )
    assert_lifecycle_conformance(adapter, sample_req)

    return {
        "status": "PASSED",
        "adapter_id": manifest.adapter_id,
        "verification_level": manifest.verification_level.value,
        "is_real_host": adapter.detect_capabilities().is_real_host,
    }


# ==============================================================================
# Compliant Test Adapter & Helper Fixtures
# ==============================================================================

class StandardTestFakeAdapter(BaseHostAdapter):
    """A compliant FakeHostAdapter for testing 2D-1 generic infrastructure."""
    def __init__(self, adapter_id: str = "standard_test_fake"):
        self.adapter_id = adapter_id
        self._instance_id = f"fake-inst:{uuid.uuid4().hex[:8]}"
        self._running_sessions: Dict[str, AgentHandle] = {}
        self._capabilities = HostCapabilities(
            is_real_host=False,
            supports_real_subagents=CapabilitySupport.UNSUPPORTED,
            supports_parallelism=CapabilitySupport.SUPPORTED,
            supports_isolated_context=CapabilitySupport.SUPPORTED,
            supports_worktree=CapabilitySupport.SUPPORTED,
            supports_permission_approval=CapabilitySupport.UNSUPPORTED,
            supports_mcp=CapabilitySupport.UNSUPPORTED,
            supports_interactive_confirmation=CapabilitySupport.UNSUPPORTED,
            supports_usage_telemetry=CapabilitySupport.UNSUPPORTED,
            max_concurrent_agents=4
        )

    def detect_capabilities(self) -> HostCapabilities:
        return self._capabilities

    def dispatch_agent(self, request: AgentRequest) -> AgentHandle:
        handle = AgentHandle(
            session_id=request.session_id,
            host_id=self.adapter_id,
            status="running",
            is_real_host=False,
            adapter_instance_id=self._instance_id
        )
        self._running_sessions[request.session_id] = handle
        return handle

    def wait_for_result(self, handle: AgentHandle, timeout_seconds: Optional[float] = None) -> AgentResult:
        if not isinstance(handle, AgentHandle):
            raise AgentInvalidHandleError("Invalid handle instance")
        if handle.adapter_instance_id != self._instance_id:
            raise AgentInvalidHandleError(f"Handle belongs to foreign adapter instance {handle.adapter_instance_id}")
        if handle.session_id not in self._running_sessions:
            raise AgentInvalidHandleError(f"Session {handle.session_id} not found in this adapter")

        return AgentResult(
            session_id=handle.session_id,
            status=AgentStatus.SUCCESS,
            output="Standard fake result output",
            is_real_host=False
        )

    def cancel_agent(self, handle: AgentHandle) -> bool:
        if handle.session_id in self._running_sessions:
            del self._running_sessions[handle.session_id]
            return True
        return False

    def request_confirmation(self, req: ConfirmationRequest) -> ConfirmationResult:
        return ConfirmationResult(
            request_id=req.request_id,
            selected_option="confirm",
            is_confirmed=True,
            is_real_host=False
        )


def create_standard_fake_manifest(adapter_id: str = "standard_test_fake") -> AdapterManifest:
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
        verification_level=VerificationLevel.STATIC_ONLY,
        verified_version="1.0.0"
    )
    return AdapterManifest(
        schema_version="2.0",
        adapter_id=adapter_id,
        display_name="Standard Test Fake Adapter",
        implementation_version="1.0.0",
        host_surface=HostSurface.SIMULATED,
        verification_level=VerificationLevel.STATIC_ONLY,
        capabilities={
            "real_subagents": "unsupported",
            "parallelism": "supported",
            "isolated_context": "supported",
            "worktree": "supported",
            "permission_approval": "unsupported",
            "mcp": "unsupported",
            "interactive_confirmation": "unsupported",
            "usage_telemetry": "unsupported"
        },
        workspace_modes=("isolated", "shared", "worktree"),
        identity_fields=("session_id", "host_id"),
        auth_boundary=AuthBoundaryType.NONE,
        billing_boundary=BillingBoundaryType.UNMETERED,
        platform_version_constraint=">=1.0.0",
        supported_operating_systems=("windows", "macos", "linux"),
        platform_verifications={"windows": pv_win, "macos": pv_mac, "linux": pv_linux},
        executable_candidates_by_os={"windows": (), "macos": (), "linux": ()},
        config_path_templates_by_os={"windows": (), "macos": (), "linux": ()},
        conformance_suite_version="2.0"
    )


# ==============================================================================
# Deliberately Non-Compliant / Malicious Test Fixtures
# ==============================================================================

class FaultyCapabilitiesMismatchAdapter(StandardTestFakeAdapter):
    """Claims parallelism is unsupported in detect_capabilities, but manifest says supported."""
    def detect_capabilities(self) -> HostCapabilities:
        return HostCapabilities(
            is_real_host=False,
            supports_parallelism=CapabilitySupport.UNSUPPORTED
        )


class FaultyForgedRealHostHandleAdapter(StandardTestFakeAdapter):
    """A fake adapter that unlawfully claims is_real_host=True in its returned handle."""
    def dispatch_agent(self, request: AgentRequest) -> AgentHandle:
        return AgentHandle(
            session_id=request.session_id,
            host_id=self.adapter_id,
            status="running",
            is_real_host=True,  # VIOLATION: Fake claiming real host
            adapter_instance_id=self._instance_id
        )


class FaultyAcceptForeignHandleAdapter(StandardTestFakeAdapter):
    """An adapter that accepts foreign handles without validation."""
    def wait_for_result(self, handle: AgentHandle, timeout_seconds: Optional[float] = None) -> AgentResult:
        # VIOLATION: accepts any handle
        return AgentResult(session_id=handle.session_id, status=AgentStatus.SUCCESS, output="unauthorized accepted output")


class FaultySideEffectWriteFileAdapter(StandardTestFakeAdapter):
    """DEF-T0049-3: A malicious adapter that attempts file writing via open() during detect_capabilities."""
    def detect_capabilities(self) -> HostCapabilities:
        # VIOLATION: attempts file write during capability detection
        with open("unauthorized_side_effect.tmp", "w") as f:
            f.write("malicious payload")
        return self._capabilities


class FaultySideEffectIoOpenAdapter(StandardTestFakeAdapter):
    """DEF-T0049-9: A malicious adapter that attempts file writing via io.open() during detect_capabilities."""
    def detect_capabilities(self) -> HostCapabilities:
        # VIOLATION: attempts io.open write during capability detection
        with io.open("unauthorized_io_effect.tmp", "w") as f:
            f.write("malicious io payload")
        return self._capabilities


class FaultySideEffectPathlibWriteTextAdapter(StandardTestFakeAdapter):
    """DEF-T0049-9: A malicious adapter that attempts file writing via pathlib.Path.write_text() during detect_capabilities."""
    def detect_capabilities(self) -> HostCapabilities:
        # VIOLATION: attempts pathlib write_text during capability detection
        pathlib.Path("unauthorized_pathlib_effect.tmp").write_text("malicious pathlib payload")
        return self._capabilities


class FaultySideEffectSubprocessAdapter(StandardTestFakeAdapter):
    """DEF-T0049-3: A malicious adapter that attempts subprocess execution during detect_capabilities."""
    def detect_capabilities(self) -> HostCapabilities:
        # VIOLATION: attempts subprocess during capability detection
        subprocess.run(["echo", "malicious"])
        return self._capabilities
