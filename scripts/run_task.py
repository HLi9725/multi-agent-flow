#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
scripts/run_task.py
2F-PROD 通用自动编排 Runner CLI 入口。
支持: start / status / resume / cancel
"""
import argparse
import json
import os
import sys

_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
if _SCRIPT_DIR not in sys.path:
    sys.path.insert(0, _SCRIPT_DIR)

import paths
from _lib.core.production_runner import ProductionRunner
from _lib.core.runner_checkpoint_store import RunnerCheckpointStore
from _lib.core.task_spec_loader import load_task_execution_spec


def _runner_for(project_root, authority_root, project_id=None):
    authority = os.path.realpath(authority_root or project_root)
    identity = project_id or os.path.basename(os.path.realpath(project_root))
    store = RunnerCheckpointStore(
        data_root=paths.resolve_data_root(cwd=authority),
        project_root=os.path.realpath(project_root),
        project_id=identity,
    )
    return ProductionRunner(checkpoint_store=store)


def cmd_start(args):
    project_root = os.path.realpath(args.project_root or os.getcwd())
    authority_root = os.path.realpath(args.authority_root) if args.authority_root else None

    overrides = {}
    if args.builder_adapter:
        overrides["builder_adapter_id"] = args.builder_adapter
    if args.reviewer_adapter:
        overrides["reviewer_adapter_id"] = args.reviewer_adapter
    if args.qa_adapter:
        overrides["qa_adapter_id"] = args.qa_adapter
    if args.max_review_cycles is not None:
        overrides["max_review_cycles"] = args.max_review_cycles
    if args.max_qa_cycles is not None:
        overrides["max_qa_cycles"] = args.max_qa_cycles
    if args.timeout_seconds is not None:
        overrides["builder_timeout_seconds"] = args.timeout_seconds
        overrides["reviewer_timeout_seconds"] = args.timeout_seconds
        overrides["qa_timeout_seconds"] = args.timeout_seconds
    if args.test_command:
        overrides["test_command"] = args.test_command
    if args.workspace_mode:
        overrides["workspace_mode"] = args.workspace_mode

    try:
        spec = load_task_execution_spec(
            project_root=project_root,
            task_id=args.task_id,
            authority_root=authority_root,
            overrides=overrides,
        )
    except Exception as e:
        print(f"[ERROR] Failed to load task execution spec for {args.task_id}: {e}", file=sys.stderr)
        return 1

    runner = _runner_for(project_root, authority_root, spec.project_id)
    result = runner.start(spec)

    print(json.dumps(result.to_dict(), indent=2, ensure_ascii=False))
    return 0 if result.success else 1


def cmd_status(args):
    project_root = os.path.realpath(args.project_root or os.getcwd())
    authority_root = os.path.realpath(args.authority_root) if args.authority_root else None

    runner = _runner_for(project_root, authority_root)
    status_data = runner.status(project_root=project_root, task_id=args.task_id, authority_root=authority_root)
    print(json.dumps(status_data, indent=2, ensure_ascii=False))
    return 0


def cmd_resume(args):
    project_root = os.path.realpath(args.project_root or os.getcwd())
    authority_root = os.path.realpath(args.authority_root) if args.authority_root else None

    runner = _runner_for(project_root, authority_root)
    result = runner.resume(
        project_root=project_root,
        task_id=args.task_id,
        authority_root=authority_root,
        pre_granted_approval=getattr(args, "approve", False),
    )
    print(json.dumps(result.to_dict(), indent=2, ensure_ascii=False))
    return 0 if result.success else 1


def cmd_cancel(args):
    project_root = os.path.realpath(args.project_root or os.getcwd())
    authority_root = os.path.realpath(args.authority_root) if args.authority_root else None

    runner = _runner_for(project_root, authority_root)
    result = runner.cancel(project_root=project_root, task_id=args.task_id, authority_root=authority_root)
    print(json.dumps(result.to_dict(), indent=2, ensure_ascii=False))
    return 0 if result.success else 1


def main():
    parser = argparse.ArgumentParser(description="Multi-Agent Flow Universal Production Runner")
    subparsers = parser.add_subparsers(dest="subcommand", required=True)

    # start
    p_start = subparsers.add_parser("start", help="Start automatic orchestration for a task")
    p_start.add_argument("--task-id", required=True, help="Task ID to execute (e.g. T0061)")
    p_start.add_argument("--project-root", default=".", help="Project root directory")
    p_start.add_argument("--authority-root", default=None, help="Authoritative board root directory")
    p_start.add_argument("--builder-adapter", default="codex_cli", help="Builder adapter ID")
    p_start.add_argument("--reviewer-adapter", default="antigravity", help="Reviewer adapter ID")
    p_start.add_argument("--qa-adapter", default="codex_cli", help="QA adapter ID")
    p_start.add_argument("--max-review-cycles", type=int, default=3, help="Max review rejection loop cycles")
    p_start.add_argument("--max-qa-cycles", type=int, default=3, help="Max QA failure loop cycles")
    p_start.add_argument("--timeout-seconds", type=int, default=300, help="Timeout per host dispatch")
    p_start.add_argument("--test-command", default=None, help="Test command to run in QA")
    p_start.add_argument("--workspace-mode", default="branch", choices=["branch", "inherit", "share"], help="Workspace isolation mode")
    p_start.set_defaults(func=cmd_start)

    # status
    p_status = subparsers.add_parser("status", help="Query task execution status (read-only)")
    p_status.add_argument("--task-id", required=True, help="Task ID")
    p_status.add_argument("--project-root", default=".", help="Project root directory")
    p_status.add_argument("--authority-root", default=None, help="Authoritative board root directory")
    p_status.set_defaults(func=cmd_status)

    # resume
    p_resume = subparsers.add_parser("resume", help="Resume task from checkpoint")
    p_resume.add_argument("--task-id", required=True, help="Task ID")
    p_resume.add_argument("--project-root", default=".", help="Project root directory")
    p_resume.add_argument("--authority-root", default=None, help="Authoritative board root directory")
    p_resume.add_argument("--approve", action="store_true", help="Grant permission approval if task was paused at APPROVAL_REQUIRED")
    p_resume.set_defaults(func=cmd_resume)

    # cancel
    p_cancel = subparsers.add_parser("cancel", help="Cancel task execution safely")
    p_cancel.add_argument("--task-id", required=True, help="Task ID")
    p_cancel.add_argument("--project-root", default=".", help="Project root directory")
    p_cancel.add_argument("--authority-root", default=None, help="Authoritative board root directory")
    p_cancel.set_defaults(func=cmd_cancel)

    args = parser.parse_args()
    sys.exit(args.func(args))


if __name__ == "__main__":
    main()
