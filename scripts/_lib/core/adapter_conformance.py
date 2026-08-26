from collections.abc import Mapping
import io
import multiprocessing
import os
import pathlib
import subprocess
import sys
import threading
import time
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


_ISOLATED_PROBE_ACTIVE = False
_ISOLATED_INTERCEPTED_CALLS: List[str] = []

_MUTATING_AUDIT_EVENTS = frozenset({
    "os.chdir",
    "os.chmod",
    "os.chown",
    "os.exec",
    "os.fork",
    "os.forkpty",
    "os.kill",
    "os.killpg",
    "os.link",
    "os.mkdir",
    "os.posix_spawn",
    "os.putenv",
    "os.remove",
    "os.rename",
    "os.rmdir",
    "os.spawn",
    "os.startfile",
    "os.symlink",
    "os.system",
    "os.truncate",
    "os.unsetenv",
    "os.utime",
    "shutil.chown",
    "shutil.copyfile",
    "shutil.copymode",
    "shutil.copystat",
    "shutil.move",
    "winreg.CreateKey",
    "winreg.CreateKeyEx",
    "winreg.DeleteKey",
    "winreg.DeleteValue",
    "winreg.SaveKey",
    "winreg.SetValue",
    "winreg.SetValueEx",
})


def _isolated_probe_audit_hook(event: str, args: Tuple[Any, ...]) -> None:
    """Block observable side effects across every thread in the probe process."""
    if not _ISOLATED_PROBE_ACTIVE:
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
    elif event.startswith("subprocess."):
        description = "subprocess execution attempt"
    elif event in _MUTATING_AUDIT_EVENTS:
        description = f"filesystem mutation via {event}"
    elif event.startswith("socket.") or event.startswith("http.client.") or event.startswith("urllib.Request"):
        description = f"network operation via {event}"

    if description is not None:
        _ISOLATED_INTERCEPTED_CALLS.append(f"{event}: {description}")
        raise ConformanceError(f"Side-effect intercepted: {description}")


def _isolated_capability_probe_worker(
    adapter_class: type,
    constructor_args: Tuple[Any, ...],
    constructor_kwargs: Dict[str, Any],
    connection: Any,
) -> None:
    """Construct and probe an adapter inside a dedicated, process-wide guard."""
    global _ISOLATED_PROBE_ACTIVE, _ISOLATED_INTERCEPTED_CALLS
    sys.dont_write_bytecode = True
    _ISOLATED_INTERCEPTED_CALLS = []
    sys.addaudithook(_isolated_probe_audit_hook)
    baseline_threads = {thread.ident for thread in threading.enumerate()}
    _ISOLATED_PROBE_ACTIVE = True

    try:
        isolated_adapter = adapter_class(*constructor_args, **constructor_kwargs)
        caps = isolated_adapter.detect_capabilities()
        if not isinstance(caps, HostCapabilities):
            raise ConformanceError("detect_capabilities did not return HostCapabilities instance")

        lingering_threads = [
            thread.name
            for thread in threading.enumerate()
            if thread.ident not in baseline_threads and thread.is_alive()
        ]
        if lingering_threads:
            _ISOLATED_INTERCEPTED_CALLS.append(
                f"lingering adapter threads: {sorted(lingering_threads)}"
            )
        if _ISOLATED_INTERCEPTED_CALLS:
            raise ConformanceError(
                f"Zero side-effect violation: intercepted {_ISOLATED_INTERCEPTED_CALLS}"
            )
        message = {"status": "ok"}
    except BaseException as exc:
        message = {
            "status": "error",
            "error_type": type(exc).__name__,
            "message": str(exc),
            "intercepted_calls": tuple(_ISOLATED_INTERCEPTED_CALLS),
        }

    try:
        connection.send(message)
        connection.close()
    finally:
        # The process is disposable. Hard exit prevents delayed/background adapter
        # work from escaping after the guard is disabled.
        os._exit(0)


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


def assert_zero_side_effects(adapter: BaseHostAdapter, timeout_seconds: float = 10.0) -> None:
    """
    Intercept writes, process creation, and network activity during detect_capabilities().

    The adapter is reconstructed in a disposable spawn process. The audit hook is
    process-wide only inside that worker, so adapter-created threads are covered while
    unrelated parent-process threads remain untouched.
    """
    if not isinstance(timeout_seconds, (int, float)) or isinstance(timeout_seconds, bool) or timeout_seconds <= 0:
        raise ConformanceError("timeout_seconds must be a positive number")

    spec_factory = getattr(adapter, "get_conformance_probe_spec", None)
    if not callable(spec_factory):
        raise ConformanceError(
            "Adapter must implement get_conformance_probe_spec() for isolated capability probing"
        )
    try:
        constructor_args, constructor_kwargs = spec_factory()
    except Exception as exc:
        raise ConformanceError(f"Failed to build isolated conformance probe spec: {exc}") from exc

    if not isinstance(constructor_args, tuple) or not isinstance(constructor_kwargs, dict):
        raise ConformanceError("Conformance probe spec must be a (tuple args, dict kwargs) pair")
    adapter_class = adapter.__class__
    if "<locals>" in adapter_class.__qualname__:
        raise ConformanceError("Adapter class must be module-level for spawn-process conformance probing")

    context = multiprocessing.get_context("spawn")
    receive_connection, send_connection = context.Pipe(duplex=False)
    process = context.Process(
        target=_isolated_capability_probe_worker,
        args=(adapter_class, constructor_args, constructor_kwargs, send_connection),
        name=f"adapter-conformance-{getattr(adapter, 'adapter_id', 'unknown')}",
    )
    process.daemon = True
    process_started = False
    try:
        process.start()
        process_started = True
        send_connection.close()
        if not receive_connection.poll(float(timeout_seconds)):
            raise ConformanceError(
                f"Isolated capability probe timed out after {timeout_seconds} seconds"
            )
        try:
            message = receive_connection.recv()
        except EOFError as exc:
            raise ConformanceError(
                f"Isolated capability probe exited without a result (exit code {process.exitcode})"
            ) from exc
    except ConformanceError:
        raise
    except Exception as exc:
        raise ConformanceError(f"Failed to start or communicate with isolated capability probe: {exc}") from exc
    finally:
        receive_connection.close()
        send_connection.close()
        if process_started:
            process.join(timeout=1.0)
            if process.is_alive():
                process.terminate()
                process.join(timeout=5.0)

    if message.get("status") != "ok":
        error_type = message.get("error_type", "ConformanceError")
        detail = message.get("message", "isolated probe failed")
        raise ConformanceError(f"{error_type}: {detail}")


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

    def get_conformance_probe_spec(self) -> Tuple[Tuple[Any, ...], Dict[str, Any]]:
        """Return spawn-safe constructor inputs for the isolated capability probe."""
        return (self.adapter_id,), {}

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


class SlowCapabilityAdapter(StandardTestFakeAdapter):
    """Keeps the isolated probe active long enough for parent concurrency tests."""
    def detect_capabilities(self) -> HostCapabilities:
        time.sleep(0.5)
        return self._capabilities


class FaultySideEffectChildThreadAdapter(StandardTestFakeAdapter):
    """Attempts a filesystem write from an adapter-created child thread."""
    def __init__(self, target_path: str, adapter_id: str = "child_thread_writer"):
        super().__init__(adapter_id)
        self.target_path = target_path

    def get_conformance_probe_spec(self) -> Tuple[Tuple[Any, ...], Dict[str, Any]]:
        return (self.target_path, self.adapter_id), {}

    def detect_capabilities(self) -> HostCapabilities:
        errors: List[BaseException] = []

        def write_file() -> None:
            try:
                pathlib.Path(self.target_path).write_text("escaped", encoding="utf-8")
            except BaseException as exc:
                errors.append(exc)

        worker = threading.Thread(target=write_file, name="adapter-child-writer")
        worker.start()
        worker.join()
        if errors:
            raise errors[0]
        return self._capabilities


class FaultySideEffectHardlinkAdapter(StandardTestFakeAdapter):
    """Attempts to create a hard link during capability detection."""
    def __init__(self, source_path: str, target_path: str, adapter_id: str = "hardlink_writer"):
        super().__init__(adapter_id)
        self.source_path = source_path
        self.target_path = target_path

    def get_conformance_probe_spec(self) -> Tuple[Tuple[Any, ...], Dict[str, Any]]:
        return (self.source_path, self.target_path, self.adapter_id), {}

    def detect_capabilities(self) -> HostCapabilities:
        os.link(self.source_path, self.target_path)
        return self._capabilities


class FaultySideEffectSymlinkAdapter(StandardTestFakeAdapter):
    """Attempts to create a symbolic link during capability detection."""
    def __init__(self, source_path: str, target_path: str, adapter_id: str = "symlink_writer"):
        super().__init__(adapter_id)
        self.source_path = source_path
        self.target_path = target_path

    def get_conformance_probe_spec(self) -> Tuple[Tuple[Any, ...], Dict[str, Any]]:
        return (self.source_path, self.target_path, self.adapter_id), {}

    def detect_capabilities(self) -> HostCapabilities:
        os.symlink(self.source_path, self.target_path)
        return self._capabilities


class FaultySideEffectUtimeAdapter(StandardTestFakeAdapter):
    """Attempts to mutate file timestamps during capability detection."""
    def __init__(self, target_path: str, adapter_id: str = "utime_writer"):
        super().__init__(adapter_id)
        self.target_path = target_path

    def get_conformance_probe_spec(self) -> Tuple[Tuple[Any, ...], Dict[str, Any]]:
        return (self.target_path, self.adapter_id), {}

    def detect_capabilities(self) -> HostCapabilities:
        os.utime(self.target_path, (1, 1))
        return self._capabilities
