# -*- coding: utf-8 -*-
"""DEF-T0034-5..9：真实 ProductionRunner 方法与检查点，宿主为测试双重。"""
import json
import os
import subprocess
import sys
from pathlib import Path

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
    state = {"builder_calls": 0, "verifier_calls": 0, "prompts": [], "builder_prompts": []}

    def detect(self):
        return _caps()

    def codex_dispatch(self, request):
        state["builder_prompts"].append(request.prompt)
        return _handle(request, "codex_cli")

    def codex_wait(self, handle, timeout_seconds=None):
        state["builder_calls"] += 1
        name = f"calc_{state['builder_calls']}.py"
        text = builder_text(state) if callable(builder_text) else builder_text
        (repo / name).write_text(text, encoding="utf-8")
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
    assert checkpoint.active_elapsed_seconds > 0
    from dataclasses import replace
    store.save_checkpoint(replace(checkpoint, active_elapsed_seconds=0))
    assert store.load_checkpoint("T9101").active_elapsed_seconds == checkpoint.active_elapsed_seconds
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


@pytest.mark.parametrize("partial_edit", [False, True])
def test_first_builder_transport_failure_can_resume_without_candidate(tmp_path, monkeypatch, partial_edit):
    repo = _prepare_repo(tmp_path, "T9118")
    hosts = _install_hosts(monkeypatch, repo, PURE, lambda state, handle: _pass_report(state["prompts"][-1]))
    successful_wait = CodexCliAdapter.wait_for_result

    def interrupted(self, handle, timeout_seconds=None):
        if partial_edit:
            (repo / "calc_1.py").write_text(PURE, encoding="utf-8")
        return AgentResult(
            session_id=handle.session_id, status=AgentStatus.FAILED,
            output="",
            error_message="Reconnecting... 5/5 (request timed out)",
            partial_results=({"invocation_id": "real_failed_turn"},), is_real_host=True,
        )

    monkeypatch.setattr(CodexCliAdapter, "wait_for_result", interrupted)
    runner, store = _runner(repo)
    failed = runner.start(_spec(repo, "T9118", "lite"))
    assert failed.state == RunnerState.NEEDS_USER_INPUT.value
    assert store.load_checkpoint("T9118").candidate_commit is None
    assert store.load_checkpoint("T9118").builder_invocation_id == "real_failed_turn"
    monkeypatch.setattr(CodexCliAdapter, "wait_for_result", successful_wait)
    if partial_edit:
        blocked = runner.resume(str(repo), "T9118", authority_root=str(repo))
        assert not blocked.success
        assert hosts["builder_calls"] == 0
        assert (repo / "calc_1.py").read_text(encoding="utf-8") == PURE
    resumed = runner.resume(str(repo), "T9118", authority_root=str(repo),
                            overrides={"recover_partial_builder_changes": partial_edit})
    assert resumed.success, resumed.message
    assert resumed.state == RunnerState.PENDING_USER_ACCEPTANCE.value
    assert hosts["builder_calls"] == 1
    assert hosts["verifier_calls"] == 1


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


def _replace_test(repo, source, message):
    (repo / "test_app.py").write_text(source, encoding="utf-8")
    _git(repo, "add", "test_app.py")
    _git(repo, "commit", "-m", message)


def _count_pytest(monkeypatch):
    from scripts._lib.core import lightweight_profile as profile

    seen = []
    real = profile._run_lite_command

    def wrapped(argv, cwd, timeout):
        if any("pytest" in str(part) for part in argv):
            seen.append(subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=cwd, text=True).strip())
        return real(argv, cwd, timeout)

    monkeypatch.setattr(profile, "_run_lite_command", wrapped)
    return seen


def test_def15_classifier_keeps_assertion_failures_out_of_environment_pause():
    from scripts._lib.core.lightweight_profile import _lite_command_failure_kind

    assert _lite_command_failure_kind(
        "python -m pytest -q", 2,
        "E   ModuleNotFoundError: No module named 'pytest'\n",
    ) == "environment"
    assert _lite_command_failure_kind(
        "python -m pytest -q", 1,
        "E   ModuleNotFoundError: No module named 'requests'\n",
    ) == "environment"
    assert _lite_command_failure_kind(
        "python -m pytest -q", 1,
        "E   ConnectionRefusedError: [Errno 111] connection refused\nFAILED test_app.py::test_app\n",
    ) == "environment"
    assert _lite_command_failure_kind(
        "python -m pytest -q", 1,
        "E   TimeoutError: timed out\nFAILED test_app.py::test_app\n",
    ) == "environment"
    assert _lite_command_failure_kind(
        "python -m pytest -q", 1,
        "socket.gaierror: [Errno -2] Name or service not known\n",
    ) == "environment"
    assert _lite_command_failure_kind(
        "python -m pytest -q", 1,
        "python is not recognized as an internal or external command\n",
    ) == "environment"
    assert _lite_command_failure_kind(
        "python -m pytest -q", 124, "Runner command timed out\n",
    ) == "environment"
    asserted = (
        "E       AssertionError: assert 'connection refused' == 'handled'\n"
        "E        +  where 'connection refused' = ModuleNotFoundError\n"
        "FAILED test_app.py::test_app - AssertionError: assert 'connection refused' == 'handled'\n"
        "test_app.py:4: AssertionError\n"
    )
    assert _lite_command_failure_kind("python -m pytest -q", 1, asserted) == "business"
    assert _lite_command_failure_kind(
        "python -m pytest -q", 1,
        "FAILED test_app.py::test_app - AssertionError: assert 1 == 2\ntest_app.py:3: AssertionError\n",
    ) == "business"


@pytest.mark.parametrize("legacy_role", [False, True])
def test_risk_pause_resumes_candidate_checks_without_rebuilding(tmp_path, monkeypatch, legacy_role):
    from dataclasses import replace
    from scripts._lib.core import lightweight_profile as profile
    repo = _prepare_repo(tmp_path, "T9120")
    hosts = _install_hosts(monkeypatch, repo, PURE, lambda state, handle: _pass_report(state["prompts"][-1]))
    original = profile.evaluate_candidate_risk
    monkeypatch.setattr(profile, "evaluate_candidate_risk", lambda *a, **kw: {
        "stop_for_replan": True, "risk_level": "unknown", "risk_signals": [],
        "message": "风险不明确，停止轻量路径并重新计划",
    })
    runner, store = _runner(repo)
    result = runner.start(_spec(repo, "T9120", "lite"))
    assert not result.success
    cp = store.load_checkpoint("T9120")
    assert cp.current_role == "RUNNER"
    assert hosts["builder_calls"] == 1
    if legacy_role:
        store.save_checkpoint(replace(cp, current_role="BUILDER"))
    monkeypatch.setattr(profile, "evaluate_candidate_risk", original)
    result = runner.resume(str(repo), "T9120", authority_root=str(repo))
    assert result.success, result.message
    assert result.candidate_commit == cp.candidate_commit
    assert hosts["builder_calls"] == 1
    assert hosts["verifier_calls"] == 1


@pytest.mark.parametrize("reclaimed", [False, True])
def test_user_rejection_cannot_impersonate_legacy_risk_pause(tmp_path, monkeypatch, reclaimed):
    repo = _prepare_repo(tmp_path, "T9121")
    hosts = _install_hosts(monkeypatch, repo, PURE, lambda state, handle: _pass_report(state["prompts"][-1]))
    runner, store = _runner(repo)
    result = runner.start(_spec(repo, "T9121", "lite"))
    assert result.success
    rejected = runner.reject(str(repo), "T9121", result.confirmation_request_id,
                             "风险不明确，停止轻量路径并重新计划", authority_root=str(repo))
    assert rejected.success
    if reclaimed:
        ok, error = runner._do_state_transition(str(repo), "T9121", "DEV", "已退回", "进行中", "李开发", "领取修复", "A")
        assert ok, error
    resumed = runner.resume(str(repo), "T9121", authority_root=str(repo))
    assert hosts["builder_calls"] == 2
    assert resumed.success, resumed.message
    # The fixture Builder writes a new candidate on every repair. Crucially,
    # the rejected candidate is not simply checked and completed again.
    assert resumed.candidate_commit != result.candidate_commit
    assert resumed.confirmation_request_id != result.confirmation_request_id
    assert hosts["verifier_calls"] == 2


def test_def14_missing_dependency_pauses_check_and_resume_rechecks_same_candidate(tmp_path, monkeypatch):
    repo = _prepare_repo(tmp_path, "T9114")
    _replace_test(
        repo,
        "import missing_module_t0034_env\n\ndef test_app():\n    assert True\n",
        "test: missing dependency",
    )
    checks = _count_pytest(monkeypatch)
    hosts = _install_hosts(monkeypatch, repo, PURE, lambda state, handle: _pass_report(state["prompts"][-1]))
    runner, store = _runner(repo)
    result = runner.start(_spec(repo, "T9114", "lite"))
    checkpoint = store.load_checkpoint("T9114")
    assert result.success is False
    assert (result.diagnostics or {}).get("failure_kind") == "environment"
    assert result.state == RunnerState.NEEDS_USER_INPUT.value
    assert checkpoint.current_role == "RUNNER"
    assert checkpoint.current_role != "BUILDER"
    assert checkpoint.candidate_commit
    assert hosts["builder_calls"] == 1
    assert hosts["verifier_calls"] == 0
    assert _board_status(repo, "T9114") != "已退回"
    assert len(checks) == 1
    before = hosts["builder_calls"]
    candidate = checkpoint.candidate_commit
    resumed = runner.resume(str(repo), "T9114", authority_root=str(repo))
    again = store.load_checkpoint("T9114")
    assert hosts["builder_calls"] == before
    assert len(checks) >= 2
    assert checks[-1] == candidate
    assert resumed.candidate_commit == candidate
    assert again.current_role == "RUNNER"
    assert again.state == RunnerState.NEEDS_USER_INPUT.value
    assert (resumed.diagnostics or {}).get("failure_kind") == "environment"
    assert _board_status(repo, "T9114") != "已退回"
    assert hosts["verifier_calls"] == 0


def test_def15_connection_failure_pauses_check_stage(tmp_path, monkeypatch):
    repo = _prepare_repo(tmp_path, "T9115")
    _replace_test(
        repo,
        "def test_app():\n"
        "    import socket\n"
        "    socket.create_connection(('127.0.0.1', 9), timeout=0.2)\n",
        "test: closed local port",
    )
    hosts = _install_hosts(monkeypatch, repo, PURE, lambda state, handle: _pass_report(state["prompts"][-1]))
    runner, store = _runner(repo)
    result = runner.start(_spec(repo, "T9115", "lite"))
    checkpoint = store.load_checkpoint("T9115")
    assert hosts["builder_calls"] == 1
    assert hosts["verifier_calls"] == 0
    assert (result.diagnostics or {}).get("failure_kind") == "environment"
    assert checkpoint.current_role == "RUNNER"
    assert checkpoint.state == RunnerState.NEEDS_USER_INPUT.value
    assert _board_status(repo, "T9115") != "已退回"
    before = hosts["builder_calls"]
    candidate = result.candidate_commit
    resumed = runner.resume(str(repo), "T9115", authority_root=str(repo))
    assert hosts["builder_calls"] == before
    assert resumed.candidate_commit == candidate
    assert (resumed.diagnostics or {}).get("failure_kind") == "environment"
    assert store.load_checkpoint("T9115").current_role == "RUNNER"


def test_def15_asserted_environment_text_returns_to_builder(tmp_path, monkeypatch):
    repo = _prepare_repo(tmp_path, "T9117")
    _replace_test(
        repo,
        "def test_app():\n"
        "    assert 'connection refused' == 'handled'\n"
        "    assert 'ModuleNotFoundError' == 'handled'\n",
        "test: business assertion quotes environment text",
    )
    hosts = _install_hosts(monkeypatch, repo, PURE, lambda state, handle: _pass_report(state["prompts"][-1]))
    runner, store = _runner(repo)
    result = runner.start(_spec(repo, "T9117", "lite", max_total_attempts=2))
    checkpoint = store.load_checkpoint("T9117")
    assert hosts["builder_calls"] == 2
    assert hosts["verifier_calls"] == 0
    assert (result.diagnostics or {}).get("failure_kind") != "environment"
    assert checkpoint.current_role != "RUNNER"
    repair = hosts["builder_prompts"][1]
    assert "AssertionError" in repair
    assert "test_app.py" in repair
    assert "test_app" in repair
    assert "exit=" not in repair or "AssertionError" in repair


def test_def16_assertion_diagnostics_reach_builder_and_recheck_precedes_verifier(tmp_path, monkeypatch):
    repo = _prepare_repo(tmp_path, "T9118")
    _replace_test(
        repo,
        "def test_app():\n"
        "    leaked = 'postgres://user:password=supersecret@localhost/db'\n"
        "    from pathlib import Path\n"
        "    files = sorted(Path('.').glob('calc_*.py'))\n"
        "    assert files\n"
        "    namespace = {}\n"
        "    exec(files[-1].read_text(encoding='utf-8'), namespace)\n"
        "    assert namespace['add'](1, 2) == 3\n",
        "test: check generated add",
    )
    checks = _count_pytest(monkeypatch)

    def builder_text(state):
        if state["builder_calls"] == 1:
            return "def add(left, right):\n    return left - right\n"
        return PURE

    hosts = _install_hosts(
        monkeypatch, repo, builder_text, lambda state, handle: _pass_report(state["prompts"][-1]),
    )
    runner, _store = _runner(repo)
    result = runner.start(_spec(repo, "T9118", "lite"))
    assert result.success, result.message
    assert hosts["builder_calls"] == 2
    assert hosts["verifier_calls"] == 1
    assert len(checks) >= 2
    assert checks[0] != checks[-1]
    repair = hosts["builder_prompts"][1]
    assert "AssertionError" in repair
    assert "test_app.py" in repair
    assert "FAILED" in repair or "test_app.py::test_app" in repair
    assert "supersecret" not in repair
    assert "postgres://" not in repair
    assert "password=" not in repair
    verifier_prompt = hosts["prompts"][0]
    assert checks[-1] in verifier_prompt
    assert checks[0] not in verifier_prompt


def _bind_fake_hosts(repo, builder_text):
    state = {"builder_calls": 0, "verifier_calls": 0, "prompts": [], "builder_prompts": []}

    def detect(self):
        return _caps()

    def codex_dispatch(self, request):
        state["builder_prompts"].append(request.prompt)
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
        return AgentResult(
            session_id=handle.session_id,
            status=AgentStatus.SUCCESS,
            output=_pass_report(state["prompts"][-1]),
            partial_results=({"invocation_id": f"inv_verifier_{state['verifier_calls']}"},),
            is_real_host=True,
        )

    CodexCliAdapter.detect_capabilities = detect
    CodexCliAdapter.dispatch_agent = codex_dispatch
    CodexCliAdapter.wait_for_result = codex_wait
    AntigravityAdapter.detect_capabilities = detect
    AntigravityAdapter.dispatch_agent = ag_dispatch
    AntigravityAdapter.wait_for_result = ag_wait
    return state


def child_main(mode, repo_s, task_id):
    """Entry point for a fresh interpreter. Not a same-process function call."""
    repo = Path(repo_s)
    state = _bind_fake_hosts(repo, PURE)
    runner, store = _runner(repo)
    if mode == "pause":
        result = runner.start(_spec(repo, task_id, "lite"))
        checkpoint = store.load_checkpoint(task_id)
        payload = {
            "success": result.success,
            "state": result.state,
            "message": result.message,
            "candidate": result.candidate_commit,
            "role": None if checkpoint is None else checkpoint.current_role,
            "ckpt_state": None if checkpoint is None else checkpoint.state,
            "builder_calls": state["builder_calls"],
            "verifier_calls": state["verifier_calls"],
            "board": _board_status(repo, task_id),
            "failure_kind": (result.diagnostics or {}).get("failure_kind"),
        }
    elif mode == "resume":
        from scripts._lib.core import lightweight_profile as profile

        calls = {"n": 0}
        real = profile._run_lite_command

        def wrapped(argv, cwd, timeout):
            if any("pytest" in str(part) for part in argv):
                calls["n"] += 1
            return real(argv, cwd, timeout)

        profile._run_lite_command = wrapped
        result = runner.resume(str(repo), task_id, authority_root=str(repo))
        checkpoint = store.load_checkpoint(task_id)
        payload = {
            "success": result.success,
            "state": result.state,
            "message": result.message,
            "candidate": result.candidate_commit,
            "role": None if checkpoint is None else checkpoint.current_role,
            "builder_calls": state["builder_calls"],
            "verifier_calls": state["verifier_calls"],
            "check_calls": calls["n"],
            "board": _board_status(repo, task_id),
            "failure_kind": (result.diagnostics or {}).get("failure_kind"),
        }
    else:
        raise SystemExit(f"unknown mode {mode}")
    print("CHILD_RESULT " + json.dumps(payload, ensure_ascii=False))


def _run_child(mode, repo, task_id):
    project = Path(__file__).resolve().parents[1]
    completed = subprocess.run(
        [
            sys.executable,
            "-c",
            "from tests.test_t0034_lite_runner import child_main; "
            f"child_main({mode!r}, {str(repo)!r}, {task_id!r})",
        ],
        cwd=str(project),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=180,
    )
    marker = "CHILD_RESULT "
    line = next((item for item in reversed(completed.stdout.splitlines()) if item.startswith(marker)), "")
    payload = json.loads(line[len(marker):]) if line else {}
    payload["exit_code"] = completed.returncode
    payload["stderr_tail"] = (completed.stderr or "")[-2500:]
    payload["stdout_tail"] = (completed.stdout or "")[-1500:]
    return payload


def test_def14_cross_process_resume_reruns_checks_without_builder(tmp_path):
    repo = _prepare_repo(tmp_path, "T9116")
    _replace_test(
        repo,
        "import missing_module_t0034_cross\n\ndef test_app():\n    assert True\n",
        "test: cross process missing dependency",
    )
    first = _run_child("pause", repo, "T9116")
    assert first["exit_code"] == 0, first.get("stderr_tail")
    assert first["builder_calls"] == 1
    assert first["verifier_calls"] == 0
    assert first["failure_kind"] == "environment"
    assert first["role"] == "RUNNER"
    assert first["board"] != "已退回"
    assert first["candidate"]
    second = _run_child("resume", repo, "T9116")
    assert second["exit_code"] == 0, second.get("stderr_tail")
    assert second["builder_calls"] == 0
    assert second["check_calls"] >= 1
    assert second["candidate"] == first["candidate"]
    assert second["role"] == "RUNNER"
    assert second["failure_kind"] == "environment"
    assert second["board"] != "已退回"
    assert second["verifier_calls"] == 0
