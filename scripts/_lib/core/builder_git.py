"""Literal, NUL-delimited candidate staging; never force-add control data."""
import hashlib
import os
import subprocess


def is_control_path(path: str) -> bool:
    path = path.replace("\\", "/")
    return (
        path == ".yy-flow" or path.startswith(".yy-flow/")
        or path.startswith("user_data/board.json")
        or path.startswith(("user_data/locks/", "user_data/logs/"))
        or path == "config/workflow.config.yaml"
    )


def git_paths(worktree: str, *args: str) -> list[str]:
    raw = subprocess.check_output(["git", *args], cwd=worktree)
    return [os.fsdecode(value) for value in raw.split(b"\0") if value]


def candidate_paths(worktree: str) -> list[str]:
    # --no-renames includes both the removed source and added destination.
    paths = git_paths(worktree, "diff", "--name-only", "--no-renames", "-z", "--")
    paths += git_paths(worktree, "diff", "--cached", "--name-only", "--no-renames", "-z", "--")
    paths += git_paths(worktree, "ls-files", "--others", "--exclude-standard", "-z")
    return sorted({path for path in paths if not is_control_path(path)})


def assert_safe_index(worktree: str) -> None:
    staged = git_paths(worktree, "diff", "--cached", "--name-only", "--no-renames", "-z", "--")
    if any(is_control_path(path) for path in staged):
        raise RuntimeError("Runner control files are already staged; refusing to commit or unstage user data.")


def stage_candidate(worktree: str) -> None:
    assert_safe_index(worktree)
    paths = candidate_paths(worktree)
    if not paths:
        raise RuntimeError("No business paths available for candidate staging.")
    proc = subprocess.run(
        ["git", "--literal-pathspecs", "add", "--all", "--pathspec-from-file=-", "--pathspec-file-nul"],
        cwd=worktree, input=b"\0".join(os.fsencode(path) for path in paths) + b"\0",
        capture_output=True,
    )
    if proc.returncode:
        raise RuntimeError("Failed to stage Builder changes: " + (proc.stderr or proc.stdout).decode("utf-8", "replace"))
    assert_safe_index(worktree)


def workspace_fingerprint(worktree: str) -> str:
    """Detect content changes even when porcelain status remains 'M'/'??'."""
    digest = hashlib.sha256()
    for args in (("rev-parse", "HEAD"), ("diff", "--binary", "HEAD", "--"),
                 ("diff", "--cached", "--binary", "--")):
        digest.update(subprocess.check_output(["git", *args], cwd=worktree))
    for path in candidate_paths(worktree):
        digest.update(os.fsencode(path) + b"\0")
        full = os.path.join(worktree, path)
        if os.path.islink(full):
            digest.update(os.fsencode(os.readlink(full)))
        elif os.path.isfile(full):
            with open(full, "rb") as stream:
                for chunk in iter(lambda: stream.read(65536), b""):
                    digest.update(chunk)
    return digest.hexdigest()
