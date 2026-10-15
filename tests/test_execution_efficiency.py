"""Efficiency must not weaken quality, identity, or command boundaries."""
from pathlib import Path
import subprocess
import threading

import pytest

from scripts._lib.core.command_observation import observe_command
from scripts.build_agent_context import build_context
from scripts._lib.core.rule_source import load_safety_text

ROOT = Path(__file__).resolve().parents[1]


def test_heartbeat_reports_time_and_stops_on_exit():
    observed = []
    notified = threading.Event()

    def waiting(elapsed):
        observed.append(elapsed)
        notified.set()

    with observe_command(waiting, interval_seconds=0.01) as timing:
        assert notified.wait(2)
    assert observed and all(value >= 0 for value in observed)
    assert timing.duration_seconds >= 0
    assert not any(t.name == "yy-flow-command-heartbeat" for t in threading.enumerate())


@pytest.mark.parametrize("error", [RuntimeError("observer failed"), OSError("output closed")])
def test_observer_error_cannot_change_command_outcome(error):
    called = threading.Event()

    def broken(elapsed):
        called.set()
        raise error

    with observe_command(broken, interval_seconds=0.01):
        assert called.wait(2)
        result = subprocess.CompletedProcess(["controlled-test"], 1)
    assert result.returncode == 1


def test_timeout_is_not_swallowed_or_retried_by_observer():
    error = subprocess.TimeoutExpired(["controlled-test"], 0.1)
    with pytest.raises(subprocess.TimeoutExpired) as caught:
        with observe_command() as timing:
            raise error
    assert caught.value is error
    assert timing.duration_seconds >= 0


@pytest.mark.parametrize("interval", [0, -1, float("inf"), float("nan")])
def test_invalid_heartbeat_interval_rejected(interval):
    with pytest.raises(ValueError):
        with observe_command(interval_seconds=interval):
            pytest.fail("invalid interval was accepted")


@pytest.mark.parametrize("role,action", [("REVIEWER", "review"), ("QA", "test")])
def test_context_has_one_complete_safety_source_and_no_write_delegation(role, action):
    text = build_context(role, action)
    safety = load_safety_text(str(ROOT)).strip()
    assert text.count(safety) == 1
    assert "不另造可能漂移的摘要" in text
    assert "主协调者" in text
    assert "只读" in text
    assert "- 通关：更至" not in text


def test_rule_sources_no_longer_offer_tag_based_automatic_acceptance():
    for name in ("rules/AGENTS.md", "references/02-State-Flow-Rules.md"):
        text = (ROOT / name).read_text(encoding="utf-8")
        assert "允许执行方直接置为" not in text
        assert "允许跳过 PM 直接验收" not in text
        assert "真实用户" in text
    cursor = (ROOT / ".cursor/rules/yy-flow-orchestrator.mdc").read_text(encoding="utf-8")
    assert "Reviewer 不直接写看板" in cursor
    assert "不重复执行" in cursor
    assert "禁止跨候选复用" in cursor


@pytest.mark.parametrize("exit_code", [0, 1])
def test_lite_observation_does_not_change_checks_or_exit_codes(tmp_path, monkeypatch, exit_code):
    from types import SimpleNamespace
    import scripts._lib.core.lightweight_profile as lite
    import scripts._lib.core.production_runner as production

    events = []
    seen = threading.Event()
    def emit(**payload):
        events.append(payload)
        if payload["event"] == "lite_command_waiting":
            seen.set()

    def run(argv, cwd, timeout):
        assert timeout == 2
        if argv[0] == "git":
            return subprocess.CompletedProcess(argv, 0, "", "")
        assert seen.wait(2)
        return subprocess.CompletedProcess(argv, exit_code, "1 passed" if exit_code == 0 else "FAILED test_x - AssertionError", "")

    monkeypatch.setattr(lite, "observe_command", lambda callback: observe_command(callback, interval_seconds=0.01))
    monkeypatch.setattr(lite, "_run_lite_command", run)
    monkeypatch.setattr(production, "_validate_qa_test_command", lambda command, cwd: (True, "", ["controlled-test"]))
    runner = SimpleNamespace(
        _emit_progress=emit,
        evidence_store=SimpleNamespace(_mask_text=lambda text: text),
        _run_reviewer_security_scan=lambda *args, **kwargs: {"passed": True, "exit_code": 0},
    )
    spec = SimpleNamespace(task_id="T9999", qa_timeout_seconds=2, reviewer_timeout_seconds=2,
                           baseline_commit="a" * 40, test_commands=("python -m pytest",))
    checks, diagnostics = lite._collect_runner_checks(runner, spec, str(tmp_path), "b" * 40)
    assert checks["all_passed"] is (exit_code == 0)
    assert diagnostics[-1]["exit_code"] == exit_code
    assert diagnostics[-1]["duration_seconds"] >= 0
    assert "duration_seconds" not in checks["tests"][0]
    assert any(event["event"] == "lite_command_completed" and f"exited {exit_code}" in event["message"] for event in events)
