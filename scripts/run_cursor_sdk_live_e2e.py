#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
scripts/run_cursor_sdk_live_e2e.py
Optional Live E2E verification script for Cursor Python SDK Reference Adapter.

Executes a live multi-agent orchestration pipeline across:
Builder (cursor_sdk) -> Reviewer (cursor_sdk) -> QA (cursor_sdk)
halting at PENDING_USER_ACCEPTANCE.

Strict Requirements:
- Task ID must follow '^T\\d+$' (e.g. T99999).
- Exits with clear error and non-zero code if CURSOR_API_KEY or --cursor-model is missing.
- Configures isolated fixture repository as authoritative root (--authority-root).
- Provides executable acceptance criteria and controlled QA test command.
- Confirms terminal state reaches PENDING_USER_ACCEPTANCE on success.
- Cleans up temporary fixture on both success and failure (unless --keep-fixture is specified).
- Never fabricates success evidence on missing credentials.
- Never permanently modifies adapter manifest verification level to CLI_VERIFIED.
"""
import argparse
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import time
from typing import Any, Dict, Optional

_SCRIPTS_DIR = os.path.dirname(os.path.abspath(__file__))
if _SCRIPTS_DIR not in sys.path:
    sys.path.insert(0, _SCRIPTS_DIR)


def _safe_rmtree(target_dir: str) -> None:
    """Safely remove a directory tree, overcoming Windows read-only git pack/object file attributes."""
    if not target_dir or not os.path.exists(target_dir):
        return

    def _handle_remove_readonly(func, path, exc_info):
        try:
            os.chmod(path, stat.S_IWRITE)
            func(path)
        except Exception:
            pass

    try:
        shutil.rmtree(target_dir, onerror=_handle_remove_readonly)
    except Exception:
        try:
            shutil.rmtree(target_dir, ignore_errors=True)
        except Exception:
            pass


def _setup_isolated_fixture(task_id: str) -> str:
    """Create a self-contained temporary git repository fixture with task definitions."""
    temp_dir = tempfile.mkdtemp(prefix="cursor_live_e2e_")

    # 1. Initialize git repo
    subprocess.run(["git", "init"], cwd=temp_dir, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.name", "LiveCursorRunner"], cwd=temp_dir, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "cursor_runner@multi-agent-flow.local"], cwd=temp_dir, check=True, capture_output=True)

    # 2. Add initial project files
    calc_py = os.path.join(temp_dir, "calc.py")
    with open(calc_py, "w", encoding="utf-8") as f:
        f.write("# -*- coding: utf-8 -*-\ndef add(a: int, b: int) -> int:\n    return 0\n")

    tests_dir = os.path.join(temp_dir, "tests")
    os.makedirs(tests_dir, exist_ok=True)
    test_calc = os.path.join(tests_dir, "test_calc.py")
    with open(test_calc, "w", encoding="utf-8") as f:
        f.write("from calc import add\n\ndef test_add():\n    assert add(2, 3) == 5\n")

    # 3. Commit initial baseline
    subprocess.run(["git", "add", "."], cwd=temp_dir, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "chore: initial baseline"], cwd=temp_dir, check=True, capture_output=True)

    # 4. Setup authority board files in user_data and .yy-flow/user_data
    user_data_dir = os.path.join(temp_dir, "user_data")
    os.makedirs(user_data_dir, exist_ok=True)
    yy_flow_dir = os.path.join(temp_dir, ".yy-flow", "user_data")
    os.makedirs(yy_flow_dir, exist_ok=True)

    acceptance_text = (
        "需求: 在 calc.py 中实现两数相加函数。\n\n"
        "【验收标准】:\n"
        "- calc.py 中的 add(a, b) 函数返回 a 与 b 之和的整数值\n"
        "- 运行 pytest tests/test_calc.py 退出码为 0 且断言正确\n"
    )

    board_record = [
        {
            "id": task_id,
            "name": "在 calc.py 中实现正确的加法逻辑以通过测试",
            "status": "进行中",
            "type": "A",
            "assignee": "李开发",
            "handler": "李开发",
            "owner": "李开发",
            "stage": "-",
            "wp": "-",
            "wbs": "-",
            "act_hours": 0,
            "creator": "live_runner",
            "seq": 1,
            "start_date": time.strftime("%Y-%m-%d %H:%M:%S"),
            "updated_at": "1.0",
            "description": acceptance_text,
            "remarks": acceptance_text,
            "process": (
                f"[{task_id}-N01] [{time.strftime('%Y-%m-%d %H:%M:%S')}] 建单并进入【进行中】\n"
                + acceptance_text
            ),
        }
    ]

    for target_dir in (user_data_dir, yy_flow_dir):
        board_file = os.path.join(target_dir, "board.json")
        with open(board_file, "w", encoding="utf-8") as f:
            json.dump(board_record, f, indent=2, ensure_ascii=False)

    # 5. Setup workflow config in config/workflow.config.yaml
    config_dir = os.path.join(temp_dir, "config")
    os.makedirs(config_dir, exist_ok=True)
    wf_config = os.path.join(config_dir, "workflow.config.yaml")
    with open(wf_config, "w", encoding="utf-8") as f:
        f.write(
            "project:\n"
            "  name: 'Live-Cursor-E2E'\n"
            "  version: '1.0.0'\n"
            "board:\n"
            "  provider: 'local'\n"
            "  board_file: 'user_data/board.json'\n"
        )

    # Also place config in user_data and .yy-flow/user_data for fallback resolution
    for target_dir in (user_data_dir, yy_flow_dir):
        wf_copy = os.path.join(target_dir, "workflow.config.yaml")
        with open(wf_copy, "w", encoding="utf-8") as f:
            f.write(
                "project:\n"
                "  name: 'Live-Cursor-E2E'\n"
                "  version: '1.0.0'\n"
                "board:\n"
                "  provider: 'local'\n"
                "  board_file: 'user_data/board.json'\n"
            )

    return temp_dir


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Run live E2E verification on official Cursor Python SDK across Builder -> Reviewer -> QA."
    )
    parser.add_argument(
        "--cursor-model",
        dest="cursor_model",
        default=None,
        help="Explicit Cursor model identifier (e.g. composer-2.5)",
    )
    parser.add_argument(
        "--cursor-api-key-env",
        dest="cursor_api_key_env",
        default="CURSOR_API_KEY",
        help="Environment variable containing Cursor API key (default: CURSOR_API_KEY)",
    )
    parser.add_argument(
        "--task-id",
        dest="task_id",
        default="T99999",
        help="Task ID to execute in the isolated live test fixture (must match ^T\\d+$, default: T99999)",
    )
    parser.add_argument(
        "--keep-fixture",
        action="store_true",
        help="Do not clean up temporary fixture workspace upon test completion",
    )
    args = parser.parse_args()

    # 1. Strict Validation: --cursor-model
    cursor_model = (args.cursor_model or "").strip()
    if not cursor_model:
        print(
            "[ERROR] --cursor-model is required (e.g. --cursor-model composer-2.5). Live E2E aborted.",
            file=sys.stderr,
        )
        return 1

    # 2. Strict Validation: CURSOR_API_KEY
    api_key_env = (args.cursor_api_key_env or "CURSOR_API_KEY").strip()
    api_key = os.environ.get(api_key_env, "").strip()
    if not api_key:
        print(
            f"[ERROR] Cursor API key environment variable '{api_key_env}' is missing or empty. "
            f"Live E2E test requires valid credentials and cannot proceed.",
            file=sys.stderr,
        )
        return 1

    # 3. Strict Validation: --task-id format
    task_id = (args.task_id or "").strip()
    if not re.match(r"^T\d+$", task_id):
        print(
            f"[ERROR] Invalid --task-id '{task_id}'. Must match '^T\\d+$' (e.g. T99999).",
            file=sys.stderr,
        )
        return 1

    temp_repo: Optional[str] = None
    try:
        # 4. Prepare isolated fixture repository
        temp_repo = _setup_isolated_fixture(task_id)
        print(f"[LIVE E2E] Created isolated workspace fixture at: {temp_repo}")

        # 5. Invoke run_task.py start with authoritative root, test command, and cursor_sdk adapters
        cmd = [
            sys.executable,
            os.path.join(_SCRIPTS_DIR, "run_task.py"),
            "start",
            "--project-root", temp_repo,
            "--authority-root", temp_repo,
            "--task-id", task_id,
            "--test-command", "python -m pytest tests/test_calc.py",
            "--builder-adapter", "cursor_sdk",
            "--reviewer-adapter", "cursor_sdk",
            "--qa-adapter", "cursor_sdk",
            "--cursor-runtime", "local",
            "--cursor-model", cursor_model,
            "--cursor-api-key-env", api_key_env,
            "--approve",
        ]

        print(f"[LIVE E2E] Executing pipeline: {' '.join(cmd)}")
        start_time = time.time()
        proc = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace")
        elapsed = time.time() - start_time

        # 6. Parse and inspect execution outcome
        out_json: Optional[Dict[str, Any]] = None
        try:
            out_json = json.loads(proc.stdout.strip())
        except Exception:
            pass

        if proc.returncode != 0 or not out_json or not out_json.get("success"):
            print(f"[LIVE E2E FAILED] Exit code: {proc.returncode}, Elapsed: {elapsed:.2f}s", file=sys.stderr)
            if proc.stderr:
                print(f"[STDERR]\n{proc.stderr}", file=sys.stderr)
            if proc.stdout:
                print(f"[STDOUT]\n{proc.stdout}", file=sys.stderr)
            return 1

        state = out_json.get("state")
        if state != "PENDING_USER_ACCEPTANCE":
            print(
                f"[LIVE E2E FAILED] Expected terminal state 'PENDING_USER_ACCEPTANCE', got '{state}'",
                file=sys.stderr,
            )
            return 1

        candidate_commit = out_json.get("candidate_commit")
        evidence_ids = out_json.get("evidence_ids", [])
        evidence_dir = os.path.join(temp_repo, ".yy-flow", "runner_evidence")
        if not os.path.isdir(evidence_dir):
            evidence_dir = os.path.join(temp_repo, "user_data", "runner_evidence")

        print("=" * 70)
        print(f"[LIVE E2E SUCCESS] Multi-agent flow reached terminal state: {state}")
        print(f"Elapsed Time: {elapsed:.2f}s")
        print(f"Candidate Commit: {candidate_commit}")
        print(f"Evidence Directory: {evidence_dir}")
        print(f"Evidence IDs ({len(evidence_ids)}): {evidence_ids}")
        print("=" * 70)

        return 0
    finally:
        # Guarantee cleanup on both success and failure unless --keep-fixture was specified
        if temp_repo and not args.keep_fixture:
            _safe_rmtree(temp_repo)


if __name__ == "__main__":
    sys.exit(main())
