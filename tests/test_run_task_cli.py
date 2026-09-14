import os
from types import SimpleNamespace

from scripts.run_task import _runner_for, _overrides_from_args


def test_resume_overrides_are_optional_and_complete():
    assert _overrides_from_args(SimpleNamespace()) == {}
    args = SimpleNamespace(
        builder_adapter="antigravity", reviewer_adapter="antigravity",
        qa_adapter="antigravity", max_review_cycles=4, max_qa_cycles=5,
        timeout_seconds=901, test_commands=["python -m pytest tests -q", "npm run build"],
        workspace_mode="branch", max_total_attempts=25,
    )
    assert _overrides_from_args(args) == {
        "builder_adapter_id": "antigravity", "reviewer_adapter_id": "antigravity",
        "qa_adapter_id": "antigravity", "max_review_cycles": 4, "max_qa_cycles": 5,
        "builder_timeout_seconds": 901, "reviewer_timeout_seconds": 901,
        "qa_timeout_seconds": 901,
        "test_commands": ("python -m pytest tests -q", "npm run build"),
        "workspace_mode": "branch",
        "max_total_attempts": 25,
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
    from scripts._lib.core.production_runner import _is_repairable_test_failure
    assert _is_repairable_test_failure("npm test", 1, "FAILED tests/test_api.py::test_auth - AssertionError\n1 failed")
    assert not _is_repairable_test_failure("npm test", 1, "No module named pytest")
    assert not _is_repairable_test_failure("npm test", 1, "unknown execution error")
    assert not _is_repairable_test_failure("npm test", 124, "1 failed")
    assert not _is_repairable_test_failure("npm test", 0, "1 failed")


def test_diagnostic_masking_covers_multiline_private_key(tmp_path):
    from scripts._lib.core.evidence_store import EvidenceStore
    store = EvidenceStore(str(tmp_path / "evidence"))
    raw = "-----BEGIN PRIVATE KEY-----\nexample-body\n-----END PRIVATE KEY-----\nFAILED tests/test_app.py"
    masked = store._mask_text(raw)
    assert "example-body" not in masked
    assert "FAILED tests/test_app.py" in masked
    assert "example-body" not in store._mask_text("-----BEGIN PRIVATE KEY-----\nexample-body")
    assert "example-value" not in store._mask_text('"password":\n"example-value"\nFAILED test_login')
