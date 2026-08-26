import os
import subprocess
import pytest
import time
import shutil
import json
import copy
import stat
import threading
from concurrent.futures import ThreadPoolExecutor

from scripts._lib.core.worktree_schema import (
    WorktreeRequest, WorktreeDescriptor, WorktreeStatus,
    WorktreeError, WorktreeSecurityError, WorktreeGitError
)
from scripts._lib.core.worktree_manager import WorktreeManager
import scripts._lib.core.worktree_manager as worktree_manager_module

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
    assert "proj1_T001_DEV" in desc.branch_name
    assert os.path.exists(desc.absolute_path)
    assert desc.target_repo_root == manager.target_repo_root
    assert desc.git_common_dir == manager.git_common_dir
    assert desc.repository_identity == manager.repository_identity

    # Meta should NOT be inside worktree
    assert not os.path.exists(os.path.join(desc.absolute_path, ".agent_worktree_meta.json"))

    # Inspect
    inspected = manager.inspect(desc.worktree_id)
    assert inspected.branch_name == desc.branch_name
    assert inspected.request == req
    assert inspected.repository_identity == manager.repository_identity

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
    # Option injection payload in host_session_id
    req_injection = WorktreeRequest("p", "t", "DEV", "s1 -o option", commit_hash)
    with pytest.raises(WorktreeSecurityError, match="Invalid host_session_id"):
        manager.create_worktree(req_injection)

    # Path traversal payload in project_id
    req_traversal = WorktreeRequest("../p", "t", "DEV", "s1", commit_hash)
    with pytest.raises(WorktreeSecurityError, match="Invalid project_id"):
        manager.create_worktree(req_traversal)

def test_path_traversal_and_escapes(manager, test_repo):
    _, commit_hash = test_repo
    # 1. Relative traversal
    bad_req1 = WorktreeRequest("../../../evil", "t", "DEV", "s1", commit_hash)
    with pytest.raises(WorktreeSecurityError, match="Invalid project_id"):
        manager.create_worktree(bad_req1)

    # 2. Windows absolute path
    bad_req2 = WorktreeRequest("C:\\evil", "t", "DEV", "s1", commit_hash)
    with pytest.raises(WorktreeSecurityError, match="Invalid project_id"):
        manager.create_worktree(bad_req2)

    # 3. POSIX absolute path
    bad_req3 = WorktreeRequest("/evil", "t", "DEV", "s1", commit_hash)
    with pytest.raises(WorktreeSecurityError, match="Invalid project_id"):
        manager.create_worktree(bad_req3)

def test_request_field_fail_closed_validation(manager, test_repo):
    _, commit_hash = test_repo
    # Whitespace only
    with pytest.raises(WorktreeSecurityError, match="cannot be whitespace-only"):
        manager.create_worktree(WorktreeRequest("   ", "t", "DEV", "s", commit_hash))

    # Control characters
    with pytest.raises(WorktreeSecurityError, match="contains control characters"):
        manager.create_worktree(WorktreeRequest("p\nevil", "t", "DEV", "s", commit_hash))

    # Leading option
    with pytest.raises(WorktreeSecurityError, match="leading options are forbidden"):
        manager.create_worktree(WorktreeRequest("-o", "t", "DEV", "s", commit_hash))

    # Non-string type
    with pytest.raises(WorktreeSecurityError, match="must be a string"):
        manager.create_worktree(WorktreeRequest(123, "t", "DEV", "s", commit_hash))

def test_worktree_id_fixed_length_and_opaque_host_session(manager, test_repo):
    _, commit_hash = test_repo
    request = WorktreeRequest(
        project_id="p" * 128,
        task_id="t" * 128,
        actor_role="R" * 128,
        host_session_id="fake-session:123e4567-e89b-12d3-a456-426614174000",
        baseline_commit=commit_hash,
    )

    worktree_id = manager._compute_worktree_id(
        request.project_id,
        request.task_id,
        request.actor_role,
        request.host_session_id,
    )

    assert len(worktree_id) <= 128
    assert manager._safe_path(worktree_id).endswith(os.path.normcase(worktree_id))
    descriptor = manager.create_worktree(request)
    assert descriptor.request.host_session_id == request.host_session_id
    assert manager.inspect(descriptor.worktree_id).request == request

def test_inspect_illegal_id_zero_filesystem_access(manager, monkeypatch):
    # DEF-T0023-28: inspect must validate ID before touching filesystem
    def forbidden_fs_call(*args, **kwargs):
        pytest.fail(f"Filesystem was accessed with args: {args}")

    monkeypatch.setattr(os.path, "exists", forbidden_fs_call)

    illegal_ids = [
        "../../../evil",
        "/etc/passwd",
        "C:\\Windows\\win.ini",
        "invalid id with spaces",
        "-o option",
        "",
        "p/t/DEV",
        "p\\t\\DEV"
    ]

    for bad_id in illegal_ids:
        with pytest.raises(WorktreeSecurityError):
            manager.inspect(bad_id)

def test_repo_root_normalization_from_subdirectories(test_repo, tmp_path):
    # DEF-T0023-29: Normalized target_repo_root from root and subdirectories
    repo_dir, _ = test_repo
    sub_dir = os.path.join(repo_dir, "sub", "dir", "nested")
    os.makedirs(sub_dir, exist_ok=True)

    cr1 = str(tmp_path / "cr1")
    cr2 = str(tmp_path / "cr2")
    manager_root = WorktreeManager(cr1, repo_dir)
    manager_sub = WorktreeManager(cr2, sub_dir)

    assert manager_root.target_repo_root == manager_sub.target_repo_root
    assert manager_root.git_common_dir == manager_sub.git_common_dir
    assert manager_root.repository_identity == manager_sub.repository_identity

def test_distinct_clones_distinct_identity(tmp_path):
    # DEF-T0023-29: Distinct clones have distinct identities
    repo_a = tmp_path / "clone_a"
    repo_b = tmp_path / "clone_b"
    for r in [repo_a, repo_b]:
        r.mkdir()
        subprocess.run(["git", "init"], cwd=str(r), check=True)
        (r / "init.txt").write_text("common_content")
        subprocess.run(["git", "add", "init.txt"], cwd=str(r), check=True)
        subprocess.run(["git", "commit", "-m", "init"], cwd=str(r), check=True)

    manager_a = WorktreeManager(str(tmp_path / "cr_a"), str(repo_a))
    manager_b = WorktreeManager(str(tmp_path / "cr_b"), str(repo_b))

    assert manager_a.target_repo_root != manager_b.target_repo_root
    assert manager_a.git_common_dir != manager_b.git_common_dir
    assert manager_a.repository_identity != manager_b.repository_identity

def test_linked_worktree_common_dir(test_repo, tmp_path):
    # Linked worktree correctly identifies common dir
    repo_dir, commit_hash = test_repo
    linked_wt_path = str(tmp_path / "linked_worktree")
    subprocess.run(["git", "worktree", "add", linked_wt_path, "HEAD"], cwd=repo_dir, check=True)

    manager_main = WorktreeManager(str(tmp_path / "cr_main"), repo_dir)
    manager_linked = WorktreeManager(str(tmp_path / "cr_linked"), linked_wt_path)

    assert manager_linked.git_common_dir == manager_main.git_common_dir

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
    worktree_id = manager._compute_worktree_id(req.project_id, req.task_id, req.actor_role, req.host_session_id)
    branch_name = f"agent-branch-{worktree_id}"

    subprocess.run(["git", "branch", branch_name, commit_hash], cwd=repo_path, check=True)

    with pytest.raises(WorktreeSecurityError, match="already exists"):
        manager.create_worktree(req)

def test_junction_escape(tmp_path, test_repo):
    repo_path, commit_hash = test_repo

    ext_dir = tmp_path / "external"
    ext_dir.mkdir()

    root_dir = tmp_path / "controlled"
    root_dir.mkdir()

    manager = WorktreeManager(str(root_dir), repo_path)
    req = WorktreeRequest("proj", "task", "DEV", "sess", commit_hash)
    worktree_id = manager._compute_worktree_id(req.project_id, req.task_id, req.actor_role, req.host_session_id)

    link_path = root_dir / worktree_id
    if os.name == 'nt':
        subprocess.run(f'cmd /c mklink /J "{link_path}" "{ext_dir}"', shell=True, check=True)
    else:
        os.symlink(ext_dir, link_path)

    with pytest.raises(WorktreeSecurityError, match="traversal|already exists"):
        manager.create_worktree(req)

def test_registry_junction_within_controlled_root_is_rejected(tmp_path, test_repo):
    repo_path, _ = test_repo
    root_dir = tmp_path / "controlled-registry"
    root_dir.mkdir()
    manager = WorktreeManager(str(root_dir), repo_path)
    foreign_registry = root_dir / "foreign-registry"
    foreign_registry.mkdir()

    os.rmdir(manager.registry_dir)
    if os.name == 'nt':
        subprocess.run(
            ["cmd", "/c", "mklink", "/J", manager.registry_dir, str(foreign_registry)],
            check=True,
        )
    else:
        os.symlink(foreign_registry, manager.registry_dir, target_is_directory=True)

    with pytest.raises(WorktreeSecurityError, match="symbolic link or Junction"):
        manager.list_worktrees("proj")

def test_posix_temp_unlink_failure_is_reported_with_real_retention(manager, tmp_path, monkeypatch):
    tmp_file = tmp_path / "owned-temp.json"
    meta_file = tmp_path / "published.json"
    tmp_file.write_text("evidence", encoding="utf-8")
    real_unlink = os.unlink

    def fail_owned_temp_unlink(path):
        if os.path.normcase(os.path.abspath(path)) == os.path.normcase(os.path.abspath(tmp_file)):
            raise OSError("simulated unlink denial")
        return real_unlink(path)

    with monkeypatch.context() as patcher:
        patcher.setattr(worktree_manager_module.os, "name", "posix")
        patcher.setattr(worktree_manager_module.os, "unlink", fail_owned_temp_unlink)
        with pytest.raises(WorktreeError, match="temporary link cleanup failed") as exc_info:
            manager._publish_registry_create_if_absent(str(tmp_file), str(meta_file))

    assert exc_info.value.recovery_required is True
    assert exc_info.value.branch_retained is True
    assert exc_info.value.registry_retained is True
    assert exc_info.value.tmp_retained is True
    assert tmp_file.exists()
    assert meta_file.exists()

def test_meta_json_integrity(manager, test_repo):
    _, commit_hash = test_repo
    req1 = WorktreeRequest("p", "t", "DEV", "s1", commit_hash)
    d1 = manager.create_worktree(req1)

    meta_path = os.path.join(manager.registry_dir, f"{d1.worktree_id}.json")
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

def test_json_root_types(manager, test_repo):
    _, commit_hash = test_repo
    req = WorktreeRequest("p", "t", "DEV", "json", commit_hash)
    d = manager.create_worktree(req)
    meta_path = os.path.join(manager.registry_dir, f"{d.worktree_id}.json")
    for invalid_root in [[], 123, "str", None]:
        with open(meta_path, 'w', encoding='utf-8') as f:
            json.dump(invalid_root, f)
        with pytest.raises(WorktreeSecurityError, match="JSON root must be an object"):
            manager.inspect(d.worktree_id)
        status = manager.verify(d.worktree_id)
        assert not status.is_valid

def test_inspect_schema_validation_advanced(manager, test_repo):
    _, commit_hash = test_repo
    req = WorktreeRequest("p", "t", "DEV", "sadv", commit_hash)
    d = manager.create_worktree(req)
    meta_path = os.path.join(manager.registry_dir, f"{d.worktree_id}.json")

    with open(meta_path, 'r', encoding='utf-8') as f:
        original_data = json.load(f)

    def write_tampered(mod):
        with open(meta_path, 'w', encoding='utf-8') as f:
            json.dump(mod, f)

    # test 000...000
    tampered = copy.deepcopy(original_data)
    tampered['baseline_commit'] = "0"*40
    tampered['request']['baseline_commit'] = "0"*40
    write_tampered(tampered)
    with pytest.raises(WorktreeError, match="does not exist"):
        manager.inspect(d.worktree_id)

    # test created_at
    tampered = copy.deepcopy(original_data)
    tampered['created_at'] = "str"
    write_tampered(tampered)
    with pytest.raises(WorktreeSecurityError, match="must be a number"):
        manager.inspect(d.worktree_id)

    # verify fail-closed
    assert not manager.verify(d.worktree_id).is_valid

def test_worktree_add_fails_retains_branch_and_registry(manager, test_repo):
    repo_path, commit_hash = test_repo
    req = WorktreeRequest("p", "t", "DEV", "failwt", commit_hash)

    original_run_git = manager._run_git
    def mocked_run_git(cwd, args):
        if args[:2] == ["worktree", "add"]:
            raise WorktreeGitError("mock fail")
        return original_run_git(cwd, args)
    manager._run_git = mocked_run_git

    with pytest.raises(WorktreeError) as exc_info:
        manager.create_worktree(req)

    assert exc_info.value.recovery_required is True
    assert exc_info.value.branch_retained is True
    assert exc_info.value.registry_retained is True

    manager._run_git = original_run_git
    worktree_id = manager._compute_worktree_id(req.project_id, req.task_id, req.actor_role, req.host_session_id)
    meta_path = os.path.join(manager.registry_dir, f"{worktree_id}.json")
    assert os.path.exists(meta_path)

def test_os_write_failures(manager, test_repo, monkeypatch):
    _, commit_hash = test_repo
    req = WorktreeRequest("p", "t", "DEV", "failwt2", commit_hash)

    # 1 byte write loop emulation
    original_write = os.write
    def mock_write_1byte(fd, view):
        return original_write(fd, view[:1])
    monkeypatch.setattr(os, "write", mock_write_1byte)

    d = manager.create_worktree(req)
    assert os.path.exists(d.absolute_path)

    req2 = WorktreeRequest("p", "t", "DEV", "failwt3", commit_hash)
    def mock_write_0byte(fd, view):
        return 0
    monkeypatch.setattr(os, "write", mock_write_0byte)
    with pytest.raises(WorktreeError) as exc_info:
        manager.create_worktree(req2)
    assert exc_info.value.recovery_required is True
    assert exc_info.value.branch_retained is True
    assert exc_info.value.registry_retained is False

    req3 = WorktreeRequest("p", "t", "DEV", "failwt4", commit_hash)
    def mock_write_exc(fd, view):
        raise OSError("mock")
    monkeypatch.setattr(os, "write", mock_write_exc)
    with pytest.raises(WorktreeError) as exc_info:
        manager.create_worktree(req3)
    assert exc_info.value.recovery_required is True
    assert exc_info.value.branch_retained is True
    assert exc_info.value.registry_retained is False

def test_canonical_commit_uppercase_rejected(manager, test_repo):
    _, commit_hash = test_repo
    req = WorktreeRequest("p", "t", "DEV", "upp", commit_hash.upper())
    with pytest.raises(WorktreeSecurityError, match="lowercase full 40-char SHA"):
        manager.create_worktree(req)

def test_inspect_unknown_and_missing_keys_rejected(manager, test_repo):
    _, commit_hash = test_repo
    req = WorktreeRequest("p", "t", "DEV", "keys", commit_hash)
    d = manager.create_worktree(req)
    meta_path = os.path.join(manager.registry_dir, f"{d.worktree_id}.json")

    with open(meta_path, 'r', encoding='utf-8') as f:
        original_data = json.load(f)

    # 1. Extra key in root
    tampered = copy.deepcopy(original_data)
    tampered["unexpected_root_key"] = "evil"
    with open(meta_path, 'w', encoding='utf-8') as f:
        json.dump(tampered, f)
    with pytest.raises(WorktreeSecurityError, match="top-level keys mismatch"):
        manager.inspect(d.worktree_id)
    assert not manager.verify(d.worktree_id).is_valid

    # 2. Missing key in root
    tampered = copy.deepcopy(original_data)
    del tampered["created_at"]
    with open(meta_path, 'w', encoding='utf-8') as f:
        json.dump(tampered, f)
    with pytest.raises(WorktreeSecurityError, match="top-level keys mismatch"):
        manager.inspect(d.worktree_id)
    assert not manager.verify(d.worktree_id).is_valid

    # 3. Extra key in request
    tampered = copy.deepcopy(original_data)
    tampered["request"]["unexpected_req_key"] = "evil"
    with open(meta_path, 'w', encoding='utf-8') as f:
        json.dump(tampered, f)
    with pytest.raises(WorktreeSecurityError, match="request keys mismatch"):
        manager.inspect(d.worktree_id)
    assert not manager.verify(d.worktree_id).is_valid

    # 4. Missing key in request
    tampered = copy.deepcopy(original_data)
    del tampered["request"]["actor_role"]
    with open(meta_path, 'w', encoding='utf-8') as f:
        json.dump(tampered, f)
    with pytest.raises(WorktreeSecurityError, match="request keys mismatch"):
        manager.inspect(d.worktree_id)
    assert not manager.verify(d.worktree_id).is_valid

def test_inspect_control_chars_and_whitespace_rejected(manager, test_repo):
    _, commit_hash = test_repo
    req = WorktreeRequest("p", "t", "DEV", "chars", commit_hash)
    d = manager.create_worktree(req)
    meta_path = os.path.join(manager.registry_dir, f"{d.worktree_id}.json")

    with open(meta_path, 'r', encoding='utf-8') as f:
        original_data = json.load(f)

    # Control chars in fields
    for ctrl in ["\n", "\r", "\t", "\x00", "\x1f", "\x7f"]:
        tampered = copy.deepcopy(original_data)
        tampered["request"]["task_id"] = f"T001{ctrl}evil"
        with open(meta_path, 'w', encoding='utf-8') as f:
            json.dump(tampered, f)
        with pytest.raises(WorktreeSecurityError, match="control characters"):
            manager.inspect(d.worktree_id)
        assert not manager.verify(d.worktree_id).is_valid

    # Whitespace-only in string fields
    tampered = copy.deepcopy(original_data)
    tampered["request"]["actor_role"] = "   "
    with open(meta_path, 'w', encoding='utf-8') as f:
        json.dump(tampered, f)
    with pytest.raises(WorktreeSecurityError, match="whitespace-only"):
        manager.inspect(d.worktree_id)
    assert not manager.verify(d.worktree_id).is_valid

    # Non-string types in string fields
    for invalid_val in [123, True, ["list"], {"dict": 1}]:
        tampered = copy.deepcopy(original_data)
        tampered["request"]["project_id"] = invalid_val
        with open(meta_path, 'w', encoding='utf-8') as f:
            json.dump(tampered, f)
        with pytest.raises(WorktreeSecurityError, match="must be a string"):
            manager.inspect(d.worktree_id)
        assert not manager.verify(d.worktree_id).is_valid

def test_registry_collision_foreign_evidence(manager, test_repo):
    repo_path, commit_hash = test_repo
    req = WorktreeRequest("pforeign", "t1", "DEV", "sesscoll", commit_hash)
    worktree_id = manager._compute_worktree_id(req.project_id, req.task_id, req.actor_role, req.host_session_id)
    meta_path = os.path.join(manager.registry_dir, f"{worktree_id}.json")

    # Pre-create foreign registry file
    foreign_content = json.dumps({"foreign": "unauthorized_data"}, ensure_ascii=False).encode('utf-8')
    with open(meta_path, 'wb') as f:
        f.write(foreign_content)

    # Calling create_worktree should detect existing_foreign_registry collision
    with pytest.raises(WorktreeError) as exc_info:
        manager.create_worktree(req)

    # Assert structured error evidence accurately reflects the site
    assert exc_info.value.recovery_required is True
    assert exc_info.value.branch_retained is True
    assert exc_info.value.registry_retained is True
    assert exc_info.value.tmp_retained is True
    assert "existing_foreign_registry" in str(exc_info.value)

    # Assert foreign file was NOT deleted or overwritten
    assert os.path.exists(meta_path)
    with open(meta_path, 'rb') as f:
        assert f.read() == foreign_content

    # Assert branch was created and retained
    branch_name = f"agent-branch-{worktree_id}"
    branch_check = subprocess.run(["git", "rev-parse", "--verify", branch_name], cwd=repo_path, stdout=subprocess.PIPE, text=True)
    assert branch_check.returncode == 0

def test_atomic_publish_concurrency_isolation(manager, test_repo):
    # DEF-T0023-23: Test that inspect/list cannot observe partial/corrupted registry during write
    _, commit_hash = test_repo
    req = WorktreeRequest("patomic", "t1", "DEV", "sessatomic", commit_hash)
    worktree_id = manager._compute_worktree_id(req.project_id, req.task_id, req.actor_role, req.host_session_id)

    write_started_event = threading.Event()
    allow_publish_event = threading.Event()
    inspect_result = {}

    original_publish = manager._publish_registry_create_if_absent
    def hooked_publish(tmp_path, meta_path):
        write_started_event.set()
        allow_publish_event.wait(timeout=5.0)
        return original_publish(tmp_path, meta_path)

    manager._publish_registry_create_if_absent = hooked_publish

    def create_worker():
        try:
            return manager.create_worktree(req)
        except Exception as e:
            return e

    def inspect_worker():
        write_started_event.wait(timeout=5.0)
        # At this point, temp file is written but publish hasn't occurred
        try:
            manager.inspect(worktree_id)
            inspect_result["status"] = "found"
        except WorktreeError as e:
            if "not found" in str(e).lower():
                inspect_result["status"] = "not_found"
            else:
                inspect_result["status"] = f"corrupt_or_error: {e}"
        except Exception as e:
            inspect_result["status"] = f"other_error: {e}"

        # list_worktrees should also not see the temp file
        listed = manager.list_worktrees(req.project_id)
        inspect_result["listed_count"] = len(listed)

        allow_publish_event.set()

    t_create = threading.Thread(target=create_worker)
    t_inspect = threading.Thread(target=inspect_worker)

    t_create.start()
    t_inspect.start()

    t_create.join(timeout=10.0)
    t_inspect.join(timeout=10.0)

    manager._publish_registry_create_if_absent = original_publish

    # Inspect worker must have seen "not_found", NOT corrupted or partial JSON!
    assert inspect_result.get("status") == "not_found"
    assert inspect_result.get("listed_count") == 0

    # After completion, descriptor is valid and inspect works
    desc = manager.inspect(worktree_id)
    assert desc.worktree_id == worktree_id

def test_dual_repo_identity_binding(tmp_path):
    # DEF-T0023-24: Two repos with identical commit SHA sharing controlled_root must reject each other's registry
    repo_a = tmp_path / "repo_a"
    repo_b = tmp_path / "repo_b"
    for r in [repo_a, repo_b]:
        r.mkdir()
        subprocess.run(["git", "init"], cwd=str(r), check=True)
        (r / "init.txt").write_text("common_content")
        subprocess.run(["git", "add", "init.txt"], cwd=str(r), check=True)
        subprocess.run(["git", "commit", "-m", "init"], cwd=str(r), check=True)

    commit_a = subprocess.run(["git", "rev-parse", "HEAD"], cwd=str(repo_a), stdout=subprocess.PIPE, text=True).stdout.strip()
    commit_b = subprocess.run(["git", "rev-parse", "HEAD"], cwd=str(repo_b), stdout=subprocess.PIPE, text=True).stdout.strip()

    controlled_root = tmp_path / "shared_controlled_root"
    controlled_root.mkdir()

    manager_a = WorktreeManager(str(controlled_root), str(repo_a))
    manager_b = WorktreeManager(str(controlled_root), str(repo_b))

    assert manager_a.repository_identity != manager_b.repository_identity
    assert manager_a.git_common_dir != manager_b.git_common_dir

    req_a = WorktreeRequest("proj_dual", "t1", "DEV", "sessA", commit_a)
    desc_a = manager_a.create_worktree(req_a)

    # Manager A can inspect and verify
    assert manager_a.inspect(desc_a.worktree_id).worktree_id == desc_a.worktree_id
    assert manager_a.verify(desc_a.worktree_id).is_valid is True

    # Manager B MUST reject Manager A's worktree registry
    with pytest.raises(WorktreeSecurityError, match="Registry repository identity mismatch"):
        manager_b.inspect(desc_a.worktree_id)

    assert manager_b.verify(desc_a.worktree_id).is_valid is False

    # Manager B list_worktrees should NOT include Manager A's worktree
    assert len(manager_b.list_worktrees("proj_dual")) == 0
    assert len(manager_a.list_worktrees("proj_dual")) == 1

def test_unambiguous_worktree_id_boundary_collision(manager, test_repo):
    # DEF-T0023-25: Different field combinations that would collide under hyphen concatenation must produce distinct IDs
    _, commit_hash = test_repo
    req1 = WorktreeRequest(project_id="proj-task", task_id="sub", actor_role="DEV", host_session_id="sess", baseline_commit=commit_hash)
    req2 = WorktreeRequest(project_id="proj", task_id="task-sub", actor_role="DEV", host_session_id="sess", baseline_commit=commit_hash)

    id1 = manager._compute_worktree_id(req1.project_id, req1.task_id, req1.actor_role, req1.host_session_id)
    id2 = manager._compute_worktree_id(req2.project_id, req2.task_id, req2.actor_role, req2.host_session_id)

    # Under old formula, both were 'proj-task-sub-DEV-sess'
    assert id1 != id2

    d1 = manager.create_worktree(req1)
    d2 = manager.create_worktree(req2)

    assert d1.worktree_id != d2.worktree_id
    assert d1.absolute_path != d2.absolute_path
    assert d1.branch_name != d2.branch_name

    assert manager.inspect(d1.worktree_id).request == req1
    assert manager.inspect(d2.worktree_id).request == req2
    assert manager.verify(d1.worktree_id).is_valid is True
    assert manager.verify(d2.worktree_id).is_valid is True

def test_no_destructive_operations_in_product_code():
    manager_file = os.path.join(os.path.dirname(__file__), "..", "scripts", "_lib", "core", "worktree_manager.py")
    with open(manager_file, "r", encoding="utf-8") as f:
        code = f.read()

    assert "branch -D" not in code
    assert "worktree remove" not in code
    assert "git prune" not in code
    assert "shell=True" not in code
