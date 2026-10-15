# -*- coding: utf-8 -*-
"""项目根、技能根与数据根。多个候选或路径逃逸时停止，不猜测第二份看板。"""

from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass

from .common import ARCH_MARKER, EMPTY_BOARD, ORG_ID, OnboardError, raw_has_parent, symlink_escape, within


@dataclass
class Layout:
    project_root: str
    skill_root: str
    script_path: str
    data_root: str
    mode: str
    project_id: str
    display_name: str
    board_path: str
    evidence_root: str
    registry_path: str
    create_board: bool
    create_arch: bool
    pointer_path: str


def _real_dir(path: str, label: str) -> str:
    if raw_has_parent(path):
        raise OnboardError("PATH", f"{label} 含父目录跳转，已停止")
    absolute = os.path.abspath(path)
    if not os.path.isdir(absolute):
        raise OnboardError("PATH", f"{label} 不是已存在的目录")
    if symlink_escape(absolute):
        raise OnboardError("PATH", f"{label} 经符号链接或目录联接跳出，已停止")
    return os.path.realpath(absolute)


def project_id_for(project_root: str) -> str:
    digest = hashlib.sha256(os.path.normcase(project_root).encode("utf-8")).hexdigest()[:16]
    return "p" + digest


def _board_candidates(project_root: str) -> list[tuple[str, str, str]]:
    found = []
    legacy = os.path.join(project_root, "user_data", "board.json")
    modern = os.path.join(project_root, ".yy-flow", "user_data", "board.json")
    if os.path.isfile(legacy):
        found.append(("legacy", legacy, project_root))
    if os.path.isfile(modern):
        found.append(("project", modern, os.path.join(project_root, ".yy-flow")))
    return found


def _skill_has_server(root: str) -> bool:
    return os.path.isfile(os.path.join(root, "scripts", "mcp_server.py"))


def resolve_layout(project_root: str, skill_root: str | None) -> Layout:
    project = _real_dir(project_root, "项目根")
    skill, mode = _resolve_skill(project, skill_root)
    script = os.path.join(skill, "scripts", "mcp_server.py")
    if not _skill_has_server(skill):
        raise OnboardError("SKILL_ROOT", "技能根缺少 scripts/mcp_server.py")
    if not within(skill, script):
        raise OnboardError("PATH", "MCP 脚本跳出技能根，已停止")

    candidates = _board_candidates(project)
    for _kind, board, root in candidates:
        if not within(project, board) or not within(project, root):
            raise OnboardError("PATH", "看板路径跳出项目根，已停止")
    if len(candidates) > 1:
        listed = "、".join(item[1] for item in candidates)
        raise OnboardError("AMBIGUOUS_LAYOUT", "同时存在多份看板，已停止且不新建空看板：" + listed)
    if len(candidates) == 1:
        mode_from_board, board_path, data_root = candidates[0]
        if mode_from_board == "legacy":
            mode = "legacy"
        elif mode == "shared":
            mode = "shared"
        else:
            mode = "project"
        create_board = False
    else:
        data_root = os.path.join(project, ".yy-flow")
        board_path = os.path.join(data_root, "user_data", "board.json")
        create_board = True
        if not within(project, data_root):
            raise OnboardError("PATH", "数据根跳出项目根，已停止")

    if os.path.normcase(os.path.realpath(skill)) == os.path.normcase(project) and mode == "shared":
        raise OnboardError("SKILL_ROOT", "共享技能根不能与当前项目根相同")

    evidence_root = os.path.join(data_root, "evidence")
    registry_path = os.path.join(data_root, "user_data", "mcp_registry.json")
    arch_path = os.path.join(data_root, "user_data", "project_architecture.config.yaml")
    if mode == "legacy":
        pointer_path = os.path.join(data_root, "user_data", "skill-pointer.json")
    else:
        pointer_path = os.path.join(project, ".yy-flow", "skill-pointer.json")
    for path in (evidence_root, registry_path, board_path, pointer_path):
        if not within(project, path):
            raise OnboardError("PATH", "计划路径跳出项目根，已停止")
    display = os.path.basename(project) or "project"
    return Layout(
        project_root=project,
        skill_root=skill,
        script_path=script,
        data_root=os.path.realpath(data_root) if os.path.isdir(data_root) else os.path.abspath(data_root),
        mode=mode,
        project_id=project_id_for(project),
        display_name=display[:128],
        board_path=os.path.abspath(board_path),
        evidence_root=os.path.abspath(evidence_root),
        registry_path=os.path.abspath(registry_path),
        create_board=create_board,
        create_arch=create_board and not os.path.isfile(arch_path),
        pointer_path=os.path.abspath(pointer_path),
    )


def _resolve_skill(project: str, skill_root: str | None) -> tuple[str, str]:
    if skill_root:
        if raw_has_parent(skill_root):
            raise OnboardError("PATH", "技能根含父目录跳转，已停止")
        absolute = os.path.abspath(skill_root)
        if not os.path.isdir(absolute):
            raise OnboardError("SKILL_ROOT", "技能根不是已存在的目录")
        if symlink_escape(absolute):
            raise OnboardError("PATH", "技能根经符号链接跳出，已停止")
        skill = os.path.realpath(absolute)
    else:
        bundled = os.path.join(project, ".yy-flow", "skill")
        pointer = _read_pointer(project)
        if _skill_has_server(bundled):
            if symlink_escape(bundled):
                target = os.path.realpath(bundled)
                if not _skill_has_server(target):
                    raise OnboardError("PATH", "项目内技能链接无效")
                skill = target
            else:
                skill = os.path.realpath(bundled)
        elif pointer:
            skill = pointer
        elif _skill_has_server(project):
            skill = project
        else:
            raise OnboardError("SKILL_ROOT", "未找到项目内技能。共享安装请显式传入 --skill-root")
    parent = os.path.basename(os.path.dirname(skill))
    if parent == ".yy-flow":
        owner = os.path.dirname(os.path.dirname(skill))
        if os.path.normcase(os.path.realpath(owner)) != os.path.normcase(project):
            raise OnboardError(
                "SKILL_ROOT",
                "拒绝把其他业务项目的 .yy-flow/skill 当作共享正本，以免数据根指向该项目",
            )
        return skill, "project"
    bundled_real = os.path.realpath(os.path.join(project, ".yy-flow", "skill")) if os.path.exists(os.path.join(project, ".yy-flow", "skill")) else ""
    if bundled_real and os.path.normcase(bundled_real) == os.path.normcase(skill) and os.path.basename(os.path.dirname(skill)) != ".yy-flow":
        return skill, "shared"
    if os.path.normcase(skill) == os.path.normcase(project):
        return skill, "legacy"
    return skill, "shared"


def _read_pointer(project: str) -> str:
    candidates = (
        os.path.join(project, ".yy-flow", "skill-pointer.json"),
        os.path.join(project, "user_data", "skill-pointer.json"),
    )
    for path in candidates:
        if not os.path.isfile(path):
            continue
        try:
            with open(path, "r", encoding="utf-8") as handle:
                data = json.load(handle)
        except (OSError, UnicodeError, json.JSONDecodeError):
            raise OnboardError("BAD_CONFIG", "技能指向文件无法解析") from None
        raw = data.get("skill_root") if isinstance(data, dict) else ""
        if not isinstance(raw, str) or not raw.strip() or raw_has_parent(raw):
            raise OnboardError("BAD_CONFIG", "技能指向文件无效")
        if symlink_escape(raw) or not os.path.isdir(raw):
            raise OnboardError("SKILL_ROOT", "技能指向的代码根不可用")
        return os.path.realpath(raw)
    return ""


def registry_document(layout: Layout, os_user: str, revision: str) -> dict:
    if not os_user.strip():
        raise OnboardError("PYTHON", "无法确定当前操作系统用户，拒绝登记")
    return {
        "server_revision": revision[:64] or "unknown",
        "projects": [
            {
                "project_id": layout.project_id,
                "display_name": layout.display_name,
                "org_id": ORG_ID,
                "data_root": os.path.abspath(layout.data_root),
                "board_file": "user_data/board.json",
                "evidence_root": "evidence",
                "board_kind": "offline",
                "write_enabled": False,
            }
        ],
        "local_principals": [
            {
                "os_user": os_user,
                "principal_id": "local-" + layout.project_id,
                "org_id": ORG_ID,
                "role": "DEV",
                "grants": {layout.project_id: ["read"]},
            }
        ],
    }


def new_project_files(layout: Layout) -> dict[str, bytes]:
    files = {}
    if layout.create_board:
        files[layout.board_path] = EMPTY_BOARD
    if layout.create_arch:
        arch = os.path.join(layout.data_root, "user_data", "project_architecture.config.yaml")
        files[arch] = ARCH_MARKER.encode("utf-8")
    return files
