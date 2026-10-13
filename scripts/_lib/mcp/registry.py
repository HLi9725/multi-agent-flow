# -*- coding: utf-8 -*-
"""project_id 到数据根的服务端映射。路径只来自登记文件。"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from typing import Dict, List, Mapping

from .schema import DISPLAY_NAME_MAX, PROJECT_ID_RE

_REGISTRY_MAX_BYTES = 1024 * 1024


@dataclass(frozen=True)
class ProjectRecord:
    project_id: str
    display_name: str
    org_id: str
    data_root: str
    board_file: str
    evidence_root: str
    board_kind: str
    board_config: str = ""


@dataclass(frozen=True)
class PrincipalRecord:
    os_user: str
    principal_id: str
    org_id: str
    grants: Mapping[str, frozenset]
    disabled: bool = False
    role: str = "DEV"


@dataclass
class Registry:
    path: str
    server_revision: str
    projects: Dict[str, ProjectRecord] = field(default_factory=dict)
    principals: List[PrincipalRecord] = field(default_factory=list)
    cursor_key: bytes = b""

    def get_project(self, project_id: str) -> ProjectRecord | None:
        return self.projects.get(project_id)


class RegistryError(Exception):
    """登记文件无效。消息不得携带路径。"""


def _within(root: str, candidate: str) -> bool:
    root_real = os.path.realpath(root)
    path_real = os.path.realpath(candidate)
    try:
        return os.path.commonpath([root_real, path_real]) == root_real
    except ValueError:
        return False


def _resolve_under(data_root: str, raw: str, default_relative: str) -> str:
    chosen = raw.strip() if isinstance(raw, str) and raw.strip() else default_relative
    if _has_parent_segment(chosen):
        raise RegistryError("登记路径无效")
    if os.path.isabs(chosen):
        target = os.path.realpath(chosen)
    else:
        target = os.path.realpath(os.path.join(data_root, chosen))
    if not _within(data_root, target):
        raise RegistryError("登记路径无效")
    return target


def _has_parent_segment(value: str) -> bool:
    return ".." in re.split(r"[\\/]", value)


def _load_json(path: str) -> dict:
    real = os.path.realpath(path)
    if not os.path.isfile(real):
        raise RegistryError("登记文件无法读取")
    try:
        size = os.path.getsize(real)
    except OSError:
        raise RegistryError("登记文件无法读取") from None
    if size > _REGISTRY_MAX_BYTES:
        raise RegistryError("登记文件无法读取")
    try:
        with open(real, "r", encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, UnicodeError, json.JSONDecodeError):
        raise RegistryError("登记文件无法读取") from None
    if not isinstance(data, dict):
        raise RegistryError("登记文件无法读取")
    return data


def _cursor_key(path: str, data: Mapping[str, object]) -> bytes:
    secret = data.get("cursor_secret")
    if isinstance(secret, str) and secret.strip():
        return secret.encode("utf-8")
    try:
        with open(path, "rb") as handle:
            raw = handle.read()
    except OSError:
        raise RegistryError("登记文件无法读取") from None
    import hashlib

    return hashlib.sha256(raw).digest()


def _parse_project(item: Mapping[str, object]) -> ProjectRecord:
    project_id = item.get("project_id")
    if not isinstance(project_id, str) or PROJECT_ID_RE.fullmatch(project_id) is None:
        raise RegistryError("登记文件无法读取")
    display = item.get("display_name")
    org_id = item.get("org_id")
    data_root_raw = item.get("data_root")
    board_kind = item.get("board_kind")
    if not isinstance(display, str) or not isinstance(org_id, str) or not org_id.strip():
        raise RegistryError("登记文件无法读取")
    if not isinstance(data_root_raw, str) or not data_root_raw.strip():
        raise RegistryError("登记文件无法读取")
    if board_kind not in {"offline", "github"}:
        raise RegistryError("登记文件无法读取")
    if _has_parent_segment(data_root_raw):
        raise RegistryError("登记路径无效")
    data_root = os.path.realpath(data_root_raw)
    board_file = _resolve_under(
        data_root,
        item.get("board_file") if isinstance(item.get("board_file"), str) else "",
        os.path.join("user_data", "board.json"),
    )
    evidence_root = _resolve_under(
        data_root,
        item.get("evidence_root") if isinstance(item.get("evidence_root"), str) else "",
        "evidence",
    )
    board_config = ""
    raw_config = item.get("board_config")
    if isinstance(raw_config, str) and raw_config.strip():
        board_config = _resolve_under(data_root, raw_config, "")
    return ProjectRecord(
        project_id=project_id,
        display_name=display[:DISPLAY_NAME_MAX],
        org_id=org_id.strip(),
        data_root=data_root,
        board_file=board_file,
        evidence_root=evidence_root,
        board_kind=str(board_kind),
        board_config=board_config,
    )


def _parse_principal(item: Mapping[str, object]) -> PrincipalRecord:
    os_user = item.get("os_user")
    principal_id = item.get("principal_id")
    org_id = item.get("org_id")
    grants_raw = item.get("grants")
    if not isinstance(os_user, str) or not os_user.strip():
        raise RegistryError("登记文件无法读取")
    if not isinstance(principal_id, str) or not principal_id.strip():
        raise RegistryError("登记文件无法读取")
    if not isinstance(org_id, str) or not org_id.strip():
        raise RegistryError("登记文件无法读取")
    if not isinstance(grants_raw, dict):
        raise RegistryError("登记文件无法读取")
    grants = {}
    for project_id, scopes in grants_raw.items():
        if not isinstance(project_id, str) or PROJECT_ID_RE.fullmatch(project_id) is None:
            raise RegistryError("登记文件无法读取")
        if not isinstance(scopes, list) or not all(isinstance(scope, str) for scope in scopes):
            raise RegistryError("登记文件无法读取")
        grants[project_id] = frozenset(scopes)
    disabled = bool(item.get("disabled", False))
    role = item.get("role", "DEV")
    if not isinstance(role, str) or role.upper() not in {
        "PM", "DEV", "FRONTEND", "REVIEWER", "QA", "DOCS", "DEVOPS", "ARCHITECT",
    }:
        raise RegistryError("登记文件无法读取")
    return PrincipalRecord(
        os_user=os_user.strip(),
        principal_id=principal_id.strip(),
        org_id=org_id.strip(),
        grants=grants,
        disabled=disabled,
        role=role.upper(),
    )


def load_registry(path: str) -> Registry:
    if not isinstance(path, str) or not path.strip():
        raise RegistryError("登记文件无法读取")
    real = os.path.realpath(path)
    data = _load_json(real)
    projects_raw = data.get("projects")
    principals_raw = data.get("local_principals")
    if not isinstance(projects_raw, list) or not isinstance(principals_raw, list):
        raise RegistryError("登记文件无法读取")
    projects: Dict[str, ProjectRecord] = {}
    for item in projects_raw:
        if not isinstance(item, dict):
            raise RegistryError("登记文件无法读取")
        project = _parse_project(item)
        if project.project_id in projects:
            raise RegistryError("登记文件无法读取")
        projects[project.project_id] = project
    principals = []
    for item in principals_raw:
        if not isinstance(item, dict):
            raise RegistryError("登记文件无法读取")
        principals.append(_parse_principal(item))
    revision = data.get("server_revision")
    if not isinstance(revision, str) or not revision.strip():
        revision = "4.0.0"
    return Registry(
        path=real,
        server_revision=revision.strip()[:64],
        projects=projects,
        principals=principals,
        cursor_key=_cursor_key(real, data),
    )


def assert_registered_path(project: ProjectRecord, candidate: str) -> None:
    """运行中再次确认路径仍落在登记根内。"""
    if not _within(project.data_root, candidate):
        from .errors import error

        raise error("PATH_REJECTED", details={"reason": "PATH"})
