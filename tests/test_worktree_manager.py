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
    # create initial commit
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
    
    # Inspect
    inspected = manager.inspect(desc.worktree_id)
    assert inspected.branch_name == desc.branch_name
    
    # Verify
    status = manager.verify(desc.worktree_id)
    assert status.is_valid
    assert status.is_clean
    assert status.current_commit == commit_hash
    assert status.head_ref == desc.branch_name
    
    # Cleanup plan
    plan = manager.get_cleanup_plan(desc.worktree_id)
    assert "git worktree remove" in plan["git_commands"][0]

def test_isolation(manager, test_repo):
    _, commit_hash = test_repo
    req1 = WorktreeRequest("p", "t", "DEV", "s1", commit_hash)
    req2 = WorktreeRequest("p", "t", "QA", "s2", commit_hash)
    
    d1 = manager.create_worktree(req1)
    d2 = manager.create_worktree(req2)
    
    assert d1.absolute_path != d2.absolute_path
    
    # Write file in wt1
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
    # Injection attempt in session ID -> forms invalid branch name
    req = WorktreeRequest("p", "t", "DEV", "s1 -o option", commit_hash)
    with pytest.raises(WorktreeSecurityError, match="Invalid worktree_id format"):
        manager.create_worktree(req)

def test_path_traversal_and_escapes(manager, test_repo, tmp_path):
    _, commit_hash = test_repo
    
    req = WorktreeRequest("p", "t", "DEV", "s1", commit_hash)
    
    # Try traversing using project_id (though safe_path prevents bad chars, if it passed it would hit traversal check)
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

def test_spaces_in_path(tmp_path):
    # Setup repo with spaces
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
    
    # Intentionally cause `git worktree add` to fail (e.g., branch exists but worktree doesn't)
    # We will manually create the branch so git branch creation fails gracefully,
    # wait, if branch creation fails it raises WorktreeSecurityError.
    subprocess.run(["git", "branch", "agent-branch-proj-task-DEV-sess1", commit_hash], cwd=repo_path, check=True)
    
    with pytest.raises(WorktreeSecurityError, match="already exists"):
        manager.create_worktree(req)
        
    # The directory should not be left behind
    assert not os.path.exists(os.path.join(manager.controlled_root, "proj-task-DEV-sess1"))

def test_junction_escape(tmp_path, test_repo):
    repo_path, commit_hash = test_repo
    
    ext_dir = tmp_path / "external"
    ext_dir.mkdir()
    
    root_dir = tmp_path / "controlled"
    root_dir.mkdir()
    
    manager = WorktreeManager(str(root_dir), repo_path)
    
    # create a junction inside controlled_root
    link_path = root_dir / "proj-task-DEV-sess"
    if os.name == 'nt':
        subprocess.run(f'cmd /c mklink /J "{link_path}" "{ext_dir}"', shell=True, check=True)
    else:
        os.symlink(ext_dir, link_path)
        
    req = WorktreeRequest("proj", "task", "DEV", "sess", commit_hash)
    
    with pytest.raises(WorktreeSecurityError, match="traversal|already exists"):
        manager.create_worktree(req)
