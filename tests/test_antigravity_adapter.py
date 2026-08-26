import json
import os
import subprocess
import sys
import threading
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
from scripts._lib.hosts.antigravity_adapter import (
    AntigravityAdapter,
    _find_default_antigravity_executable,
    create_antigravity_manifest,
)


def test_antigravity_manifest_structure_and_anti_forgery():
    manifest = create_antigravity_manifest()
    assert manifest.adapter_id == "antigravity"
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


def test_antigravity_adapter_capabilities_detection_zero_side_effects():
    adapter = AntigravityAdapter(is_real_host=False)
    # Zero side effects assertion
    assert_zero_side_effects(adapter)

    # Capabilities match manifest
    caps = adapter.detect_capabilities()
    assert caps.supports_real_subagents == CapabilitySupport.SUPPORTED
    assert caps.supports_worktree == CapabilitySupport.SUPPORTED
    assert caps.supports_isolated_context == CapabilitySupport.SUPPORTED
    assert caps.supports_parallelism == CapabilitySupport.SUPPORTED
    assert caps.supports_permission_approval == CapabilitySupport.SUPPORTED


def test_antigravity_adapter_role_based_routing_and_sandbox():
    adapter = AntigravityAdapter(is_real_host=False)

    # 1. REVIEWER defaults to plan/read-only mode
    req_reviewer = AgentRequest(
        session_id="sess_ag_rev_01",
        prompt="Review codebase",
        role="REVIEWER",
        workspace_dir=os.path.abspath(".")
    )
    cmd_rev = adapter.build_antigravity_exec_command(req_reviewer)
    assert "--agent" in cmd_rev
    assert cmd_rev[cmd_rev.index("--agent") + 1] == "flow-reviewer"
    assert "--mode" in cmd_rev
    assert cmd_rev[cmd_rev.index("--mode") + 1] == "plan"
    assert "--dangerously-skip-permissions" not in cmd_rev

    # 2. REVIEWER requesting accept-edits must be rejected
    req_rev_illegal = AgentRequest(
        session_id="sess_ag_rev_bad",
        prompt="Attempting edits as reviewer",
        role="REVIEWER",
        workspace_dir=os.path.abspath("."),
        extra_context={"mode": "accept-edits"}
    )
    with pytest.raises(AgentNotSupportedError, match="strictly read-only"):
        adapter.dispatch_agent(req_rev_illegal)

    # 3. DEV defaults to flow-dev with accept-edits and sandbox
    req_dev = AgentRequest(
        session_id="sess_ag_dev_01",
        prompt="Implement feature",
        role="DEV",
        workspace_dir=os.path.abspath(".")
    )
    cmd_dev = adapter.build_antigravity_exec_command(req_dev)
    assert cmd_dev[cmd_dev.index("--agent") + 1] == "flow-dev"
    assert cmd_dev[cmd_dev.index("--mode") + 1] == "accept-edits"
    assert "--sandbox" in cmd_dev
    assert "--dangerously-skip-permissions" not in cmd_dev


def test_antigravity_adapter_parsing_real_stream_json():
    adapter = AntigravityAdapter(is_real_host=False)
    sample_stdout = (
        '{"type":"conversation.started","id":"conv-6e9f4305-win"}\n'
        '{"type":"PLANNER_RESPONSE","step_index":1,"content":"Architecture plan created."}\n'
        '{"type":"usage","usage":{"input_tokens":12500,"output_tokens":42,"cached_tokens":8192}}\n'
    )
    text, events, err, conv_id, inv_id, usage = adapter._parse_antigravity_output(sample_stdout, "")

    assert conv_id == "conv-6e9f4305-win"
    assert inv_id == "conv-6e9f4305-win:step_1"
    assert usage.get("input_tokens") == 12500
    assert usage.get("output_tokens") == 42
    assert usage.get("cached_tokens") == 8192
    assert len(events) == 3
    assert err is None
    assert "Architecture plan created." in text


def test_antigravity_adapter_distinct_session_invocations_no_collision():
    # Two distinct Antigravity conversations returning step_1 must not collide
    adapter = AntigravityAdapter(is_real_host=False)
    stdout_a = '{"type":"conversation.started","id":"conv-alpha-001"}\n{"step_index":1,"content":"A"}\n'
    stdout_b = '{"type":"conversation.started","id":"conv-beta-002"}\n{"step_index":1,"content":"B"}\n'

    _, _, _, conv_a, inv_a, _ = adapter._parse_antigravity_output(stdout_a, "")
    _, _, _, conv_b, inv_b, _ = adapter._parse_antigravity_output(stdout_b, "")

    assert inv_a == "conv-alpha-001:step_1"
    assert inv_b == "conv-beta-002:step_1"
    assert inv_a != inv_b


def test_antigravity_adapter_exit_zero_missing_canonical_identity_fails_closed(monkeypatch):
    adapter = AntigravityAdapter(is_real_host=True)

    class MockExitZeroNoIdentityProcess:
        pid = 88888
        returncode = 0
        def communicate(self, timeout=None):
            return "Generic stdout without conversation.started\n", ""
        def poll(self):
            return 0

    monkeypatch.setattr(subprocess, "Popen", lambda *args, **kwargs: MockExitZeroNoIdentityProcess())

    req = AgentRequest(
        session_id="sess_ag_no_id",
        prompt="Execute task",
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


def test_antigravity_adapter_duplicate_session_and_concurrency():
    adapter = AntigravityAdapter(is_real_host=False)

    req = AgentRequest(
        session_id="sess_ag_dup_01",
        prompt="Concurrent task",
        role="DEV",
        workspace_dir=os.path.abspath(".")
    )
    handle1 = adapter.dispatch_agent(req)
    assert handle1.session_id == "sess_ag_dup_01"

    # Duplicate session_id dispatch must fail
    with pytest.raises(AgentInvalidHandleError, match="already exists"):
        adapter.dispatch_agent(req)

    # Complete the session
    res = adapter.wait_for_result(handle1)
    assert res.status == AgentStatus.SUCCESS

    # Historical session ID still cannot be re-dispatched
    with pytest.raises(AgentInvalidHandleError, match="already exists"):
        adapter.dispatch_agent(req)


def test_antigravity_adapter_spawn_failure_rollback(monkeypatch):
    adapter = AntigravityAdapter(is_real_host=True)

    def mock_popen_fail(*args, **kwargs):
        raise OSError("Process creation failed: access denied")

    monkeypatch.setattr(subprocess, "Popen", mock_popen_fail)

    req = AgentRequest(
        session_id="sess_ag_fail_01",
        prompt="Failing process launch",
        role="DEV",
        workspace_dir=os.path.abspath(".")
    )

    with pytest.raises(RuntimeError, match="Failed to launch Antigravity CLI process"):
        adapter.dispatch_agent(req)

    # Verify session reservation was rolled back
    assert "sess_ag_fail_01" not in adapter._running_sessions


def test_antigravity_adapter_handle_forgery_rejection():
    adapter = AntigravityAdapter(is_real_host=True)

    # 1. Non-AgentHandle instance
    with pytest.raises(AgentInvalidHandleError):
        adapter.wait_for_result("not_a_handle")  # type: ignore

    # 2. Foreign adapter instance
    h_foreign = AgentHandle(
        session_id="sess_f",
        host_id=adapter.adapter_id,
        status="running",
        is_real_host=True,
        adapter_instance_id="foreign_inst",
        invocation_token="tok1"
    )
    with pytest.raises(AgentInvalidHandleError, match="foreign adapter instance"):
        adapter.wait_for_result(h_foreign)

    # 3. Foreign host_id
    h_wrong_host = AgentHandle(
        session_id="sess_f",
        host_id="other_host",
        status="running",
        is_real_host=True,
        adapter_instance_id=adapter._instance_id,
        invocation_token="tok1"
    )
    with pytest.raises(AgentInvalidHandleError, match="does not match adapter"):
        adapter.wait_for_result(h_wrong_host)

    # 4. Forged invocation token
    req = AgentRequest(
        session_id="sess_tok_chk",
        prompt="Check token",
        role="DEV",
        workspace_dir=os.path.abspath(".")
    )
    real_handle = adapter.dispatch_agent(req)
    forged_handle = AgentHandle(
        session_id=real_handle.session_id,
        host_id=real_handle.host_id,
        status=real_handle.status,
        is_real_host=real_handle.is_real_host,
        adapter_instance_id=real_handle.adapter_instance_id,
        invocation_token="forged_token_value"
    )
    with pytest.raises(AgentInvalidHandleError, match="Handle attributes mismatch"):
        adapter.wait_for_result(forged_handle)

    with pytest.raises(AgentInvalidHandleError, match="Handle attributes mismatch"):
        adapter.cancel_agent(forged_handle)


def test_antigravity_adapter_timeout_and_cancel_lifecycle(monkeypatch):
    adapter = AntigravityAdapter(is_real_host=True)

    class MockHangingProcess:
        pid = 77777
        returncode = None
        def poll(self):
            return None
        def communicate(self, timeout=None):
            raise subprocess.TimeoutExpired(cmd=["agy"], timeout=timeout)
        def terminate(self):
            pass
        def kill(self):
            pass

    monkeypatch.setattr(subprocess, "Popen", lambda *args, **kwargs: MockHangingProcess())

    req = AgentRequest(
        session_id="sess_ag_timeout",
        prompt="Hanging job",
        role="DEV",
        workspace_dir=os.path.abspath(".")
    )
    handle = adapter.dispatch_agent(req)

    # Test timeout
    with pytest.raises(AgentTimeoutError, match="timed out"):
        adapter.wait_for_result(handle, timeout_seconds=0.01)

    # Test cancel lifecycle on a fresh session
    req_cancel = AgentRequest(
        session_id="sess_ag_cancel",
        prompt="Job to cancel",
        role="DEV",
        workspace_dir=os.path.abspath(".")
    )
    handle_cancel = adapter.dispatch_agent(req_cancel)
    assert adapter.cancel_agent(handle_cancel) is True
    # Cancel again returns False
    assert adapter.cancel_agent(handle_cancel) is False


def test_antigravity_adapter_7tuple_permission_isolation():
    adapter = AntigravityAdapter(is_real_host=False)

    # Record 7-tuple permission approval
    adapter.record_permission_approval(
        project_id="proj_alpha",
        auth_context="user_local_ctx",
        session_id="sess_01",
        workspace_dir="/ws_alpha",
        command_family="safe_local_pytest",
        permission_boundary="workspace_read",
    )

    # Valid check
    assert adapter.has_permission_approval(
        "proj_alpha", "user_local_ctx", "sess_01", "/ws_alpha", "safe_local_pytest", "workspace_read"
    ) is True

    # Boundary isolation tests
    assert adapter.has_permission_approval(
        "proj_beta", "user_local_ctx", "sess_01", "/ws_alpha", "safe_local_pytest", "workspace_read"
    ) is False
    assert adapter.has_permission_approval(
        "proj_alpha", "other_user_ctx", "sess_01", "/ws_alpha", "safe_local_pytest", "workspace_read"
    ) is False
    assert adapter.has_permission_approval(
        "proj_alpha", "user_local_ctx", "sess_02", "/ws_alpha", "safe_local_pytest", "workspace_read"
    ) is False
    assert adapter.has_permission_approval(
        "proj_alpha", "user_local_ctx", "sess_01", "/ws_beta", "safe_local_pytest", "workspace_read"
    ) is False
    assert adapter.has_permission_approval(
        "proj_alpha", "user_local_ctx", "sess_01", "/ws_alpha", "controlled_network", "workspace_read"
    ) is False
    assert adapter.has_permission_approval(
        "proj_alpha", "user_local_ctx", "sess_01", "/ws_alpha", "safe_local_pytest", "global_write"
    ) is False


def test_antigravity_adapter_confirmation_unfaked():
    adapter = AntigravityAdapter(is_real_host=False)

    # Any options (including user_confirmed) raise AgentNotSupportedError on non-interactive CLI surface
    conf_prompt = ConfirmationRequest(
        request_id="req_conf_01",
        prompt="Delete temporary build files?",
        options=("approve", "deny")
    )
    with pytest.raises(AgentNotSupportedError, match="non-interactive"):
        adapter.request_confirmation(conf_prompt)

    conf_user = ConfirmationRequest(
        request_id="req_conf_02",
        prompt="Confirm merge?",
        options=("user_confirmed",)
    )
    with pytest.raises(AgentNotSupportedError, match="non-interactive"):
        adapter.request_confirmation(conf_user)


def test_antigravity_adapter_thread_only_no_step_fails_closed(monkeypatch):
    adapter = AntigravityAdapter(is_real_host=True)

    class MockThreadOnlyProcess:
        pid = 44444
        returncode = 0
        def communicate(self, timeout=None):
            return '{"type":"conversation.started","id":"conv-thread-only"}\n', ""
        def poll(self):
            return 0

    monkeypatch.setattr(subprocess, "Popen", lambda *args, **kwargs: MockThreadOnlyProcess())

    req = AgentRequest(
        session_id="sess_thread_only",
        prompt="Thread only output",
        role="DEV",
        workspace_dir=os.path.abspath(".")
    )
    handle = adapter.dispatch_agent(req)
    result = adapter.wait_for_result(handle)

    # Missing step identity must Fail-Closed
    assert result.status == AgentStatus.FAILED
    assert "Fail-Closed" in (result.error_message or "")
    assert adapter.get_session_thread_id(handle.session_id) is None
    assert adapter.get_session_invocation_id(handle.session_id) is None


def test_antigravity_evidence_gate_real_judgment(tmp_path, monkeypatch):
    adapter = AntigravityAdapter(is_real_host=True)
    manifest = create_antigravity_manifest()
    caps = adapter.detect_capabilities()

    sample_stdout = (
        '{"type":"conversation.started","id":"conv-6e9f4305-real"}\n'
        '{"type":"PLANNER_RESPONSE","step_index":1,"content":"Antigravity adapter verification complete."}\n'
        '{"type":"usage","usage":{"input_tokens":12500,"output_tokens":42,"cached_tokens":8192}}\n'
    )

    class MockAgyProcess:
        pid = 33333
        returncode = 0
        def communicate(self, timeout=None):
            return sample_stdout, ""
        def poll(self):
            return 0

    monkeypatch.setattr(subprocess, "Popen", lambda *args, **kwargs: MockAgyProcess())

    req = AgentRequest(
        session_id="sess_ag_ev_01",
        prompt="Verify Antigravity adapter",
        role="DEV",
        workspace_dir=os.path.abspath(".")
    )
    handle = adapter.dispatch_agent(req)
    result = adapter.wait_for_result(handle)

    real_conv_id = adapter.get_session_thread_id(handle.session_id)
    real_inv_id = adapter.get_session_invocation_id(handle.session_id)
    assert real_conv_id == "conv-6e9f4305-real"
    assert real_inv_id == "conv-6e9f4305-real:step_1"

    store_dir = str(tmp_path / "evidence_store")
    store = EvidenceStore(root_dir=store_dir)
    gate = EvidenceGate(store=store, project_root=os.path.abspath("."))

    ctx = EvidenceValidationContext(
        project_id="phase2_proj",
        task_id="T0052",
        actor_role="DEV",
        transition_from="进行中",
        transition_to="审查中",
        baseline_commit="e4550d4d0a87226adc7da67785700c1fdc7a2e44",
        result_commit="e4550d4d0a87226adc7da67785700c1fdc7a2e44",
        expected_invocation_id=real_inv_id,
        expected_adapter="antigravity",
        expected_workspace_mode="worktree",
        expected_evidence_type=EvidenceType.TASK_TRANSITION,
        host_handle=handle,
        expected_capabilities=caps,
        agent_result=result
    )

    meta_extra = {f"capability_{k}": v for k, v in caps.__dict__.items() if k != "extra"}
    meta_extra["conversation_id"] = real_conv_id
    meta_extra["host_identity_source"] = "antigravity_host_conversation_id"

    metadata = EvidenceMetadata(
        project_id="phase2_proj",
        task_id="T0052",
        actor_role="DEV",
        host_id="antigravity",
        adapter="antigravity",
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
        evidence_id="ev-antigravity-cli-001",
        evidence_type=EvidenceType.TASK_TRANSITION,
        baseline_commit="e4550d4d0a87226adc7da67785700c1fdc7a2e44",
        result_commit="e4550d4d0a87226adc7da67785700c1fdc7a2e44",
        artifacts=(),
        metadata=metadata
    )

    store.append(record)
    assert gate.validate_evidence(record.evidence_id, ctx) is True


def test_antigravity_adapter_registration_and_resolution():
    registry = AdapterRegistry(context_id="p_ag")
    adapter = AntigravityAdapter(is_real_host=True)
    manifest = create_antigravity_manifest()

    registry.register(adapter, manifest)
    assert registry.get("antigravity") is adapter

    # Resolution on Windows
    req_win = AdapterResolutionRequest(
        project_id="p_ag",
        adapter_id="antigravity",
        target_os="windows",
        allowed_verification_levels=(VerificationLevel.CLI_VERIFIED,),
        auth_context_id="user_local_ctx",
        billing_context_id="user_sub_ctx"
    )
    decision_win = registry.resolve(req_win)
    assert decision_win.decision_status == ResolutionStatus.SELECTED
    assert decision_win.selected_adapter_id == "antigravity"
    assert decision_win.verification_level == VerificationLevel.CLI_VERIFIED

    # Resolution on macOS returns STATIC_ONLY
    req_mac = AdapterResolutionRequest(
        project_id="p_ag",
        adapter_id="antigravity",
        target_os="macos",
        allowed_verification_levels=(VerificationLevel.STATIC_ONLY,),
        auth_context_id="user_local_ctx",
        billing_context_id="user_sub_ctx"
    )
    decision_mac = registry.resolve(req_mac)
    assert decision_mac.decision_status == ResolutionStatus.SELECTED
    assert decision_mac.verification_level == VerificationLevel.STATIC_ONLY


def test_antigravity_real_executable_detection_and_help():
    exe_path = _find_default_antigravity_executable()
    if exe_path and os.path.isfile(exe_path):
        proc_ver = subprocess.run([exe_path, "--version"], capture_output=True, text=True)
        assert proc_ver.returncode == 0
        assert "1." in (proc_ver.stdout + proc_ver.stderr) or "0." in (proc_ver.stdout + proc_ver.stderr)

        proc_help = subprocess.run([exe_path, "--help"], capture_output=True, text=True)
        assert proc_help.returncode == 0
        help_output = proc_help.stdout + proc_help.stderr
        assert "--print" in help_output or "--agent" in help_output


def test_antigravity_adapter_five_tier_command_risk_evaluation():
    from scripts._lib.hosts.antigravity_adapter import evaluate_command_risk

    # 1. safe_local
    assert evaluate_command_risk("git status") == "safe_local"
    assert evaluate_command_risk("git diff --shortstat") == "safe_local"
    assert evaluate_command_risk("git log -n 5") == "safe_local"
    assert evaluate_command_risk("python -m pytest tests/test_antigravity_adapter.py -q") == "safe_local"
    assert evaluate_command_risk("python scripts/heartbeat.py") == "safe_local"
    assert evaluate_command_risk("python scripts/quick_task.py ...") == "safe_local"
    assert evaluate_command_risk("python scripts/transition_task.py --role DEV --task-id T0052 ...") == "safe_local"

    # 2. controlled_external
    assert evaluate_command_risk("pip install some-package") == "controlled_external"
    assert evaluate_command_risk("npm install") == "controlled_external"
    assert evaluate_command_risk("curl https://api.openai.com/v1/models") == "controlled_external"
    assert evaluate_command_risk("git clone https://github.com/repo.git") == "controlled_external"
    # python -c is strictly NOT safe_local
    assert evaluate_command_risk("python -c \"import os; print(os.environ)\"") == "controlled_external"

    # 3. destructive
    assert evaluate_command_risk("git reset --hard HEAD~1") == "destructive"
    assert evaluate_command_risk("git clean -fdx") == "destructive"
    assert evaluate_command_risk("git rebase -i main") == "destructive"
    assert evaluate_command_risk("git branch -D feature/phase2e") == "destructive"

    # 4. billing
    assert evaluate_command_risk("request_billing_expansion(tier=Pro)") == "billing"

    # 5. acceptance
    assert evaluate_command_risk("git push origin main") == "acceptance"
    assert evaluate_command_risk("git merge feature/phase2e-antigravity-adapter") == "acceptance"


def test_antigravity_adapter_safe_local_second_execution_no_prompt():
    adapter = AntigravityAdapter(is_real_host=False)
    project_id = "phase2_proj"
    auth_ctx = "user_local_ctx"
    sess_id = "sess_safe_01"
    ws_dir = os.path.abspath(".")
    cmd_family = "safe_local:pytest"
    perm_boundary = "workspace_read"

    # Initially unapproved
    assert adapter.has_permission_approval(
        project_id, auth_ctx, sess_id, ws_dir, cmd_family, perm_boundary
    ) is False

    # User approves once
    adapter.record_permission_approval(
        project_id, auth_ctx, sess_id, ws_dir, cmd_family, perm_boundary
    )

    # Second execution is approved automatically without prompt
    assert adapter.has_permission_approval(
        project_id, auth_ctx, sess_id, ws_dir, cmd_family, perm_boundary
    ) is True

    # But changing workspace or session requires prompt
    assert adapter.has_permission_approval(
        project_id, auth_ctx, "sess_safe_02", ws_dir, cmd_family, perm_boundary
    ) is False


def test_antigravity_adapter_user_rejection_halts_execution():
    adapter = AntigravityAdapter(is_real_host=False)

    # Calling request_confirmation on non-interactive CLI must fail closed
    conf_req = ConfirmationRequest(
        request_id="req_deny_01",
        prompt="Authorize destructive cleanup?",
        options=("deny", "approve")
    )
    with pytest.raises(AgentNotSupportedError, match="non-interactive"):
        adapter.request_confirmation(conf_req)
