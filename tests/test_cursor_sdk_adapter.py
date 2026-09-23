# -*- coding: utf-8 -*-
"""
tests/test_cursor_sdk_adapter.py
Unit and conformance tests for CursorSdkAdapter.
Uses FakeCursorSdk for complete isolation and zero network access.
"""
import json
import hashlib
import os
import re
import subprocess
import sys
import pytest
from unittest.mock import patch
import yaml

from scripts._lib.core.adapter_registry import AdapterRegistry
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
    HostCapabilities,
)
from scripts._lib.core.adapter_manifest import (
    AdapterManifest,
    AuthBoundaryType,
    BillingBoundaryType,
    HostSurface,
    VerificationLevel,
)
from scripts._lib.core.evidence_gate import EvidenceGate
from scripts._lib.core.evidence_store import EvidenceStore
from scripts._lib.core.production_runner import (
    ProductionRunner,
    create_default_registry,
    _execution_options_from_spec,
)
from scripts._lib.core.runner_checkpoint_store import RunnerCheckpointStore
from scripts._lib.core.runner_schema import (
    RunnerCheckpoint,
    RunnerState,
    TaskExecutionSpec,
)
from scripts._lib.core.adapter_conformance import (
    assert_capabilities_conformance,
    assert_handle_conformance,
    assert_manifest_conformance,
    assert_zero_side_effects,
    run_adapter_conformance_suite,
)
from scripts._lib.hosts.cursor_sdk_adapter import (
    CursorSdkAdapter,
    create_cursor_sdk_manifest,
)
from tests.fixtures.cursor_sdk import fake_cursor_sdk
from tests.fixtures.cursor_sdk.fake_cursor_sdk import (
    Agent,
    AuthenticationError,
    ConfigurationError,
    CursorAgentError,
    FakeCursorSdkState,
    RateLimitError,
)


@pytest.fixture(autouse=True)
def reset_fake_sdk():
    FakeCursorSdkState.reset()
    yield
    FakeCursorSdkState.reset()


def test_cursor_sdk_manifest_structure_and_conformance():
    manifest = create_cursor_sdk_manifest()
    assert manifest.adapter_id == "cursor_sdk"
    assert manifest.host_surface == HostSurface.NATIVE
    assert manifest.verification_level == VerificationLevel.STATIC_ONLY
    assert manifest.auth_boundary == AuthBoundaryType.ENVIRONMENT
    assert manifest.billing_boundary == BillingBoundaryType.API_KEY
    assert "windows" in manifest.platform_verifications
    assert manifest.platform_verifications["windows"].verification_level == VerificationLevel.STATIC_ONLY

    assert_manifest_conformance(manifest)


def test_cursor_sdk_adapter_capabilities_zero_side_effects():
    adapter = CursorSdkAdapter(is_real_host=False, sdk_module=fake_cursor_sdk)
    manifest = create_cursor_sdk_manifest()

    assert_zero_side_effects(adapter)

    caps = adapter.detect_capabilities()
    assert caps.supports_real_subagents == CapabilitySupport.SUPPORTED
    assert caps.supports_worktree == CapabilitySupport.SUPPORTED
    assert caps.supports_isolated_context == CapabilitySupport.SUPPORTED
    assert caps.supports_parallelism == CapabilitySupport.SUPPORTED
    assert caps.supports_interactive_confirmation == CapabilitySupport.UNSUPPORTED


def test_cursor_sdk_adapter_lazy_import_and_missing_sdk_error(monkeypatch):
    adapter = CursorSdkAdapter(is_real_host=True)
    # Ensure cursor_sdk cannot be imported
    monkeypatch.setitem(sys.modules, "cursor_sdk", None)

    req = AgentRequest(
        session_id="sess_missing_sdk",
        prompt="hello",
        role="BUILDER",
        workspace_dir=os.path.abspath("."),
        extra_context={"cursor_model": "composer-2.5"},
    )
    with pytest.raises(AgentNotSupportedError, match="pip install cursor-sdk"):
        adapter.dispatch_agent(req)


def test_cursor_sdk_adapter_model_and_runtime_validation():
    adapter = CursorSdkAdapter(is_real_host=False, sdk_module=fake_cursor_sdk)

    # 1. Missing model must raise AgentNotSupportedError
    req_no_model = AgentRequest(
        session_id="sess_no_model",
        prompt="implement feature",
        role="BUILDER",
        workspace_dir=os.path.abspath("."),
    )
    with pytest.raises(AgentNotSupportedError, match="Cursor model must be explicitly specified"):
        adapter.dispatch_agent(req_no_model)

    # 2. Cloud runtime must be rejected
    req_cloud = AgentRequest(
        session_id="sess_cloud",
        prompt="implement feature",
        role="BUILDER",
        workspace_dir=os.path.abspath("."),
        extra_context={"cursor_model": "composer-2.5", "cursor_runtime": "cloud"},
    )
    with pytest.raises(AgentNotSupportedError, match="Only 'local' runtime is supported"):
        adapter.dispatch_agent(req_cloud)

    # 3. Invalid workspace dir rejected
    req_bad_dir = AgentRequest(
        session_id="sess_bad_dir",
        prompt="implement feature",
        role="BUILDER",
        workspace_dir="C:\\nonexistent_workspace_dir_12345",
        extra_context={"cursor_model": "composer-2.5"},
    )
    with pytest.raises(AgentNotSupportedError, match="Workspace directory"):
        adapter.dispatch_agent(req_bad_dir)

    # 4. Reviewer requesting writable workspace sandbox rejected
    req_rev_write = AgentRequest(
        session_id="sess_rev_write",
        prompt="review",
        role="REVIEWER",
        workspace_dir=os.path.abspath("."),
        extra_context={"cursor_model": "composer-2.5", "sandbox_mode": "workspace-write"},
    )
    with pytest.raises(AgentNotSupportedError, match="strictly read-only"):
        adapter.dispatch_agent(req_rev_write)


def test_cursor_sdk_adapter_dispatch_and_identity_mapping(tmp_path):
    adapter = CursorSdkAdapter(
        is_real_host=False,
        default_model="composer-2.5",
        sdk_module=fake_cursor_sdk,
    )

    req = AgentRequest(
        session_id="sess_dispatch_01",
        prompt="hello builder",
        role="BUILDER",
        workspace_dir=str(tmp_path),
    )
    FakeCursorSdkState.set_next_response("Builder output text")
    handle = adapter.dispatch_agent(req)

    assert handle.host_id == "cursor_sdk"
    assert handle.status == "running"
    assert handle.session_id == "sess_dispatch_01"
    assert handle.invocation_token
    assert handle.adapter_instance_id == adapter._instance_id

    res = adapter.wait_for_result(handle)

    assert res.status == AgentStatus.SUCCESS
    assert res.output == "Builder output text"
    assert res.session_id == handle.session_id
    assert len(res.partial_results) == 1
    assert res.partial_results[0]["invocation_id"].startswith("run_")
    assert res.partial_results[0]["agent_id"].startswith("ag_")
    assert adapter.get_agent_id(handle) == res.partial_results[0]["agent_id"]


def test_cursor_sdk_adapter_cancel_and_idempotence(tmp_path):
    adapter = CursorSdkAdapter(
        is_real_host=False,
        default_model="composer-2.5",
        sdk_module=fake_cursor_sdk,
    )

    req = AgentRequest(
        session_id="sess_cancel",
        prompt="do work",
        role="BUILDER",
        workspace_dir=str(tmp_path),
    )
    handle = adapter.dispatch_agent(req)

    # Cancel once
    ok1 = adapter.cancel_agent(handle)
    assert ok1 is True

    # Cancel again idempotently
    ok2 = adapter.cancel_agent(handle)
    assert ok2 is True

    # Wait after cancel returns CANCELLED
    res = adapter.wait_for_result(handle)
    assert res.status == AgentStatus.CANCELLED


def test_cursor_sdk_adapter_anti_forgery_and_cross_instance_rejection(tmp_path):
    adapter = CursorSdkAdapter(is_real_host=False, default_model="composer-2.5", sdk_module=fake_cursor_sdk)
    req = AgentRequest(
        session_id="sess_sec",
        prompt="work",
        role="BUILDER",
        workspace_dir=str(tmp_path),
    )
    handle = adapter.dispatch_agent(req)

    # 1. Tampered adapter instance id
    forged_inst_handle = AgentHandle(
        session_id=handle.session_id,
        host_id=handle.host_id,
        status="running",
        is_real_host=handle.is_real_host,
        adapter_instance_id="foreign_inst_123",
        invocation_token=handle.invocation_token,
    )
    with pytest.raises(AgentInvalidHandleError, match="adapter_instance_id"):
        adapter.wait_for_result(forged_inst_handle)

    # 2. Tampered token
    forged_token_handle = AgentHandle(
        session_id=handle.session_id,
        host_id=handle.host_id,
        status="running",
        is_real_host=handle.is_real_host,
        adapter_instance_id=handle.adapter_instance_id,
        invocation_token="wrong_token_abc",
    )
    with pytest.raises(AgentInvalidHandleError, match="token"):
        adapter.wait_for_result(forged_token_handle)


def test_cursor_sdk_adapter_error_classification(tmp_path):
    adapter = CursorSdkAdapter(is_real_host=False, default_model="composer-2.5", sdk_module=fake_cursor_sdk)

    # 1. RateLimitError -> transient retryable
    req1 = AgentRequest(session_id="s1", prompt="p", role="BUILDER", workspace_dir=str(tmp_path))
    h1 = adapter.dispatch_agent(req1)
    FakeCursorSdkState.set_next_error(RateLimitError("Quota limit hit, please retry"))
    res1 = adapter.wait_for_result(h1)
    assert res1.status == AgentStatus.FAILED
    assert "transient_cursor_host_failure" in res1.error_message
    assert "429 rate limit" in res1.error_message

    # 2. AuthenticationError -> non-retryable
    req2 = AgentRequest(session_id="s2", prompt="p", role="BUILDER", workspace_dir=str(tmp_path))
    h2 = adapter.dispatch_agent(req2)
    FakeCursorSdkState.set_next_error(AuthenticationError("API key invalid"))
    res2 = adapter.wait_for_result(h2)
    assert res2.status == AgentStatus.FAILED
    assert "Cursor authentication failed" in res2.error_message
    assert "transient" not in res2.error_message

    # 3. ConfigurationError -> non-retryable
    req3 = AgentRequest(session_id="s3", prompt="p", role="BUILDER", workspace_dir=str(tmp_path))
    h3 = adapter.dispatch_agent(req3)
    FakeCursorSdkState.set_next_error(ConfigurationError("Invalid model configuration"))
    res3 = adapter.wait_for_result(h3)
    assert res3.status == AgentStatus.FAILED
    assert "Cursor configuration or request error" in res3.error_message
    assert "transient" not in res3.error_message


def test_cursor_sdk_adapter_interactive_confirmation_unsupported():
    adapter = CursorSdkAdapter(is_real_host=False, sdk_module=fake_cursor_sdk)
    req = ConfirmationRequest(request_id="c1", prompt="Confirm this?", options=("yes", "no"))
    with pytest.raises(AgentNotSupportedError, match="does not support interactive confirmation"):
        adapter.request_confirmation(req)


def test_cursor_sdk_adapter_conformance_suite(tmp_path):
    adapter = CursorSdkAdapter(
        is_real_host=False,
        default_model="composer-2.5",
        sdk_module=fake_cursor_sdk,
    )
    manifest = create_cursor_sdk_manifest()
    result = run_adapter_conformance_suite(adapter, manifest)
    assert result["status"] == "PASSED"
    assert result["adapter_id"] == "cursor_sdk"
    assert result["is_real_host"] is False


@pytest.fixture
def mock_repo(tmp_path):
    repo_dir = tmp_path / "cursor_test_repo"
    repo_dir.mkdir()
    subprocess.run(["git", "init"], cwd=repo_dir, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.name", "TestDev"], cwd=repo_dir, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "test@test.com"], cwd=repo_dir, check=True, capture_output=True)
    (repo_dir / "README.md").write_text("# Test\n", encoding="utf-8")
    (repo_dir / "test_app.py").write_text("def test_dummy(): assert True\n", encoding="utf-8")
    agents_src = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".cursor", "agents")
    if os.path.isdir(agents_src):
        import shutil
        shutil.copytree(agents_src, repo_dir / ".cursor" / "agents")
    config_dir = repo_dir / "config"
    config_dir.mkdir()
    user_data_dir = repo_dir / "user_data"
    user_data_dir.mkdir()
    board_file = user_data_dir / "board.json"
    with open(config_dir / "workflow.config.yaml", "w", encoding="utf-8") as f:
        yaml.safe_dump({"board": {"provider": "local", "board_file": str(board_file)}}, f)
    board_file.write_text(json.dumps([{
        "id": "T0099", "name": "Cursor E2E Test", "status": "\u5f85\u5f00\u59cb", "type": "A",
        "owner": "\u674e\u5f00\u53d1", "handler": "\u674e\u5f00\u53d1", "updated_at": "1.0",
        "process": "\u9700\u6c42: \u589e\u52a0\u529f\u80fd\u3002\u9a8c\u6536\u6807\u51c6: \u6d4b\u8bd5\u901a\u8fc7",
    }], ensure_ascii=False), encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=repo_dir, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "initial commit"], cwd=repo_dir, check=True, capture_output=True)
    head_sha = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=repo_dir, text=True).strip()
    return repo_dir, head_sha


def test_default_registry_includes_cursor_sdk():
    reg = create_default_registry()
    manifest = reg.get_manifest("cursor_sdk")
    assert manifest is not None
    assert manifest.adapter_id == "cursor_sdk"
    adapter = reg.get("cursor_sdk")
    assert adapter is not None
    assert isinstance(adapter, CursorSdkAdapter)


def test_runner_validation_requires_cursor_model_when_using_cursor_sdk(mock_repo, tmp_path):
    repo_dir, head_sha = mock_repo
    data_root = tmp_path / "data_val"
    data_root.mkdir()
    checkpoint_store = RunnerCheckpointStore(data_root=str(data_root), project_root=str(repo_dir), project_id="test-proj")
    evidence_store = EvidenceStore(root_dir=str(data_root / "evidence"))
    evidence_gate = EvidenceGate(store=evidence_store, project_root=str(repo_dir))

    reg = AdapterRegistry()
    fake_adapter = CursorSdkAdapter(is_real_host=True, sdk_module=fake_cursor_sdk)
    manifest = create_cursor_sdk_manifest()
    reg.register(fake_adapter, manifest)

    runner = ProductionRunner(
        registry=reg,
        evidence_store=evidence_store,
        evidence_gate=evidence_gate,
        checkpoint_store=checkpoint_store,
    )
    ac_text = "验收标准: 测试通过"
    ac_hash = hashlib.sha256(ac_text.encode("utf-8")).hexdigest()
    spec = TaskExecutionSpec(
        project_id="test-proj",
        project_root=str(repo_dir),
        authority_root=str(repo_dir),
        task_id="T0099",
        task_name="Cursor E2E Test",
        requirement_text="需求: 增加功能",
        acceptance_criteria=ac_text,
        acceptance_criteria_hash=ac_hash,
        task_version="1.0",
        status_at_read="待开始",
        baseline_commit=head_sha,
        builder_adapter_id="cursor_sdk",
        reviewer_adapter_id="cursor_sdk",
        qa_adapter_id="cursor_sdk",
        workspace_mode="inherit",
        cursor_model=None,
    )
    res = runner.start(spec)
    assert res.success is False
    assert res.state == RunnerState.NEEDS_USER_INPUT.value
    assert "--cursor-model" in res.message


def test_runner_reviewer_immutability_fail_closed(mock_repo):
    repo_dir, head_sha = mock_repo
    runner = ProductionRunner()

    # Clean initially
    clean, err = runner._verify_qa_immutability(str(repo_dir), head_sha)
    assert clean is True

    # Mutated file triggers Fail-Closed
    (repo_dir / "unauthorized_file.txt").write_text("tampered content", encoding="utf-8")
    clean2, err2 = runner._verify_qa_immutability(str(repo_dir), head_sha)
    assert clean2 is False
    assert "Code immutability boundary violated" in err2
    (repo_dir / "unauthorized_file.txt").unlink()


def test_runner_checkpoint_preserves_cursor_configuration():
    spec = TaskExecutionSpec(
        project_id="p1",
        project_root=".",
        authority_root=".",
        task_id="T1",
        task_name="N",
        requirement_text="R",
        acceptance_criteria="AC",
        acceptance_criteria_hash="hash",
        task_version="1",
        status_at_read="待开始",
        builder_adapter_id="cursor_sdk",
        cursor_model="composer-2.5",
        cursor_api_key_env="MY_CURSOR_KEY",
        cursor_runtime="local",
    )
    opts = _execution_options_from_spec(spec)
    assert opts["cursor_model"] == "composer-2.5"
    assert opts["cursor_api_key_env"] == "MY_CURSOR_KEY"
    assert opts["cursor_runtime"] == "local"

    ckpt = RunnerCheckpoint(
        task_id="T1",
        project_id="p1",
        state=RunnerState.BUILDING.value,
        current_role="BUILDER",
        execution_options=opts,
    )
    assert ckpt.execution_options["cursor_model"] == "composer-2.5"
    assert ckpt.execution_options["cursor_api_key_env"] == "MY_CURSOR_KEY"
    assert ckpt.execution_options["cursor_runtime"] == "local"


def test_runner_full_e2e_with_cursor_sdk(mock_repo, tmp_path, monkeypatch):
    monkeypatch.setenv("CURSOR_API_KEY", "mock_key_test_123")
    repo_dir, head_sha = mock_repo
    data_root = tmp_path / "data"
    data_root.mkdir()

    FakeCursorSdkState.reset()

    def sdk_hook(prompt, agent):
        cwd = agent.local.cwd if agent.local else str(repo_dir)
        if "review_request_id:" in prompt:
            m_task = re.search(r"- task_id: '([^']+)'", prompt)
            m_base = re.search(r"- baseline_commit: '([^']+)'", prompt)
            m_cand = re.search(r"- candidate_commit: '([^']+)'", prompt)
            m_sess = re.search(r"- session_id: '([^']+)'", prompt)
            m_req = re.search(r"- review_request_id: '([^']+)'", prompt)
            return json.dumps({
                "task_id": m_task.group(1) if m_task else "T0099",
                "baseline_commit": m_base.group(1) if m_base else head_sha,
                "candidate_commit": m_cand.group(1) if m_cand else head_sha,
                "session_id": m_sess.group(1) if m_sess else "sess_rev",
                "review_request_id": m_req.group(1) if m_req else "req_rev",
                "decision": "PASS",
                "defects": [],
                "summary": "Review passed successfully",
            })

        if "independent QA gate" in prompt or "Acceptance criteria hash:" in prompt:
            return json.dumps({
                "decision": "PASS",
                "acceptance_coverage": [{
                    "criterion_id": "AC-01",
                    "status": "PASS",
                    "evidence": "Observed test_app.py passed cleanly",
                }],
                "negative_scenarios": [{
                    "name": "negative check",
                    "status": "PASS",
                    "evidence": "Observed negative conditions",
                }],
                "uncovered_risks": [],
                "defects": [],
                "summary": "All QA criteria passed",
            })

        # Builder: write file and leave uncommitted for Runner to finalize
        feature_file = os.path.join(cwd, "feature.py")
        with open(feature_file, "w", encoding="utf-8") as f:
            f.write("def dummy(): return True\n")
        return "Builder successfully added feature."

    FakeCursorSdkState.set_on_send_hook(sdk_hook)

    reg = AdapterRegistry()
    fake_adapter = CursorSdkAdapter(is_real_host=True, sdk_module=fake_cursor_sdk)
    manifest = create_cursor_sdk_manifest()
    reg.register(fake_adapter, manifest)

    evidence_store = EvidenceStore(root_dir=str(data_root / "evidence"))
    evidence_gate = EvidenceGate(store=evidence_store, project_root=str(repo_dir))
    checkpoint_store = RunnerCheckpointStore(data_root=str(data_root), project_root=str(repo_dir), project_id="test-proj")
    progress_events = []

    runner = ProductionRunner(
        registry=reg,
        evidence_store=evidence_store,
        evidence_gate=evidence_gate,
        checkpoint_store=checkpoint_store,
        progress_callback=progress_events.append,
    )

    ac_text = "验收标准: 测试通过"
    ac_hash = hashlib.sha256(ac_text.encode("utf-8")).hexdigest()
    spec = TaskExecutionSpec(
        project_id="test-proj",
        project_root=str(repo_dir),
        authority_root=str(repo_dir),
        task_id="T0099",
        task_name="Cursor E2E Test",
        requirement_text="需求: 增加功能",
        acceptance_criteria=ac_text,
        acceptance_criteria_hash=ac_hash,
        task_version="1.0",
        status_at_read="待开始",
        baseline_commit=head_sha,
        builder_adapter_id="cursor_sdk",
        reviewer_adapter_id="cursor_sdk",
        qa_adapter_id="cursor_sdk",
        workspace_mode="inherit",
        test_command="python -m pytest test_app.py -q",
        cursor_model="composer-2.5",
        cursor_runtime="local",
    )

    result = runner.start(spec)
    assert result.success is True, f"Runner failed with: {result.message}"
    assert result.state == RunnerState.PENDING_USER_ACCEPTANCE.value
    assert result.candidate_commit is not None
    assert len(result.evidence_ids) >= 3
    FakeCursorSdkState.reset()


def test_runner_mixed_host_cursor_builder_with_other_hosts(mock_repo, tmp_path, monkeypatch):
    """Test mixed-host orchestration: Builder on cursor_sdk, Reviewer on cursor_sdk, QA on custom mock host."""
    monkeypatch.setenv("CURSOR_API_KEY", "mock_key_test_123")
    repo_dir, head_sha = mock_repo
    data_root = tmp_path / "data_mixed"
    data_root.mkdir()

    FakeCursorSdkState.reset()

    def sdk_hook(prompt, agent):
        cwd = agent.local.cwd if agent.local else str(repo_dir)
        if "review_request_id:" in prompt:
            m_task = re.search(r"- task_id: '([^']+)'", prompt)
            m_base = re.search(r"- baseline_commit: '([^']+)'", prompt)
            m_cand = re.search(r"- candidate_commit: '([^']+)'", prompt)
            m_sess = re.search(r"- session_id: '([^']+)'", prompt)
            m_req = re.search(r"- review_request_id: '([^']+)'", prompt)
            return json.dumps({
                "task_id": m_task.group(1) if m_task else "T0099",
                "baseline_commit": m_base.group(1) if m_base else head_sha,
                "candidate_commit": m_cand.group(1) if m_cand else head_sha,
                "session_id": m_sess.group(1) if m_sess else "sess_rev",
                "review_request_id": m_req.group(1) if m_req else "req_rev",
                "decision": "PASS",
                "defects": [],
                "summary": "Review passed successfully",
            })
        # Builder
        feature_file = os.path.join(cwd, "feature_mixed.py")
        with open(feature_file, "w", encoding="utf-8") as f:
            f.write("def dummy(): return True\n")
        return "Builder implemented feature"

    FakeCursorSdkState.set_on_send_hook(sdk_hook)

    # Codex QA adapter conforming to EvidenceGate real host rules
    from scripts._lib.core.host_adapter import BaseHostAdapter

    class CodexQAAdapter(BaseHostAdapter):
        def __init__(self, adapter_id="codex_cli", is_real_host=True):
            self.adapter_id = adapter_id
            self.is_real_host = is_real_host

        def detect_capabilities(self):
            return HostCapabilities(
                is_real_host=True,
                supports_interactive_confirmation=CapabilitySupport.UNSUPPORTED,
            )
        def dispatch_agent(self, request):
            return AgentHandle(
                session_id=request.session_id,
                host_id="codex_cli",
                status="running",
                is_real_host=True,
                adapter_instance_id="codex_inst_qa",
                invocation_token="codex_tok_12345678",
            )
        def wait_for_result(self, handle, timeout_seconds=None):
            qa_json = {
                "decision": "PASS",
                "acceptance_coverage": [{
                    "criterion_id": "AC-01",
                    "status": "PASS",
                    "evidence": "Observed test_app.py passed cleanly",
                }],
                "negative_scenarios": [{
                    "name": "negative check",
                    "status": "PASS",
                    "evidence": "Observed negative conditions",
                }],
                "uncovered_risks": [],
                "defects": [],
                "summary": "Codex QA passed cleanly",
            }
            return AgentResult(
                session_id=handle.session_id,
                status=AgentStatus.SUCCESS,
                output=json.dumps(qa_json),
                partial_results=({"invocation_id": "inv_qa_codex_real"},),
                is_real_host=True,
            )
        def cancel_agent(self, handle):
            return True
        def request_confirmation(self, request):
            raise AgentNotSupportedError("Not supported")

    from scripts._lib.hosts.codex_cli_adapter import create_codex_cli_manifest
    qa_manifest = create_codex_cli_manifest(adapter_id="codex_cli")

    reg = AdapterRegistry()
    fake_cursor_adapter = CursorSdkAdapter(is_real_host=True, sdk_module=fake_cursor_sdk)
    cursor_manifest = create_cursor_sdk_manifest()
    reg.register(fake_cursor_adapter, cursor_manifest)
    reg.register(CodexQAAdapter(adapter_id="codex_cli", is_real_host=True), qa_manifest)

    evidence_store = EvidenceStore(root_dir=str(data_root / "evidence"))
    evidence_gate = EvidenceGate(store=evidence_store, project_root=str(repo_dir))
    checkpoint_store = RunnerCheckpointStore(data_root=str(data_root), project_root=str(repo_dir), project_id="test-proj")

    runner = ProductionRunner(
        registry=reg,
        evidence_store=evidence_store,
        evidence_gate=evidence_gate,
        checkpoint_store=checkpoint_store,
    )

    ac_text = "验收标准: 测试通过"
    ac_hash = hashlib.sha256(ac_text.encode("utf-8")).hexdigest()
    spec = TaskExecutionSpec(
        project_id="test-proj",
        project_root=str(repo_dir),
        authority_root=str(repo_dir),
        task_id="T0099",
        task_name="Cursor Mixed Host Test",
        requirement_text="需求: 增加功能",
        acceptance_criteria=ac_text,
        acceptance_criteria_hash=ac_hash,
        task_version="1.0",
        status_at_read="待开始",
        baseline_commit=head_sha,
        builder_adapter_id="cursor_sdk",
        reviewer_adapter_id="cursor_sdk",
        qa_adapter_id="codex_cli",
        workspace_mode="inherit",
        test_command="python -m pytest test_app.py -q",
        cursor_model="composer-2.5",
        cursor_runtime="local",
    )

    result = runner.start(spec)
    assert result.success is True, f"Runner failed with: {result.message}"
    assert result.state == RunnerState.PENDING_USER_ACCEPTANCE.value
    assert result.candidate_commit is not None
    assert len(result.evidence_ids) >= 3
    FakeCursorSdkState.reset()


def test_cursor_sdk_real_api_signatures_and_agent_id(tmp_path, monkeypatch):
    """Verify Agent.create receives api_key directly and does not enable project setting sources."""
    monkeypatch.setenv("CURSOR_API_KEY", "real_style_api_key_xyz")
    adapter = CursorSdkAdapter(is_real_host=True, default_model="composer-2.5", sdk_module=fake_cursor_sdk)

    req = AgentRequest(
        session_id="sess_real_api_01",
        prompt="verify api signatures",
        role="BUILDER",
        workspace_dir=str(tmp_path),
    )
    handle = adapter.dispatch_agent(req)
    session_data = adapter._validate_handle(handle)
    agent = session_data["agent"]

    assert hasattr(agent, "agent_id")
    assert agent.agent_id.startswith("ag_")
    assert agent.api_key == "real_style_api_key_xyz"
    assert agent.local.cwd == str(tmp_path.resolve())
    assert agent.local.setting_sources is None
    assert not agent.local.dirs
    assert not agent.agents
    assert not (tmp_path / ".cursor").exists()
    res = adapter.wait_for_result(handle)
    assert res.status == AgentStatus.SUCCESS
    assert agent.is_closed is True  # Resource cleanup called


def test_cursor_sdk_agent_close_on_cancellation(tmp_path):
    """Verify agent.close() is called upon run cancellation to terminate subprocesses."""
    adapter = CursorSdkAdapter(is_real_host=False, default_model="composer-2.5", sdk_module=fake_cursor_sdk)

    req = AgentRequest(
        session_id="sess_cancel_cleanup",
        prompt="cancel test",
        role="BUILDER",
        workspace_dir=str(tmp_path),
    )
    handle = adapter.dispatch_agent(req)
    session_data = adapter._validate_handle(handle)
    agent = session_data["agent"]
    assert agent.is_closed is False

    ok = adapter.cancel_agent(handle)
    assert ok is True
    assert agent.is_closed is True


def test_cursor_sdk_thread_level_timeout_enforcement(tmp_path):
    """Verify that thread-level timeout cancels run and raises AgentTimeoutError when wait() takes no args."""
    adapter = CursorSdkAdapter(
        is_real_host=False,
        default_model="composer-2.5",
        default_timeout_seconds=0.1,
        sdk_module=fake_cursor_sdk,
    )

    req = AgentRequest(
        session_id="sess_timeout_01",
        prompt="sleep test",
        role="BUILDER",
        workspace_dir=str(tmp_path),
    )
    handle = adapter.dispatch_agent(req)
    session_data = adapter._validate_handle(handle)
    run = session_data["run"]
    agent = session_data["agent"]
    run.delay_seconds = 2.0  # simulate long operation

    with pytest.raises(AgentTimeoutError) as exc_info:
        adapter.wait_for_result(handle, timeout_seconds=0.1)

    assert "timed out after 0.1 seconds" in str(exc_info.value)
    assert run.is_cancelled is True
    assert agent.is_closed is True


def test_cursor_sdk_resume_existing_session(tmp_path):
    """Verify Agent.resume is called instead of Agent.create when resuming an existing agent session."""
    adapter = CursorSdkAdapter(is_real_host=False, default_model="composer-2.5", sdk_module=fake_cursor_sdk)

    # Initial dispatch
    req1 = AgentRequest(
        session_id="sess_initial",
        prompt="first turn",
        role="BUILDER",
        workspace_dir=str(tmp_path),
    )
    handle1 = adapter.dispatch_agent(req1)
    session_data1 = adapter._validate_handle(handle1)
    original_agent = session_data1["agent"]
    orig_agent_id = original_agent.agent_id
    adapter.wait_for_result(handle1)
    assert original_agent.is_closed is True

    # Resumed turn referencing original agent
    req2 = AgentRequest(
        session_id="sess_resumed",
        prompt="second turn resuming original",
        role="BUILDER",
        workspace_dir=str(tmp_path),
        extra_context={
            "cursor_model": "composer-2.5",
            "resume_agent_id": orig_agent_id,
        },
    )
    handle2 = adapter.dispatch_agent(req2)
    session_data2 = adapter._validate_handle(handle2)
    resumed_agent = session_data2["agent"]

    assert resumed_agent.agent_id == orig_agent_id
    assert resumed_agent.is_closed is False
    res2 = adapter.wait_for_result(handle2)
    assert res2.status == AgentStatus.SUCCESS
    assert resumed_agent.is_closed is True


def test_cursor_sdk_custom_api_key_env_propagation(tmp_path, monkeypatch):
    """Verify cursor_api_key_env in extra_context takes precedence over default CURSOR_API_KEY."""
    monkeypatch.setenv("MY_SPECIAL_CURSOR_KEY", "special_token_98765")
    adapter = CursorSdkAdapter(
        is_real_host=True,
        default_model="composer-2.5",
        api_key_env_var="CURSOR_API_KEY",  # default, but not set in env
        sdk_module=fake_cursor_sdk,
    )

    req = AgentRequest(
        session_id="sess_custom_key",
        prompt="custom env test",
        role="BUILDER",
        workspace_dir=str(tmp_path),
        extra_context={
            "cursor_model": "composer-2.5",
            "cursor_api_key_env": "MY_SPECIAL_CURSOR_KEY",
        },
    )
    handle = adapter.dispatch_agent(req)
    session_data = adapter._validate_handle(handle)
    agent = session_data["agent"]
    assert agent.api_key == "special_token_98765"

    res = adapter.wait_for_result(handle)
    assert res.status == AgentStatus.SUCCESS


def test_cursor_sdk_readonly_role_tool_isolation(tmp_path):
    """Verify Reviewer and QA agents are issued disallowed_tools and read-only tools on dispatch via AgentOptions."""
    adapter = CursorSdkAdapter(is_real_host=False, default_model="composer-2.5", sdk_module=fake_cursor_sdk)

    req_rev = AgentRequest(
        session_id="sess_rev_tools",
        prompt="review code",
        role="REVIEWER",
        workspace_dir=str(tmp_path),
        extra_context={"production_runner_managed": True},
    )
    handle_rev = adapter.dispatch_agent(req_rev)
    agent_rev = adapter._validate_handle(handle_rev)["agent"]
    assert "edit" in agent_rev.disallowed_tools
    assert "shell" in agent_rev.disallowed_tools
    assert "read" not in agent_rev.disallowed_tools
    assert "grep" in agent_rev.disallowed_tools
    assert "glob" in agent_rev.disallowed_tools
    assert "write" not in agent_rev.disallowed_tools
    assert "mcp" in agent_rev.disallowed_tools
    assert not agent_rev.agents
    assert agent_rev.tools == ["read"]

    assert agent_rev.is_tool_allowed("read") is True
    assert agent_rev.is_tool_allowed("edit") is False
    assert agent_rev.is_tool_allowed("shell") is False
    assert agent_rev.is_tool_allowed("grep") is False
    assert agent_rev.is_tool_allowed("glob") is False
    for prohibited in ["edit", "shell", "grep", "glob"]:
        with pytest.raises(fake_cursor_sdk.ToolNotAllowedError):
            agent_rev.execute_tool(prohibited)

    res_rev = adapter.wait_for_result(handle_rev)
    assert res_rev.status == AgentStatus.SUCCESS

    req_qa = AgentRequest(
        session_id="sess_qa_tools",
        prompt="test code",
        role="QA",
        workspace_dir=str(tmp_path),
        extra_context={"production_runner_managed": True},
    )
    handle_qa = adapter.dispatch_agent(req_qa)
    agent_qa = adapter._validate_handle(handle_qa)["agent"]
    assert "edit" in agent_qa.disallowed_tools
    assert "shell" in agent_qa.disallowed_tools
    assert "read" in agent_qa.disallowed_tools
    assert "grep" in agent_qa.disallowed_tools
    assert "glob" in agent_qa.disallowed_tools
    assert "write" not in agent_qa.disallowed_tools
    assert "mcp" in agent_qa.disallowed_tools
    assert not agent_qa.agents
    assert agent_qa.tools == []

    for prohibited in ["read", "edit", "shell", "grep", "glob"]:
        assert agent_qa.is_tool_allowed(prohibited) is False
        with pytest.raises(fake_cursor_sdk.ToolNotAllowedError):
            agent_qa.execute_tool(prohibited)

    res_qa = adapter.wait_for_result(handle_qa)
    assert res_qa.status == AgentStatus.SUCCESS

    # Unknown tool names (like uppercase Edit or Bash, or non-SDK 'write') reject with BadRequestError
    with pytest.raises(fake_cursor_sdk.BadRequestError) as exc_opt:
        fake_cursor_sdk.AgentOptions(model="composer-2.5", tools=["Edit"])
    assert "Unknown tool 'Edit'" in str(exc_opt.value)

    with pytest.raises(fake_cursor_sdk.BadRequestError) as exc_write:
        fake_cursor_sdk.AgentOptions(model="composer-2.5", tools=["write"])
    assert "Unknown tool 'write'" in str(exc_write.value)

    with pytest.raises(fake_cursor_sdk.BadRequestError) as exc_create:
        fake_cursor_sdk.Agent.create("composer-2.5", disallowed_tools=["Bash"])
    assert "Unknown tool 'Bash'" in str(exc_create.value)

    # Official AgentDefinition strictly rejects tools with TypeError
    with pytest.raises(TypeError) as exc_sub_bad:
        fake_cursor_sdk.AgentDefinition(description="d", prompt="p", tools=["Read"])
    assert "unexpected keyword argument" in str(exc_sub_bad.value)


def test_cursor_sdk_strict_signatures_rejects_client_kwargs(tmp_path):
    """Verify that Agent.create, Agent.resume, and agent.send strictly reject invalid arguments."""
    with pytest.raises(TypeError) as exc1:
        fake_cursor_sdk.Agent.create("composer-2.5", client=object())
    assert "unexpected keyword argument" in str(exc1.value) and "client" in str(exc1.value)

    with pytest.raises(TypeError) as exc2:
        fake_cursor_sdk.Agent.resume("ag_123", client=object())
    assert "unexpected keyword argument" in str(exc2.value)

    # Reject api_key passed directly as keyword argument
    with pytest.raises(TypeError) as exc3:
        fake_cursor_sdk.Agent.resume("ag_123", api_key="secret")
    assert "unexpected keyword argument" in str(exc3.value)

    # Valid resume via options=AgentOptions(...)
    resumed = fake_cursor_sdk.Agent.resume("ag_123", options=fake_cursor_sdk.AgentOptions(api_key="secret"))
    assert resumed.agent_id == "ag_123"
    assert resumed.api_key == "secret"

    # agent.send() must reject extra keyword arguments like prompt=, tools=, disallowed_tools=
    ag = fake_cursor_sdk.Agent.create("composer-2.5")
    with pytest.raises(TypeError):
        ag.send(prompt="do something")

    with pytest.raises(TypeError) as exc4:
        ag.send("message", tools=["read"])
    assert "unexpected keyword argument" in str(exc4.value)

    with pytest.raises(TypeError) as exc5:
        ag.send("message", disallowed_tools=["edit"])
    assert "unexpected keyword argument" in str(exc5.value)


def test_cursor_sdk_role_mapping_normal_and_managed(tmp_path):
    """Verify role resolution under normal and production_runner_managed modes."""
    adapter = CursorSdkAdapter(is_real_host=False, default_model="composer-2.5", sdk_module=fake_cursor_sdk)

    # 1. Normal mappings (8+ roles)
    normal_expectations = {
        "DEV": "flow-dev",
        "BUILDER": "flow-dev",
        "REVIEWER": "flow-reviewer",
        "QA": "flow-qa",
        "ARCHITECT": "flow-architect",
        "PM": "flow-pm",
        "DOCS": "flow-docs",
        "DEVOPS": "flow-devops",
        "FRONTEND": "flow-frontend",
    }
    for role, expected_agent in normal_expectations.items():
        assert adapter._resolve_target_agent(role, {}) == expected_agent

    # 2. Production runner managed mode (only BUILDER, REVIEWER, QA permitted)
    managed_ctx = {"production_runner_managed": True}
    assert adapter._resolve_target_agent("BUILDER", managed_ctx) == "flow-runner-builder"
    assert adapter._resolve_target_agent("REVIEWER", managed_ctx) == "flow-runner-reviewer"
    assert adapter._resolve_target_agent("QA", managed_ctx) == "flow-runner-qa"

    # Managed mode Fail-Closed on any other role
    for role in ["DEV", "ARCHITECT", "PM", "DOCS", "DEVOPS", "FRONTEND", "UNKNOWN"]:
        with pytest.raises(AgentNotSupportedError, match="not supported under production_runner_managed mode"):
            adapter._resolve_target_agent(role, managed_ctx)

    # Normal mode rejects completely unknown role
    with pytest.raises(AgentNotSupportedError, match="not supported by CursorSdkAdapter"):
        adapter._resolve_target_agent("NONEXISTENT_ROLE", {})


def test_cursor_sdk_parent_agent_has_only_task_tool_and_single_agent(tmp_path):
    """Verify parent agent has only 'task' tool, disallowed prohibited tools, and exactly one registered subagent."""
    adapter = CursorSdkAdapter(is_real_host=False, default_model="composer-2.5", sdk_module=fake_cursor_sdk)

    req = AgentRequest(
        session_id="sess_parent_gating",
        prompt="execute work",
        role="BUILDER",
        workspace_dir=str(tmp_path),
        extra_context={"production_runner_managed": True},
    )
    handle = adapter.dispatch_agent(req)
    agent = adapter._validate_handle(handle)["agent"]

    assert agent.tools == ["edit", "glob", "grep", "read"]
    assert "shell" in agent.disallowed_tools
    assert "task" in agent.disallowed_tools
    assert "mcp" in agent.disallowed_tools
    assert "write" not in agent.disallowed_tools
    assert not agent.agents
    assert agent.local.setting_sources is None
    assert not (tmp_path / ".cursor").exists()

    assert agent.is_tool_allowed("glob") is True
    assert agent.is_tool_allowed("edit") is True
    assert agent.is_tool_allowed("shell") is False
    with pytest.raises(fake_cursor_sdk.ToolNotAllowedError):
        agent.execute_tool("shell")

    res = adapter.wait_for_result(handle)
    assert res.status == AgentStatus.SUCCESS


def test_cursor_sdk_task_call_gate_enforcement(tmp_path):
    """Verify the role allowlist rejects tools that are not declared in the markdown file."""
    adapter = CursorSdkAdapter(is_real_host=False, default_model="composer-2.5", sdk_module=fake_cursor_sdk)

    # Case A: text only, no tool call -> SUCCESS
    fake_cursor_sdk.FakeCursorSdkState.set_next_tool_calls([])
    req_zero = AgentRequest(
        session_id="sess_gate_zero",
        prompt="work",
        role="BUILDER",
        workspace_dir=str(tmp_path),
        extra_context={"production_runner_managed": True},
    )
    h_zero = adapter.dispatch_agent(req_zero)
    res_zero = adapter.wait_for_result(h_zero)
    assert res_zero.status == AgentStatus.SUCCESS

    # Case B: task is not in the builder file allowlist -> FAILED
    fake_cursor_sdk.FakeCursorSdkState.set_next_tool_calls([
        {"tool": "task", "subagent": "flow-dev"},
        {"tool": "task", "subagent": "flow-dev"},
    ])
    req_multi = AgentRequest(
        session_id="sess_gate_multi",
        prompt="work",
        role="BUILDER",
        workspace_dir=str(tmp_path),
    )
    h_multi = adapter.dispatch_agent(req_multi)
    res_multi = adapter.wait_for_result(h_multi)
    assert res_multi.status == AgentStatus.FAILED
    assert "prohibited tool(s) ['task']" in res_multi.error_message

    # Case C: Wrong subagent called -> FAILED
    fake_cursor_sdk.FakeCursorSdkState.set_next_tool_calls([
        {"tool": "task", "subagent": "flow-qa"},
    ])
    req_wrong = AgentRequest(
        session_id="sess_gate_wrong",
        prompt="work",
        role="BUILDER",
        workspace_dir=str(tmp_path),
    )
    h_wrong = adapter.dispatch_agent(req_wrong)
    res_wrong = adapter.wait_for_result(h_wrong)
    assert res_wrong.status == AgentStatus.FAILED
    assert "prohibited tool(s) ['task']" in res_wrong.error_message

    # Case D: Prohibited tool called -> FAILED
    fake_cursor_sdk.FakeCursorSdkState.set_next_tool_calls([
        {"tool": "edit", "args": {"file": "evil.py"}},
        {"tool": "task", "subagent": "flow-dev"},
    ])
    req_prohibited = AgentRequest(
        session_id="sess_gate_prohibited",
        prompt="work",
        role="BUILDER",
        workspace_dir=str(tmp_path),
    )
    h_prohibited = adapter.dispatch_agent(req_prohibited)
    res_prohibited = adapter.wait_for_result(h_prohibited)
    assert res_prohibited.status == AgentStatus.FAILED
    assert "prohibited tool(s)" in res_prohibited.error_message

    # Case E: edit is declared for the unmanaged builder file -> SUCCESS
    fake_cursor_sdk.FakeCursorSdkState.set_next_tool_calls([
        {"tool": "edit", "args": {"file": "app.py"}},
    ])
    req_ok = AgentRequest(
        session_id="sess_gate_ok",
        prompt="work",
        role="BUILDER",
        workspace_dir=str(tmp_path),
    )
    h_ok = adapter.dispatch_agent(req_ok)
    res_ok = adapter.wait_for_result(h_ok)
    assert res_ok.status == AgentStatus.SUCCESS


def test_cursor_sdk_resume_recarries_agents_and_task_tool(tmp_path):
    """Verify that resuming via Agent.resume recarries agents and task tool constraints."""
    adapter = CursorSdkAdapter(is_real_host=False, default_model="composer-2.5", sdk_module=fake_cursor_sdk)

    # Initial turn
    req1 = AgentRequest(
        session_id="sess_init_gate",
        prompt="first",
        role="BUILDER",
        workspace_dir=str(tmp_path),
        extra_context={"production_runner_managed": True},
    )
    h1 = adapter.dispatch_agent(req1)
    ag1 = adapter._validate_handle(h1)["agent"]
    orig_id = ag1.agent_id
    adapter.wait_for_result(h1)

    # Resumed turn
    req2 = AgentRequest(
        session_id="sess_resume_gate",
        prompt="second",
        role="BUILDER",
        workspace_dir=str(tmp_path),
        extra_context={
            "production_runner_managed": True,
            "resume_agent_id": orig_id,
        },
    )
    h2 = adapter.dispatch_agent(req2)
    ag2 = adapter._validate_handle(h2)["agent"]
    assert ag2.agent_id == orig_id
    assert ag2.tools == ["edit", "glob", "grep", "read"]
    assert "shell" in ag2.disallowed_tools
    assert "mcp" in ag2.disallowed_tools
    assert "write" not in ag2.disallowed_tools
    assert not ag2.agents
    assert ag2.local.setting_sources is None

    res2 = adapter.wait_for_result(h2)
    assert res2.status == AgentStatus.SUCCESS


def test_cursor_sdk_missing_or_corrupt_agent_markdown_fails_closed(tmp_path):
    """Verify missing agent markdown or invalid frontmatter triggers Fail-Closed AgentNotSupportedError."""
    adapter = CursorSdkAdapter(is_real_host=False, default_model="composer-2.5", sdk_module=fake_cursor_sdk)

    # 1. Nonexistent agent id
    with pytest.raises(AgentNotSupportedError, match="Subagent markdown definition.*does not exist.*Fail-Closed"):
        adapter._load_subagent_definition(str(tmp_path), "flow-nonexistent-agent")

    # 2. Corrupt markdown: missing frontmatter
    corrupt_dir = tmp_path / ".cursor" / "agents"
    corrupt_dir.mkdir(parents=True)
    bad_md1 = corrupt_dir / "bad1.md"
    bad_md1.write_text("Just plain text without yaml frontmatter", encoding="utf-8")
    with pytest.raises(AgentNotSupportedError, match="missing YAML frontmatter header"):
        adapter._load_subagent_definition(str(tmp_path), "bad1")

    # 3. Missing name in frontmatter
    bad_md2 = corrupt_dir / "bad2.md"
    bad_md2.write_text("---\ndescription: some desc\n---\nPrompt body", encoding="utf-8")
    with pytest.raises(AgentNotSupportedError, match="missing required 'name' field"):
        adapter._load_subagent_definition(str(tmp_path), "bad2")

    # 4. Missing prompt body
    bad_md3 = corrupt_dir / "bad3.md"
    bad_md3.write_text("---\nname: bad3\ndescription: some desc\n---\n   \n", encoding="utf-8")
    with pytest.raises(AgentNotSupportedError, match="Subagent body prompt.*is empty"):
        adapter._load_subagent_definition(str(tmp_path), "bad3")



def test_runner_resume_wires_builder_session_id_to_extra_context(mock_repo, tmp_path, monkeypatch):
    """Verify ProductionRunner injects Cursor canonical agent_id into resume_agent_id when resuming from checkpoint."""
    monkeypatch.setenv("CURSOR_API_KEY", "mock_key_test_123")
    repo_dir, head_sha = mock_repo
    # Overwrite board.json with executable acceptance criteria
    board_file = repo_dir / "user_data" / "board.json"
    board_file.write_text(json.dumps([{
        "id": "T0099", "name": "Cursor Resume Test", "status": "待开始", "type": "A",
        "owner": "李开发", "handler": "李开发", "updated_at": "1.0",
        "process": "需求: 断点续跑支持。\n验收标准:\n- resume_agent_id 正确传入 extra_context",
    }], ensure_ascii=False), encoding="utf-8")
    data_root = tmp_path / "data_resume_check"
    data_root.mkdir()

    dispatched_requests = []

    class CapturingCursorAdapter(CursorSdkAdapter):
        def dispatch_agent(self, request):
            dispatched_requests.append(request)
            return super().dispatch_agent(request)

    reg = AdapterRegistry()
    cap_adapter = CapturingCursorAdapter(is_real_host=True, sdk_module=fake_cursor_sdk)
    reg.register(cap_adapter, create_cursor_sdk_manifest())

    evidence_store = EvidenceStore(root_dir=str(data_root / "evidence"))
    evidence_gate = EvidenceGate(store=evidence_store, project_root=str(repo_dir))
    checkpoint_store = RunnerCheckpointStore(data_root=str(data_root), project_root=str(repo_dir), project_id="test-proj")

    from scripts._lib.core.task_spec_loader import load_task_execution_spec
    spec = load_task_execution_spec(
        project_root=str(repo_dir),
        task_id="T0099",
        authority_root=str(repo_dir),
        overrides={
            "builder_adapter_id": "cursor_sdk",
            "reviewer_adapter_id": "cursor_sdk",
            "qa_adapter_id": "cursor_sdk",
            "cursor_model": "composer-2.5",
            "cursor_api_key_env": "CURSOR_API_KEY",
            "cursor_runtime": "local",
        },
    )
    from scripts._lib.core.production_runner import _execution_spec_snapshot
    snapshot = _execution_spec_snapshot(spec)

    # Manually create a checkpoint with a canonical Cursor agent_id (ag_...) in BUILDING state
    prior_cursor_agent_id = "ag_canonical_cursor_session_12345"
    ckpt = RunnerCheckpoint(
        task_id="T0099",
        project_id="test-proj",
        state=RunnerState.BUILDING.value,
        current_role="BUILDER",
        candidate_commit=head_sha,
        candidate_generation=0,
        review_cycle=0,
        qa_cycle=0,
        total_attempts=1,
        builder_session_id=prior_cursor_agent_id,
        worktree_path=str(repo_dir),
        execution_options={
            "builder_adapter_id": "cursor_sdk",
            "reviewer_adapter_id": "cursor_sdk",
            "qa_adapter_id": "cursor_sdk",
            "cursor_model": "composer-2.5",
            "cursor_api_key_env": "CURSOR_API_KEY",
            "cursor_runtime": "local",
        },
        execution_spec_snapshot=snapshot,
    )
    checkpoint_store.save_checkpoint(ckpt)

    # Resume the task
    runner = ProductionRunner(
        registry=reg,
        evidence_store=evidence_store,
        evidence_gate=evidence_gate,
        checkpoint_store=checkpoint_store,
    )

    # Intercept builder run to check extra_context and stop early
    def fail_after_builder(prompt, agent):
        return "builder done"
    fake_cursor_sdk.FakeCursorSdkState.set_on_send_hook(fail_after_builder)

    runner.resume(
        project_root=str(repo_dir),
        task_id="T0099",
        authority_root=str(repo_dir),
    )

    assert len(dispatched_requests) >= 1
    builder_req = dispatched_requests[0]
    assert builder_req.role == "BUILDER"
    # Must equal canonical Cursor agent_id, not internal Runner prefix sess_builder_runner_...
    assert builder_req.extra_context.get("resume_agent_id") == prior_cursor_agent_id
    assert builder_req.extra_context.get("resume_agent_id").startswith("ag_")
    assert builder_req.extra_context.get("is_resume") is True


def test_cursor_sdk_timeout_thread_fully_joined(tmp_path):
    """Verify that when a timeout occurs, run.cancel is called and worker thread is cleanly joined."""
    adapter = CursorSdkAdapter(is_real_host=False, default_model="composer-2.5", sdk_module=fake_cursor_sdk)
    req = AgentRequest(
        session_id="sess_timeout_join",
        prompt="hang",
        role="BUILDER",
        workspace_dir=str(tmp_path),
    )
    handle = adapter.dispatch_agent(req)
    session_data = adapter._running_sessions[handle.invocation_token]
    run = session_data["run"]
    run.delay_seconds = 2.0
    with pytest.raises(AgentTimeoutError):
        adapter.wait_for_result(handle, timeout_seconds=0.1)

    worker = session_data.get("worker")
    assert worker is not None
    assert not worker.is_alive()


def test_cursor_sdk_timeout_thread_refuses_to_exit_logs_warning_and_raises_timeout(tmp_path, caplog):
    """Verify that when a thread refuses to exit after timeout and cancellation,
    the adapter logs a warning (without NameError on logger) and properly raises AgentTimeoutError."""
    import logging
    adapter = CursorSdkAdapter(is_real_host=False, default_model="composer-2.5", sdk_module=fake_cursor_sdk)
    req = AgentRequest(
        session_id="sess_timeout_refuse",
        prompt="hang forever",
        role="BUILDER",
        workspace_dir=str(tmp_path),
    )
    handle = adapter.dispatch_agent(req)
    session_data = adapter._running_sessions[handle.invocation_token]
    run = session_data["run"]
    run.delay_seconds = 4.0
    run.refuse_cancel = True

    with caplog.at_level(logging.WARNING):
        with pytest.raises(AgentTimeoutError) as exc_info:
            adapter.wait_for_result(handle, timeout_seconds=0.05)

    assert "timed out after 0.05 seconds" in str(exc_info.value)
    # Verify warning was logged by logger without crashing with NameError
    assert "did not exit after grace period following run.cancel()" in caplog.text
    worker = session_data.get("worker")
    assert worker is not None
    assert worker.is_alive()


def test_cursor_sdk_glob_and_grep_distinct_tools():
    """Verify glob and grep are distinct SDK tools and both supported without conflation."""
    assert "glob" in fake_cursor_sdk.VALID_TOOLS
    assert "grep" in fake_cursor_sdk.VALID_TOOLS
    assert fake_cursor_sdk.CURSOR_SESSION_TO_SDK_TOOL_MAP["glob"] == "glob"
    assert fake_cursor_sdk.CURSOR_SESSION_TO_SDK_TOOL_MAP["find_by_name"] == "glob"
    assert fake_cursor_sdk.CURSOR_SESSION_TO_SDK_TOOL_MAP["grep"] == "grep"
    assert fake_cursor_sdk.CURSOR_SESSION_TO_SDK_TOOL_MAP["grep_search"] == "grep"


def test_cursor_sdk_subagent_tool_rejection_and_inline_override(tmp_path, monkeypatch):
    """
    Verify:
    1. Subagents dispatched through CursorSdkAdapter enforce tool permissions via file-based definitions:
       - Reviewer attempting 'edit' is rejected and fails the adapter gate.
       - QA attempting 'read' is rejected and fails the adapter gate.
       - Reviewer using 'read' passes the adapter gate.
       - Builder using 'edit' and 'glob' passes the adapter gate.
    2. The adapter never passes inline AgentDefinition for file-based subagents,
       ensuring file-based toolsets (tools: [Read]) are preserved natively without being overwritten.
    3. Explicitly passing an inline AgentDefinition demonstrates the overwrite behavior,
       confirming why the adapter's agents=None design is essential.
    """
    monkeypatch.setenv("CURSOR_API_KEY", "test_key_gate_123")
    FakeCursorSdkState.reset()

    # Workspace directory without any .cursor directory: verifies .cursor is never copied
    clean_workspace = tmp_path / "clean_ws"
    clean_workspace.mkdir(parents=True, exist_ok=True)

    adapter = CursorSdkAdapter(is_real_host=True, sdk_module=fake_cursor_sdk)

    # 1. Reviewer attempting prohibited tool 'edit' fails through adapter gate
    rev_req = AgentRequest(
        session_id="test_rev_reject_edit",
        role="REVIEWER",
        prompt="Review code changes",
        workspace_dir=str(clean_workspace),
        extra_context={"production_runner_managed": True, "cursor_model": "composer-2.5"},
    )
    FakeCursorSdkState.set_next_messages([
        fake_cursor_sdk.SDKMessage("tool_call", name="edit", args={"path": "app.py"}),
    ])
    handle_rev = adapter.dispatch_agent(rev_req)
    result_rev = adapter.wait_for_result(handle_rev)
    assert result_rev.status == AgentStatus.FAILED
    assert "prohibited tool(s) ['edit']" in result_rev.error_message

    # 2. QA attempting prohibited tool 'read' fails through adapter gate
    qa_req = AgentRequest(
        session_id="test_qa_reject_read",
        role="QA",
        prompt="Verify test execution",
        workspace_dir=str(clean_workspace),
        extra_context={"production_runner_managed": True, "cursor_model": "composer-2.5"},
    )
    FakeCursorSdkState.set_next_messages([
        fake_cursor_sdk.SDKMessage("tool_call", name="read", args={"path": "app.py"}),
    ])
    handle_qa = adapter.dispatch_agent(qa_req)
    result_qa = adapter.wait_for_result(handle_qa)
    assert result_qa.status == AgentStatus.FAILED
    assert "prohibited tool(s) ['read']" in result_qa.error_message

    # 3. Builder attempting prohibited tool 'shell' fails through adapter gate
    bld_shell_req = AgentRequest(
        session_id="test_bld_reject_shell",
        role="BUILDER",
        prompt="Execute command",
        workspace_dir=str(clean_workspace),
        extra_context={"production_runner_managed": True, "cursor_model": "composer-2.5"},
    )
    FakeCursorSdkState.set_next_messages([
        fake_cursor_sdk.SDKMessage("tool_call", name="shell", args={"command": "rm -rf /"}),
    ])
    handle_bld_shell = adapter.dispatch_agent(bld_shell_req)
    result_bld_shell = adapter.wait_for_result(handle_bld_shell)
    assert result_bld_shell.status == AgentStatus.FAILED
    assert "prohibited tool(s) ['shell']" in result_bld_shell.error_message

    # 4. Builder attempting prohibited tool 'task' fails through adapter gate
    bld_task_req = AgentRequest(
        session_id="test_bld_reject_task",
        role="BUILDER",
        prompt="Delegate task",
        workspace_dir=str(clean_workspace),
        extra_context={"production_runner_managed": True, "cursor_model": "composer-2.5"},
    )
    FakeCursorSdkState.set_next_messages([
        fake_cursor_sdk.SDKMessage("tool_call", name="task", args={"subagent": "flow-dev"}),
    ])
    handle_bld_task = adapter.dispatch_agent(bld_task_req)
    result_bld_task = adapter.wait_for_result(handle_bld_task)
    assert result_bld_task.status == AgentStatus.FAILED
    assert "prohibited tool(s) ['task']" in result_bld_task.error_message

    # 5. Builder using authorized 'edit' and 'glob' tools succeeds through adapter
    builder_ok_req = AgentRequest(
        session_id="test_bld_pass_edit_glob",
        role="BUILDER",
        prompt="Implement feature",
        workspace_dir=str(clean_workspace),
        extra_context={"production_runner_managed": True, "cursor_model": "composer-2.5"},
    )
    FakeCursorSdkState.set_next_messages([
        fake_cursor_sdk.SDKMessage("tool_call", name="glob", args={"pattern": "*.py"}),
        fake_cursor_sdk.SDKMessage("tool_call", name="edit", args={"path": "app.py"}),
        fake_cursor_sdk.SDKMessage("assistant", text="Implemented feature successfully"),
    ])
    handle_bld = adapter.dispatch_agent(builder_ok_req)
    result_bld = adapter.wait_for_result(handle_bld)
    assert result_bld.status == AgentStatus.SUCCESS

    # 6. Reviewer using authorized 'read' tool succeeds through adapter
    rev_ok_req = AgentRequest(
        session_id="test_rev_pass_read",
        role="REVIEWER",
        prompt="Review code changes",
        workspace_dir=str(clean_workspace),
        extra_context={"production_runner_managed": True, "cursor_model": "composer-2.5"},
    )
    FakeCursorSdkState.set_next_messages([
        fake_cursor_sdk.SDKMessage("tool_call", name="read", args={"path": "app.py"}),
        fake_cursor_sdk.SDKMessage("assistant", text='{"decision": "PASS", "defects": [], "summary": "Looks good"}'),
    ])
    handle_rev_ok = adapter.dispatch_agent(rev_ok_req)
    result_rev_ok = adapter.wait_for_result(handle_rev_ok)
    assert result_rev_ok.status == AgentStatus.SUCCESS
    assert "decision" in result_rev_ok.output

    # 7. Official nested message shape: AssistantPayload with TextBlock extracts into output
    rev_official_req = AgentRequest(
        session_id="test_rev_official_shape",
        role="REVIEWER",
        prompt="Review code changes with official shape",
        workspace_dir=str(clean_workspace),
        extra_context={"production_runner_managed": True, "cursor_model": "composer-2.5"},
    )
    official_json = '{"task_id": "T0001", "decision": "PASS", "defects": [], "summary": "Official schema pass"}'
    FakeCursorSdkState.set_next_messages([
        fake_cursor_sdk.SDKMessage(
            "assistant",
            message=fake_cursor_sdk.AssistantPayload(
                content=[
                    fake_cursor_sdk.TextBlock(text=official_json),
                ]
            ),
        ),
    ])
    handle_official = adapter.dispatch_agent(rev_official_req)
    result_official = adapter.wait_for_result(handle_official)
    assert result_official.status == AgentStatus.SUCCESS
    assert "Official schema pass" in result_official.output

    # 8. Official nested message shape: Nested ToolUseBlock with prohibited tool is intercepted
    rev_nested_tool_req = AgentRequest(
        session_id="test_rev_nested_tool_violation",
        role="REVIEWER",
        prompt="Review code with nested tool violation",
        workspace_dir=str(clean_workspace),
        extra_context={"production_runner_managed": True, "cursor_model": "composer-2.5"},
    )
    FakeCursorSdkState.set_next_messages([
        fake_cursor_sdk.SDKMessage(
            "assistant",
            message=fake_cursor_sdk.AssistantPayload(
                content=[
                    fake_cursor_sdk.ToolUseBlock(id="call_nested_edit", name="edit", input={"path": "secret.py"}),
                ]
            ),
        ),
    ])
    handle_nested = adapter.dispatch_agent(rev_nested_tool_req)
    result_nested = adapter.wait_for_result(handle_nested)
    assert result_nested.status == AgentStatus.FAILED
    assert "prohibited tool(s) ['edit']" in result_nested.error_message

    # 9. Verify boundary invariants:
    # - No .cursor directory was copied into clean_workspace
    assert not (clean_workspace / ".cursor").exists()

    # - Created Agent invariants
    session_data = adapter._session_history.get(handle_rev_ok.invocation_token)
    assert session_data is not None
    created_agent = session_data["agent"]
    assert created_agent.tools == ["read"]
    assert not created_agent.agents
    assert created_agent.local.setting_sources is None
    assert not created_agent.local.dirs


def test_live_e2e_cli_validations(monkeypatch, capsys):
    from scripts.run_cursor_sdk_live_e2e import main as live_e2e_main

    # 1. Missing --cursor-model
    monkeypatch.setattr(sys, "argv", ["run_cursor_sdk_live_e2e.py"])
    rc = live_e2e_main()
    assert rc == 1
    err = capsys.readouterr().err
    assert "--cursor-model is required" in err

    # 2. Missing CURSOR_API_KEY
    monkeypatch.delenv("CURSOR_API_KEY", raising=False)
    monkeypatch.setattr(sys, "argv", ["run_cursor_sdk_live_e2e.py", "--cursor-model", "composer-2.5"])
    rc = live_e2e_main()
    assert rc == 1
    err = capsys.readouterr().err
    assert "Cursor API key environment variable" in err

    # 3. Invalid --task-id
    monkeypatch.setenv("CURSOR_API_KEY", "test_key")
    monkeypatch.setattr(sys, "argv", ["run_cursor_sdk_live_e2e.py", "--cursor-model", "composer-2.5", "--task-id", "T_LIVE_CURSOR_01"])
    rc = live_e2e_main()
    assert rc == 1
    err = capsys.readouterr().err
    assert "Invalid --task-id" in err


def test_live_e2e_setup_fixture_and_spec_loading():
    from scripts.run_cursor_sdk_live_e2e import _setup_isolated_fixture, _safe_rmtree
    from scripts._lib.core.task_spec_loader import load_task_execution_spec

    temp_repo = _setup_isolated_fixture("T99999")
    try:
        assert os.path.isdir(temp_repo)
        assert os.path.isfile(os.path.join(temp_repo, "calc.py"))
        assert os.path.isfile(os.path.join(temp_repo, "tests", "test_calc.py"))
        assert os.path.isfile(os.path.join(temp_repo, "config", "workflow.config.yaml"))
        assert os.path.isfile(os.path.join(temp_repo, "user_data", "board.json"))

        spec = load_task_execution_spec(
            project_root=temp_repo,
            task_id="T99999",
            authority_root=temp_repo,
            overrides={"test_commands": ("python -m pytest tests/test_calc.py",)},
        )
        assert spec.task_id == "T99999"
        assert len(spec.acceptance_criteria_items) >= 2
        assert "add(a, b)" in spec.acceptance_criteria
    finally:
        _safe_rmtree(temp_repo)
        assert not os.path.exists(temp_repo)


def test_live_e2e_execution_terminal_state_and_cleanup(monkeypatch):
    from scripts.run_cursor_sdk_live_e2e import main as live_e2e_main

    monkeypatch.setenv("CURSOR_API_KEY", "test_key")
    created_fixtures = []

    real_setup = __import__("scripts.run_cursor_sdk_live_e2e", fromlist=["_setup_isolated_fixture"])._setup_isolated_fixture

    def tracked_setup(task_id):
        repo = real_setup(task_id)
        created_fixtures.append(repo)
        return repo

    monkeypatch.setattr("scripts.run_cursor_sdk_live_e2e._setup_isolated_fixture", tracked_setup)

    # Case A: Pipeline exits with failure code -> returns 1 and cleans up fixture
    with patch("subprocess.run") as mock_run:
        mock_run.return_value = subprocess.CompletedProcess(
            args=["run_task.py"],
            returncode=1,
            stdout=json.dumps({"success": False, "state": "BUILDING"}),
            stderr="Simulated failure",
        )
        monkeypatch.setattr(sys, "argv", ["run_cursor_sdk_live_e2e.py", "--cursor-model", "composer-2.5", "--task-id", "T99999"])
        rc = live_e2e_main()
        assert rc == 1
        assert len(created_fixtures) == 1
        assert not os.path.exists(created_fixtures[0])

    # Case B: Pipeline succeeds but terminal state is not PENDING_USER_ACCEPTANCE -> returns 1 and cleans up
    with patch("subprocess.run") as mock_run:
        mock_run.return_value = subprocess.CompletedProcess(
            args=["run_task.py"],
            returncode=0,
            stdout=json.dumps({"success": True, "state": "COMPLETED", "candidate_commit": "abc", "evidence_ids": []}),
            stderr="",
        )
        monkeypatch.setattr(sys, "argv", ["run_cursor_sdk_live_e2e.py", "--cursor-model", "composer-2.5", "--task-id", "T99999"])
        rc = live_e2e_main()
        assert rc == 1
        assert len(created_fixtures) == 2
        assert not os.path.exists(created_fixtures[1])

    # Case C: Pipeline succeeds and terminal state is PENDING_USER_ACCEPTANCE -> returns 0 and cleans up
    with patch("subprocess.run") as mock_run:
        mock_run.return_value = subprocess.CompletedProcess(
            args=["run_task.py"],
            returncode=0,
            stdout=json.dumps({"success": True, "state": "PENDING_USER_ACCEPTANCE", "candidate_commit": "abc", "evidence_ids": ["evi-1"]}),
            stderr="",
        )
        monkeypatch.setattr(sys, "argv", ["run_cursor_sdk_live_e2e.py", "--cursor-model", "composer-2.5", "--task-id", "T99999"])
        rc = live_e2e_main()
        assert rc == 0
        assert len(created_fixtures) == 3
        assert not os.path.exists(created_fixtures[2])

    # Case D: --keep-fixture preserves directory
    with patch("subprocess.run") as mock_run:
        mock_run.return_value = subprocess.CompletedProcess(
            args=["run_task.py"],
            returncode=0,
            stdout=json.dumps({"success": True, "state": "PENDING_USER_ACCEPTANCE", "candidate_commit": "abc", "evidence_ids": ["evi-1"]}),
            stderr="",
        )
        monkeypatch.setattr(sys, "argv", ["run_cursor_sdk_live_e2e.py", "--cursor-model", "composer-2.5", "--task-id", "T99999", "--keep-fixture"])
        rc = live_e2e_main()
        assert rc == 0
        assert len(created_fixtures) == 4
        assert os.path.exists(created_fixtures[3])
        # Manually cleanup fixture 4
        from scripts.run_cursor_sdk_live_e2e import _safe_rmtree
        _safe_rmtree(created_fixtures[3])
        assert not os.path.exists(created_fixtures[3])





