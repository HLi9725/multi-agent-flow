# -*- coding: utf-8 -*-
"""DEF-T0034-5..9：真实 ProductionRunner 方法与检查点，宿主为测试双重。"""
import json
import os
import subprocess

import pytest
import yaml

from scripts._lib.core.adapter_registry import AdapterRegistry
from scripts._lib.core.agent_schema import (
    AgentHandle,
    AgentResult,
    AgentStatus,
    CapabilitySupport,
    HostCapabilities,
)
from scripts._lib.core.evidence_gate import EvidenceGate
from scripts._lib.core.evidence_store import EvidenceStore
from scripts._lib.core.production_runner import ProductionRunner
from scripts._lib.core.runner_checkpoint_store import RunnerCheckpointStore
from scripts._lib.core.lightweight_profile import _parse_verifier
from scripts._lib.core.runner_schema import RunnerCheckpoint, RunnerState
from scripts._lib.core.task_spec_loader import load_task_execution_spec
from scripts._lib.core.workflow_policy import PolicyError, select_profile
from scripts._lib.hosts.antigravity_adapter import AntigravityAdapter, create_antigravity_manifest
from scripts._lib.hosts.codex_cli_adapter import CodexCliAdapter, create_codex_cli_manifest


PURE = "def add(left, right):\n    return left + right\n"
DOC = """---
title: 说明
type: guide
---

只补充使用说明。
"""


def _git(repo, *args):
    subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True)


def _prepare_repo(tmp_path, task_id):
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init")
    _git(repo, "config", "user.name", "Dev")
    _git(repo, "config", "user.email", "dev@example.com")
    (repo / ".gitignore").write_text(
        "user_data/\nrunner_checkpoints/\n__pycache__/\n.pytest_cache/\n*.pyc\n",
        encoding="utf-8",
    )
    (repo / "test_app.py").write_text("def test_app():\n    assert True\n", encoding="utf-8")
    (repo / "README.md").write_text("# demo\n", encoding="utf-8")
    config = repo / "config"
    config.mkdir()
    user_data = repo / "user_data"
    user_data.mkdir()
    board = user_data / "board.json"
    yaml.safe_dump(
        {"board": {"provider": "local", "board_file": str(board)}},
        (config / "workflow.config.yaml").open("w", encoding="utf-8"),
    )
    _git(repo, "add", ".gitignore", "test_app.py", "README.md", "config/workflow.config.yaml")
    _git(repo, "commit", "-m", "init")
    board.write_text(json.dumps([{
        "id": task_id,
        "name": "整数相加",
        "status": "待开始",
        "type": "A",
        "owner": "李开发",
        "handler": "李开发",
        "updated_at": "1.0",
        "process": "需求: 新增整数相加函数。\n验收标准:\n- add(1, 2) 等于 3\n- add(0, 0) 等于 0",
    }], ensure_ascii=False), encoding="utf-8")
    return repo


def _caps():
    return HostCapabilities(
        is_real_host=True,
        supports_real_subagents=CapabilitySupport.SUPPORTED,
        supports_parallelism=CapabilitySupport.SUPPORTED,
        supports_isolated_context=CapabilitySupport.SUPPORTED,
        supports_worktree=CapabilitySupport.SUPPORTED,
        supports_permission_approval=CapabilitySupport.SUPPORTED,
        supports_mcp=CapabilitySupport.SUPPORTED,
        supports_interactive_confirmation=CapabilitySupport.UNSUPPORTED,
        supports_usage_telemetry=CapabilitySupport.SUPPORTED,
        max_concurrent_agents=2,
    )


def _handle(request, host_id):
    return AgentHandle(
        session_id=request.session_id,
        host_id=host_id,
        status="completed",
        is_real_host=True,
        adapter_instance_id="inst_host",
        invocation_token="tok_host_1234567890",
    )


def _pass_report(prompt):
    marker = "runner_checks:\n"
    body = prompt.split(marker, 1)[1].split("\nReturn JSON", 1)[0]
    checks = json.loads(body)
    sha = prompt.split("candidate_commit: ", 1)[1].split("\n", 1)[0].strip()
    ac_hash = prompt.split("acceptance_criteria_hash: ", 1)[1].split("\n", 1)[0].strip()
    items_raw = prompt.split("acceptance_criteria_items:\n", 1)[1].split("\nrunner_checks:", 1)[0]
    items = json.loads(items_raw)
    return json.dumps({
        "decision": "PASS",
        "candidate_commit": sha,
        "acceptance_criteria_hash": ac_hash,
        "acceptance_coverage": [
            {"criterion_id": item["criterion_id"], "status": "PASS"} for item in items
        ],
        "runner_checks": checks,
    }, ensure_ascii=False)


def _install_hosts(monkeypatch, repo, builder_text, verifier_output):
    state = {"builder_calls": 0, "verifier_calls": 0, "prompts": []}

    def detect(self):
        return _caps()

    def codex_dispatch(self, request):
        return _handle(request, "codex_cli")

    def codex_wait(self, handle, timeout_seconds=None):
        state["builder_calls"] += 1
        name = f"calc_{state['builder_calls']}.py"
        (repo / name).write_text(builder_text, encoding="utf-8")
        subprocess.run(["git", "add", name], cwd=repo, check=True, capture_output=True)
        subprocess.run(
            ["git", "commit", "-m", f"feat: add calc {state['builder_calls']}"],
            cwd=repo, check=True, capture_output=True,
        )
        return AgentResult(
            session_id=handle.session_id,
            status=AgentStatus.SUCCESS,
            output="builder wrote a local function",
            partial_results=({"invocation_id": f"inv_builder_{state['builder_calls']}"},),
            is_real_host=True,
        )

    def ag_dispatch(self, request):
        state["prompts"].append(request.prompt)
        return _handle(request, "antigravity")

    def ag_wait(self, handle, timeout_seconds=None):
        state["verifier_calls"] += 1
        output = verifier_output(state, handle)
        if isinstance(output, BaseException):
            raise output
        return AgentResult(
            session_id=handle.session_id,
            status=AgentStatus.SUCCESS,
            output=output,
            partial_results=({"invocation_id": f"inv_verifier_{state['verifier_calls']}"},),
            is_real_host=True,
        )

    monkeypatch.setattr(CodexCliAdapter, "detect_capabilities", detect)
    monkeypatch.setattr(CodexCliAdapter, "dispatch_agent", codex_dispatch)
    monkeypatch.setattr(CodexCliAdapter, "wait_for_result", codex_wait)
    monkeypatch.setattr(AntigravityAdapter, "detect_capabilities", detect)
    monkeypatch.setattr(AntigravityAdapter, "dispatch_agent", ag_dispatch)
    monkeypatch.setattr(AntigravityAdapter, "wait_for_result", ag_wait)
    return state


def _runner(repo):
    evidence = EvidenceStore(root_dir=str(repo / "user_data" / "runner_evidence"))
    registry = AdapterRegistry(context_id="lite_runner")
    registry.register(CodexCliAdapter(is_real_host=True), create_codex_cli_manifest(adapter_id="codex_cli", verified_version="0.149.0"))
    registry.register(AntigravityAdapter(is_real_host=True), create_antigravity_manifest(adapter_id="antigravity", verified_version="1.1.22"))
    store = RunnerCheckpointStore(data_root=str(repo), project_root=str(repo), project_id="repo")
    return ProductionRunner(
        registry=registry,
        evidence_store=evidence,
        evidence_gate=EvidenceGate(store=evidence, project_root=str(repo)),
        checkpoint_store=store,
    ), store


def _spec(repo, task_id, profile, **extra):
    overrides = {
        "workspace_mode": "inherit",
        "workflow_profile": profile,
        "test_commands": ("python -m pytest -q test_app.py",),
        "host_transient_max_retries": 0,
        "host_transient_retry_base_seconds": 0,
        "host_transient_retry_max_seconds": 0,
        "builder_timeout_seconds": 60,
        "reviewer_timeout_seconds": 60,
        "qa_timeout_seconds": 90,
    }
    overrides.update(extra)
    return load_task_execution_spec(
        str(repo), task_id, authority_root=str(repo), overrides=overrides,
    )


def _board_status(repo, task_id):
    board = json.loads((repo / "user_data" / "board.json").read_text(encoding="utf-8"))
    return next(item["status"] for item in board if item["id"] == task_id)


def test_lite_runner_reaches_completed_and_accepts_only_server_confirmation(tmp_path, monkeypatch):
    repo = _prepare_repo(tmp_path, "T9101")
    _install_hosts(monkeypatch, repo, PURE, lambda state, handle: _pass_report(state["prompts"][-1]))
    runner, store = _runner(repo)
    result = runner.start(_spec(repo, "T9101", "lite"))
    assert result.success, result.message
    assert result.state == RunnerState.PENDING_USER_ACCEPTANCE.value
    assert result.confirmation_request_id
    assert result.confirmation_request_id.startswith("conf_req_t9101_")
    assert _board_status(repo, "T9101") == "已完成"
    checkpoint = store.load_checkpoint("T9101")
    assert checkpoint.confirmation_request_id == result.confirmation_request_id
    assert not any(item.startswith("evi_reviewer_") or item.startswith("evi_qa_") for item in result.evidence_ids)
    wrong = runner.accept(str(repo), "T9101", "conf_req_wrong", authority_root=str(repo))
    assert wrong.success is False
    assert "mismatch" in wrong.message.lower() or "Fail-Closed" in wrong.message
    assert store.load_checkpoint("T9101").state == RunnerState.PENDING_USER_ACCEPTANCE.value
    accepted = runner.accept(str(repo), "T9101", result.confirmation_request_id, authority_root=str(repo))
    assert accepted.success, accepted.message
    assert accepted.state == RunnerState.ACCEPTED.value
    assert _board_status(repo, "T9101") == "已验收"


def test_lite_runner_reject_returns_original_task_to_builder(tmp_path, monkeypatch):
    repo = _prepare_repo(tmp_path, "T9102")
    hosts = _install_hosts(monkeypatch, repo, PURE, lambda state, handle: _pass_report(state["prompts"][-1]))
    runner, store = _runner(repo)
    result = runner.start(_spec(repo, "T9102", "lite"))
    assert result.success, result.message
    rejected = runner.reject(
        str(repo), "T9102", result.confirmation_request_id, "验收发现结果不符", authority_root=str(repo),
    )
    assert rejected.success, rejected.message
    assert rejected.task_id == "T9102"
    assert _board_status(repo, "T9102") == "已退回"
    checkpoint = store.load_checkpoint("T9102")
    assert checkpoint.current_role == "BUILDER"
    assert any(item.get("role") == "USER_ACCEPTANCE" for item in checkpoint.defects_history)
    before = hosts["builder_calls"]
    resumed = runner.resume(str(repo), "T9102", authority_root=str(repo))
    assert resumed.task_id == "T9102", resumed.message
    assert hosts["builder_calls"] > before, resumed.message
    assert store.load_checkpoint("T9102").total_attempts >= checkpoint.total_attempts


def test_lite_runner_verifying_interrupt_resumes_verifier(tmp_path, monkeypatch):
    repo = _prepare_repo(tmp_path, "T9103")

    def verifier_output(state, handle):
        if state["verifier_calls"] == 1:
            return SystemExit("verifier interrupted")
        return _pass_report(state["prompts"][-1])

    hosts = _install_hosts(monkeypatch, repo, PURE, verifier_output)
    runner, store = _runner(repo)
    with pytest.raises(SystemExit):
        runner.start(_spec(repo, "T9103", "lite"))
    interrupted = store.load_checkpoint("T9103")
    assert interrupted.state == RunnerState.VERIFYING.value
    assert interrupted.current_role == "VERIFIER"
    assert interrupted.total_attempts >= 1
    assert interrupted.candidate_commit
    builders_after_interrupt = hosts["builder_calls"]
    resumed = runner.resume(str(repo), "T9103", authority_root=str(repo))
    assert resumed.success, resumed.message
    assert resumed.confirmation_request_id
    assert hosts["builder_calls"] == builders_after_interrupt
    assert hosts["verifier_calls"] >= 2
    assert store.load_checkpoint("T9103").total_attempts >= interrupted.total_attempts


def test_lite_runner_decision_only_pass_cannot_complete(tmp_path, monkeypatch):
    repo = _prepare_repo(tmp_path, "T9104")
    _install_hosts(monkeypatch, repo, PURE, lambda state, handle: '{"decision":"PASS"}')
    runner, store = _runner(repo)
    result = runner.start(_spec(repo, "T9104", "lite"))
    assert result.success is False
    assert result.state != RunnerState.PENDING_USER_ACCEPTANCE.value
    assert not result.confirmation_request_id
    assert _board_status(repo, "T9104") != "已完成"
    assert store.load_checkpoint("T9104").state != RunnerState.PENDING_USER_ACCEPTANCE.value


def test_docs_short_runner_gets_server_confirmation_id(tmp_path, monkeypatch):
    repo = _prepare_repo(tmp_path, "T9105")
    _install_hosts(monkeypatch, repo, DOC, lambda state, handle: '{"decision":"PASS"}')
    # Builder writes the document; the verifier double must not be required.
    hosts = {"builder_calls": 0}

    def codex_wait(self, handle, timeout_seconds=None):
        hosts["builder_calls"] += 1
        path = repo / "docs" / "note.md"
        path.parent.mkdir(exist_ok=True)
        path.write_text(DOC, encoding="utf-8")
        subprocess.run(["git", "add", "docs/note.md"], cwd=repo, check=True, capture_output=True)
        subprocess.run(["git", "commit", "-m", "docs: add note"], cwd=repo, check=True, capture_output=True)
        return AgentResult(
            session_id=handle.session_id,
            status=AgentStatus.SUCCESS,
            output="builder wrote a document",
            partial_results=({"invocation_id": "inv_builder_docs"},),
            is_real_host=True,
        )

    monkeypatch.setattr(CodexCliAdapter, "wait_for_result", codex_wait)
    runner, store = _runner(repo)
    result = runner.start(_spec(repo, "T9105", "docs-short"))
    assert result.success, result.message
    assert result.confirmation_request_id.startswith("conf_req_t9105_")
    assert store.load_checkpoint("T9105").confirmation_request_id == result.confirmation_request_id
    assert _board_status(repo, "T9105") == "已完成"
    wrong = runner.accept(str(repo), "T9105", "conf_req_wrong", authority_root=str(repo))
    assert wrong.success is False


def test_can_access_return_true_blocks_lite_and_unclear_code_stays_standard():
    opened = select_profile(
        task_type="A",
        requested="lite",
        files=[{"path": "auth.py", "content": "def can_access(user, resource):\n    return True\n"}],
        title="低风险",
        user_claims_low_risk=True,
    )
    assert opened.blocked is True
    assert opened.risk_level == "high"
    assert any(item["category"] == "permission" for item in opened.risk_signals)
    unclear = select_profile(
        task_type="A",
        requested="auto",
        files=[{"path": "app.py", "content": "def ready():\n    return True\n"}],
        title="低风险",
        user_claims_low_risk=True,
    )
    assert unclear.profile_id == "standard"
    assert unclear.risk_level == "unclear"
    assert unclear.risk_signals == []
    kept = select_profile(
        task_type="A",
        requested="standard",
        files=[{"path": "app.py", "content": "def ready():\n    return True\n"}],
    )
    assert kept.profile_id == "standard"
    assert kept.blocked is False


_RUNNER_CHECKS = {
    "diff_check": {"exit_code": 0, "passed": True, "summary": "clean"},
    "security_scan": {"exit_code": 0, "passed": True, "summary": "clean"},
    "tests": [{"command": "python -m pytest -q", "exit_code": 0, "summary": "1 passed"}],
}
_AC_HASH = "a" * 64


def _verifier_pass(coverage, ac_hash=_AC_HASH, candidate="abc"):
    return json.dumps({
        "decision": "PASS",
        "candidate_commit": candidate,
        "acceptance_criteria_hash": ac_hash,
        "acceptance_coverage": coverage,
        "runner_checks": _RUNNER_CHECKS,
    }, ensure_ascii=False)


def _parse_pass(coverage, expected_ids=("AC-01", "AC-02"), ac_hash=_AC_HASH):
    return _parse_verifier(
        _verifier_pass(coverage, ac_hash=ac_hash),
        candidate="abc",
        runner_checks=_RUNNER_CHECKS,
        acceptance_criteria_hash=ac_hash,
        expected_criterion_ids=expected_ids,
    )


def test_made_up_duplicate_missing_and_unknown_coverage_cannot_pass():
    with pytest.raises(PolicyError):
        _parse_pass([{"criterion_id": "MADE_UP", "status": "PASS"}])
    with pytest.raises(PolicyError):
        _parse_pass([{"criterion_id": "AC-01", "status": "PASS"}])
    with pytest.raises(PolicyError):
        _parse_pass([
            {"criterion_id": "AC-01", "status": "PASS"},
            {"criterion_id": "AC-01", "status": "PASS"},
        ])
    with pytest.raises(PolicyError):
        _parse_pass([
            {"criterion_id": "AC-01", "status": "PASS"},
            {"criterion_id": "AC-02", "status": "PASS"},
            {"criterion_id": "MADE_UP", "status": "PASS"},
        ])
    with pytest.raises(PolicyError):
        _parse_pass(
            [{"criterion_id": "MADE_UP", "status": "PASS"}],
            expected_ids=(),
            ac_hash="",
        )
    with pytest.raises(PolicyError):
        _parse_verifier(
            _verifier_pass([
                {"criterion_id": "AC-01", "status": "PASS"},
                {"criterion_id": "AC-02", "status": "PASS"},
            ]),
            candidate="abc",
            runner_checks=_RUNNER_CHECKS,
            acceptance_criteria_hash="b" * 64,
            expected_criterion_ids=("AC-01", "AC-02"),
        )
    parsed = _parse_pass([
        {"criterion_id": "AC-02", "status": "PASS"},
        {"criterion_id": "AC-01", "status": "PASS"},
    ])
    assert parsed["decision"] == "PASS"
    assert parsed["acceptance_criteria_hash"] == _AC_HASH


def test_lite_made_up_coverage_cannot_complete(tmp_path, monkeypatch):
    repo = _prepare_repo(tmp_path, "T9110")

    def verifier_output(state, handle):
        prompt = state["prompts"][-1]
        sha = prompt.split("candidate_commit: ", 1)[1].split("\n", 1)[0].strip()
        checks = json.loads(prompt.split("runner_checks:\n", 1)[1].split("\nReturn JSON", 1)[0])
        ac_hash = ""
        if "acceptance_criteria_hash:" in prompt:
            ac_hash = prompt.split("acceptance_criteria_hash: ", 1)[1].split("\n", 1)[0].strip()
        return json.dumps({
            "decision": "PASS",
            "candidate_commit": sha,
            "acceptance_criteria_hash": ac_hash,
            "acceptance_coverage": [{"criterion_id": "MADE_UP", "status": "PASS"}],
            "runner_checks": checks,
        }, ensure_ascii=False)

    hosts = _install_hosts(monkeypatch, repo, PURE, verifier_output)
    runner, store = _runner(repo)
    result = runner.start(_spec(repo, "T9110", "lite"))
    assert result.success is False
    assert result.state != RunnerState.PENDING_USER_ACCEPTANCE.value
    assert not result.confirmation_request_id
    assert _board_status(repo, "T9110") != "已完成"
    assert store.load_checkpoint("T9110").state != RunnerState.PENDING_USER_ACCEPTANCE.value
    prompt = hosts["prompts"][-1]
    assert "acceptance_criteria_hash:" in prompt
    assert "acceptance_criteria_items:" in prompt
    assert "MADE_UP" not in prompt


def test_new_candidate_rejects_previous_confirmation_request_id(tmp_path, monkeypatch):
    repo = _prepare_repo(tmp_path, "T9111")
    hosts = _install_hosts(monkeypatch, repo, PURE, lambda state, handle: _pass_report(state["prompts"][-1]))
    runner, store = _runner(repo)
    first = runner.start(_spec(repo, "T9111", "lite"))
    assert first.success, first.message
    old_id = first.confirmation_request_id
    old_commit = first.candidate_commit
    rejected = runner.reject(str(repo), "T9111", old_id, "验收发现结果不符", authority_root=str(repo))
    assert rejected.success, rejected.message
    assert store.load_checkpoint("T9111").confirmation_request_id in (None, "")
    resumed = runner.resume(str(repo), "T9111", authority_root=str(repo))
    assert resumed.success, resumed.message
    assert resumed.state == RunnerState.PENDING_USER_ACCEPTANCE.value
    assert resumed.candidate_commit != old_commit
    assert resumed.confirmation_request_id
    assert resumed.confirmation_request_id != old_id
    denied_accept = runner.accept(str(repo), "T9111", old_id, authority_root=str(repo))
    assert denied_accept.success is False
    denied_reject = runner.reject(str(repo), "T9111", old_id, "旧编号不能再退回", authority_root=str(repo))
    assert denied_reject.success is False
    assert store.load_checkpoint("T9111").state == RunnerState.PENDING_USER_ACCEPTANCE.value
    accepted = runner.accept(str(repo), "T9111", resumed.confirmation_request_id, authority_root=str(repo))
    assert accepted.success, accepted.message
    assert accepted.state == RunnerState.ACCEPTED.value
    assert hosts["builder_calls"] >= 2


def test_confirmation_id_reused_only_for_same_candidate_completion(tmp_path):
    repo = _prepare_repo(tmp_path, "T9112")
    runner, _store = _runner(repo)
    spec = _spec(repo, "T9112", "lite")
    sha = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=repo, text=True).strip()
    old_id = "conf_req_t9112_samecandidate"

    def enter(state, candidate, stored_candidate):
        checkpoint = RunnerCheckpoint(
            task_id="T9112",
            project_id=spec.project_id,
            state=state,
            current_role="VERIFIER",
            candidate_commit=stored_candidate,
            confirmation_request_id=old_id,
        )
        return runner._enter_user_acceptance(
            spec,
            checkpoint,
            evidence_ids=(),
            board_status="已完成",
            transition_role="VERIFIER",
            remarks="completion recovery",
            success_message="waiting",
            candidate_commit=candidate,
        )

    reused = enter("COMPLETION_PENDING", sha, sha)
    assert reused.confirmation_request_id == old_id
    other = "b" * 40
    fresh = enter("COMPLETION_PENDING", other, sha)
    assert fresh.success, fresh.message
    assert fresh.confirmation_request_id != old_id
    stale = enter("NEEDS_USER_INPUT", sha, sha)
    assert stale.confirmation_request_id != old_id


def test_default_argument_open_is_not_low_and_cannot_enter_lite():
    pure = select_profile(
        task_type="A",
        requested="lite",
        files=[{"path": "calc.py", "content": "def add(a, b=1):\n    return a + b\n"}],
        requirement_hash="req",
        acceptance_criteria_hash="ac",
    )
    assert pure.risk_level == "low"
    assert pure.blocked is False
    assert pure.profile_id == "lite"
    samples = (
        'def add(a, b=open("output.txt", "w")):\n    return a + b\n',
        'def add(a, *, b=open("output.txt", "w")):\n    return a + b\n',
        'def add(a: open("output.txt")):\n    return a\n',
        'def add(a) -> open("output.txt"):\n    return a\n',
        '@open("output.txt")\ndef add(a, b):\n    return a + b\n',
    )
    for source in samples:
        opened = select_profile(
            task_type="A",
            requested="lite",
            files=[{"path": "calc.py", "content": source}],
            title="低风险",
            user_claims_low_risk=True,
            requirement_hash="req",
            acceptance_criteria_hash="ac",
        )
        assert opened.risk_level == "unclear", source
        assert opened.blocked is True, source
        auto = select_profile(
            task_type="A",
            requested="auto",
            files=[{"path": "calc.py", "content": source}],
            title="低风险",
            user_claims_low_risk=True,
        )
        assert auto.profile_id == "standard", source
        assert auto.risk_level == "unclear", source


def test_lite_failing_controlled_test_does_not_dispatch_verifier(tmp_path, monkeypatch):
    repo = _prepare_repo(tmp_path, "T9113")
    (repo / "test_app.py").write_text("def test_app():\n    assert False\n", encoding="utf-8")
    _git(repo, "add", "test_app.py")
    _git(repo, "commit", "-m", "test: record a failing check")
    hosts = _install_hosts(monkeypatch, repo, PURE, lambda state, handle: _pass_report(state["prompts"][-1]))
    runner, store = _runner(repo)
    result = runner.start(_spec(repo, "T9113", "lite", max_total_attempts=1))
    assert hosts["verifier_calls"] == 0
    assert hosts["prompts"] == []
    assert result.success is False
    assert result.state != RunnerState.PENDING_USER_ACCEPTANCE.value
    assert "协议" not in (result.message or "")
    assert (result.diagnostics or {}).get("failure_kind") != "protocol"
    assert _board_status(repo, "T9113") != "已完成"
    checkpoint = store.load_checkpoint("T9113")
    assert checkpoint.state != RunnerState.PENDING_USER_ACCEPTANCE.value
    blob = json.dumps(list(checkpoint.defects_history), ensure_ascii=False)
    assert "测试" in blob or "exit" in blob or "pytest" in (checkpoint.last_error or "")
