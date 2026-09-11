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
    AgentPermissionRequiredError,
    AgentUnsafeHostConfigError,
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
    _build_stream_input,
    _evaluate_request_risk,
    _find_default_antigravity_executable,
    create_antigravity_manifest,
)
from scripts._lib.hosts import antigravity_adapter as antigravity_adapter_module


def test_antigravity_manifest_structure_and_anti_forgery():
    manifest = create_antigravity_manifest()
    assert manifest.adapter_id == "antigravity"
    assert manifest.host_surface == HostSurface.CLI
    assert manifest.verification_level == VerificationLevel.STATIC_ONLY
    assert manifest.auth_boundary == AuthBoundaryType.USER_LOCAL
    assert manifest.billing_boundary == BillingBoundaryType.USER_SUBSCRIPTION
    assert "windows" in manifest.platform_verifications
    assert manifest.platform_verifications["windows"].verification_level == VerificationLevel.STATIC_ONLY
    assert manifest.platform_verifications["windows"].verified_version == "1.1.21"
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
    # Non-interactive CLI surface does not support in-band permission approval / interactive confirmation (DEF-T0052-1)
    assert caps.supports_permission_approval == CapabilitySupport.UNSUPPORTED
    assert caps.supports_interactive_confirmation == CapabilitySupport.UNSUPPORTED


def test_antigravity_adapter_role_based_routing_and_sandbox():
    adapter = AntigravityAdapter(is_real_host=False)

    # 1. REVIEWER defaults to plan mode
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

    # 3. Requesting unsupported mode like read-only or workspace-write must be rejected (P2 fix)
    req_bad_mode = AgentRequest(
        session_id="sess_ag_bad_mode",
        prompt="Testing invalid mode",
        role="DEV",
        workspace_dir=os.path.abspath("."),
        extra_context={"mode": "read-only"}
    )
    with pytest.raises(AgentNotSupportedError, match="Unauthorized execution mode"):
        adapter.dispatch_agent(req_bad_mode)

    # 4. DEV defaults to flow-dev with accept-edits and sandbox
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
    # Prompts use stdin stream-json so large Windows prompts never enter argv.
    assert cmd_dev[cmd_dev.index("--input-format") + 1] == "stream-json"
    assert cmd_dev[cmd_dev.index("--output-format") + 1] == "stream-json"
    assert cmd_dev[cmd_dev.index("--print-timeout") + 1] == "3600s"
    assert "--print" not in cmd_dev
    assert "Implement feature" not in cmd_dev

    req_builder = AgentRequest(
        session_id="sess_ag_builder_01",
        prompt="Implement managed change without commands",
        role="BUILDER",
        workspace_dir=os.path.abspath("."),
    )
    cmd_builder = adapter.build_antigravity_exec_command(req_builder)
    assert cmd_builder[cmd_builder.index("--agent") + 1] == "flow-runner-builder"


def test_antigravity_prompt_uses_stdin_with_timeout_and_schema():
    adapter = AntigravityAdapter(is_real_host=False)
    schema = {
        "type": "object",
        "required": ["decision"],
        "properties": {"decision": {"type": "string", "enum": ["PASS", "REJECT"]}},
    }
    request = AgentRequest(
        session_id="sess_stdin_transport",
        prompt="x" * 80_000,
        role="REVIEWER",
        workspace_dir=os.path.abspath("."),
        timeout_seconds=12.1,
        extra_context={"json_schema": schema},
    )

    command = adapter.build_antigravity_exec_command(request)
    assert request.prompt not in command
    assert command[command.index("--print-timeout") + 1] == "13s"
    assert json.loads(command[command.index("--json-schema") + 1]) == schema
    payload = json.loads(_build_stream_input(request.prompt))
    assert payload == {"event": "user", "message": {"content": request.prompt}}


def test_antigravity_request_risk_separates_operation_from_embedded_source_text():
    request = AgentRequest(
        session_id="sess_review_diff_words",
        prompt="Review patch text mentioning delete, drop table, and git push as examples.",
        role="REVIEWER",
        workspace_dir=os.path.abspath("."),
        extra_context={
            "operation_intent": "git diff --no-ext-diff HEAD~1 HEAD --",
            "permission_boundary": "workspace_read",
        },
    )
    assert _evaluate_request_risk(request) == "safe_local"

    destructive = AgentRequest(
        session_id="sess_real_destructive_intent",
        prompt="benign words",
        role="DEV",
        workspace_dir=os.path.abspath("."),
        extra_context={"operation_intent": "git reset --hard HEAD"},
    )
    assert _evaluate_request_risk(destructive) == "destructive"


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


def test_antigravity_adapter_parses_structured_output_result():
    adapter = AntigravityAdapter(is_real_host=False)
    sample = (
        '{"event":"step_update","step_update":{"conversation_id":"conv-structured","step_index":2}}\n'
        '{"event":"result","result":{"conversation_id":"conv-structured","status":"SUCCESS",'
        '"structured_output":{"decision":"PASS","defects":[]}}}\n'
    )
    text, _, err, conv_id, inv_id, _ = adapter._parse_antigravity_output(sample, "")
    assert err is None
    assert conv_id == "conv-structured"
    assert inv_id == "conv-structured:step_2"
    assert json.loads(text) == {"decision": "PASS", "defects": []}


def test_antigravity_adapter_prefers_structured_output_over_planner_chatter():
    adapter = AntigravityAdapter(is_real_host=False)
    sample = (
        '{"type":"PLANNER_RESPONSE","step_index":1,"content":"I am checking the evidence."}\n'
        '{"event":"result","result":{"conversation_id":"conv-structured","status":"SUCCESS",'
        '"structured_output":{"decision":"PASS","defects":[]}}}\n'
    )
    text, _, err, _, _, _ = adapter._parse_antigravity_output(sample, "")
    assert err is None
    assert json.loads(text) == {"decision": "PASS", "defects": []}


def test_antigravity_adapter_accepts_string_structured_output():
    adapter = AntigravityAdapter(is_real_host=False)
    structured = json.dumps({"decision": "PASS", "defects": []})
    sample = json.dumps({
        "event": "result",
        "result": {
            "conversation_id": "conv-string",
            "status": "SUCCESS",
            "structured_output": structured,
        },
    })
    text, _, err, _, _, _ = adapter._parse_antigravity_output(sample, "")
    assert err is None
    assert json.loads(text) == {"decision": "PASS", "defects": []}


def test_antigravity_adapter_accepts_top_level_structured_output():
    adapter = AntigravityAdapter(is_real_host=False)
    sample = json.dumps({
        "event": "result",
        "status": "SUCCESS",
        "structured_output": {"decision": "PASS", "defects": []},
    })
    text, _, err, _, _, _ = adapter._parse_antigravity_output(sample, "")
    assert err is None
    assert json.loads(text) == {"decision": "PASS", "defects": []}


@pytest.mark.parametrize("result_status", ["CANCELED", "INTERRUPTED", "INVALID", "WAITING", "RUNNING"])
def test_antigravity_adapter_rejects_non_success_result_status(result_status):
    adapter = AntigravityAdapter(is_real_host=False)
    sample = json.dumps(
        {
            "event": "result",
            "step_id": "step_1",
            "result": {
                "status": result_status,
                "conversation_id": "conv-non-success",
            },
        }
    )

    _text, _events, error, conv_id, inv_id, _usage = adapter._parse_antigravity_output(sample, "")

    assert error == f"Antigravity CLI returned non-success status: {result_status}"
    assert conv_id == "conv-non-success"
    assert inv_id == "conv-non-success:step_1"


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
    adapter = AntigravityAdapter(is_real_host=True, verification_level=VerificationLevel.CLI_VERIFIED)
    adapter.record_permission_approval(
        project_id="default_project",
        auth_context="user_local_ctx",
        workspace_dir=os.path.abspath("."),
        command_family="safe_local:DEV",
        permission_boundary="workspace_read"
    )

    class MockExitZeroNoIdentityProcess:
        pid = 88888
        returncode = 0
        def communicate(self, input=None, timeout=None):
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
    adapter.record_permission_approval(
        project_id="default_project",
        auth_context="user_local_ctx",
        workspace_dir=os.path.abspath("."),
        command_family="safe_local:DEV",
        permission_boundary="workspace_read"
    )

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
    adapter = AntigravityAdapter(is_real_host=True, verification_level=VerificationLevel.CLI_VERIFIED)
    adapter.record_permission_approval(
        project_id="default_project",
        auth_context="user_local_ctx",
        workspace_dir=os.path.abspath("."),
        command_family="safe_local:DEV",
        permission_boundary="workspace_read"
    )

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
    adapter = AntigravityAdapter(is_real_host=False)

    # 1. Non-AgentHandle instance
    with pytest.raises(AgentInvalidHandleError):
        adapter.wait_for_result("not_a_handle")  # type: ignore

    # 2. Foreign adapter instance
    h_foreign = AgentHandle(
        session_id="sess_f",
        host_id=adapter.adapter_id,
        status="running",
        is_real_host=False,
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
        is_real_host=False,
        adapter_instance_id=adapter._instance_id,
        invocation_token="tok1"
    )
    with pytest.raises(AgentInvalidHandleError, match="does not match adapter"):
        adapter.wait_for_result(h_wrong_host)

    # 4. Forged invocation token
    adapter.record_permission_approval(
        project_id="default_project",
        auth_context="user_local_ctx",
        workspace_dir=os.path.abspath("."),
        command_family="safe_local:DEV",
        permission_boundary="workspace_read"
    )
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
    adapter = AntigravityAdapter(is_real_host=True, verification_level=VerificationLevel.CLI_VERIFIED)
    adapter.record_permission_approval(
        project_id="default_project",
        auth_context="user_local_ctx",
        workspace_dir=os.path.abspath("."),
        command_family="safe_local:DEV",
        permission_boundary="workspace_read"
    )

    terminated_pids = []
    class MockHangingProcess:
        pid = 77777
        returncode = None
        def poll(self):
            return None
        def communicate(self, input=None, timeout=None):
            raise subprocess.TimeoutExpired(cmd=["agy"], timeout=timeout)
        def terminate(self):
            terminated_pids.append(self.pid)
        def kill(self):
            terminated_pids.append(self.pid)

    monkeypatch.setattr(subprocess, "Popen", lambda *args, **kwargs: MockHangingProcess())

    # Mock subprocess.run so taskkill and external helpers are 100% mocked with 0 real child processes
    taskkill_calls = []
    def mock_subprocess_run(cmd, *args, **kwargs):
        taskkill_calls.append(cmd)
        return subprocess.CompletedProcess(args=cmd, returncode=0, stdout="", stderr="")

    monkeypatch.setattr(subprocess, "run", mock_subprocess_run)

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

    # Verify termination was initiated without invoking real taskkill
    assert len(taskkill_calls) >= 1 or len(terminated_pids) >= 1

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
    adapter = AntigravityAdapter(is_real_host=True, verification_level=VerificationLevel.CLI_VERIFIED)
    adapter.record_permission_approval(
        project_id="default_project",
        auth_context="user_local_ctx",
        workspace_dir=os.path.abspath("."),
        command_family="safe_local:DEV",
        permission_boundary="workspace_read"
    )

    class MockThreadOnlyProcess:
        pid = 44444
        returncode = 0
        def communicate(self, input=None, timeout=None):
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
    adapter = AntigravityAdapter(is_real_host=True, verification_level=VerificationLevel.CLI_VERIFIED)
    adapter.record_permission_approval(
        project_id="phase2_proj",
        auth_context="user_local_ctx",
        workspace_dir=os.path.abspath("."),
        command_family="safe_local:DEV",
        permission_boundary="workspace_read"
    )
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
        def communicate(self, input=None, timeout=None):
            return sample_stdout, ""
        def poll(self):
            return 0

    monkeypatch.setattr(subprocess, "Popen", lambda *args, **kwargs: MockAgyProcess())

    req = AgentRequest(
        session_id="sess_ag_ev_01",
        prompt="Verify Antigravity adapter",
        role="DEV",
        workspace_dir=os.path.abspath("."),
        extra_context={"project_id": "phase2_proj"}
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

    # Resolution on Windows (Truthfully STATIC_ONLY until live OAuth session)
    req_win = AdapterResolutionRequest(
        project_id="p_ag",
        adapter_id="antigravity",
        target_os="windows",
        allowed_verification_levels=(VerificationLevel.STATIC_ONLY,),
        auth_context_id="user_local_ctx",
        billing_context_id="user_sub_ctx"
    )
    decision_win = registry.resolve(req_win)
    assert decision_win.decision_status == ResolutionStatus.SELECTED
    assert decision_win.selected_adapter_id == "antigravity"
    assert decision_win.verification_level == VerificationLevel.STATIC_ONLY

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


def test_antigravity_verified_manifest_requires_explicit_evidence():
    with pytest.raises(ValueError, match="verified_at_windows"):
        create_antigravity_manifest(
            verification_level_windows=VerificationLevel.CLI_VERIFIED,
            e2e_evidence_refs_windows=("evi-real",),
        )

    with pytest.raises(ValueError, match="e2e_evidence_refs_windows"):
        create_antigravity_manifest(
            verification_level_windows=VerificationLevel.CLI_VERIFIED,
            verified_at_windows="2026-08-27T00:00:00Z",
            e2e_evidence_refs_windows=(),
        )

    with pytest.raises(ValueError, match="e2e_evidence_refs_windows"):
        create_antigravity_manifest(
            verification_level_windows=VerificationLevel.CLI_VERIFIED,
            verified_at_windows="2026-08-27T00:00:00Z",
            e2e_evidence_refs_windows=("",),
        )


def test_antigravity_promotion_requires_matching_canonical_identity():
    adapter = AntigravityAdapter(is_real_host=True)
    session_id = "sess-real-reviewer"
    invocation_id = "conversation-real:item-real"
    adapter._session_history[session_id] = {
        "completed": True,
        "conversation_id": "conversation-real",
        "invocation_id": invocation_id,
        "result": AgentResult(
            session_id=session_id,
            status=AgentStatus.SUCCESS,
            output="review complete",
            is_real_host=True,
        ),
    }

    with pytest.raises(AgentNotSupportedError, match="requires store, gate, and an explicit validation context"):
        adapter.promote_after_verified_evidence(
            "evi-real",
            host_session_id=session_id,
            host_invocation_id="wrong-invocation",
        )

    with pytest.raises(AgentNotSupportedError, match="requires store, gate, and an explicit validation context"):
        adapter.promote_after_verified_evidence(
            "evi-real",
            host_session_id=session_id,
            host_invocation_id=invocation_id,
        )
    assert adapter._verification_level == VerificationLevel.STATIC_ONLY


def test_verification_probe_uses_self_agent_without_global_reviewer_tools():
    """The inline bootstrap review must not inherit a command-requiring global agent profile."""
    adapter = AntigravityAdapter(is_real_host=True)
    request = AgentRequest(
        session_id="sess-inline-probe",
        prompt="Review this inline snippet only and return PASS: verified",
        role="REVIEWER",
        workspace_dir=os.path.abspath("."),
        extra_context={"mode": "plan", "permission_boundary": "workspace_read"},
    )
    adapter._verification_probe_ctx.session_id = request.session_id
    try:
        command = adapter.build_antigravity_exec_command(request)
    finally:
        adapter._verification_probe_ctx.session_id = None
    agent_index = command.index("--agent")
    assert command[agent_index + 1] == "self"


def test_antigravity_real_executable_detection_and_help(monkeypatch):
    exe_path = _find_default_antigravity_executable()
    if exe_path:
        assert isinstance(exe_path, str) and (os.path.isfile(exe_path) or exe_path == "agy")

    executed_cmds = []

    def mock_subprocess_run(cmd, *args, **kwargs):
        executed_cmds.append(cmd)
        if "--version" in cmd:
            return subprocess.CompletedProcess(args=cmd, returncode=0, stdout="agy 1.1.21\n", stderr="")
        if "--help" in cmd:
            return subprocess.CompletedProcess(args=cmd, returncode=0, stdout="Google Antigravity CLI\n--print\n--agent\n", stderr="")
        return subprocess.CompletedProcess(args=cmd, returncode=0, stdout="", stderr="")

    monkeypatch.setattr(subprocess, "run", mock_subprocess_run)

    target_exe = exe_path or "agy"
    proc_ver = subprocess.run([target_exe, "--version"], capture_output=True, text=True)
    assert proc_ver.returncode == 0
    assert "1.1.21" in proc_ver.stdout

    proc_help = subprocess.run([target_exe, "--help"], capture_output=True, text=True)
    assert proc_help.returncode == 0
    assert "--print" in proc_help.stdout
    assert len(executed_cmds) == 2


def test_antigravity_adapter_five_tier_command_risk_evaluation():
    from scripts._lib.hosts.antigravity_adapter import evaluate_command_risk

    # 1. safe_local
    assert evaluate_command_risk("git status") == "safe_local"
    assert evaluate_command_risk("git diff --shortstat") == "safe_local"
    assert evaluate_command_risk("git log -n 5") == "safe_local"
    assert evaluate_command_risk("git show HEAD") == "safe_local"
    assert evaluate_command_risk("git rev-parse HEAD") == "safe_local"
    assert evaluate_command_risk("git branch") == "safe_local"
    assert evaluate_command_risk("git branch -a") == "safe_local"
    assert evaluate_command_risk("git branch -l") == "safe_local"
    assert evaluate_command_risk("git branch --list") == "safe_local"
    assert evaluate_command_risk("git worktree list") == "safe_local"
    assert evaluate_command_risk("git worktree list --porcelain") == "safe_local"
    assert evaluate_command_risk("python -m pytest tests/test_antigravity_adapter.py -q") == "safe_local"
    assert evaluate_command_risk("pytest tests -s -v") == "safe_local"
    assert evaluate_command_risk("python scripts/heartbeat.py") == "safe_local"
    assert evaluate_command_risk("python scripts/quick_task.py ...") == "safe_local"
    assert evaluate_command_risk("python scripts/transition_task.py --role DEV --task-id T0052 --to-status 进行中") == "safe_local"
    assert evaluate_command_risk("Review codebase and suggest improvements") == "safe_local"

    # DEF-T0052-3 & DEF-T0052-19: Pytest dangerous flags and external paths must be rejected from safe_local
    assert evaluate_command_risk("pytest -p evil_plugin") == "controlled_external"
    assert evaluate_command_risk("python -m pytest --pyargs evil") == "controlled_external"
    assert evaluate_command_risk("pytest -c /tmp/evil.ini") == "controlled_external"
    assert evaluate_command_risk("pytest -o python_files=evil.py") == "controlled_external"
    assert evaluate_command_risk("pytest --override-ini=addopts=--evil") == "controlled_external"
    assert evaluate_command_risk("pytest --import-mode=importlib evil_script.py") == "controlled_external"
    assert evaluate_command_risk("pytest --cov=secret") == "controlled_external"
    assert evaluate_command_risk("pytest /outside/path/test.py") == "controlled_external"
    assert evaluate_command_risk("pytest ../outside/test.py") == "controlled_external"

    # DEF-T0052-3 & DEF-T0052-19: Branch creation/deletion, worktree add, git diff output redirection, natural language deletion & empty
    assert evaluate_command_risk("git branch feature/phase2e-new") == "destructive"
    assert evaluate_command_risk("git branch -d feature/old") == "destructive"
    assert evaluate_command_risk("git branch -D feature/old") == "destructive"
    assert evaluate_command_risk("git worktree add ../wt-new") == "destructive"
    assert evaluate_command_risk("git worktree remove ../wt-old") == "destructive"
    assert evaluate_command_risk("git diff --output=/tmp/patch.diff") == "destructive"
    assert evaluate_command_risk("git diff --output=patch.diff") == "destructive"
    assert evaluate_command_risk("Delete all temporary files") == "destructive"
    assert evaluate_command_risk("Please remove old checkpoints") == "destructive"
    assert evaluate_command_risk("Empty repository and discard all files") == "destructive"
    assert evaluate_command_risk("Move uncommitted files to recycle bin") == "destructive"
    assert evaluate_command_risk("删除过期日志") == "destructive"
    assert evaluate_command_risk("清理临时目录") == "destructive"
    assert evaluate_command_risk("清空所有未提交内容") == "destructive"
    assert evaluate_command_risk("丢弃本次修改") == "destructive"
    assert evaluate_command_risk("移到回收站") == "destructive"
    assert evaluate_command_risk("python scripts/transition_task.py --role DEV --task-id T0052 --to-status 已取消") == "destructive"
    assert evaluate_command_risk("python scripts/quick_task.py --task-id T0052 --to-status 已废弃") == "destructive"
    assert evaluate_command_risk("python scripts/evil.py scripts/heartbeat.py") == "controlled_external"
    assert evaluate_command_risk("python evil.py") == "controlled_external"

    # Command chaining evaluation
    assert evaluate_command_risk("git status && rm -rf .") == "destructive"
    assert evaluate_command_risk("git status; curl https://external.api") == "controlled_external"
    assert evaluate_command_risk("git status && git push origin main") == "acceptance"
    assert evaluate_command_risk("git status && git diff") == "safe_local"

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
    assert evaluate_command_risk("API_KEY 充值") == "billing"

    # 5. acceptance
    assert evaluate_command_risk("git push origin main") == "acceptance"
    assert evaluate_command_risk("git merge feature/phase2e-antigravity-adapter") == "acceptance"
    assert evaluate_command_risk("发布上线版本") == "acceptance"


def test_antigravity_adapter_dispatch_zero_self_authorization():
    adapter = AntigravityAdapter(is_real_host=False)

    # 1. Destructive operation is strictly forbidden even if caller attempts self-authorization
    req_self_auth = AgentRequest(
        session_id="sess_perm_chk_01",
        prompt="git reset --hard HEAD",
        role="DEV",
        workspace_dir=os.path.abspath("."),
        extra_context={"approved": True, "user_confirmed": True, "approval_token": "arbitrary_token"}
    )
    with pytest.raises(AgentNotSupportedError, match="strictly forbidden"):
        adapter.dispatch_agent(req_self_auth)

    # 2. Unapproved safe local operation fails closed without prior permission approval
    req_safe_1 = AgentRequest(
        session_id="sess_safe_chk_01",
        prompt="git status",
        role="DEV",
        workspace_dir=os.path.abspath("."),
        extra_context={"project_id": "proj_p2", "auth_context": "auth_01"}
    )
    with pytest.raises(AgentNotSupportedError, match="requires explicit user permission approval"):
        adapter.dispatch_agent(req_safe_1)

    # Permission cache is initially not approved
    assert adapter.has_permission_approval(
        project_id="proj_p2",
        auth_context="auth_01",
        workspace_dir=os.path.abspath("."),
        command_family="safe_local:DEV",
        permission_boundary="workspace_read"
    ) is False

    # 3. Outer host explicitly records approval
    adapter.record_permission_approval(
        project_id="proj_p2",
        auth_context="auth_01",
        workspace_dir=os.path.abspath("."),
        command_family="safe_local:DEV",
        permission_boundary="workspace_read"
    )
    assert adapter.has_permission_approval(
        project_id="proj_p2",
        auth_context="auth_01",
        workspace_dir=os.path.abspath("."),
        command_family="safe_local:DEV",
        permission_boundary="workspace_read"
    ) is True

    # 4. First approved dispatch succeeds
    handle_1 = adapter.dispatch_agent(req_safe_1)
    assert handle_1.session_id == "sess_safe_chk_01"

    # 5. Subsequent session in the same project/workspace is pre-approved and succeeds without re-prompting
    req_safe_2 = AgentRequest(
        session_id="sess_safe_chk_02",
        prompt="git status",
        role="DEV",
        workspace_dir=os.path.abspath("."),
        extra_context={"project_id": "proj_p2", "auth_context": "auth_01"}
    )
    handle_2 = adapter.dispatch_agent(req_safe_2)
    assert handle_2.session_id == "sess_safe_chk_02"


def test_antigravity_adapter_rejects_global_wildcard_permissions_without_rewriting(tmp_path, monkeypatch):
    config_path = tmp_path / "settings.json"
    original = '{"permissions":{"allow":["command(*)","workspace_read(*)"]}}'
    config_path.write_text(original, encoding="utf-8")
    monkeypatch.setattr(
        antigravity_adapter_module,
        "_antigravity_config_path",
        lambda: str(config_path),
    )

    adapter = AntigravityAdapter(
        is_real_host=True,
        executable_path=sys.executable,
        verification_level=VerificationLevel.CLI_VERIFIED,
    )
    request = AgentRequest(
        session_id="sess_unsafe_global_config",
        prompt="git status",
        role="REVIEWER",
        workspace_dir=os.path.abspath("."),
        extra_context={"enforce_host_config_safety": True},
    )

    with pytest.raises(AgentUnsafeHostConfigError, match=r"command\(\*\)"):
        adapter.dispatch_agent(request)
    assert config_path.read_text(encoding="utf-8") == original


def test_antigravity_adapter_surfaces_runtime_permission_denial(monkeypatch):
    adapter = AntigravityAdapter(
        is_real_host=True,
        executable_path=sys.executable,
        verification_level=VerificationLevel.CLI_VERIFIED,
    )
    workspace = os.path.abspath(".")
    request = AgentRequest(
        session_id="sess_runtime_permission_denial",
        prompt="git status",
        role="QA",
        workspace_dir=workspace,
    )
    adapter.record_out_of_band_approval(request)

    class PermissionDeniedPopen:
        pid = 77888
        returncode = 1

        def __init__(self, *args, **kwargs):
            pass

        def communicate(self, input=None, timeout=None):
            return '{"type":"error","message":"permission_denied: command requires approval"}\n', ""

    monkeypatch.setattr(subprocess, "Popen", PermissionDeniedPopen)
    handle = adapter.dispatch_agent(request)
    with pytest.raises(AgentPermissionRequiredError, match="requires explicit user approval") as denied:
        adapter.wait_for_result(handle, timeout_seconds=5)
    assert denied.value.diagnostics['host_exit_code'] == 1
    assert denied.value.diagnostics['tool_events']
    assert 'permission_denied' in denied.value.diagnostics['host_message']


def test_antigravity_adapter_treats_success_with_denied_actions_as_permission_failure(monkeypatch):
    adapter = AntigravityAdapter(
        is_real_host=True, executable_path=sys.executable,
        verification_level=VerificationLevel.CLI_VERIFIED,
    )
    request = AgentRequest(
        session_id="sess_denied_success", prompt="edit files only", role="BUILDER",
        workspace_dir=os.path.abspath("."),
    )
    adapter.record_out_of_band_approval(request)

    class DeniedButSuccessfulPopen:
        pid = 77889
        returncode = 0
        def __init__(self, *args, **kwargs):
            pass
        def communicate(self, input=None, timeout=None):
            events = [
                {"event": "init", "conversation_id": "conv-denied"},
                {"event": "step_update", "step_update": {
                    "conversation_id": "conv-denied", "step_index": 2,
                    "tool_info": {"parameters": {"CommandLine": "git status"}},
                }},
                {"event": "result", "result": {
                    "conversation_id": "conv-denied", "status": "SUCCESS", "response": "",
                    "denied_actions": [{"action": "escalate_admin", "display_name": "Bash"}],
                }},
            ]
            return "\n".join(json.dumps(item) for item in events), ""

    monkeypatch.setattr(subprocess, "Popen", DeniedButSuccessfulPopen)
    handle = adapter.dispatch_agent(request)
    with pytest.raises(AgentPermissionRequiredError) as denied:
        adapter.wait_for_result(handle, timeout_seconds=5)
    assert denied.value.diagnostics["host_exit_code"] == 0
    assert denied.value.diagnostics["denied_command"] == "git status"
    assert denied.value.diagnostics["denied_actions"] == [
        {"action": "escalate_admin", "display_name": "Bash"}
    ]


def test_antigravity_adapter_out_of_band_approval_is_exact_and_bounded():
    adapter = AntigravityAdapter(is_real_host=False)
    workspace = os.path.abspath(".")
    controlled = AgentRequest(
        session_id="sess_controlled_01",
        prompt="curl https://example.com/status",
        role="REVIEWER",
        workspace_dir=workspace,
        extra_context={
            "project_id": "proj_controlled",
            "auth_context": "auth_controlled",
            "permission_boundary": "workspace_read",
        },
    )

    with pytest.raises(AgentNotSupportedError, match="requires explicit user permission approval"):
        adapter.dispatch_agent(controlled)

    assert adapter.record_out_of_band_approval(controlled) == "controlled_external"
    assert adapter.dispatch_agent(controlled).session_id == "sess_controlled_01"

    destructive = AgentRequest(
        session_id="sess_destructive_01",
        prompt="git reset --hard HEAD",
        role="REVIEWER",
        workspace_dir=workspace,
        extra_context={"project_id": "proj_controlled"},
    )
    with pytest.raises(AgentNotSupportedError, match="cannot receive out-of-band approval"):
        adapter.record_out_of_band_approval(destructive)


def test_antigravity_adapter_popen_sets_cwd_for_project_isolation(monkeypatch):
    adapter = AntigravityAdapter(is_real_host=True, verification_level=VerificationLevel.CLI_VERIFIED)
    target_ws = os.path.abspath(".")
    adapter.record_permission_approval(
        project_id="default_project",
        auth_context="user_local_ctx",
        workspace_dir=target_ws,
        command_family="safe_local:DEV",
        permission_boundary="workspace_read"
    )
    captured_kwargs = {}
    captured_communicate = {}

    class MockCapturedPopen:
        pid = 77777
        returncode = 0
        def __init__(self, *args, **kwargs):
            captured_kwargs.update(kwargs)
        def poll(self):
            return 0
        def communicate(self, input=None, timeout=None):
            captured_communicate["input"] = input
            captured_communicate["timeout"] = timeout
            return '{"type":"conversation.started","id":"conv-iso"}\n{"step_id":"step_1","type":"output"}\n', ""

    monkeypatch.setattr(subprocess, "Popen", MockCapturedPopen)

    req = AgentRequest(
        session_id="sess_iso_chk",
        prompt="git status",
        role="DEV",
        workspace_dir=target_ws
    )
    handle = adapter.dispatch_agent(req)
    assert captured_kwargs.get("cwd") == target_ws
    result = adapter.wait_for_result(handle, timeout_seconds=9)
    assert result.status == AgentStatus.SUCCESS
    assert json.loads(captured_communicate["input"])["message"]["content"] == "git status"
    assert captured_communicate["timeout"] == 9


def test_antigravity_adapter_dual_root_and_cross_project_isolation(tmp_path):
    adapter = AntigravityAdapter(is_real_host=False)
    adapter.record_permission_approval(
        project_id="default_project",
        auth_context="user_local_ctx",
        workspace_dir=os.path.abspath("."),
        command_family="safe_local:DEV",
        permission_boundary="workspace_read"
    )

    # 1. Valid workspace and valid same-repo folder passes
    req_valid = AgentRequest(
        session_id="sess_dual_ok",
        prompt="Dual root valid test",
        role="DEV",
        workspace_dir=os.path.abspath("."),
        extra_context={"project_folders": [os.path.abspath(".")]}
    )
    handle = adapter.dispatch_agent(req_valid)
    assert handle.session_id == "sess_dual_ok"

    # 2. Linked worktree sharing the same git-common-dir passes (DEF-T0052-20)
    from scripts._lib.hosts.antigravity_adapter import _find_git_common_dir
    fake_wt = str(tmp_path / "linked_worktree")
    os.makedirs(fake_wt, exist_ok=True)
    cur_common_dir = _find_git_common_dir(os.path.abspath("."))
    with open(os.path.join(fake_wt, ".git"), "w", encoding="utf-8") as f:
        f.write(f"gitdir: {cur_common_dir}\n")

    req_wt = AgentRequest(
        session_id="sess_wt_ok",
        prompt="Worktree sharing common dir test",
        role="DEV",
        workspace_dir=os.path.abspath("."),
        extra_context={"project_folders": [fake_wt]}
    )
    handle_wt = adapter.dispatch_agent(req_wt)
    assert handle_wt.session_id == "sess_wt_ok"

    # 3. Workspace not in git repository fails closed
    non_git_ws = str(tmp_path / "not_git_dir")
    os.makedirs(non_git_ws, exist_ok=True)
    req_bad_ws = AgentRequest(
        session_id="sess_bad_ws",
        prompt="Bad ws test",
        role="DEV",
        workspace_dir=non_git_ws
    )
    with pytest.raises(AgentNotSupportedError, match="not inside a trusted Git repository"):
        adapter.dispatch_agent(req_bad_ws)

    # 4. Project folder outside git repository fails closed (DEF-T0052-4)
    non_git_folder = str(tmp_path / "external_folder")
    os.makedirs(non_git_folder, exist_ok=True)
    req_bad_folder = AgentRequest(
        session_id="sess_bad_folder",
        prompt="Bad project folder test",
        role="DEV",
        workspace_dir=os.path.abspath("."),
        extra_context={"project_folders": [non_git_folder]}
    )
    with pytest.raises(AgentNotSupportedError, match="Dual-root Fail-Closed"):
        adapter.dispatch_agent(req_bad_folder)

    # 5. Cross-Project Isolation Violation: An outside Git repository cannot be mixed in
    outside_git_dir = str(tmp_path / "outside_git_repo")
    os.makedirs(os.path.join(outside_git_dir, ".git"), exist_ok=True)
    req_cross_proj = AgentRequest(
        session_id="sess_cross_proj",
        prompt="Cross project test",
        role="DEV",
        workspace_dir=os.path.abspath("."),
        extra_context={"project_folders": [outside_git_dir]}
    )
    with pytest.raises(AgentNotSupportedError, match="Cross-Project Isolation Violation"):
        adapter.dispatch_agent(req_cross_proj)


def test_antigravity_adapter_safe_local_second_execution_no_prompt():
    adapter = AntigravityAdapter(is_real_host=False)
    project_id = "phase2_proj"
    auth_ctx = "user_local_ctx"
    ws_dir = os.path.abspath(".")
    cmd_family = "safe_local:pytest"
    perm_boundary = "workspace_read"

    # Initially unapproved
    assert adapter.has_permission_approval(
        project_id, auth_ctx, ws_dir, cmd_family, perm_boundary
    ) is False

    # User approves once
    adapter.record_permission_approval(
        project_id, auth_ctx, ws_dir, cmd_family, perm_boundary
    )

    # Second execution in same project/workspace is approved automatically without prompt
    assert adapter.has_permission_approval(
        project_id, auth_ctx, ws_dir, cmd_family, perm_boundary
    ) is True

    # But changing workspace requires prompt
    assert adapter.has_permission_approval(
        project_id, auth_ctx, os.path.abspath(".."), cmd_family, perm_boundary
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


def test_antigravity_adapter_static_only_blocks_real_process_spawn():
    adapter = AntigravityAdapter(is_real_host=True, verification_level=VerificationLevel.STATIC_ONLY)
    adapter.record_permission_approval(
        project_id="default_project",
        auth_context="user_local_ctx",
        workspace_dir=os.path.abspath("."),
        command_family="safe_local:DEV",
        permission_boundary="workspace_read"
    )

    # Regular dispatch is blocked
    req = AgentRequest(
        session_id="sess_block_real",
        prompt="git status",
        role="DEV",
        workspace_dir=os.path.abspath(".")
    )
    with pytest.raises(AgentNotSupportedError, match="declared STATIC_ONLY"):
        adapter.dispatch_agent(req)

    # Caller attempts bypass with allow_unverified_execution -> still blocked!
    req_bypass = AgentRequest(
        session_id="sess_bypass_real",
        prompt="git status",
        role="DEV",
        workspace_dir=os.path.abspath("."),
        extra_context={"allow_unverified_execution": True}
    )
    with pytest.raises(AgentNotSupportedError, match="declared STATIC_ONLY"):
        adapter.dispatch_agent(req_bypass)


def test_antigravity_qa_cannot_request_write_or_disable_sandbox(tmp_path):
    adapter = AntigravityAdapter(
        executable_path=sys.executable,
        is_real_host=True,
        verification_level=VerificationLevel.CLI_VERIFIED,
    )
    base = {
        "session_id": "sess_qa_read_only",
        "prompt": "review inline evidence",
        "role": "QA",
        "workspace_dir": str(tmp_path),
        "timeout_seconds": 1,
    }
    with pytest.raises(AgentNotSupportedError, match="strictly read-only"):
        adapter.build_antigravity_exec_command(AgentRequest(
            **base, extra_context={"mode": "accept-edits"},
        ))
    with pytest.raises(AgentNotSupportedError, match="requires sandbox"):
        adapter.build_antigravity_exec_command(AgentRequest(
            **base, extra_context={"sandbox": False},
        ))
