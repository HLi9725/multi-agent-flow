#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
scripts/run_task.py
2F-PROD 通用自动编排 Runner CLI 入口。
支持: start / status / resume / accept / reject / cancel
"""
import argparse
import json
import math
import os
import sys

_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
if _SCRIPT_DIR not in sys.path:
    sys.path.insert(0, _SCRIPT_DIR)

import paths
from _lib.core.production_runner import ProductionRunner
from _lib.core.runner_checkpoint_store import RunnerCheckpointStore
from _lib.core.task_spec_loader import load_task_execution_spec


def _emit_progress(event):
    """Human-readable JSONL progress on stderr; final result remains clean stdout JSON."""
    print("[YY-FLOW] " + json.dumps(dict(event), ensure_ascii=True), file=sys.stderr, flush=True)


def _emit_result(data):
    """ASCII JSON survives GBK/ASCII pipes without losing any Unicode value.

    Escapes are lossless after JSON decoding. Do not reconfigure the caller's
    terminal, and never let an emoji hide an already-persisted terminal state.
    """
    print(json.dumps(data, indent=2, ensure_ascii=True), flush=True)


def _runner_for(project_root, authority_root, project_id=None):
    authority = (
        os.path.realpath(authority_root)
        if authority_root
        else os.path.realpath(paths.resolve_data_root(cwd=project_root))
    )
    identity = project_id or os.path.basename(os.path.realpath(project_root))
    store = RunnerCheckpointStore(
        data_root=authority,
        project_root=os.path.realpath(project_root),
        project_id=identity,
    )
    return ProductionRunner(checkpoint_store=store, progress_callback=_emit_progress)


def _positive_int(value):
    parsed = int(value)
    if parsed <= 0:
        raise argparse.ArgumentTypeError("must be a positive integer")
    return parsed


def _non_negative_int(value):
    parsed = int(value)
    if parsed < 0:
        raise argparse.ArgumentTypeError("must be a non-negative integer")
    return parsed


def _non_negative_float(value):
    parsed = float(value)
    if not math.isfinite(parsed) or parsed < 0:
        raise argparse.ArgumentTypeError("must be a non-negative number")
    return parsed


def _overrides_from_args(args):
    overrides = {}
    if getattr(args, "max_total_attempts", None) is not None:
        overrides["max_total_attempts"] = args.max_total_attempts
    if getattr(args, "total_wall_clock_timeout_seconds", None) is not None:
        overrides["total_wall_clock_timeout_seconds"] = args.total_wall_clock_timeout_seconds
    if getattr(args, "builder_adapter", None):
        overrides["builder_adapter_id"] = args.builder_adapter
    if getattr(args, "reviewer_adapter", None):
        overrides["reviewer_adapter_id"] = args.reviewer_adapter
    if getattr(args, "qa_adapter", None):
        overrides["qa_adapter_id"] = args.qa_adapter
    if getattr(args, "max_review_cycles", None) is not None:
        overrides["max_review_cycles"] = args.max_review_cycles
    if getattr(args, "max_qa_cycles", None) is not None:
        overrides["max_qa_cycles"] = args.max_qa_cycles
    if getattr(args, "timeout_seconds", None) is not None:
        overrides["builder_timeout_seconds"] = args.timeout_seconds
        overrides["reviewer_timeout_seconds"] = args.timeout_seconds
        overrides["qa_timeout_seconds"] = args.timeout_seconds
    if getattr(args, "test_commands", None):
        overrides["test_commands"] = tuple(args.test_commands)
    if getattr(args, "workspace_mode", None):
        overrides["workspace_mode"] = args.workspace_mode
    if getattr(args, "host_transient_max_retries", None) is not None:
        overrides["host_transient_max_retries"] = args.host_transient_max_retries
    if getattr(args, "host_transient_retry_base_seconds", None) is not None:
        overrides["host_transient_retry_base_seconds"] = args.host_transient_retry_base_seconds
    if getattr(args, "host_transient_retry_max_seconds", None) is not None:
        overrides["host_transient_retry_max_seconds"] = args.host_transient_retry_max_seconds
    if getattr(args, "recover_partial_builder_changes", False):
        overrides["recover_partial_builder_changes"] = True
    if getattr(args, "cursor_model", None):
        overrides["cursor_model"] = args.cursor_model
    if getattr(args, "cursor_api_key_env", None):
        overrides["cursor_api_key_env"] = args.cursor_api_key_env
    if getattr(args, "cursor_runtime", None):
        overrides["cursor_runtime"] = args.cursor_runtime
    return overrides


def _add_cursor_args(parser):
    parser.add_argument(
        "--cursor-model",
        default=None,
        help="Explicit model identifier for cursor_sdk adapter (e.g. composer-2.5)",
    )
    parser.add_argument(
        "--cursor-api-key-env",
        default=None,
        help="Environment variable name providing Cursor API key (default: CURSOR_API_KEY)",
    )
    parser.add_argument(
        "--cursor-runtime",
        default=None,
        choices=["local"],
        help="Cursor execution runtime (Phase 3 supports 'local' only)",
    )


def _add_host_retry_args(parser):
    parser.add_argument(
        "--host-transient-max-retries", type=_non_negative_int, default=None,
        help="Bounded retries for explicit transient host failures; does not consume business cycles",
    )
    parser.add_argument(
        "--host-transient-retry-base-seconds", type=_non_negative_float, default=None,
        help="Initial delay for transient host retries",
    )
    parser.add_argument(
        "--host-transient-retry-max-seconds", type=_non_negative_float, default=None,
        help="Maximum delay between transient host retries",
    )
    parser.add_argument(
        "--recover-partial-builder-changes", action="store_true",
        help=(
            "Explicitly authorize the resumed Builder to inspect and complete uncommitted changes "
            "left by a previously interrupted transient host turn"
        ),
    )


def cmd_start(args):
    project_root = os.path.realpath(args.project_root or os.getcwd())
    authority_root = os.path.realpath(args.authority_root) if args.authority_root else None

    overrides = _overrides_from_args(args)

    try:
        spec = load_task_execution_spec(
            project_root=project_root,
            task_id=args.task_id,
            authority_root=authority_root,
            overrides=overrides,
        )
    except Exception as e:
        print("[ERROR] " + json.dumps({"task_id": args.task_id, "message": f"Failed to load task execution spec: {e}"}, ensure_ascii=True), file=sys.stderr)
        return 1

    runner = _runner_for(project_root, authority_root, spec.project_id)
    result = runner.start(spec, pre_granted_approval=getattr(args, "approve", False),
                          restart_cancelled=getattr(args, "restart_cancelled", False))

    _emit_result(result.to_dict())
    return 0 if result.success else 1


def cmd_status(args):
    project_root = os.path.realpath(args.project_root or os.getcwd())
    authority_root = os.path.realpath(args.authority_root) if args.authority_root else None

    runner = _runner_for(project_root, authority_root)
    status_data = runner.get_status(project_root=project_root, task_id=args.task_id, authority_root=authority_root)
    _emit_result(status_data)
    return 0


def cmd_resume(args):
    project_root = os.path.realpath(args.project_root or os.getcwd())
    authority_root = os.path.realpath(args.authority_root) if args.authority_root else None

    runner = _runner_for(project_root, authority_root)
    overrides = _overrides_from_args(args)
    result = runner.resume(
        project_root=project_root,
        task_id=args.task_id,
        authority_root=authority_root,
        pre_granted_approval=getattr(args, "approve", False),
        overrides=overrides,
        rejection_reason=getattr(args, "reason", None),
    )
    _emit_result(result.to_dict())
    return 0 if result.success else 1


def cmd_cancel(args):
    project_root = os.path.realpath(args.project_root or os.getcwd())
    authority_root = os.path.realpath(args.authority_root) if args.authority_root else None

    runner = _runner_for(project_root, authority_root)
    result = runner.cancel(project_root=project_root, task_id=args.task_id, authority_root=authority_root)
    _emit_result(result.to_dict())
    return 0 if result.success else 1


def cmd_accept(args):
    project_root = os.path.realpath(args.project_root or os.getcwd())
    authority_root = os.path.realpath(args.authority_root) if args.authority_root else None
    result = _runner_for(project_root, authority_root).accept(
        project_root, args.task_id, args.confirmation_request_id, authority_root,
    )
    _emit_result(result.to_dict())
    return 0 if result.success else 1


def cmd_reject(args):
    project_root = os.path.realpath(args.project_root or os.getcwd())
    authority_root = os.path.realpath(args.authority_root) if args.authority_root else None
    result = _runner_for(project_root, authority_root).reject(
        project_root, args.task_id, args.confirmation_request_id, args.reason, authority_root,
    )
    _emit_result(result.to_dict())
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
    p_start.add_argument("--max-total-attempts", type=_positive_int, default=None, help="Total cumulative attempt limit (not additional attempts)")
    p_start.add_argument(
        "--total-wall-clock-timeout-seconds",
        type=_positive_int,
        default=None,
        help="Total cumulative active execution time limit in seconds",
    )
    p_start.add_argument("--max-qa-cycles", type=int, default=3, help="Max QA failure loop cycles")
    p_start.add_argument("--timeout-seconds", type=int, default=300, help="Timeout per host dispatch")
    p_start.add_argument(
        "--test-command",
        dest="test_commands",
        action="append",
        default=None,
        help="Controlled QA command; repeat the option to require backend, frontend, and build checks",
    )
    p_start.add_argument("--workspace-mode", default="branch", choices=["branch", "inherit", "share"], help="Workspace isolation mode")
    p_start.add_argument(
        "--approve",
        action="store_true",
        help="Record explicit outer-host approval for this run's non-destructive workspace operations",
    )
    p_start.set_defaults(func=cmd_start)
    p_start.add_argument("--restart-cancelled", action="store_true", help="Archive a cancelled run and start a new run of the same task")
    _add_host_retry_args(p_start)
    _add_cursor_args(p_start)

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
    p_resume.add_argument(
        "--reason",
        default=None,
        help="Inject acceptance defects when reconciling a legacy pending checkpoint with an already-returned task",
    )
    p_resume.add_argument("--builder-adapter", default=None, help="Override persisted Builder adapter ID")
    p_resume.add_argument("--reviewer-adapter", default=None, help="Override persisted Reviewer adapter ID")
    p_resume.add_argument("--qa-adapter", default=None, help="Override persisted QA adapter ID")
    p_resume.add_argument("--max-review-cycles", type=int, default=None, help="Override persisted review cycle limit")
    p_resume.add_argument("--max-total-attempts", type=_positive_int, default=None, help="Explicitly override cumulative attempt limit; preserves historical counts")
    p_resume.add_argument(
        "--total-wall-clock-timeout-seconds",
        type=_positive_int,
        default=None,
        help="Override cumulative active execution time limit; preserves historical elapsed time",
    )
    p_resume.add_argument("--max-qa-cycles", type=int, default=None, help="Override persisted QA cycle limit")
    p_resume.add_argument("--timeout-seconds", type=int, default=None, help="Override persisted per-host timeout")
    p_resume.add_argument(
        "--test-command",
        dest="test_commands",
        action="append",
        default=None,
        help="Override persisted controlled QA command list; repeat for multiple commands",
    )
    p_resume.add_argument(
        "--workspace-mode",
        default=None,
        choices=["branch", "inherit", "share"],
        help="Override persisted workspace mode (checkpoint worktree is still authoritative)",
    )
    p_resume.set_defaults(func=cmd_resume)
    _add_host_retry_args(p_resume)
    _add_cursor_args(p_resume)

    for name, help_text, func in (
        ("accept", "Accept the exact pending candidate", cmd_accept),
        ("reject", "Return the exact pending candidate to the original task", cmd_reject),
    ):
        p_decision = subparsers.add_parser(name, help=help_text)
        p_decision.add_argument("--task-id", required=True, help="Task ID")
        p_decision.add_argument("--project-root", default=".", help="Project root directory")
        p_decision.add_argument("--authority-root", default=None, help="Authoritative board root directory")
        p_decision.add_argument("--confirmation-request-id", required=True, help="Exact pending confirmation request ID")
        if name == "reject":
            p_decision.add_argument("--reason", required=True, help="Concrete user acceptance rejection reason")
        p_decision.set_defaults(func=func)

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
