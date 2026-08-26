import json
import os
import subprocess
import sys
import time
from types import MappingProxyType
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
    AdapterResolutionRequest,
    ResolutionStatus,
)
from scripts._lib.core.adapter_conformance import (
    assert_capabilities_conformance,
    assert_handle_conformance,
    assert_manifest_conformance,
    assert_zero_side_effects,
)
from scripts._lib.core.evidence_schema import EvidenceType
from scripts._lib.core.evidence_gate import EvidenceValidationContext
from scripts._lib.hosts.codex_cli_adapter import (
    CodexCliAdapter,
    _find_default_codex_executable,
    create_codex_cli_manifest,
)


def test_codex_cli_manifest_structure_and_anti_forgery():
    manifest = create_codex_cli_manifest()
    assert manifest.adapter_id == "codex_cli"
    assert manifest.host_surface == HostSurface.CLI
    assert manifest.verification_level == VerificationLevel.CLI_VERIFIED
    assert manifest.auth_boundary == AuthBoundaryType.USER_LOCAL
    assert manifest.billing_boundary == BillingBoundaryType.USER_SUBSCRIPTION
    assert "windows" in manifest.platform_verifications
    assert manifest.platform_verifications["windows"].verification_level == VerificationLevel.CLI_VERIFIED
    assert manifest.platform_verifications["macos"].verification_level == VerificationLevel.STATIC_ONLY
    assert manifest.platform_verifications["linux"].verification_level == VerificationLevel.STATIC_ONLY

    # Run conformance assertion on manifest
    assert_manifest_conformance(manifest)


def test_codex_cli_adapter_capabilities_detection_zero_side_effects():
    adapter = CodexCliAdapter(is_real_host=False)
    manifest = create_codex_cli_manifest()

    # Zero side effects assertion
    assert_zero_side_effects(adapter)

    # Capabilities match manifest
    caps = adapter.detect_capabilities()
    assert caps.supports_real_subagents == CapabilitySupport.SUPPORTED
    assert caps.supports_worktree == CapabilitySupport.SUPPORTED
    assert caps.supports_isolated_context == CapabilitySupport.SUPPORTED
    assert caps.supports_parallelism == CapabilitySupport.SUPPORTED


def test_codex_cli_adapter_registration_and_resolution():
    registry = AdapterRegistry(context_id="p_codex")
    adapter = CodexCliAdapter(is_real_host=True)
    manifest = create_codex_cli_manifest()

    registry.register(adapter, manifest)
    assert registry.get("codex_cli") is adapter

    # Exact ID resolution on Windows
    req_exact = AdapterResolutionRequest(
        project_id="p_codex",
        adapter_id="codex_cli",
        target_os="windows",
        allowed_verification_levels=(VerificationLevel.CLI_VERIFIED, VerificationLevel.STATIC_ONLY),
        auth_context_id="user_local_ctx",
        billing_context_id="user_sub_ctx"
    )
    decision_exact = registry.resolve(req_exact)
    assert decision_exact.decision_status == ResolutionStatus.SELECTED
    assert decision_exact.selected_adapter_id == "codex_cli"
    assert decision_exact.verification_level == VerificationLevel.CLI_VERIFIED

    # Capability-based resolution
    req_caps = AdapterResolutionRequest(
        project_id="p_codex",
        target_os="windows",
        required_capabilities=("real_subagents", "worktree"),
        allowed_verification_levels=(VerificationLevel.CLI_VERIFIED,),
        auth_context_id="user_local_ctx",
        billing_context_id="user_sub_ctx"
    )
    decision_caps = registry.resolve(req_caps)
    assert decision_caps.decision_status == ResolutionStatus.SELECTED
    assert decision_caps.selected_adapter_id == "codex_cli"

    # Resolution on macOS returns STATIC_ONLY
    req_mac = AdapterResolutionRequest(
        project_id="p_codex",
        adapter_id="codex_cli",
        target_os="macos",
        allowed_verification_levels=(VerificationLevel.STATIC_ONLY,),
        auth_context_id="user_local_ctx",
        billing_context_id="user_sub_ctx"
    )
    decision_mac = registry.resolve(req_mac)
    assert decision_mac.decision_status == ResolutionStatus.SELECTED
    assert decision_mac.verification_level == VerificationLevel.STATIC_ONLY


def test_codex_cli_adapter_simulated_dispatch_and_wait():
    adapter = CodexCliAdapter(is_real_host=False)
    req = AgentRequest(
        session_id="sess_sim_01",
        prompt="Implement user auth module",
        role="DEV",
        workspace_dir=os.path.abspath(".")
    )

    handle = adapter.dispatch_agent(req)
    assert handle.session_id == "sess_sim_01"
    assert handle.host_id == "codex_cli"
    assert handle.status == "running"

    result = adapter.wait_for_result(handle, timeout_seconds=5.0)
    assert result.session_id == "sess_sim_01"
    assert result.status == AgentStatus.SUCCESS
    assert result.is_real_host is False


def test_codex_cli_adapter_foreign_handle_rejection():
    adapter = CodexCliAdapter(is_real_host=False)
    foreign_handle = AgentHandle(
        session_id="foreign_session",
        host_id="codex_cli",
        status="running",
        is_real_host=False,
        adapter_instance_id="foreign_inst_id"
    )

    with pytest.raises(AgentInvalidHandleError, match="foreign adapter instance"):
        adapter.wait_for_result(foreign_handle)


def test_codex_cli_adapter_cancel_lifecycle():
    adapter = CodexCliAdapter(is_real_host=False)
    req = AgentRequest(
        session_id="sess_cancel_01",
        prompt="Long running task",
        role="DEV",
        workspace_dir=os.path.abspath(".")
    )

    handle = adapter.dispatch_agent(req)
    assert adapter.cancel_agent(handle) is True
    # Cancel again on removed session returns False
    assert adapter.cancel_agent(handle) is False


def test_codex_cli_adapter_timeout_handling(monkeypatch):
    adapter = CodexCliAdapter(is_real_host=True)

    class MockHangingProcess:
        pid = 99999
        returncode = None
        def poll(self):
            return None
        def communicate(self, timeout=None):
            raise subprocess.TimeoutExpired(cmd=["codex"], timeout=timeout)
        def terminate(self):
            pass
        def kill(self):
            pass

    monkeypatch.setattr(subprocess, "Popen", lambda *args, **kwargs: MockHangingProcess())

    req = AgentRequest(
        session_id="sess_hang_01",
        prompt="Hanging task",
        role="DEV",
        workspace_dir=os.path.abspath(".")
    )

    handle = adapter.dispatch_agent(req)
    with pytest.raises(AgentTimeoutError, match="timed out"):
        adapter.wait_for_result(handle, timeout_seconds=0.01)


def test_codex_cli_adapter_failed_process_execution(monkeypatch):
    adapter = CodexCliAdapter(is_real_host=True)

    class MockFailedProcess:
        pid = 12345
        returncode = 1
        def poll(self):
            return 1
        def communicate(self, timeout=None):
            return "", "Compilation failed: syntax error"

    monkeypatch.setattr(subprocess, "Popen", lambda *args, **kwargs: MockFailedProcess())

    req = AgentRequest(
        session_id="sess_fail_01",
        prompt="Fail task",
        role="DEV",
        workspace_dir=os.path.abspath(".")
    )

    handle = adapter.dispatch_agent(req)
    result = adapter.wait_for_result(handle, timeout_seconds=1.0)
    assert result.status == AgentStatus.FAILED
    assert "syntax error" in result.output


def test_codex_cli_adapter_input_validation():
    adapter = CodexCliAdapter(is_real_host=False)

    # 1. Non-AgentRequest
    with pytest.raises(TypeError, match="request must be an AgentRequest instance"):
        adapter.dispatch_agent("invalid_req")  # type: ignore

    # 2. Empty session_id
    with pytest.raises(ValueError, match="request.session_id cannot be empty"):
        adapter.dispatch_agent(AgentRequest(session_id="", prompt="p", role="DEV", workspace_dir=os.path.abspath(".")))

    # 3. Non-absolute workspace_dir
    with pytest.raises(ValueError, match="must be an absolute path"):
        adapter.dispatch_agent(AgentRequest(session_id="s1", prompt="p", role="DEV", workspace_dir="relative/path"))

    # 4. Confirmation request
    conf_req = ConfirmationRequest(request_id="cr_1", prompt="Confirm destructive action?", options=("yes", "no"))
    conf_res = adapter.request_confirmation(conf_req)
    assert conf_res.is_confirmed is True
    assert conf_res.selected_option == "yes"


def test_codex_cli_missing_executable_error():
    adapter = CodexCliAdapter(executable_path="C:\\nonexistent\\path\\codex.exe", is_real_host=True)
    req = AgentRequest(session_id="sess_miss", prompt="p", role="DEV", workspace_dir=os.path.abspath("."))

    with pytest.raises(AgentNotSupportedError, match="Codex CLI executable not found"):
        adapter.dispatch_agent(req)


def test_codex_cli_jsonl_output_parsing():
    adapter = CodexCliAdapter(is_real_host=False)
    sample_stdout = (
        '{"type":"session_start","session_id":"s_123"}\n'
        '{"type":"assistant_message","content":"Hello world"}\n'
        '{"type":"message","text":"Task complete"}\n'
        '{"type":"turn_complete"}\n'
    )
    text, events, err = adapter._parse_jsonl_output(sample_stdout, "")
    assert "Hello world" in text
    assert "Task complete" in text
    assert len(events) == 4
    assert err is None


def test_codex_cli_evidence_gate_integration():
    adapter = CodexCliAdapter(is_real_host=True)
    manifest = create_codex_cli_manifest()
    caps = adapter.detect_capabilities()

    req = AgentRequest(
        session_id="sess_ev_01",
        prompt="Implement feature",
        role="DEV",
        workspace_dir=os.path.abspath(".")
    )
    handle = adapter.dispatch_agent(req)

    ctx = EvidenceValidationContext(
        project_id="test_proj",
        task_id="T0050",
        actor_role="DEV",
        transition_from="进行中",
        transition_to="审查中",
        baseline_commit="b9c426a7c5d9226f2816fbe62ded3fb4a58d1e3c",
        result_commit="b9c426a7c5d9226f2816fbe62ded3fb4a58d1e3c",
        expected_invocation_id="inv_codex_001",
        expected_adapter="codex_cli",
        expected_workspace_mode="worktree",
        expected_evidence_type=EvidenceType.TASK_TRANSITION,
        host_handle=handle,
        expected_capabilities=caps
    )
    assert ctx.expected_adapter == "codex_cli"
    assert ctx.host_handle.host_id == "codex_cli"
    assert ctx.host_handle.is_real_host is True


def test_codex_cli_real_executable_detection():
    # Verify discovery of local binary
    exe_path = _find_default_codex_executable()
    if exe_path:
        assert os.path.exists(exe_path)
        # Execute version check
        proc = subprocess.run([exe_path, "--version"], capture_output=True, text=True)
        assert proc.returncode == 0
        assert "codex" in proc.stdout.lower()
