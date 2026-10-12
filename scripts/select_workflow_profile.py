#!/usr/bin/env python3
"""按实际文件内容选择并固化工作流 Profile。标题和用户声称不降低风险。"""
import argparse
import json
import os
import sys

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
if SCRIPT_DIR not in sys.path:
    sys.path.insert(0, SCRIPT_DIR)

from _lib.core.rule_source import RuleSourceError, ensure_safety_rules_present
from _lib.core.workflow_policy import PolicyError, freeze_policy, save_policy_file, select_profile


def _read(path: str) -> str:
    with open(path, "r", encoding="utf-8") as handle:
        return handle.read()


def main(argv=None) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description="选择并固化工作流 Profile")
    parser.add_argument("--data-root", required=True)
    parser.add_argument("--task-id", required=True)
    parser.add_argument("--profile", default="auto", choices=["auto", "standard", "lite", "docs-short"])
    parser.add_argument("--task-type", default="A")
    parser.add_argument("--root", default=".", help="文件路径的相对根目录")
    parser.add_argument("--file", action="append", default=[])
    parser.add_argument("--requirement", default="")
    parser.add_argument("--title", default="")
    parser.add_argument("--requirement-hash", default="")
    parser.add_argument("--acceptance-hash", default="")
    args = parser.parse_args(argv)
    try:
        ensure_safety_rules_present()
    except RuleSourceError as exc:
        print(json.dumps({"ok": False, "message": str(exc)}, ensure_ascii=False))
        return 2
    if not str(args.requirement_hash or "").strip() or not str(args.acceptance_hash or "").strip():
        print(json.dumps({"ok": False, "message": "没有需求哈希和验收哈希，拒绝固化"}, ensure_ascii=False))
        return 2
    files = []
    for path in args.file:
        relative = path.replace("\\", "/")
        full = path if os.path.isabs(path) else os.path.join(args.root, path)
        files.append({"path": relative, "content": _read(full)})
    decision = select_profile(
        task_type=args.task_type,
        requested=args.profile,
        files=files,
        requirement_text=args.requirement,
        title=args.title,
        requirement_hash=args.requirement_hash,
        acceptance_criteria_hash=args.acceptance_hash,
    )
    payload = decision.to_dict()
    payload["task_id"] = args.task_id
    if decision.blocked:
        print(json.dumps({"ok": False, "policy": payload}, ensure_ascii=False))
        return 2
    try:
        frozen = freeze_policy(decision, args.task_id)
    except PolicyError as exc:
        print(json.dumps({"ok": False, "message": str(exc)}, ensure_ascii=False))
        return 2
    path = save_policy_file(args.data_root, frozen)
    print(json.dumps({"ok": True, "policy_path": path, "policy": frozen}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
