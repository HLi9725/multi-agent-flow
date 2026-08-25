import os
import subprocess
import pytest
import time
import shutil
from concurrent.futures import ThreadPoolExecutor

from scripts._lib.core.worktree_schema import (
    WorktreeRequest, WorktreeDescriptor, WorktreeStatus,
    WorktreeError, WorktreeSecurityError, WorktreeGitError
)
from scripts._lib.core.worktree_manager import WorktreeManager

@pytest.fixture
def test_repo(tmp_path):
    repo_dir = tmp_path / "target_repo"
    repo_dir.mkdir()
    subprocess.run(["git", "init"], cwd=str(repo_dir), check=True)
    (repo_dir / "init.txt").write_text("init")
    subprocess.run(["git", "add", "init.txt"], cwd=str(repo_dir), check=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=str(repo_dir), check=True)

    commit_hash = subprocess.run(["git", "rev-parse", "HEAD"], cwd=str(repo_dir), stdout=subprocess.PIPE, text=True).stdout.strip()
    return str(repo_dir), commit_hash

@pytest.fixture
def manager(tmp_path, test_repo):
    repo_path, _ = test_repo
    controlled_root = tmp_path / "controlled_root"
    controlled_root.mkdir()
    return WorktreeManager(str(controlled_root), repo_path)

def test_worktree_creation_and_inspection(manager, test_repo):
    repo_path, commit_hash = test_repo
    req = WorktreeRequest(
        project_id="proj1",
        task_id="T001",
        actor_role="DEV",
        host_session_id="sess1",
        baseline_commit=commit_hash
    )
    desc = manager.create_worktree(req)
    assert desc.request == req
    assert "proj1-T001-DEV-sess1" in desc.branch_name
    assert os.path.exists(desc.absolute_path)

    # Meta should NOT be inside worktree
    assert not os.path.exists(os.path.join(desc.absolute_path, ".agent_worktree_meta.json"))

    # Inspect
    inspected = manager.inspect(desc.worktree_id)
    assert inspected.branch_name == desc.branch_name

    # Verify
    status = manager.verify(desc.worktree_id)
    assert status.is_valid
    assert status.is_clean
    assert status.current_commit == commit_hash
    assert status.head_ref == desc.branch_name

def test_cleanup_plan_format(manager, test_repo):
    repo_path, commit_hash = test_repo
    req = WorktreeRequest("p", "t", "DEV", "s1", commit_hash)
    desc = manager.create_worktree(req)
    plan = manager.get_cleanup_plan(desc.worktree_id)
    # Ensure no executable git commands
    assert "git_commands" not in plan
    assert plan["requires_user_confirmation"] is True
    assert plan["target_worktree"] == desc.absolute_path

def test_isolation(manager, test_repo):
    _, commit_hash = test_repo
    req1 = WorktreeRequest("p", "t", "DEV", "s1", commit_hash)
    req2 = WorktreeRequest("p", "t", "QA", "s2", commit_hash)

    d1 = manager.create_worktree(req1)
    d2 = manager.create_worktree(req2)

    assert d1.absolute_path != d2.absolute_path

    with open(os.path.join(d1.absolute_path, "dev.txt"), "w") as f:
        f.write("dev")

    assert not os.path.exists(os.path.join(d2.absolute_path, "dev.txt"))

    status1 = manager.verify(d1.worktree_id)
    assert not status1.is_clean
    assert status1.untracked_files == 1

    status2 = manager.verify(d2.worktree_id)
    assert status2.is_clean

def test_invalid_ref_and_injection(manager, test_repo):
    _, commit_hash = test_repo
    req = WorktreeRequest("p", "t", "DEV", "s1 -o option", commit_hash)
    with pytest.raises(WorktreeSecurityError, match="Invalid worktree_id format"):
        manager.create_worktree(req)

def test_path_traversal_and_escapes(manager, test_repo, tmp_path):
    _, commit_hash = test_repo
    bad_req = WorktreeRequest("../p", "t", "DEV", "s1", commit_hash)
    with pytest.raises(WorktreeSecurityError, match="Invalid worktree_id format"):
        manager.create_worktree(bad_req)

def test_concurrency_collision(manager, test_repo):
    _, commit_hash = test_repo
    req = WorktreeRequest("p", "t", "DEV", "s1", commit_hash)

    def create():
        try:
            return manager.create_worktree(req)
        except Exception as e:
            return e

    with ThreadPoolExecutor(max_workers=5) as ex:
        results = list(ex.map(lambda _: create(), range(5)))

    success = [r for r in results if isinstance(r, WorktreeDescriptor)]
    failures = [r for r in results if isinstance(r, Exception)]

    assert len(success) == 1
    assert len(failures) == 4
    for f in failures:
        assert isinstance(f, WorktreeSecurityError)
        assert "already exists" in str(f)

def test_spaces_in_path(tmp_path):
    repo_dir = tmp_path / "my target repo"
    repo_dir.mkdir()
    subprocess.run(["git", "init"], cwd=str(repo_dir), check=True)
    (repo_dir / "init.txt").write_text("init")
    subprocess.run(["git", "add", "init.txt"], cwd=str(repo_dir), check=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=str(repo_dir), check=True)

    commit_hash = subprocess.run(["git", "rev-parse", "HEAD"], cwd=str(repo_dir), stdout=subprocess.PIPE, text=True).stdout.strip()

    controlled_root = tmp_path / "controlled root"
    controlled_root.mkdir()

    manager = WorktreeManager(str(controlled_root), str(repo_dir))
    req = WorktreeRequest("proj", "task", "DEV", "sess", commit_hash)

    desc = manager.create_worktree(req)
    assert os.path.exists(desc.absolute_path)

def test_user_file_protection_on_failure(manager, test_repo):
    repo_path, commit_hash = test_repo
    req = WorktreeRequest("proj", "task", "DEV", "sess1", commit_hash)

    # Intentionally cause branch to fail (already exists)
    subprocess.run(["git", "branch", "agent-branch-proj-task-DEV-sess1", commit_hash], cwd=repo_path, check=True)

    with pytest.raises(WorktreeSecurityError, match="already exists"):
        manager.create_worktree(req)

def test_junction_escape(tmp_path, test_repo):
    repo_path, commit_hash = test_repo

    ext_dir = tmp_path / "external"
    ext_dir.mkdir()

    root_dir = tmp_path / "controlled"
    root_dir.mkdir()

    manager = WorktreeManager(str(root_dir), repo_path)

    link_path = root_dir / "proj-task-DEV-sess"
    if os.name == 'nt':
        subprocess.run(f'cmd /c mklink /J "{link_path}" "{ext_dir}"', shell=True, check=True)
    else:
        os.symlink(ext_dir, link_path)

    req = WorktreeRequest("proj", "task", "DEV", "sess", commit_hash)

    with pytest.raises(WorktreeSecurityError, match="traversal|already exists"):
        manager.create_worktree(req)

def test_meta_json_integrity(manager, test_repo):
    _, commit_hash = test_repo
    req1 = WorktreeRequest("p", "t", "DEV", "s1", commit_hash)
    d1 = manager.create_worktree(req1)

    meta_path = os.path.join(manager.registry_dir, f"{d1.worktree_id}.json")
    import json
    with open(meta_path, 'r', encoding='utf-8') as f:
        data = json.load(f)
    data['absolute_path'] = "/some/spoofed/path"
    with open(meta_path, 'w', encoding='utf-8') as f:
        json.dump(data, f)

    with pytest.raises(WorktreeSecurityError, match="Registry spoofed"):
        manager.inspect(d1.worktree_id)

    status = manager.verify(d1.worktree_id)
    assert not status.is_valid
    assert not status.is_clean
    assert status.untracked_files == 0

def test_short_sha_rejection(manager, test_repo):
    _, commit_hash = test_repo
    short_hash = commit_hash[:7]
    req = WorktreeRequest("p", "t", "DEV", "s2", short_hash)
    with pytest.raises(WorktreeSecurityError, match="full 40-char SHA"):
        manager.create_worktree(req)

def test_corrupt_json_handling(manager, test_repo):
    _, commit_hash = test_repo
    req = WorktreeRequest("p", "t", "DEV", "s3", commit_hash)
    d = manager.create_worktree(req)

    meta_path = os.path.join(manager.registry_dir, f"{d.worktree_id}.json")
    with open(meta_path, 'w') as f:
        f.write("{corrupt_json: ")

    with pytest.raises(WorktreeSecurityError, match="Corrupt registry"):
        manager.inspect(d.worktree_id)

    # verify gracefully returns invalid
    status = manager.verify(d.worktree_id)
    assert not status.is_valid

def test_list_worktrees(manager, test_repo):
    _, commit_hash = test_repo
    req1 = WorktreeRequest("projA", "t1", "DEV", "s1", commit_hash)
    req2 = WorktreeRequest("projB", "t2", "QA", "s2", commit_hash)
    req3 = WorktreeRequest("projA", "t3", "DEV", "s3", commit_hash)

    manager.create_worktree(req1)
    manager.create_worktree(req2)
    manager.create_worktree(req3)

    list_a = manager.list_worktrees("projA")
    assert len(list_a) == 2
    assert all(w.request.project_id == "projA" for w in list_a)

    list_b = manager.list_worktrees("projB")
    assert len(list_b) == 1

    list_c = manager.list_worktrees("projC")
    assert len(list_c) == 0
