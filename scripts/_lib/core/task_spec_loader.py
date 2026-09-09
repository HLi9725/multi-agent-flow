# -*- coding: utf-8 -*-
"""
scripts/_lib/core/task_spec_loader.py
2F-PROD 权威任务规范加载与乐观并发校验。
职责：
  1. 从权威看板 (BoardAdapter) 中只读读取指定 task_id 的工单信息；
  2. 提取需求正文、验收标准、基线分支/Commit、当前处理人与版本信息；
  3. 严格校验任务存在性与非终态约束（Fail-Closed）；
  4. 严格比对验收标准哈希、状态与版本，执行乐观并发校验 (Optimistic Concurrency Control)；
  5. 构造不可变的 TaskExecutionSpec 实体。
"""
import hashlib
import json
import os
import re
import subprocess
import sys
from typing import Any, Dict, Optional, Tuple

_SCRIPTS_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
if _SCRIPTS_ROOT not in sys.path:
    sys.path.insert(0, _SCRIPTS_ROOT)

try:
    from ... import paths
except Exception:
    try:
        from scripts import paths
    except Exception:
        import paths
from ..boards.board_adapter_factory import get_board_adapter
from ..boards.offline_board_adapter import OfflineBoardAdapter
from .runner_schema import AcceptanceCriterion, TaskExecutionSpec


class TaskSpecError(Exception):
    """任务规范加载基类异常"""
    pass


class TaskSpecNotFoundError(TaskSpecError):
    """权威看板中未找到指定任务异常 (Fail-Closed)"""
    pass


class TaskSpecInvalidStatusError(TaskSpecError):
    """任务处于不可执行状态 (如已验收/已取消) 异常"""
    pass


class TaskSpecIncompleteError(TaskSpecError):
    """任务关键信息缺失异常"""
    pass


class TaskOptimisticConcurrencyError(TaskSpecError):
    """乐观并发校验失败异常 (任务在外部被修改/冲突)"""
    pass


def _get_git_head_and_branch(repo_path: str) -> Tuple[str, str]:
    """从指定 Git 仓库读取当前 HEAD Commit SHA 与分支名称"""
    try:
        head_sha = subprocess.check_output(
            ["git", "rev-parse", "HEAD"],
            cwd=repo_path,
            stderr=subprocess.DEVNULL,
            text=True,
            timeout=5,
        ).strip()
    except Exception:
        head_sha = ""

    try:
        branch_name = subprocess.check_output(
            ["git", "rev-parse", "--abbrev-ref", "HEAD"],
            cwd=repo_path,
            stderr=subprocess.DEVNULL,
            text=True,
            timeout=5,
        ).strip()
    except Exception:
        branch_name = "main"

    return head_sha, branch_name


def _is_acceptance_section_boundary(line: str) -> bool:
    """识别验收标准之后的新章节或看板流程节点。"""
    stripped = line.strip()
    if not stripped:
        return False
    if re.match(r"^\[T\d+-N\d+\]", stripped):
        return True
    if re.match(r"^#{1,6}\s+", stripped):
        return True
    if stripped.startswith("【") and "验收标准" not in stripped:
        return True
    if re.match(r"^[一二三四五六七八九十]+[、.]", stripped):
        return True
    return False


def _normalise_acceptance_line(line: str) -> str:
    stripped = line.strip()
    stripped = re.sub(r"^(?:[-*+]\s+|\d+[.)、]\s*)", "", stripped)
    return stripped.strip().rstrip("。")


def _compose_requirement_text(
    task_name: str,
    description: str,
    remarks: str,
    process: str,
) -> str:
    """Prefer stable requirement fields; use process only for legacy boards.

    ``process`` is normally an append-only transition log.  Including it when
    description/remarks already contain the requirement would change the
    immutable task hash after every workflow transition and can also hide the
    actual requirement behind operational history.
    """
    description = description.strip()
    remarks = remarks.strip()
    acceptance_marker = re.compile(r"(?:【\s*验收标准\s*】\s*[:：]?|验收标准\s*[:：])")
    requirement_lines = []
    for line in process.splitlines():
        # append_process_node writes a multi-line audit block.  Everything from
        # the first node marker onward is mutable workflow history.
        if re.match(r"^\s*\[T\d+-N\d+\]", line):
            break
        requirement_lines.append(line)
    process_without_history = "\n".join(requirement_lines).strip()
    if remarks and acceptance_marker.search(remarks):
        return "\n\n".join(part for part in (description, remarks) if part)
    if description and acceptance_marker.search(description):
        return description
    if process_without_history and acceptance_marker.search(process_without_history):
        return "\n\n".join(part for part in (description, process_without_history) if part)
    return description or remarks or process_without_history or task_name.strip()


def _extract_acceptance_criteria(
    task_name: str,
    requirement_text: str,
) -> Tuple[str, str, Tuple[AcceptanceCriterion, ...]]:
    """完整提取多行验收标准，并生成稳定的逐项 ID 与哈希。"""
    marker = re.search(
        r"(?:【\s*验收标准\s*】\s*[:：]?|验收标准\s*[:：])",
        requirement_text,
    )
    if marker is None:
        return "", "", ()

    remainder = requirement_text[marker.end():]
    lines = remainder.splitlines()
    criteria_texts = []
    blank_after_content = False

    for raw_line in lines:
        stripped = raw_line.strip()
        if not stripped:
            if criteria_texts:
                blank_after_content = True
            continue
        if _is_acceptance_section_boundary(stripped):
            break

        is_list_item = bool(re.match(r"^(?:[-*+]\s+|\d+[.)、]\s*)", stripped))
        if blank_after_content and not is_list_item:
            break

        criterion = _normalise_acceptance_line(stripped)
        if criterion:
            criteria_texts.append(criterion)
        blank_after_content = False

    # 支持“验收标准: 单行内容”且后续没有换行的常见写法。
    if not criteria_texts:
        inline = _normalise_acceptance_line(remainder.strip())
        if inline:
            criteria_texts.append(inline)

    items = tuple(
        AcceptanceCriterion(criterion_id=f"AC-{index:02d}", text=text)
        for index, text in enumerate(criteria_texts, start=1)
    )
    if not items:
        return "", "", ()

    if len(items) == 1:
        acceptance_criteria = f"验收标准: {items[0].text}"
    else:
        acceptance_criteria = "验收标准:\n" + "\n".join(f"- {item.text}" for item in items)
    criteria_hash = hashlib.sha256(acceptance_criteria.encode("utf-8")).hexdigest()
    return acceptance_criteria, criteria_hash, items


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
    norm_project_root = os.path.abspath(project_root)
    norm_authority_root = os.path.abspath(authority_root) if authority_root else paths.resolve_data_root(cwd=norm_project_root)

    # 寻找看板配置文件
    config_candidates = [
        os.path.join(norm_authority_root, "config", "workflow.config.yaml"),
        os.path.join(norm_authority_root, "user_data", "workflow.config.yaml"),
        os.path.join(norm_project_root, "config", "workflow.config.yaml"),
        os.path.join(norm_project_root, "user_data", "workflow.config.yaml"),
    ]
    board_config = None
    for candidate in config_candidates:
        if os.path.isfile(candidate):
            board_config = candidate
            break

    if not board_config:
        board_config = paths.resolve_runtime_config(cwd=norm_authority_root)

    if not os.path.isfile(board_config):
        raise TaskSpecNotFoundError(
            f"Authoritative board configuration not found at '{board_config}' (authority_root={norm_authority_root})."
        )

    # 加载看板适配器，确保路径锚定到 authority_root
    import yaml
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

    if not record:
        raise TaskSpecNotFoundError(
            f"Task '{task_id}' not found in authoritative board at {board_config}"
        )

    fields = record.get("fields", {}) if isinstance(record, dict) and "fields" in record else record

    # 解析核心字段
    task_name = str(fields.get("name") or fields.get("task_name") or record.get("name") or "").strip()
    status = str(fields.get("status") or record.get("status") or "").strip()
    owner = str(fields.get("owner") or fields.get("assignee") or record.get("owner") or "李开发").strip()
    handler = str(fields.get("handler") or fields.get("assignee") or record.get("handler") or owner).strip()
    task_type = str(fields.get("type") or record.get("type") or "A").strip().upper()

    # 校验状态
    if not status:
        status = "待开始"

    if status in ("已验收", "已取消", "已废弃"):
        raise TaskSpecInvalidStatusError(
            f"Task '{task_id}' is in terminal status '{status}', cannot be executed by Runner."
        )

    # 提取需求正文与验收标准
    process_text = str(fields.get("process") or record.get("process") or "").strip()
    remarks_text = str(fields.get("remarks") or record.get("remarks") or "").strip()
    desc = str(fields.get("description") or record.get("description") or "").strip()

    requirement_text = _compose_requirement_text(task_name, desc, remarks_text, process_text)
    if not requirement_text:
        raise TaskSpecIncompleteError(f"Task '{task_id}' has no requirement text or process description.")

    acceptance_criteria, acceptance_criteria_hash, acceptance_criteria_items = _extract_acceptance_criteria(
        task_name,
        requirement_text,
    )
    if task_type == "A" and not acceptance_criteria_items:
        raise TaskSpecIncompleteError(
            f"Task '{task_id}' is A-class but has no explicit, executable acceptance criteria. "
            "Add an '验收标准' section before starting Production Runner."
        )
    if not acceptance_criteria_items:
        fallback = f"完成【{task_name}】的实现与验证"
        acceptance_criteria_items = (AcceptanceCriterion("AC-01", fallback),)
        acceptance_criteria = f"验收标准: {fallback}"
        acceptance_criteria_hash = hashlib.sha256(acceptance_criteria.encode("utf-8")).hexdigest()
    requirement_hash = hashlib.sha256(requirement_text.strip().encode("utf-8")).hexdigest()

    # 任务版本与更新标识
    task_version = str(fields.get("updated_at") or record.get("updated_at") or fields.get("seq") or record.get("seq") or requirement_hash[:16])

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
        requirement_hash=requirement_hash,
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
        test_commands=ov.get("test_commands") or (),
        acceptance_criteria_items=acceptance_criteria_items,
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


def verify_optimistic_concurrency(
    spec: TaskExecutionSpec,
    board_adapter: Optional[Any] = None,
    *,
    enforce_status: bool = True,
    enforce_version: bool = True,
) -> bool:
    """
    在执行真实状态流转前，对权威看板进行严格乐观并发检查。
    比对任务是否存在、当前状态是否与读取时一致、验收标准哈希是否一致、版本是否一致、是否终态。
    """
    try:
        if board_adapter is None:
            board_config = None
            for cand_dir in [spec.authority_root, spec.project_root, paths.project_root()]:
                if not cand_dir:
                    continue
                cfg = os.path.join(cand_dir, "config", "workflow.config.yaml")
                if os.path.isfile(cfg):
                    board_config = cfg
                    break
            if not board_config:
                return False
            board_adapter = get_board_adapter(board_config)

        record = board_adapter.get_record(spec.task_id)
        if not record:
            return False
        fields = record.get("fields", {}) if isinstance(record, dict) and "fields" in record else (record if isinstance(record, dict) else {})

        current_status = str(fields.get("status") or record.get("status") or "").strip()
        if not current_status:
            return False

        # 1. 终态防篡改硬拦截
        if current_status in ("已验收", "已取消", "已废弃"):
            return False

        # 2. 状态一致性检查（必须等于 spec.status_at_read）
        if enforce_status and spec.status_at_read and current_status != spec.status_at_read:
            return False

        # 3. 验收标准与需求正文哈希校验
        task_name = str(fields.get("name") or fields.get("task_name") or record.get("name") or "").strip()
        process_text = str(fields.get("process") or record.get("process") or "").strip()
        remarks_text = str(fields.get("remarks") or record.get("remarks") or "").strip()
        desc = str(fields.get("description") or record.get("description") or "").strip()
        live_req = _compose_requirement_text(task_name, desc, remarks_text, process_text)

        _, live_criteria_hash, _ = _extract_acceptance_criteria(task_name, live_req)
        if spec.acceptance_criteria_hash and live_criteria_hash != spec.acceptance_criteria_hash:
            return False
        live_requirement_hash = hashlib.sha256(live_req.strip().encode("utf-8")).hexdigest()
        if spec.requirement_hash and live_requirement_hash != spec.requirement_hash:
            return False

        # 4. 任务版本校验
        live_version = str(fields.get("updated_at") or record.get("updated_at") or fields.get("seq") or record.get("seq") or live_requirement_hash[:16])
        if enforce_version and spec.task_version and live_version != spec.task_version:
            return False

        return True
    except Exception:
        return False
