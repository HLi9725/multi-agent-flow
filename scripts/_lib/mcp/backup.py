# -*- coding: utf-8 -*-
"""本地备份与副本恢复。演练不得写回正在使用的数据目录。"""

from __future__ import annotations

import json
import os
import shutil
from typing import Dict, List

from .registry import Registry, RegistryError, load_registry

class BackupError(Exception):
    """失败消息不得包含路径、令牌或密钥。"""


def backup_tree(registry: Registry, dest: str) -> None:
    target = os.path.realpath(dest)
    source = os.path.dirname(registry.path)
    if _overlaps(source, target):
        raise BackupError("备份目录与数据目录重叠")
    if os.path.exists(target) and os.listdir(target):
        raise BackupError("备份目录必须为空")
    os.makedirs(target, exist_ok=True)
    _copy_file(registry.path, os.path.join(target, "mcp_registry.json"))
    _copy_tree_if_present(os.path.join(source, "mcp_auth"), os.path.join(target, "mcp_auth"), source)
    _copy_tree_if_present(os.path.join(source, "mcp_audit"), os.path.join(target, "mcp_audit"), source)
    projects = []
    for project in registry.projects.values():
        root = os.path.join(target, "projects", project.project_id)
        os.makedirs(root, exist_ok=True)
        _copy_file(project.board_file, os.path.join(root, "board.json"), jail=project.data_root)
        _copy_tree_if_present(project.evidence_root, os.path.join(root, "evidence"), project.data_root)
        idem = os.path.join(project.data_root, "mcp_idempotency")
        _copy_tree_if_present(idem, os.path.join(root, "mcp_idempotency"), project.data_root)
        if project.board_config:
            _copy_file(project.board_config, os.path.join(root, "board_config.yaml"), jail=project.data_root)
        projects.append(
            {
                "project_id": project.project_id,
                "display_name": project.display_name,
                "org_id": project.org_id,
                "board_kind": project.board_kind,
            }
        )
    _write_json(os.path.join(target, "manifest.json"), {"projects": projects, "server_revision": registry.server_revision})


def restore_tree(backup_dir: str, dest: str) -> str:
    source = os.path.realpath(backup_dir)
    target = os.path.realpath(dest)
    manifest_path = os.path.join(source, "manifest.json")
    registry_src = os.path.join(source, "mcp_registry.json")
    if not os.path.isfile(manifest_path) or not os.path.isfile(registry_src):
        raise BackupError("备份内容不完整")
    if os.path.exists(target):
        raise BackupError("恢复目标必须是新目录")
    try:
        with open(manifest_path, "r", encoding="utf-8") as handle:
            manifest = json.load(handle)
        with open(registry_src, "r", encoding="utf-8") as handle:
            document = json.load(handle)
    except (OSError, UnicodeError, json.JSONDecodeError):
        raise BackupError("备份内容不完整") from None
    if not isinstance(manifest, dict) or not isinstance(document, dict):
        raise BackupError("备份内容不完整")
    projects = manifest.get("projects")
    if not isinstance(projects, list) or not projects:
        raise BackupError("备份内容不完整")
    original_roots = _original_roots(document)
    for root in original_roots:
        if _overlaps(root, target) or _overlaps(os.path.dirname(os.path.realpath(registry_src)), target):
            raise BackupError("恢复目标与源数据重叠")
    os.makedirs(target, exist_ok=True)
    rewritten = _rewrite_registry(document, projects, target)
    _copy_tree_if_present(os.path.join(source, "mcp_auth"), os.path.join(target, "mcp_auth"), source)
    _write_json(os.path.join(target, "mcp_registry.json"), rewritten)
    for item in projects:
        if not isinstance(item, dict):
            raise BackupError("备份内容不完整")
        project_id = item.get("project_id")
        if not isinstance(project_id, str):
            raise BackupError("备份内容不完整")
        packed = os.path.join(source, "projects", project_id)
        idem_src = os.path.join(packed, "mcp_idempotency")
        idem_dst = os.path.join(target, "projects", project_id, "mcp_idempotency")
        _copy_tree_if_present(idem_src, idem_dst, source)
    for item in projects:
        project_id = str(item["project_id"])
        packed = os.path.join(source, "projects", project_id)
        project_root = os.path.join(target, "projects", project_id)
        board_src = os.path.join(packed, "board.json")
        if not os.path.isfile(board_src):
            raise BackupError("备份内容不完整")
        _copy_file(board_src, os.path.join(project_root, "user_data", "board.json"), jail=source)
        _copy_tree_if_present(os.path.join(packed, "evidence"), os.path.join(project_root, "evidence"), source)
        config_src = os.path.join(packed, "board_config.yaml")
        config_dst = os.path.join(project_root, "user_data", "workflow.config.yaml")
        if os.path.isfile(config_src):
            _copy_file(config_src, config_dst, jail=source)
            _retarget_config(config_dst, os.path.join(project_root, "user_data", "board.json"))
    _copy_tree_if_present(os.path.join(source, "mcp_audit"), os.path.join(target, "mcp_audit"), source)
    try:
        restored = load_registry(os.path.join(target, "mcp_registry.json"))
    except RegistryError:
        raise BackupError("恢复后的登记无法读取") from None
    return spot_check(restored)


def spot_check(registry: Registry) -> str:
    for project in registry.projects.values():
        path = os.path.join(project.data_root, "mcp_idempotency", project.project_id + ".jsonl")
        completed = _latest_completed(path)
        if completed is None:
            continue
        result = completed.get("result")
        if not isinstance(result, dict):
            continue
        task_id = result.get("task_id")
        if not isinstance(task_id, str) or not task_id:
            continue
        card = _read_card(project.board_file, task_id)
        if card is None:
            raise BackupError("幂等抽查未找到任务")
        status = result.get("status")
        if isinstance(status, str) and status and str(card.get("status") or "") != status:
            raise BackupError("幂等抽查与看板不一致")
        return task_id
    raise BackupError("没有已完成的幂等键")


def _latest_completed(path: str) -> Dict[str, object] | None:
    if not os.path.isfile(path):
        return None
    found = None
    try:
        with open(path, "r", encoding="utf-8") as handle:
            for raw in handle:
                line = raw.strip()
                if not line:
                    continue
                try:
                    item = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(item, dict) and item.get("state") == "completed":
                    found = item
    except OSError:
        return None
    return found


def _read_card(path: str, task_id: str) -> Dict[str, object] | None:
    try:
        with open(path, "r", encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, UnicodeError, json.JSONDecodeError):
        return None
    if isinstance(data, dict):
        cards = data.get("tasks", data.get("records", []))
    else:
        cards = data
    if not isinstance(cards, list):
        return None
    for card in cards:
        if isinstance(card, dict) and card.get("id") == task_id:
            return card
    return None


def _rewrite_registry(document: dict, projects: List[object], dest: str) -> dict:
    rewritten = json.loads(json.dumps(document))
    by_id = {}
    for item in projects:
        if isinstance(item, dict) and isinstance(item.get("project_id"), str):
            by_id[item["project_id"]] = item
    raw_projects = rewritten.get("projects")
    if not isinstance(raw_projects, list):
        raise BackupError("备份内容不完整")
    for item in raw_projects:
        if not isinstance(item, dict):
            raise BackupError("备份内容不完整")
        project_id = item.get("project_id")
        if not isinstance(project_id, str) or project_id not in by_id:
            raise BackupError("备份内容不完整")
        root = os.path.join(dest, "projects", project_id)
        item["data_root"] = root
        item["board_file"] = os.path.join(root, "user_data", "board.json")
        item["evidence_root"] = os.path.join(root, "evidence")
        if item.get("board_config"):
            item["board_config"] = os.path.join(root, "user_data", "workflow.config.yaml")
    return rewritten


def _original_roots(document: dict) -> List[str]:
    roots = []
    projects = document.get("projects")
    if not isinstance(projects, list):
        return roots
    for item in projects:
        if isinstance(item, dict) and isinstance(item.get("data_root"), str):
            roots.append(os.path.realpath(item["data_root"]))
    return roots


def _overlaps(left: str, right: str) -> bool:
    left_real = os.path.realpath(left)
    right_real = os.path.realpath(right)
    if left_real == right_real:
        return True
    try:
        common_left = os.path.commonpath([left_real, right_real])
    except ValueError:
        return False
    return common_left == left_real or common_left == right_real


def _copy_file(src: str, dest: str, jail: str | None = None) -> None:
    real = os.path.realpath(src)
    if jail is not None and not _inside(jail, real):
        raise BackupError("拒绝路径")
    if os.path.islink(src) or not os.path.isfile(real):
        raise BackupError("备份内容不完整")
    os.makedirs(os.path.dirname(dest), exist_ok=True)
    shutil.copy2(real, dest)


def _copy_tree_if_present(src: str, dest: str, jail: str) -> None:
    if not os.path.isdir(src):
        return
    real = os.path.realpath(src)
    if os.path.islink(src) or not _inside(jail, real):
        raise BackupError("拒绝路径")
    os.makedirs(dest, exist_ok=True)
    for root, dirs, files in os.walk(real):
        dirs[:] = [name for name in dirs if not os.path.islink(os.path.join(root, name))]
        relative = os.path.relpath(root, real)
        current = dest if relative == "." else os.path.join(dest, relative)
        os.makedirs(current, exist_ok=True)
        for name in files:
            source_file = os.path.join(root, name)
            if os.path.islink(source_file):
                raise BackupError("拒绝路径")
            shutil.copy2(source_file, os.path.join(current, name))


def _inside(root: str, candidate: str) -> bool:
    root_real = os.path.realpath(root)
    path_real = os.path.realpath(candidate)
    try:
        return os.path.commonpath([root_real, path_real]) == root_real
    except ValueError:
        return False


def _retarget_config(path: str, board_file: str) -> None:
    try:
        import yaml
    except ImportError:
        raise BackupError("恢复副本无法改写看板配置") from None
    try:
        with open(path, "r", encoding="utf-8") as handle:
            data = yaml.safe_load(handle)
    except (OSError, UnicodeError, yaml.YAMLError):
        raise BackupError("恢复副本无法改写看板配置") from None
    if not isinstance(data, dict):
        raise BackupError("恢复副本无法改写看板配置")
    board = data.get("board")
    if not isinstance(board, dict):
        raise BackupError("恢复副本无法改写看板配置")
    board["board_file"] = board_file
    project = data.get("project")
    if isinstance(project, dict):
        project["root_dir"] = "."
    temporary = path + ".tmp"
    with open(temporary, "w", encoding="utf-8") as handle:
        yaml.safe_dump(data, handle, allow_unicode=True)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)


def _write_json(path: str, payload: dict) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    temporary = path + ".tmp"
    with open(temporary, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=2)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)
