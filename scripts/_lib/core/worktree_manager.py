import os
import re
import subprocess
import time
import json
from typing import List, Dict, Any

from .worktree_schema import (
    WorktreeRequest, WorktreeDescriptor, WorktreeStatus,
    WorktreeError, WorktreeSecurityError, WorktreeGitError
)

class WorktreeManager:
    def __init__(self, controlled_root: str, target_repo_path: str):
        self.controlled_root = os.path.realpath(os.path.abspath(controlled_root))
        self.target_repo_path = os.path.realpath(os.path.abspath(target_repo_path))
        os.makedirs(self.controlled_root, exist_ok=True)
        self.registry_dir = os.path.join(self.controlled_root, ".registry")
        os.makedirs(self.registry_dir, exist_ok=True)
        self._verify_repo_identity(self.target_repo_path)
        self._repo_git_dir = self._get_absolute_git_dir(self.target_repo_path)

    def _run_git(self, cwd: str, args: List[str]) -> str:
        cmd = ["git"] + args
        try:
            result = subprocess.run(cmd, cwd=cwd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, shell=False, check=True)
            return result.stdout.strip()
        except subprocess.CalledProcessError as e:
            raise WorktreeGitError(f"Git command failed: {' '.join(cmd)}\nStderr: {e.stderr.strip()}")

    def _get_absolute_git_dir(self, repo_path: str) -> str:
        try:
            return self._run_git(repo_path, ["rev-parse", "--absolute-git-dir"])
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
        if not worktree_id or not re.match(r'^[\w\-]{1,128}$', worktree_id):
            raise WorktreeSecurityError(f"Invalid worktree_id format: {worktree_id}")

        path = os.path.join(self.controlled_root, worktree_id)
        real_path = os.path.realpath(path)

        if os.path.commonpath([self.controlled_root, real_path]) != self.controlled_root:
            raise WorktreeSecurityError("Path traversal detected.")
        if real_path == self.controlled_root or real_path == self.registry_dir:
            raise WorktreeSecurityError("Path collision.")

        return real_path

    def _check_branch_name(self, branch_name: str):
        try:
            self._run_git(self.target_repo_path, ["check-ref-format", "--branch", branch_name])
        except WorktreeGitError:
            raise WorktreeSecurityError(f"Invalid branch name format: {branch_name}")

    def _verify_commit(self, commit: str) -> str:
        if not re.match(r'^[0-9a-f]{40}$', commit.lower()):
             raise WorktreeSecurityError(f"Baseline commit must be a full 40-char SHA: {commit}")
        try:
            return self._run_git(self.target_repo_path, ["rev-parse", "--verify", f"{commit}^{{commit}}"]).lower()
        except WorktreeGitError:
            raise WorktreeError(f"Commit {commit} does not exist in target repository.")

    def create_worktree(self, request: WorktreeRequest) -> WorktreeDescriptor:
        worktree_id = f"{request.project_id}-{request.task_id}-{request.actor_role}-{request.host_session_id}"
        path = self._safe_path(worktree_id)
        branch_name = f"agent-branch-{worktree_id}"

        self._check_branch_name(branch_name)
        canonical_commit = self._verify_commit(request.baseline_commit)

        if request.baseline_commit != canonical_commit:
             raise WorktreeSecurityError("Baseline commit not provided as lowercase canonical SHA")

        if os.path.exists(path):
            raise WorktreeSecurityError(f"Worktree path already exists: {path}")

        meta_path = os.path.join(self.registry_dir, f"{worktree_id}.json")
        desc = WorktreeDescriptor(
            worktree_id=worktree_id,
            absolute_path=path,
            branch_name=branch_name,
            baseline_commit=canonical_commit,
            created_at=time.time(),
            request=request
        )
        meta_dict = {
            "worktree_id": desc.worktree_id,
            "absolute_path": desc.absolute_path,
            "branch_name": desc.branch_name,
            "baseline_commit": desc.baseline_commit,
            "created_at": desc.created_at,
            "request": {
                "project_id": request.project_id,
                "task_id": request.task_id,
                "actor_role": request.actor_role,
                "host_session_id": request.host_session_id,
                "baseline_commit": request.baseline_commit
            }
        }
        meta_bytes = json.dumps(meta_dict, ensure_ascii=False).encode('utf-8')

        # Atomic registry creation
        try:
            fd = os.open(meta_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
            try:
                os.write(fd, meta_bytes)
            finally:
                os.close(fd)
        except (FileExistsError, OSError):
            raise WorktreeSecurityError(f"Worktree registry already exists for {worktree_id}")

        try:
            self._run_git(self.target_repo_path, ["branch", branch_name, canonical_commit])
        except WorktreeGitError as e:
            if "already exists" in str(e):
                raise WorktreeSecurityError(f"Branch {branch_name} already exists.")
            raise

        try:
            self._run_git(self.target_repo_path, ["worktree", "add", path, branch_name])
        except WorktreeGitError as e:
            raise WorktreeError(f"Git worktree add failed, residual branch '{branch_name}' retained. recovery_required=True. Stderr: {str(e)}")

        return desc

    def inspect(self, worktree_id: str) -> WorktreeDescriptor:
        meta_path = os.path.join(self.registry_dir, f"{worktree_id}.json")
        if not os.path.exists(meta_path):
            raise WorktreeError(f"Registry not found: {worktree_id}")

        try:
            with open(meta_path, 'r', encoding='utf-8') as f:
                data = json.load(f)
        except Exception as e:
            raise WorktreeSecurityError(f"Corrupt registry for {worktree_id}: {str(e)}")

        req_data = data.get("request", {})
        if not isinstance(req_data, dict):
             raise WorktreeSecurityError("Corrupt registry: missing or invalid request object")

        try:
            req = WorktreeRequest(
                project_id=req_data["project_id"],
                task_id=req_data["task_id"],
                actor_role=req_data["actor_role"],
                host_session_id=req_data["host_session_id"],
                baseline_commit=req_data["baseline_commit"]
            )
        except KeyError as e:
             raise WorktreeSecurityError(f"Corrupt registry: missing key {str(e)}")

        rebuilt_id = f"{req.project_id}-{req.task_id}-{req.actor_role}-{req.host_session_id}"
        expected_path = self._safe_path(rebuilt_id)
        expected_branch = f"agent-branch-{rebuilt_id}"

        if rebuilt_id != worktree_id:
            raise WorktreeSecurityError("Registry spoofed: reconstructed ID mismatch.")
        if data.get("worktree_id") != worktree_id:
            raise WorktreeSecurityError("Registry spoofed: worktree_id mismatch.")
        if data.get("absolute_path") != expected_path:
            raise WorktreeSecurityError("Registry spoofed: absolute_path mismatch.")
        if data.get("branch_name") != expected_branch:
            raise WorktreeSecurityError("Registry spoofed: branch_name mismatch.")
        if data.get("baseline_commit") != req.baseline_commit:
            raise WorktreeSecurityError("Registry spoofed: baseline_commit mismatch.")

        return WorktreeDescriptor(
            worktree_id=data["worktree_id"],
            absolute_path=data["absolute_path"],
            branch_name=data["branch_name"],
            baseline_commit=data["baseline_commit"],
            created_at=data["created_at"],
            request=req
        )

    def verify(self, worktree_id: str) -> WorktreeStatus:
        try:
            desc = self.inspect(worktree_id)

            common_dir = self._get_common_dir(desc.absolute_path)
            repo_common_dir = self._get_common_dir(self.target_repo_path)
            if common_dir != repo_common_dir:
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
        except (WorktreeGitError, WorktreeError, OSError):
            return WorktreeStatus(False, False, "", "", 0, 0)

    def list_worktrees(self, project_id: str) -> List[WorktreeDescriptor]:
        results = []
        if not os.path.exists(self.registry_dir):
            return results

        for fname in os.listdir(self.registry_dir):
            if not fname.endswith(".json"): continue
            wid = fname[:-5]
            try:
                desc = self.inspect(wid)
                if desc.request.project_id == project_id:
                    results.append(desc)
            except (WorktreeError, WorktreeSecurityError):
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
