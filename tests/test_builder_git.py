import subprocess
from dataclasses import replace

import pytest

from scripts._lib.core.builder_git import candidate_paths, stage_candidate, workspace_fingerprint
from scripts._lib.core.production_runner import ProductionRunner, _interrupted_builder_recovery_eligible
from scripts._lib.core.runner_schema import RunnerCheckpoint


def git(repo, *args):
    return subprocess.check_output(["git", *args], cwd=repo)


@pytest.fixture
def repo(tmp_path):
    git(tmp_path, "init")
    git(tmp_path, "config", "user.name", "Test")
    git(tmp_path, "config", "user.email", "test@example.invalid")
    (tmp_path / ".gitignore").write_text(".yy-flow/\nuser_data/\n", encoding="utf-8")
    (tmp_path / "app.py").write_text("VALUE = 1\n", encoding="utf-8")
    git(tmp_path, "add", ".")
    git(tmp_path, "commit", "-m", "initial")
    for dirname in (".yy-flow", "user_data"):
        (tmp_path / dirname).mkdir()
        (tmp_path / dirname / "board.json").write_text("[]", encoding="utf-8")
    return tmp_path


@pytest.mark.parametrize("prestaged", [False, True])
def test_ignored_control_directories_do_not_block_finalization(repo, prestaged):
    baseline = git(repo, "rev-parse", "HEAD").decode().strip()
    (repo / "app.py").write_text("VALUE = 2\n", encoding="utf-8")
    if prestaged:
        git(repo, "add", "app.py")
    candidate = object.__new__(ProductionRunner)._finalize_builder_candidate(str(repo), baseline)
    assert candidate != baseline
    assert git(repo, "diff-tree", "--no-commit-id", "--name-only", "-r", candidate).strip() == b"app.py"
    assert not git(repo, "status", "--porcelain").strip()


def test_rename_delete_and_literal_unicode_paths(repo):
    (repo / "app.py").rename(repo / "中文 [1].py")
    (repo / "-option.py").write_text("x = 1\n", encoding="utf-8")
    stage_candidate(str(repo))
    assert set(git(repo, "diff", "--cached", "--name-only", "--no-renames", "-z").decode().split("\0")[:-1]) == {
        "app.py", "中文 [1].py", "-option.py"}


def test_existing_staged_control_data_is_not_committed_or_unstaged(repo):
    git(repo, "add", "-f", "user_data/board.json")  # Simulate pre-existing user index.
    before = git(repo, "diff", "--cached", "--binary")
    (repo / "app.py").write_text("VALUE = 2\n", encoding="utf-8")
    with pytest.raises(RuntimeError, match="control files are already staged"):
        stage_candidate(str(repo))
    assert git(repo, "diff", "--cached", "--binary") == before


def test_unignored_control_files_are_excluded(repo):
    (repo / ".gitignore").write_text("", encoding="utf-8")
    stage_candidate(str(repo))
    assert git(repo, "diff", "--cached", "--name-only").strip() == b".gitignore"
    assert candidate_paths(str(repo)) == [".gitignore"]


@pytest.mark.parametrize("name", ["app.py", "new.py"])
def test_retry_fingerprint_detects_edits_with_unchanged_porcelain(repo, name):
    (repo / name).write_text("first\n", encoding="utf-8")
    status = git(repo, "status", "--porcelain")
    before = workspace_fingerprint(str(repo))
    (repo / name).write_text("second\n", encoding="utf-8")
    assert git(repo, "status", "--porcelain") == status
    assert workspace_fingerprint(str(repo)) != before


def test_finalization_recovery_requires_completed_host_identity():
    cp = RunnerCheckpoint(task_id="T1", project_id="repo", state="NEEDS_USER_INPUT",
                          current_role="BUILDER", last_error="Failed to stage Builder changes: ignored",
                          builder_session_id="sess", builder_invocation_id="host:step_25")
    assert _interrupted_builder_recovery_eligible(cp)
    assert _interrupted_builder_recovery_eligible(replace(cp, last_error=
        "Failed to finalize candidate commit from Builder worktree: " + cp.last_error))
    assert not _interrupted_builder_recovery_eligible(replace(cp, builder_invocation_id=None))
    assert not _interrupted_builder_recovery_eligible(replace(cp, current_role="QA"))


def test_staged_change_reverted_in_worktree_is_not_silently_committed(repo):
    (repo / "app.py").write_text("VALUE = 2\n", encoding="utf-8")
    git(repo, "add", "app.py")
    (repo / "app.py").write_text("VALUE = 1\n", encoding="utf-8")
    stage_candidate(str(repo))
    assert not git(repo, "diff", "--cached").strip()
