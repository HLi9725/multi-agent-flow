import os
import re
import subprocess
import time
import json
import stat
import hashlib
from typing import List, Dict, Any

from .worktree_schema import (
    WorktreeRequest, WorktreeDescriptor, WorktreeStatus,
    WorktreeError, WorktreeSecurityError, WorktreeGitError
)

def _validate_request_field(val: Any, field_name: str) -> str:
    if not isinstance(val, str):
        raise WorktreeSecurityError(f"Invalid {field_name}: must be a string, got {type(val).__name__}")
    if not val:
        raise WorktreeSecurityError(f"Invalid {field_name}: cannot be empty")
    if not val.strip():
        raise WorktreeSecurityError(f"Invalid {field_name}: cannot be whitespace-only")
    if re.search(r'[\x00-\x1f\x7f]', val):
        raise WorktreeSecurityError(f"Invalid {field_name}: contains control characters")
    if '/' in val or '\\' in val:
        raise WorktreeSecurityError(f"Invalid {field_name}: path separators are forbidden: {val}")
    if val.startswith('-'):
        raise WorktreeSecurityError(f"Invalid {field_name}: leading options are forbidden: {val}")
    if val == '.' or val == '..' or '..' in val:
        raise WorktreeSecurityError(f"Invalid {field_name}: relative path segments are forbidden: {val}")
    if os.path.isabs(val) or (len(val) >= 2 and val[1] == ':'):
        raise WorktreeSecurityError(f"Invalid {field_name}: absolute paths are forbidden: {val}")
    if not re.match(r'^[a-zA-Z0-9_][a-zA-Z0-9_\-\.]{0,127}$', val):
        raise WorktreeSecurityError(f"Invalid {field_name} format: {val}")
    return val


def _validate_host_session_id(val: Any) -> str:
    """Validate an opaque Host session identifier without treating it as a path."""
    field_name = "host_session_id"
    if not isinstance(val, str):
        raise WorktreeSecurityError(f"Invalid {field_name}: must be a string, got {type(val).__name__}")
    if not val:
        raise WorktreeSecurityError(f"Invalid {field_name}: cannot be empty")
    if not val.strip():
        raise WorktreeSecurityError(f"Invalid {field_name}: cannot be whitespace-only")
    if re.search(r'[\x00-\x1f\x7f]', val):
        raise WorktreeSecurityError(f"Invalid {field_name}: contains control characters")
    if '/' in val or '\\' in val or '..' in val:
        raise WorktreeSecurityError(f"Invalid {field_name}: path segments are forbidden: {val}")
    if val.startswith('-'):
        raise WorktreeSecurityError(f"Invalid {field_name}: leading options are forbidden: {val}")
    if not re.fullmatch(r'[a-zA-Z0-9_][a-zA-Z0-9_\-\.:@]{0,511}', val):
        raise WorktreeSecurityError(f"Invalid {field_name} format: {val}")
    return val

class WorktreeManager:
    def __init__(self, controlled_root: str, target_repo_path: str):
        self.controlled_root = os.path.normcase(os.path.realpath(os.path.abspath(controlled_root)))
        if not os.path.isdir(target_repo_path):
            raise WorktreeError(f"Target repo path does not exist or is not a directory: {target_repo_path}")
        self._verify_repo_identity(target_repo_path)

        raw_toplevel = self._run_git(target_repo_path, ["rev-parse", "--show-toplevel"])
        if not os.path.isabs(raw_toplevel):
            raw_toplevel = os.path.join(target_repo_path, raw_toplevel)
        self.target_repo_root = os.path.normcase(os.path.realpath(raw_toplevel))
        self.target_repo_path = self.target_repo_root

        os.makedirs(self.controlled_root, exist_ok=True)
        self.registry_dir = os.path.join(self.controlled_root, ".registry")
        os.makedirs(self.registry_dir, exist_ok=True)
        self._validate_registry_boundary()

        self._repo_git_dir = self._get_absolute_git_dir(self.target_repo_root)
        self.git_common_dir = self._get_common_dir(self.target_repo_root)
        self.repository_identity = self._compute_repo_identity(self.target_repo_root, self.git_common_dir)

    def _compute_repo_identity(self, repo_root: str, common_dir: str) -> str:
        raw = f"root:{os.path.normcase(repo_root)}|common:{os.path.normcase(common_dir)}"
        digest = hashlib.sha256(raw.encode('utf-8')).hexdigest()
        return f"repo-identity-{digest[:32]}"

    def _compute_worktree_id(self, project_id: str, task_id: str, actor_role: str, host_session_id: str) -> str:
        _validate_request_field(project_id, "project_id")
        _validate_request_field(task_id, "task_id")
        _validate_request_field(actor_role, "actor_role")
        _validate_host_session_id(host_session_id)

        payload = json.dumps({
            "actor_role": actor_role,
            "host_session_id": host_session_id,
            "project_id": project_id,
            "task_id": task_id
        }, sort_keys=True, separators=(',', ':'), ensure_ascii=False).encode('utf-8')
        digest = hashlib.sha256(payload).hexdigest()

        # Prefixes remain readable, while the complete digest keeps the ID fixed
        # length and binds every untruncated input field without delimiter ambiguity.
        return f"{project_id[:12]}_{task_id[:12]}_{actor_role[:8]}_{digest}"

    def _validate_registry_boundary(self) -> str:
        expected = os.path.normcase(os.path.abspath(self.registry_dir))
        actual = os.path.normcase(os.path.realpath(self.registry_dir))
        if actual != expected:
            raise WorktreeSecurityError("Registry directory is a symbolic link or Junction.")
        if os.path.commonpath([self.controlled_root, actual]) != self.controlled_root:
            raise WorktreeSecurityError("Registry path traversal detected.")
        return actual

    def _publish_registry_create_if_absent(self, tmp_path: str, meta_path: str):
        if os.name == 'nt':
            os.rename(tmp_path, meta_path)
        else:
            os.link(tmp_path, meta_path)
            try:
                os.unlink(tmp_path)
            except OSError as e:
                raise WorktreeError(
                    "Registry published but owned temporary link cleanup failed: "
                    f"{str(e)}. recovery_required=True, branch_retained=True, "
                    "registry_retained=True, tmp_retained=True",
                    recovery_required=True,
                    branch_retained=True,
                    registry_retained=True,
                    tmp_retained=True,
                )

    def _run_git(self, cwd: str, args: List[str]) -> str:
        cmd = ["git"] + args
        try:
            result = subprocess.run(cmd, cwd=cwd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, shell=False, check=True)
            return result.stdout.strip()
        except subprocess.CalledProcessError as e:
            raise WorktreeGitError(f"Git command failed: {' '.join(cmd)}\nStderr: {e.stderr.strip()}")

    def _get_absolute_git_dir(self, repo_path: str) -> str:
        try:
            return os.path.normcase(os.path.realpath(self._run_git(repo_path, ["rev-parse", "--absolute-git-dir"])))
        except WorktreeGitError as e:
            raise WorktreeError(f"Failed to get git dir for {repo_path}: {str(e)}")

    def _get_common_dir(self, cwd: str) -> str:
        d = self._run_git(cwd, ["rev-parse", "--git-common-dir"])
        if not os.path.isabs(d):
            d = os.path.join(cwd, d)
        return os.path.normcase(os.path.realpath(d))

    def _verify_repo_identity(self, repo_path: str):
        if not os.path.isdir(repo_path):
            raise WorktreeError(f"Target repo path does not exist or is not a directory: {repo_path}")
        try:
            self._run_git(repo_path, ["rev-parse", "--is-inside-work-tree"])
        except WorktreeGitError:
            raise WorktreeError(f"Path is not a valid git repository: {repo_path}")

    def _safe_path(self, worktree_id: str) -> str:
        if not worktree_id or not re.fullmatch(r'^[a-zA-Z0-9_][a-zA-Z0-9_\-\.]{1,127}$', worktree_id):
            raise WorktreeSecurityError(f"Invalid worktree_id format: {worktree_id}")

        path = os.path.join(self.controlled_root, worktree_id)
        real_path = os.path.normcase(os.path.realpath(path))

        if os.path.commonpath([self.controlled_root, real_path]) != self.controlled_root:
            raise WorktreeSecurityError("Path traversal detected.")
        if real_path == self.controlled_root or real_path == os.path.normcase(self.registry_dir):
            raise WorktreeSecurityError("Path collision.")

        return real_path

    def _check_branch_name(self, branch_name: str):
        try:
            self._run_git(self.target_repo_path, ["check-ref-format", "--branch", branch_name])
        except WorktreeGitError:
            raise WorktreeSecurityError(f"Invalid branch name format: {branch_name}")

    def _verify_commit(self, commit: str) -> str:
        if not isinstance(commit, str):
            raise WorktreeSecurityError("Baseline commit must be a string")
        if not re.match(r'^[0-9a-f]{40}$', commit):
             raise WorktreeSecurityError(f"Baseline commit must be a lowercase full 40-char SHA: {commit}")
        try:
            parsed = self._run_git(self.target_repo_path, ["rev-parse", "--verify", f"{commit}^{{commit}}"])
            if parsed != commit:
                 raise WorktreeSecurityError("Baseline commit canonical mismatch")
            return parsed
        except WorktreeGitError:
            raise WorktreeError(f"Commit {commit} does not exist in target repository.")

    def create_worktree(self, request: WorktreeRequest) -> WorktreeDescriptor:
        canonical_commit = self._verify_commit(request.baseline_commit)
        if request.baseline_commit != canonical_commit:
             raise WorktreeSecurityError("Baseline commit not provided as lowercase canonical SHA")

        worktree_id = self._compute_worktree_id(
            request.project_id,
            request.task_id,
            request.actor_role,
            request.host_session_id
        )
        path = self._safe_path(worktree_id)
        branch_name = f"agent-branch-{worktree_id}"

        self._check_branch_name(branch_name)

        if os.path.exists(path):
            raise WorktreeSecurityError(f"Worktree path already exists: {path}")

        meta_path = os.path.join(self.registry_dir, f"{worktree_id}.json")
        self._validate_registry_boundary()
        desc = WorktreeDescriptor(
            worktree_id=worktree_id,
            absolute_path=path,
            branch_name=branch_name,
            baseline_commit=canonical_commit,
            created_at=time.time(),
            target_repo_root=self.target_repo_root,
            git_common_dir=self.git_common_dir,
            repository_identity=self.repository_identity,
            request=request
        )
        meta_dict = {
            "worktree_id": desc.worktree_id,
            "absolute_path": desc.absolute_path,
            "branch_name": desc.branch_name,
            "baseline_commit": desc.baseline_commit,
            "created_at": desc.created_at,
            "target_repo_root": desc.target_repo_root,
            "git_common_dir": desc.git_common_dir,
            "repository_identity": desc.repository_identity,
            "request": {
                "project_id": request.project_id,
                "task_id": request.task_id,
                "actor_role": request.actor_role,
                "host_session_id": request.host_session_id,
                "baseline_commit": request.baseline_commit
            }
        }
        meta_bytes = json.dumps(meta_dict, ensure_ascii=False).encode('utf-8')

        # 1. Create Git branch as atomic cross-process lock
        try:
            self._run_git(self.target_repo_path, ["branch", branch_name, canonical_commit])
        except WorktreeGitError as e:
            if "already exists" in str(e):
                raise WorktreeSecurityError(f"Branch {branch_name} already exists.")
            raise

        # 2. Atomic publish of Registry via create-if-absent
        tmp_filename = f".tmp-{worktree_id}-{os.getpid()}-{time.time_ns()}.json"
        tmp_path = os.path.join(self.registry_dir, tmp_filename)
        tmp_created = False
        try:
            fd = os.open(tmp_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
            tmp_created = True
            try:
                view = memoryview(meta_bytes)
                total = len(view)
                written = 0
                while written < total:
                    w = os.write(fd, view[written:])
                    if w <= 0:
                        raise OSError("os.write returned short count")
                    written += w
                os.fsync(fd)
            finally:
                os.close(fd)
        except Exception as e:
            raise WorktreeError(f"Registry temp write failed (partial_owned_temp): {str(e)}. recovery_required=True, branch_retained=True, registry_retained=False, tmp_retained={tmp_created}", recovery_required=True, branch_retained=True, registry_retained=False, tmp_retained=tmp_created)

        try:
            self._publish_registry_create_if_absent(tmp_path, meta_path)
        except FileExistsError as e:
            raise WorktreeError(f"Registry collision (existing_foreign_registry): {meta_path} already exists. recovery_required=True, branch_retained=True, registry_retained=True, tmp_retained=True", recovery_required=True, branch_retained=True, registry_retained=True, tmp_retained=True)
        except OSError as e:
            raise WorktreeError(f"Registry publish failed: {str(e)}. recovery_required=True, branch_retained=True, registry_retained=False, tmp_retained=True", recovery_required=True, branch_retained=True, registry_retained=False, tmp_retained=True)

        # 3. Create Worktree
        try:
            self._run_git(self.target_repo_path, ["worktree", "add", path, branch_name])
        except WorktreeGitError as e:
            raise WorktreeError(f"Git worktree add failed. recovery_required=True, branch_retained=True, registry_retained=True. Stderr: {str(e)}", recovery_required=True, branch_retained=True, registry_retained=True)

        return desc

    def inspect(self, worktree_id: str) -> WorktreeDescriptor:
        if not isinstance(worktree_id, str) or not worktree_id:
            raise WorktreeSecurityError(f"Invalid worktree_id: must be a non-empty string, got {type(worktree_id).__name__}")
        if not re.fullmatch(r'^[a-zA-Z0-9_][a-zA-Z0-9_\-\.]{1,127}$', worktree_id):
            raise WorktreeSecurityError(f"Invalid worktree_id format: {worktree_id}")
        if '/' in worktree_id or '\\' in worktree_id or '..' in worktree_id:
            raise WorktreeSecurityError(f"Path traversal or separator in worktree_id forbidden: {worktree_id}")
        if worktree_id.startswith('-') or (len(worktree_id) >= 2 and worktree_id[1] == ':'):
            raise WorktreeSecurityError(f"Invalid worktree_id prefix or drive format: {worktree_id}")

        registry_root = self._validate_registry_boundary()
        meta_path = os.path.join(self.registry_dir, f"{worktree_id}.json")
        real_meta_path = os.path.normcase(os.path.realpath(meta_path))
        if os.path.commonpath([registry_root, real_meta_path]) != registry_root:
            raise WorktreeSecurityError("Registry path traversal detected.")

        if not os.path.exists(meta_path):
            raise WorktreeError(f"Registry not found: {worktree_id}")

        try:
            with open(meta_path, 'r', encoding='utf-8') as f:
                data = json.load(f)
        except Exception as e:
            raise WorktreeSecurityError(f"Corrupt registry for {worktree_id}: {str(e)}")

        if not isinstance(data, dict):
            raise WorktreeSecurityError("Corrupt registry: JSON root must be an object")

        EXPECTED_ROOT_KEYS = {
            "worktree_id", "absolute_path", "branch_name", "baseline_commit",
            "created_at", "target_repo_root", "git_common_dir", "repository_identity",
            "request"
        }
        if set(data.keys()) != EXPECTED_ROOT_KEYS:
            raise WorktreeSecurityError(f"Corrupt registry: top-level keys mismatch. Expected {EXPECTED_ROOT_KEYS}, got {set(data.keys())}")

        def _assert_str(val, name):
            if not isinstance(val, str):
                raise WorktreeSecurityError(f"Corrupt registry: {name} must be a string, got {type(val).__name__}")
            if not val:
                raise WorktreeSecurityError(f"Corrupt registry: {name} cannot be empty")
            if not val.strip():
                raise WorktreeSecurityError(f"Corrupt registry: {name} cannot be whitespace-only")
            if re.search(r'[\x00-\x1f\x7f]', val):
                raise WorktreeSecurityError(f"Corrupt registry: {name} contains control characters")

        def _assert_float(val, name):
            if isinstance(val, bool) or not isinstance(val, (int, float)):
                raise WorktreeSecurityError(f"Corrupt registry: {name} must be a number")
            import math
            if math.isnan(val) or math.isinf(val) or val <= 0 or val > time.time() + 86400:
                raise WorktreeSecurityError(f"Corrupt registry: {name} invalid time")

        _assert_str(data["worktree_id"], "worktree_id")
        _assert_str(data["absolute_path"], "absolute_path")
        _assert_str(data["branch_name"], "branch_name")
        _assert_str(data["baseline_commit"], "baseline_commit")
        _assert_float(data["created_at"], "created_at")
        _assert_str(data["target_repo_root"], "target_repo_root")
        _assert_str(data["git_common_dir"], "git_common_dir")
        _assert_str(data["repository_identity"], "repository_identity")

        # DEF-T0023-24: 1:1 repository identity check
        if os.path.normcase(os.path.realpath(data["target_repo_root"])) != os.path.normcase(self.target_repo_root):
            raise WorktreeSecurityError("Registry repository identity mismatch: target_repo_root")
        if os.path.normcase(os.path.realpath(data["git_common_dir"])) != os.path.normcase(self.git_common_dir):
            raise WorktreeSecurityError("Registry repository identity mismatch: git_common_dir")
        if data["repository_identity"] != self.repository_identity:
            raise WorktreeSecurityError("Registry repository identity mismatch: repository_identity")

        req_data = data["request"]
        if not isinstance(req_data, dict):
             raise WorktreeSecurityError("Corrupt registry: missing or invalid request object")

        EXPECTED_REQ_KEYS = {"project_id", "task_id", "actor_role", "host_session_id", "baseline_commit"}
        if set(req_data.keys()) != EXPECTED_REQ_KEYS:
            raise WorktreeSecurityError(f"Corrupt registry: request keys mismatch. Expected {EXPECTED_REQ_KEYS}, got {set(req_data.keys())}")

        _assert_str(req_data["project_id"], "request.project_id")
        _assert_str(req_data["task_id"], "request.task_id")
        _assert_str(req_data["actor_role"], "request.actor_role")
        _assert_str(req_data["host_session_id"], "request.host_session_id")
        _assert_str(req_data["baseline_commit"], "request.baseline_commit")

        if data["baseline_commit"] != req_data["baseline_commit"]:
            raise WorktreeSecurityError("Corrupt registry: baseline_commit mismatch between outer and request")

        req = WorktreeRequest(
            project_id=req_data["project_id"],
            task_id=req_data["task_id"],
            actor_role=req_data["actor_role"],
            host_session_id=req_data["host_session_id"],
            baseline_commit=req_data["baseline_commit"]
        )

        parsed_commit = self._verify_commit(req.baseline_commit)
        if parsed_commit != data["baseline_commit"]:
            raise WorktreeSecurityError("Registry spoofed: baseline_commit canonical mismatch")

        rebuilt_id = self._compute_worktree_id(req.project_id, req.task_id, req.actor_role, req.host_session_id)
        expected_path = self._safe_path(rebuilt_id)
        expected_branch = f"agent-branch-{rebuilt_id}"

        if rebuilt_id != worktree_id or data["worktree_id"] != worktree_id:
            raise WorktreeSecurityError("Registry spoofed: reconstructed ID mismatch.")
        if data["absolute_path"] != expected_path:
            raise WorktreeSecurityError("Registry spoofed: absolute_path mismatch.")
        if data["branch_name"] != expected_branch:
            raise WorktreeSecurityError("Registry spoofed: branch_name mismatch.")

        return WorktreeDescriptor(
            worktree_id=data["worktree_id"],
            absolute_path=data["absolute_path"],
            branch_name=data["branch_name"],
            baseline_commit=data["baseline_commit"],
            created_at=data["created_at"],
            target_repo_root=data["target_repo_root"],
            git_common_dir=data["git_common_dir"],
            repository_identity=data["repository_identity"],
            request=req
        )

    def verify(self, worktree_id: str) -> WorktreeStatus:
        try:
            desc = self.inspect(worktree_id)

            common_dir = self._get_common_dir(desc.absolute_path)
            if common_dir != self.git_common_dir:
                 return WorktreeStatus(False, False, "", "", 0, 0)

            current_commit = self._run_git(desc.absolute_path, ["rev-parse", "HEAD"])
            head_ref = self._run_git(desc.absolute_path, ["rev-parse", "--abbrev-ref", "HEAD"])

            status_out = self._run_git(desc.absolute_path, ["status", "--porcelain"])
            untracked = 0
            modified = 0
            for line in status_out.split('\n'):
                line = line.strip()
                if not line: continue
                if line.startswith('??'):
                    untracked += 1
                else:
                    modified += 1

            is_clean = (untracked == 0 and modified == 0)
            is_valid = (head_ref == desc.branch_name)

            return WorktreeStatus(
                is_valid=is_valid,
                is_clean=is_clean,
                current_commit=current_commit,
                head_ref=head_ref,
                untracked_files=untracked,
                modified_files=modified
            )
        except (WorktreeGitError, WorktreeError, OSError, Exception):
            return WorktreeStatus(False, False, "", "", 0, 0)

    def list_worktrees(self, project_id: str) -> List[WorktreeDescriptor]:
        results = []
        self._validate_registry_boundary()
        if not os.path.exists(self.registry_dir):
            return results

        for fname in os.listdir(self.registry_dir):
            if not fname.endswith(".json") or fname.startswith("."):
                continue
            wid = fname[:-5]
            try:
                desc = self.inspect(wid)
                if desc.request.project_id == project_id:
                    results.append(desc)
            except WorktreeSecurityError as e:
                # If repository identity doesn't match this Manager, skip foreign repo worktree
                if "repository identity mismatch" in str(e).lower():
                    continue
                raise WorktreeError(f"Corrupt registry detected during list: {wid}")
            except WorktreeError:
                raise WorktreeError(f"Corrupt registry detected during list: {wid}")
        return results

    def get_cleanup_plan(self, worktree_id: str) -> Dict[str, Any]:
        desc = self.inspect(worktree_id)
        plan = {
            "action": "Cleanup Plan",
            "target_worktree": desc.absolute_path,
            "target_branch": desc.branch_name,
            "requires_user_confirmation": True,
            "recovery_reason": "Automated deletion is prohibited in Phase 2C.",
            "risks": "Removing worktree and branch might lead to loss of uncommitted work."
        }
        return plan
