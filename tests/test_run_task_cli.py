import os
import io
import json
import pytest
from types import SimpleNamespace

from scripts.run_task import _runner_for, _overrides_from_args


@pytest.mark.parametrize('encoding', ['gbk', 'ascii', 'utf-8'])
def test_terminal_result_and_progress_roundtrip_on_legacy_console(monkeypatch, encoding):
    from scripts import run_task
    out_bytes, err_bytes = io.BytesIO(), io.BytesIO()
    out = io.TextIOWrapper(out_bytes, encoding=encoding, errors='strict')
    err = io.TextIOWrapper(err_bytes, encoding=encoding, errors='strict')
    monkeypatch.setattr(run_task.sys, 'stdout', out)
    monkeypatch.setattr(run_task.sys, 'stderr', err)
    data = {'success': False, 'state': 'NEEDS_USER_INPUT', 'message': '测试🚨 Unicode \ud800', 'task_id': 'T1'}
    run_task._emit_result(data)
    run_task._emit_progress(data)
    out.flush()
    err.flush()
    assert json.loads(out_bytes.getvalue().decode(encoding)) == data
    assert json.loads(err_bytes.getvalue().decode(encoding).removeprefix('[YY-FLOW] ')) == data


def test_resume_error_exit_preserves_persisted_result_on_gbk(monkeypatch):
    from scripts import run_task
    payload = {'success': False, 'state': 'NEEDS_USER_INPUT', 'message': '🚨 请检查字段'}
    fake_result = SimpleNamespace(success=False, to_dict=lambda: payload)
    monkeypatch.setattr(run_task, '_runner_for', lambda *args: SimpleNamespace(resume=lambda **kw: fake_result))
    buffer = io.BytesIO()
    stream = io.TextIOWrapper(buffer, encoding='gbk')
    monkeypatch.setattr(run_task.sys, 'stdout', stream)
    args = SimpleNamespace(project_root='.', authority_root=None, task_id='T1')
    assert run_task.cmd_resume(args) == 1
    assert json.loads(buffer.getvalue().decode('gbk')) == payload


def test_resume_overrides_are_optional_and_complete():
    assert _overrides_from_args(SimpleNamespace()) == {}
    args = SimpleNamespace(
        builder_adapter="antigravity", reviewer_adapter="antigravity",
        qa_adapter="antigravity", max_review_cycles=4, max_qa_cycles=5,
        timeout_seconds=901, test_commands=["python -m pytest tests -q", "npm run build"],
        workspace_mode="branch", max_total_attempts=25,
        total_wall_clock_timeout_seconds=4500,
    )
    assert _overrides_from_args(args) == {
        "builder_adapter_id": "antigravity", "reviewer_adapter_id": "antigravity",
        "qa_adapter_id": "antigravity", "max_review_cycles": 4, "max_qa_cycles": 5,
        "builder_timeout_seconds": 901, "reviewer_timeout_seconds": 901,
        "qa_timeout_seconds": 901,
        "test_commands": ("python -m pytest tests -q", "npm run build"),
        "workspace_mode": "branch",
        "max_total_attempts": 25,
        "total_wall_clock_timeout_seconds": 4500,
    }


def test_attempt_budget_cli_parsing(monkeypatch):
    import pytest
    from scripts import run_task
    for command in ("start", "resume"):
        seen = []
        monkeypatch.setattr(run_task, "cmd_" + command, lambda args: seen.append(_overrides_from_args(args)) or 0)
        for values, expected in (([], None), (["--max-total-attempts", "25"], 25)):
            monkeypatch.setattr("sys.argv", ["run_task.py", command, "--task-id", "T1", *values])
            with pytest.raises(SystemExit) as result:
                run_task.main()
            assert result.value.code == 0
            assert seen[-1].get("max_total_attempts") == expected
        for invalid in ("0", "-1", "1.5", "bad"):
            monkeypatch.setattr("sys.argv", ["run_task.py", command, "--task-id", "T1", "--max-total-attempts", invalid])
            with pytest.raises(SystemExit) as result:
                run_task.main()
            assert result.value.code == 2


def test_wall_clock_budget_cli_parsing(monkeypatch):
    import pytest
    from scripts import run_task
    for command in ("start", "resume"):
        seen = []
        monkeypatch.setattr(run_task, "cmd_" + command, lambda args: seen.append(_overrides_from_args(args)) or 0)
        for values, expected in (([], None), (["--total-wall-clock-timeout-seconds", "4500"], 4500)):
            monkeypatch.setattr("sys.argv", ["run_task.py", command, "--task-id", "T1", *values])
            with pytest.raises(SystemExit) as result:
                run_task.main()
            assert result.value.code == 0
            assert seen[-1].get("total_wall_clock_timeout_seconds") == expected
        for invalid in ("0", "-1", "1.5", "bad"):
            monkeypatch.setattr(
                "sys.argv",
                ["run_task.py", command, "--task-id", "T1", "--total-wall-clock-timeout-seconds", invalid],
            )
            with pytest.raises(SystemExit) as result:
                run_task.main()
            assert result.value.code == 2


def test_explicit_authority_root_owns_checkpoint_store(tmp_path):
    project = tmp_path / "project"
    authority = project / ".yy-flow"
    authority.mkdir(parents=True)

    runner = _runner_for(str(project), str(authority), project_id="project")

    assert runner.checkpoint_store.data_root == os.path.realpath(authority)
    assert runner.checkpoint_store.checkpoint_dir.startswith(os.path.realpath(authority))


def test_diagnostic_masking_preserves_failure_context(tmp_path):
    from scripts._lib.core.evidence_store import EvidenceStore
    store = EvidenceStore(str(tmp_path / "evidence"))
    raw = "FAILED tests/test_login.py::test_refresh\npassword=" + "example-value\nAssertionError: expected 200\n1 failed"
    masked = store._mask_text(raw)
    assert "example-value" not in masked
    assert "AssertionError: expected 200" in masked
    assert "1 failed" in masked
    assert store._mask_text(masked) == masked
    assert store._mask_text("1 passed\n\n") == "1 passed\n\n"


def test_failure_classification_does_not_send_environment_errors_to_builder():
    from scripts._lib.core.production_runner import (
        _is_known_test_infrastructure_failure,
        _is_repairable_test_failure,
    )
    assert _is_repairable_test_failure("npm test", 1, "FAILED tests/test_api.py::test_auth - AssertionError\n1 failed")
    assert not _is_repairable_test_failure("npm test", 1, "No module named pytest")
    assert not _is_repairable_test_failure("npm test", 1, "unknown execution error")
    assert not _is_repairable_test_failure("npm test", 124, "1 failed")
    assert not _is_repairable_test_failure("npm test", 0, "1 failed")
    assert _is_known_test_infrastructure_failure(124, "1 failed")
    assert _is_known_test_infrastructure_failure(1, "No module named pytest")
    assert _is_known_test_infrastructure_failure(1, "connectex: connection refused")
    assert _is_known_test_infrastructure_failure(1, "Access denied for user 'test'")
    assert _is_known_test_infrastructure_failure(1, "No space left on device")


@pytest.mark.parametrize("output", [
    "ERROR tests/test_mysql.py::test_cleanup - sqlalchemy.exc.OperationalError: Unknown column 'remarks'\n6 errors",
    "ERROR at setup of test_case\nNameError: name 'Session' is not defined\n1 error",
    "ProgrammingError: Table 'test.orders' doesn't exist\n10 errors",
    "SyntaxError: invalid syntax",
    "BUILD FAILURE\nCOMPILATION ERROR",
    "unknown execution error",
])
def test_candidate_failures_reach_semantic_qa_instead_of_pausing(output):
    from scripts._lib.core.production_runner import _is_known_test_infrastructure_failure
    assert not _is_known_test_infrastructure_failure(1, output)


def test_zero_tests_is_not_classified_as_host_infrastructure():
    from scripts._lib.core.production_runner import _is_known_test_infrastructure_failure
    assert not _is_known_test_infrastructure_failure(0, "collected 0 items\nno tests ran")


@pytest.mark.parametrize("detail", ["connection refused", "Access denied for user 'test'", "TLS handshake timeout"])
def test_external_error_inside_named_test_reaches_semantic_qa(detail):
    from scripts._lib.core.production_runner import _is_known_test_infrastructure_failure
    assert not _is_known_test_infrastructure_failure(
        1, f"FAILED tests/test_external.py::test_boundary - RuntimeError: {detail}\n1 failed"
    )


def test_diagnostic_masking_covers_multiline_private_key(tmp_path):
    from scripts._lib.core.evidence_store import EvidenceStore
    store = EvidenceStore(str(tmp_path / "evidence"))
    raw = "-----BEGIN PRIVATE KEY-----\nexample-body\n-----END PRIVATE KEY-----\nFAILED tests/test_app.py"
    masked = store._mask_text(raw)
    assert "example-body" not in masked
    assert "FAILED tests/test_app.py" in masked
    assert "example-body" not in store._mask_text("-----BEGIN PRIVATE KEY-----\nexample-body")
    assert "example-value" not in store._mask_text('"password":\n"example-value"\nFAILED test_login')
