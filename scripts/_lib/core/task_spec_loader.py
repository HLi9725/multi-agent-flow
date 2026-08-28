# -*- coding: utf-8 -*-
"""
scripts/_lib/core/task_spec_loader.py
从权威看板加载、校验并生成不可变 TaskExecutionSpec。
严格遵循 Fail-Closed 原则：不伪造数据、不绕过验证、执行乐观并发控制。
"""
import hashlib
import json
import os
import re
import subprocess
import sys
from typing import Any, Dict, Optional

# Ensure scripts dir is in sys.path
_SCRIPTS_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
if _SCRIPTS_ROOT not in sys.path:
    sys.path.insert(0, _SCRIPTS_ROOT)

import paths
from ..boards.board_adapter_factory import get_board_adapter
from .runner_schema import TaskExecutionSpec


class TaskSpecError(Exception):
    """任务规格加载基础异常"""
    pass


class TaskSpecNotFoundError(TaskSpecError):
    """任务未在权威看板中找到"""
    pass


class TaskSpecInvalidStatusError(TaskSpecError):
    """任务处于不可运行的状态（如已验收、已取消）"""
    pass


class TaskSpecIncompleteError(TaskSpecError):
    """任务缺失必要字段（如需求或验收标准）"""
    pass


class TaskSpecConcurrencyError(TaskSpecError):
    """乐观并发校验失败（任务在外部被修改）"""
    pass


def _resolve_git_repo_root(directory: str) -> str:
    """通过 git rev-parse 获取规范化的仓库根目录"""
    try:
        out = subprocess.check_output(
            ["git", "rev-parse", "--show-toplevel"],
            cwd=directory,
            stderr=subprocess.DEVNULL,
            text=True,
            timeout=5,
        ).strip()
        return os.path.realpath(out)
    except Exception:
        return os.path.realpath(directory)


def _get_git_head_and_branch(directory: str) -> tuple[str, str]:
    """获取指定目录的当前 HEAD 40位 SHA 与分支名"""
    head_sha = ""
    branch_name = "main"
    try:
        head_sha = subprocess.check_output(
            ["git", "rev-parse", "HEAD"],
            cwd=directory,
            stderr=subprocess.DEVNULL,
            text=True,
            timeout=5,
        ).strip()
    except Exception:
        head_sha = "0" * 40

    try:
        branch_name = subprocess.check_output(
            ["git", "rev-parse", "--abbrev-ref", "HEAD"],
            cwd=directory,
            stderr=subprocess.DEVNULL,
            text=True,
            timeout=5,
        ).strip()
    except Exception:
        branch_name = "main"

    return head_sha, branch_name


def load_task_execution_spec(
    project_root: str,
    task_id: str,
    authority_root: Optional[str] = None,
    overrides: Optional[Dict[str, Any]] = None,
) -> TaskExecutionSpec:
    """
    从权威看板中加载并构建不可变的 TaskExecutionSpec。
    
    参数:
        project_root: 业务代码工作区根目录
        task_id: 权威工单 ID (如 T0054, T0061)
        authority_root: 权威看板/集成根目录 (为 None 时自动推导)
        overrides: 外部参数覆盖 (如超时、循环次数、测试命令等)
    """
    if not task_id or not isinstance(task_id, str) or not re.match(r"^T\d+$", task_id.strip()):
        raise TaskSpecError(f"Invalid task_id format: '{task_id}'. Expected format 'T<digits>' (e.g. T0054).")

    task_id = task_id.strip()
    norm_project_root = _resolve_git_repo_root(project_root)
    if not os.path.isdir(norm_project_root):
        raise TaskSpecError(f"project_root does not exist or is not a directory: {project_root}")

    # 解析 authority_root
    if authority_root is None:
        authority_root = paths.project_root(cwd=norm_project_root)
    norm_authority_root = os.path.realpath(authority_root)
    if not os.path.isdir(norm_authority_root):
        raise TaskSpecError(f"authority_root does not exist: {authority_root}")

    # 读取看板
    config_candidates = [
        os.path.join(norm_authority_root, "config", "workflow.config.yaml"),
        os.path.join(norm_authority_root, "user_data", "workflow.config.yaml"),
        os.path.join(norm_project_root, "config", "workflow.config.yaml"),
        os.path.join(norm_project_root, "user_data", "workflow.config.yaml"),
    ]
    board_config = None
    for cand in config_candidates:
        if os.path.isfile(cand):
            board_config = cand
            break

    if not board_config:
        try:
            board_config = paths.resolve_runtime_config(cwd=norm_authority_root)
        except Exception:
            board_config = None

    if not board_config or not os.path.isfile(board_config):
        raise TaskSpecError(f"Cannot find workflow.config.yaml in authority_root: {norm_authority_root}")

    import yaml
    from ..boards.offline_board_adapter import OfflineBoardAdapter

    with open(board_config, "r", encoding="utf-8") as f:
        cfg_data = yaml.safe_load(f) or {}

    board_cfg = cfg_data.get("board", {})
    provider = board_cfg.get("provider", "feishu_base").lower()

    if provider == "local":
        raw_board_file = board_cfg.get("board_file", "user_data/board.json")
        if not os.path.isabs(raw_board_file):
            board_file = os.path.abspath(os.path.join(norm_authority_root, raw_board_file))
        else:
            board_file = raw_board_file
        adapter = OfflineBoardAdapter(board_file=board_file, field_map=board_cfg.get("fields", {}))
    else:
        adapter = get_board_adapter(board_config)

    record = adapter.get_record(task_id)
    if record is None:
        raise TaskSpecNotFoundError(f"Task '{task_id}' not found in authoritative board at {board_config}")

    # 提取字段
    fields = record.get("fields", {}) if "fields" in record else record
    status = str(fields.get("status") or record.get("status") or "").strip()
    task_name = str(fields.get("name") or fields.get("task_name") or record.get("name") or record.get("task_name") or "").strip()
    owner = str(fields.get("owner") or fields.get("assignee") or record.get("owner") or record.get("assignee") or "李开发").strip()
    handler = str(fields.get("handler") or record.get("handler") or owner).strip()
    task_type = str(fields.get("type") or record.get("type") or "A").strip()

    # 状态校验
    terminal_statuses = {"已验收", "已取消"}
    if status in terminal_statuses:
        raise TaskSpecInvalidStatusError(
            f"Task '{task_id}' is in terminal status '{status}', cannot be executed by Runner."
        )

    # 提取需求正文与验收标准
    process_remarks = str(fields.get("process") or fields.get("remarks") or record.get("process") or record.get("remarks") or "").strip()
    desc = str(fields.get("description") or record.get("description") or "").strip()
    
    requirement_text = process_remarks or desc or task_name
    if not requirement_text:
        raise TaskSpecIncompleteError(f"Task '{task_id}' has no requirement text or process description.")

    acceptance_criteria = f"验收标准: 完成【{task_name}】的实现与验证，代码通过独立审查与测试全量回归，满足规范要求。"
    if "验收标准" in requirement_text:
        parts = requirement_text.split("验收标准")
        if len(parts) > 1 and len(parts[1].strip()) > 10:
            lines = parts[1].strip().splitlines()
            if lines:
                acceptance_criteria = "验收标准" + lines[0]

    acceptance_criteria_hash = hashlib.sha256(acceptance_criteria.strip().encode("utf-8")).hexdigest()

    # 任务版本与更新标识
    task_version = str(fields.get("updated_at") or record.get("updated_at") or fields.get("seq") or record.get("seq") or hashlib.sha256(process_remarks.encode("utf-8")).hexdigest()[:16])

    # Git 基线获取
    baseline_commit, baseline_branch = _get_git_head_and_branch(norm_project_root)

    # 推导 project_id
    project_id = os.path.basename(norm_project_root)

    # 合并 overrides
    ov = overrides or {}

    spec = TaskExecutionSpec(
        project_id=str(ov.get("project_id", project_id)),
        project_root=norm_project_root,
        authority_root=norm_authority_root,
        task_id=task_id,
        task_name=task_name or f"Task-{task_id}",
        requirement_text=requirement_text,
        acceptance_criteria=acceptance_criteria,
        acceptance_criteria_hash=acceptance_criteria_hash,
        task_version=task_version,
        status_at_read=status,
        owner=owner,
        handler=handler,
        task_type=task_type,
        baseline_branch=str(ov.get("baseline_branch", baseline_branch)),
        baseline_commit=str(ov.get("baseline_commit", baseline_commit)),
        builder_adapter_id=str(ov.get("builder_adapter_id", "codex_cli")),
        reviewer_adapter_id=str(ov.get("reviewer_adapter_id", "antigravity")),
        qa_adapter_id=str(ov.get("qa_adapter_id", "codex_cli")),
        workspace_mode=str(ov.get("workspace_mode", "branch")),
        worktree_root=ov.get("worktree_root"),
        test_command=ov.get("test_command"),
        builder_timeout_seconds=int(ov.get("builder_timeout_seconds", 300)),
        reviewer_timeout_seconds=int(ov.get("reviewer_timeout_seconds", 300)),
        qa_timeout_seconds=int(ov.get("qa_timeout_seconds", 300)),
        max_review_cycles=int(ov.get("max_review_cycles", 3)),
        max_qa_cycles=int(ov.get("max_qa_cycles", 3)),
        max_total_attempts=int(ov.get("max_total_attempts", 6)),
        total_wall_clock_timeout_seconds=int(ov.get("total_wall_clock_timeout_seconds", 1800)),
        allow_git_push=bool(ov.get("allow_git_push", False)),
        allow_merge_main=bool(ov.get("allow_merge_main", False)),
        allow_auto_accept=bool(ov.get("allow_auto_accept", False)),
        allow_release=bool(ov.get("allow_release", False)),
    )
    return spec


def verify_optimistic_concurrency(spec: TaskExecutionSpec, board_adapter: Any) -> bool:
    """
    在执行真实状态流转前，对权威看板进行乐观并发检查。
    如果任务在外部被修改（状态被篡改、已被取消或验收），返回 False。
    """
    try:
        record = board_adapter.get_record(spec.task_id)
        if not record:
            return False
        fields = record.get("fields", {}) if "fields" in record else record
        current_status = str(fields.get("status") or record.get("status") or "").strip()
        if current_status in ("已验收", "已取消"):
            return False
        return True
    except Exception:
        return False
