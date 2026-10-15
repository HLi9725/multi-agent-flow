# -*- coding: utf-8 -*-
"""共享正本安装。先在隔离暂存区 fetch + checkout 指定提交，再替换受管正本。

默认源 HLi9725/multi-agent-flow，默认入口 main。不把功能分支写成默认稳定入口。
本模块不修改用户主目录下的客户端全局配置。无法证明可以安全热更新时拒绝更新。
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import time

_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from scripts._lib.core.file_lock import LockBusyError, acquire_lock, release_lock

DEFAULT_SOURCE = "HLi9725/multi-agent-flow"
DEFAULT_REF = "main"
SOURCE_RE = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
SHA_RE = re.compile(r"^[0-9a-fA-F]{40}$")


class InstallError(Exception):
    def __init__(self, code: str, message: str, exit_code: int = 2) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.exit_code = exit_code


def default_dest() -> str:
    return os.path.join(os.path.expanduser("~"), "agent-skills", "multi-agent-flow")


def git_argv(args: list[str]) -> list[str]:
    raw = os.environ.get("YY_FLOW_GIT_ARGV", "").strip()
    if not raw:
        return ["git", *args]
    try:
        prefix = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise InstallError("GIT", "Git 命令前缀无法解析") from exc
    if not isinstance(prefix, list) or not prefix or not all(isinstance(item, str) and item.strip() for item in prefix):
        raise InstallError("GIT", "Git 命令前缀无效")
    return [*prefix, *args]


def repo_url(source: str) -> str:
    if not isinstance(source, str) or SOURCE_RE.fullmatch(source) is None:
        raise InstallError("SOURCE", "仓库源必须是 owner/name，默认 HLi9725/multi-agent-flow")
    return "https://github.com/" + source + ".git"


def run_git(args: list[str], cwd: str | None = None) -> subprocess.CompletedProcess:
    command = git_argv(args)
    if "pull" in command:
        raise InstallError("GIT", "禁止用 git pull 切换指定版本")
    try:
        proc = subprocess.run(command, cwd=cwd, capture_output=True, text=True, timeout=120, check=False)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise InstallError("GIT", "Git 命令无法执行") from exc
    if proc.returncode != 0:
        raise InstallError("GIT", f"Git 命令失败，退出码 {proc.returncode}")
    return proc


def install(source: str, ref: str, dest: str) -> dict:
    source = source or DEFAULT_SOURCE
    ref = ref or DEFAULT_REF
    dest = os.path.abspath(dest or default_dest())
    if not ref.strip() or any(part == ".." for part in re.split(r"[\\/]", ref)):
        raise InstallError("REF", "版本引用无效")
    url = repo_url(source)
    if os.path.basename(os.path.dirname(os.path.realpath(dest))) == ".yy-flow":
        raise InstallError("DEST", "拒绝把业务项目的 .yy-flow/skill 当作共享正本")
    _reject_unsafe_dest(dest)
    parent = os.path.dirname(dest)
    os.makedirs(parent, exist_ok=True)
    staging = dest + ".staging"
    previous = dest + ".previous"
    if os.path.exists(staging):
        raise InstallError("STAGING", "存在未清理的暂存目录，已停止以免覆盖半成品")
    handle = None
    swapped = False
    parked_previous = previous + ".old"
    try:
        try:
            handle = acquire_lock(dest + ".install.lock", blocking=False, write_meta=True)
        except LockBusyError as exc:
            raise InstallError("RUNNING", "正本锁被占用。请先停用正在使用该正本的进程后再更新") from exc
        run_git(["init", staging])
        run_git(["-C", staging, "remote", "add", "origin", url])
        run_git(["-C", staging, "fetch", "origin", ref])
        run_git(["-C", staging, "checkout", "--detach", "FETCH_HEAD"])
        sha = run_git(["-C", staging, "rev-parse", "HEAD"]).stdout.strip()
        if SHA_RE.fullmatch(sha) is None:
            raise InstallError("GIT", "无法确认检出的提交")
        if os.path.isfile(os.path.join(staging, "user_data", "board.json")):
            raise InstallError("POLLUTED", "暂存内容含运行看板，拒绝应用到正本")
        if not os.path.isfile(os.path.join(staging, "scripts", "mcp_server.py")):
            raise InstallError("VERIFY", "暂存内容缺少 MCP 入口，拒绝替换正本")
        if os.path.exists(previous) and not _is_managed_copy(previous):
            raise InstallError("PREVIOUS", "旧的 previous 目录不是受管备份，拒绝替换")
        if os.path.exists(parked_previous):
            raise InstallError("PREVIOUS", "存在未完成的旧备份，已停止")
        if os.path.exists(previous):
            os.replace(previous, parked_previous)
        if os.path.exists(dest):
            os.replace(dest, previous)
            swapped = True
        os.replace(staging, dest)
        record = {
            "source": source,
            "ref": ref,
            "sha": sha.lower(),
            "previous": previous if os.path.exists(previous) else "",
            "updated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        }
        _write_markers(dest, record)
        if os.path.isdir(parked_previous):
            shutil.rmtree(parked_previous)
        return {"ok": True, "sha": sha.lower(), "source": source, "ref": ref, "dest": dest, "message": "共享正本已更新"}
    except Exception:
        _restore(dest, previous, parked_previous, swapped)
        raise
    finally:
        if handle is not None:
            release_lock(handle)
        if os.path.isdir(staging):
            shutil.rmtree(staging, ignore_errors=True)


def _reject_unsafe_dest(dest: str) -> None:
    if not os.path.exists(dest):
        return
    if os.path.isfile(os.path.join(dest, ".yy-flow-running")):
        raise InstallError("RUNNING", "正本存在运行标记 .yy-flow-running。请先停用相关进程后再更新，安装器不会结束这些进程")
    if os.path.isfile(os.path.join(dest, "user_data", "board.json")):
        raise InstallError("POLLUTED", "正本含 user_data/board.json，拒绝把运行数据当成可更新代码")
    if os.path.isdir(os.path.join(dest, ".git")):
        proc = run_git(["-C", dest, "status", "--porcelain"])
        if proc.stdout.strip():
            raise InstallError("DIRTY", "正本有未提交改动，已停止且不会自动 stash")
        return
    if not _is_managed_copy(dest):
        raise InstallError("DEST", "目标已存在且不是受管正本，拒绝覆盖")


def _is_managed_copy(path: str) -> bool:
    return os.path.isfile(os.path.join(path, "install-record.json")) or os.path.isfile(os.path.join(path, ".yy-flow-shared"))


def _write_markers(dest: str, record: dict) -> None:
    shared = os.path.join(dest, ".yy-flow-shared")
    with open(shared, "w", encoding="utf-8") as handle:
        handle.write("")
    raw = json.dumps(record, ensure_ascii=False, indent=2) + "\n"
    temporary = os.path.join(dest, ".install-record.tmp")
    with open(temporary, "w", encoding="utf-8") as handle:
        handle.write(raw)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, os.path.join(dest, "install-record.json"))


def _restore(dest: str, previous: str, parked_previous: str, swapped: bool) -> None:
    failed = dest + ".failed"
    try:
        if swapped and os.path.exists(dest):
            if os.path.exists(failed):
                shutil.rmtree(failed, ignore_errors=True)
            os.replace(dest, failed)
        if swapped and os.path.exists(previous) and not os.path.exists(dest):
            os.replace(previous, dest)
        if os.path.exists(parked_previous) and not os.path.exists(previous):
            os.replace(parked_previous, previous)
    except OSError:
        return


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="安装或更新 YY-Flow 共享正本。默认源 HLi9725/multi-agent-flow，默认入口 main。")
    parser.add_argument("--source", default=DEFAULT_SOURCE)
    parser.add_argument("--ref", default=DEFAULT_REF)
    parser.add_argument("--dest", default="")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    try:
        result = install(args.source, args.ref, args.dest)
    except InstallError as exc:
        payload = {"ok": False, "code": exc.code, "message": exc.message}
        _emit(payload, args.json, sys.stderr)
        return exc.exit_code
    _emit(result, args.json, sys.stdout)
    return 0


def _emit(payload: dict, as_json: bool, stream) -> None:
    if as_json:
        stream.write(json.dumps(payload, ensure_ascii=False) + "\n")
    else:
        stream.write(payload.get("message", payload.get("code", "")) + "\n")
    stream.flush()


if __name__ == "__main__":
    raise SystemExit(main())
