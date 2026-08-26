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
from scripts._lib.core.evidence_schema import (
    EvidenceMetadata,
    EvidenceRecord,
    EvidenceType,
    ArtifactRecord,
)
from scripts._lib.core.evidence_store import EvidenceStore
from scripts._lib.core.evidence_gate import (
    EvidenceGate,
    EvidenceValidationContext,
)
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
    assert caps.supports_interactive_confirmation == CapabilitySupport.SUPPORTED


def test_codex_cli_adapter_sandbox_role_enforcement():
    # DEF-T0050-1: Test role-based sandboxing and injection defense
    adapter = CodexCliAdapter(is_real_host=False)

    # 1. REVIEWER defaults to read-only
    req_reviewer = AgentRequest(
        session_id="sess_rev_01",
        prompt="Review pull request",
        role="REVIEWER",
        workspace_dir=os.path.abspath(".")
    )
    handle_rev = adapter.dispatch_agent(req_reviewer)
    res_rev = adapter.wait_for_result(handle_rev)
    assert res_rev.status == AgentStatus.SUCCESS

    # 2. REVIEWER requesting workspace-write must be rejected!
    req_rev_illegal = AgentRequest(
        session_id="sess_rev_bad",
        prompt="Attempting write as reviewer",
        role="REVIEWER",
        workspace_dir=os.path.abspath("."),
        extra_context={"sandbox_mode": "workspace-write"}
    )
    with pytest.raises(AgentNotSupportedError, match="strictly read-only"):
        adapter.dispatch_agent(req_rev_illegal)

    # 3. danger-full-access injection must be strictly rejected!
    req_danger = AgentRequest(
        session_id="sess_danger",
        prompt="Dangerous execution",
        role="DEV",
        workspace_dir=os.path.abspath("."),
        extra_context={"sandbox_mode": "danger-full-access"}
    )
    with pytest.raises(AgentNotSupportedError, match="Unauthorized or dangerous sandbox mode"):
        adapter.dispatch_agent(req_danger)

    # 4. Unknown sandbox mode rejected
    req_unknown_sandbox = AgentRequest(
        session_id="sess_unknown_sb",
        prompt="Invalid sandbox",
        role="DEV",
        workspace_dir=os.path.abspath("."),
        extra_context={"sandbox_mode": "custom-bypass"}
    )
    with pytest.raises(AgentNotSupportedError, match="Unauthorized or dangerous sandbox mode"):
        adapter.dispatch_agent(req_unknown_sandbox)


def test_codex_cli_adapter_command_building_and_no_invalid_a_flag():
    # DEF-T0050-6, DEF-T0050-7: Ensure `-a` is never passed, and `--approve-for-me` is mutually exclusive with `-s`
    adapter = CodexCliAdapter(is_real_host=False)

    # 1. REVIEWER role generates read-only sandbox without -a and without --approve-for-me
    req_rev = AgentRequest(
        session_id="sess_rev_cmd",
        prompt="Check code correctness",
        role="REVIEWER",
        workspace_dir=os.path.abspath(".")
    )
    cmd_rev = adapter.build_codex_exec_command(req_rev)
    assert "-a" not in cmd_rev
    assert "-s" in cmd_rev
    assert "--approve-for-me" not in cmd_rev
    assert cmd_rev[cmd_rev.index("-s") + 1] == "read-only"
    assert cmd_rev[cmd_rev.index("-C") + 1] == os.path.abspath(".")

    # 2. DEV role with auto approval generates --approve-for-me WITHOUT -s (DEF-T0050-7)
    req_dev_auto = AgentRequest(
        session_id="sess_dev_auto",
        prompt="Build module",
        role="DEV",
        workspace_dir=os.path.abspath("."),
        extra_context={"approval_policy": "auto"}
    )
    cmd_dev_auto = adapter.build_codex_exec_command(req_dev_auto)
    assert "-a" not in cmd_dev_auto
    assert "--approve-for-me" in cmd_dev_auto
    assert "-s" not in cmd_dev_auto  # Mutually exclusive: -s must NOT be present!

    # 3. REVIEWER attempting auto approval / approve_for_me must be rejected!
    req_rev_auto = AgentRequest(
        session_id="sess_rev_auto",
        prompt="Attempting auto write as reviewer",
        role="REVIEWER",
        workspace_dir=os.path.abspath("."),
        extra_context={"approval_policy": "auto"}
    )
    with pytest.raises(AgentNotSupportedError, match="strictly read-only"):
        adapter.build_codex_exec_command(req_rev_auto)


def test_codex_cli_adapter_approval_and_confirmation_contract():
    # DEF-T0050-2 & DEF-T0050-13: Test §7.1 permission approval without faked confirmation
    adapter = CodexCliAdapter(is_real_host=False)

    # 1. Multi-option request without user confirmation token must NOT pick options[0] (DEF-T0050-13)
    conf_prompt = ConfirmationRequest(
        request_id="req_conf_01",
        prompt="Execute git clean?",
        options=("approve", "deny")
    )
    res_prompt = adapter.request_confirmation(conf_prompt)
    assert res_prompt.is_confirmed is False
    assert res_prompt.selected_option == "deny"

    # 2. Verified user approval token
    conf_user = ConfirmationRequest(
        request_id="req_conf_02",
        prompt="Delete production database?",
        options=("user_confirmed",)
    )
    res_user = adapter.request_confirmation(conf_user)
    assert res_user.is_confirmed is True
    assert res_user.selected_option == "user_confirmed"

    # 3. 5-tuple permission approval caching strictly forbids cross-boundary reuse (DEF-T0050-13)
    adapter.record_permission_approval("proj1", "/ws1", "sess1", "inv1", "op_write")
    assert adapter.has_permission_approval("proj1", "/ws1", "sess1", "inv1", "op_write") is True
    # Cross-project boundary
    assert adapter.has_permission_approval("proj2", "/ws1", "sess1", "inv1", "op_write") is False
    # Cross-workspace boundary
    assert adapter.has_permission_approval("proj1", "/ws2", "sess1", "inv1", "op_write") is False
    # Cross-session boundary
    assert adapter.has_permission_approval("proj1", "/ws1", "sess2", "inv1", "op_write") is False
    # Cross-invocation boundary
    assert adapter.has_permission_approval("proj1", "/ws1", "sess1", "inv2", "op_write") is False
    # Cross-operation boundary
    assert adapter.has_permission_approval("proj1", "/ws1", "sess1", "inv1", "op_delete") is False

    # 4. Whitespace request_id validation
    with pytest.raises(ValueError, match="cannot be empty"):
        adapter.request_confirmation(ConfirmationRequest(request_id="", prompt="p", options=()))


def test_codex_cli_adapter_real_thread_id_and_telemetry_binding():
    # DEF-T0050-3, DEF-T0050-8, DEF-T0050-9 & DEF-T0050-10: Test parsing canonical composite invocation
    adapter = CodexCliAdapter(is_real_host=False)
    sample_stdout = (
        '{"type":"thread.started","thread_id":"01a03d0f-ed4f-7191-a8b5-4c8810ad207d"}\n'
        '{"type":"turn.started"}\n'
        '{"type":"item.completed","item":{"id":"item_0","content":"Review completed."}}\n'
        '{"type":"turn.completed","usage":{"input_tokens":15800,"output_tokens":5,"cached_tokens":11008}}\n'
    )
    text, events, err, thread_id, inv_id, usage = adapter._parse_jsonl_output(sample_stdout, "")

    assert thread_id == "01a03d0f-ed4f-7191-a8b5-4c8810ad207d"
    assert inv_id == "01a03d0f-ed4f-7191-a8b5-4c8810ad207d:item_0"
    assert usage.get("input_tokens") == 15800
    assert usage.get("output_tokens") == 5
    assert usage.get("cached_tokens") == 11008
    assert len(events) == 4
    assert err is None
    assert "Review completed." in text


def test_codex_cli_adapter_distinct_thread_invocations_no_collision():
    # DEF-T0050-10: Two different threads returning item_0 must NOT collide
    adapter = CodexCliAdapter(is_real_host=False)
    stdout_a = (
        '{"type":"thread.started","thread_id":"01a03d0f-ed4f-7191-a8b5-4c8810ad207d"}\n'
        '{"type":"item.completed","item":{"id":"item_0","content":"Result A"}}\n'
    )
    stdout_b = (
        '{"type":"thread.started","thread_id":"02b14e1a-fa5e-8202-b9c6-5d9921be318e"}\n'
        '{"type":"item.completed","item":{"id":"item_0","content":"Result B"}}\n'
    )
    _, _, _, th_a, inv_a, _ = adapter._parse_jsonl_output(stdout_a, "")
    _, _, _, th_b, inv_b, _ = adapter._parse_jsonl_output(stdout_b, "")

    assert inv_a == "01a03d0f-ed4f-7191-a8b5-4c8810ad207d:item_0"
    assert inv_b == "02b14e1a-fa5e-8202-b9c6-5d9921be318e:item_0"
    assert inv_a != inv_b


def test_codex_cli_adapter_exit_zero_missing_canonical_identity_fails_closed(monkeypatch):
    # DEF-T0050-11: Process exits with rc=0 but missing canonical identity -> Fail-Closed!
    adapter = CodexCliAdapter(is_real_host=True)

    class MockExitZeroNoIdentityProcess:
        pid = 12345
        returncode = 0
        def communicate(self, timeout=None):
            return "Some generic non-JSONL text output without thread.started\n", ""
        def poll(self):
            return 0

    monkeypatch.setattr(subprocess, "Popen", lambda *args, **kwargs: MockExitZeroNoIdentityProcess())

    req = AgentRequest(
        session_id="sess_no_identity",
        prompt="Task prompt",
        role="DEV",
        workspace_dir=os.path.abspath(".")
    )
    handle = adapter.dispatch_agent(req)
    result = adapter.wait_for_result(handle)

    # Must Fail-Closed
    assert result.status == AgentStatus.FAILED
    assert "Fail-Closed" in (result.error_message or "")
    assert adapter.get_session_thread_id(handle.session_id) is None
    assert adapter.get_session_invocation_id(handle.session_id) is None


def test_codex_cli_adapter_cancel_handle_ownership_validation():
    # DEF-T0050-12: cancel_agent must enforce identical handle ownership validation as wait_for_result
    adapter = CodexCliAdapter(is_real_host=True)

    # 1. Non-AgentHandle type
    with pytest.raises(AgentInvalidHandleError, match="AgentHandle instance"):
        adapter.cancel_agent("invalid_handle")  # type: ignore

    # 2. Foreign adapter instance
    h_foreign_inst = AgentHandle(
        session_id="sess_cancel_chk",
        host_id=adapter.adapter_id,
        status="running",
        is_real_host=True,
        adapter_instance_id="foreign_inst_xyz"
    )
    with pytest.raises(AgentInvalidHandleError, match="foreign adapter instance"):
        adapter.cancel_agent(h_foreign_inst)

    # 3. Foreign host_id
    h_foreign_host = AgentHandle(
        session_id="sess_cancel_chk",
        host_id="antigravity_cli",
        status="running",
        is_real_host=True,
        adapter_instance_id=adapter._instance_id
    )
    with pytest.raises(AgentInvalidHandleError, match="does not match adapter"):
        adapter.cancel_agent(h_foreign_host)

    # 4. Mismatched is_real_host
    h_mismatch_real = AgentHandle(
        session_id="sess_cancel_chk",
        host_id=adapter.adapter_id,
        status="running",
        is_real_host=False,
        adapter_instance_id=adapter._instance_id
    )
    with pytest.raises(AgentInvalidHandleError, match="does not match adapter"):
        adapter.cancel_agent(h_mismatch_real)


def test_codex_cli_adapter_error_events_and_git_repo_check(tmp_path):
    # DEF-T0050-5: JSONL error events must be in events, and non-git directory is rejected
    adapter = CodexCliAdapter(is_real_host=False)

    # 1. Error event in JSONL
    sample_err_stdout = (
        '{"type":"thread.started","thread_id":"01a03d0f-ed4f-7191-a8b5-4c8810ad207d"}\n'
        '{"type":"error","message":"permission_denied: outside sandbox"}\n'
    )
    text, events, err, thread_id, inv_id, usage = adapter._parse_jsonl_output(sample_err_stdout, "")
    assert len(events) == 2  # Error event is properly appended!
    assert err == "permission_denied: outside sandbox"

    # 2. Non-git workspace directory rejection
    non_git_dir = str(tmp_path / "non_git_folder")
    os.makedirs(non_git_dir, exist_ok=True)
    req_non_git = AgentRequest(
        session_id="sess_non_git",
        prompt="test",
        role="DEV",
        workspace_dir=non_git_dir
    )
    with pytest.raises(AgentNotSupportedError, match="not inside a trusted Git repository"):
        adapter.dispatch_agent(req_non_git)


def test_codex_cli_evidence_gate_real_judgment(tmp_path, monkeypatch):
    # DEF-T0050-4, DEF-T0050-8, DEF-T0050-9 & DEF-T0050-10: Test real EvidenceGate judgment with composite host identity
    adapter = CodexCliAdapter(is_real_host=True)
    manifest = create_codex_cli_manifest()
    caps = adapter.detect_capabilities()

    sample_stdout = (
        '{"type":"thread.started","thread_id":"01a03d0f-ed4f-7191-a8b5-4c8810ad207d"}\n'
        '{"type":"turn.started"}\n'
        '{"type":"item.completed","item":{"id":"item_0","content":"Implemented adapter features."}}\n'
        '{"type":"turn.completed","usage":{"input_tokens":15800,"output_tokens":5,"cached_tokens":11008}}\n'
    )

    class MockCodexProcess:
        pid = 12345
        returncode = 0
        def communicate(self, timeout=None):
            return sample_stdout, ""
        def poll(self):
            return 0

    monkeypatch.setattr(subprocess, "Popen", lambda *args, **kwargs: MockCodexProcess())

    req = AgentRequest(
        session_id="sess_ev_02",
        prompt="Feature implementation",
        role="DEV",
        workspace_dir=os.path.abspath(".")
    )
    handle = adapter.dispatch_agent(req)
    result = adapter.wait_for_result(handle)

    # Retrieve dynamically bound real thread_id and composite invocation_id (DEF-T0050-10)
    real_thread_id = adapter.get_session_thread_id(handle.session_id)
    real_inv_id = adapter.get_session_invocation_id(handle.session_id)
    assert real_thread_id == "01a03d0f-ed4f-7191-a8b5-4c8810ad207d"
    assert real_inv_id == "01a03d0f-ed4f-7191-a8b5-4c8810ad207d:item_0"

    store_dir = str(tmp_path / "evidence_store")
    store = EvidenceStore(root_dir=store_dir)
    gate = EvidenceGate(store=store, project_root=os.path.abspath("."))

    ctx = EvidenceValidationContext(
        project_id="phase2_proj",
        task_id="T0050",
        actor_role="DEV",
        transition_from="进行中",
        transition_to="审查中",
        baseline_commit="b9c426a7c5d9226f2816fbe62ded3fb4a58d1e3c",
        result_commit="de9c4a60de22bbe6fc007952b409a85c5b2a09f7",
        expected_invocation_id=real_inv_id,
        expected_adapter="codex_cli",
        expected_workspace_mode="worktree",
        expected_evidence_type=EvidenceType.TASK_TRANSITION,
        host_handle=handle,
        expected_capabilities=caps,
        agent_result=result
    )

    meta_extra = {f"capability_{k}": v for k, v in caps.__dict__.items() if k != "extra"}
    meta_extra["thread_id"] = real_thread_id
    meta_extra["host_identity_source"] = "openai_codex_host_thread_id"

    metadata = EvidenceMetadata(
        project_id="phase2_proj",
        task_id="T0050",
        actor_role="DEV",
        host_id="codex_cli",
        adapter="codex_cli",
        host_session_id=handle.session_id,
        host_invocation_id=real_inv_id,
        is_real_host=True,
        workspace_mode="worktree",
        transition_from="进行中",
        transition_to="审查中",
        created_at=time.time(),
        extra=meta_extra
    )

    record = EvidenceRecord(
        evidence_id="ev-codex-e2e-001",
        evidence_type=EvidenceType.TASK_TRANSITION,
        baseline_commit="b9c426a7c5d9226f2816fbe62ded3fb4a58d1e3c",
        result_commit="de9c4a60de22bbe6fc007952b409a85c5b2a09f7",
        artifacts=(),
        metadata=metadata
    )

    store.append(record)
    assert gate.validate_evidence(record.evidence_id, ctx) is True


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


def test_codex_cli_real_executable_detection_and_help():
    # DEF-T0050-6: Verify discovery and parameters against real binary
    exe_path = _find_default_codex_executable()
    if exe_path:
        assert os.path.exists(exe_path)
        # Execute version check
        proc_ver = subprocess.run([exe_path, "--version"], capture_output=True, text=True)
        assert proc_ver.returncode == 0
        assert "codex" in proc_ver.stdout.lower()

        # Execute codex exec --help to verify valid parameters
        proc_help = subprocess.run([exe_path, "exec", "--help"], capture_output=True, text=True)
        assert proc_help.returncode == 0
        assert "--sandbox" in proc_help.stdout
        assert "--json" in proc_help.stdout
        assert "--approve-for-me" in proc_help.stdout
