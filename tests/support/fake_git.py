# -*- coding: utf-8 -*-
"""安装器测试替身。不访问网络。"""

from __future__ import annotations

import json
import os
import sys


def main() -> int:
    args = sys.argv[1:]
    log = os.environ.get("FAKE_GIT_LOG", "")
    if log:
        with open(log, "a", encoding="utf-8") as handle:
            handle.write(json.dumps(args, ensure_ascii=False) + "\n")
    cwd = os.getcwd()
    if args and args[0] == "-C":
        cwd = args[1]
        args = args[2:]
    if not args:
        sys.stderr.write("missing command\n")
        return 2
    command = args[0]
    if "pull" == command:
        sys.stderr.write("pull refused\n")
        return 9
    fail = os.environ.get("FAKE_GIT_FAIL_CMD", "")
    if fail and command == fail:
        sys.stderr.write("fake-git-fail\n")
        return int(os.environ.get("FAKE_GIT_FAIL_CODE", "3"))
    if command == "init":
        target = args[1] if len(args) > 1 else cwd
        os.makedirs(os.path.join(target, ".git"), exist_ok=True)
        return 0
    if command == "remote":
        return 0
    if command == "fetch":
        os.makedirs(os.path.join(cwd, ".git"), exist_ok=True)
        with open(os.path.join(cwd, ".git", "FETCH_HEAD"), "w", encoding="utf-8") as handle:
            handle.write(os.environ.get("FAKE_GIT_SHA", "a" * 40) + "\n")
        return 0
    if command == "checkout":
        script_dir = os.path.join(cwd, "scripts")
        os.makedirs(script_dir, exist_ok=True)
        with open(os.path.join(script_dir, "mcp_server.py"), "w", encoding="utf-8") as handle:
            handle.write("# mcp\n")
        with open(os.path.join(cwd, "README.md"), "w", encoding="utf-8") as handle:
            handle.write("sha=" + os.environ.get("FAKE_GIT_SHA", "a" * 40) + "\n")
        return 0
    if command == "rev-parse":
        sys.stdout.write(os.environ.get("FAKE_GIT_SHA", "a" * 40) + "\n")
        return 0
    if command == "status":
        if os.environ.get("FAKE_GIT_DIRTY") == "1":
            sys.stdout.write(" M README.md\n")
        return 0
    sys.stderr.write("unsupported\n")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
