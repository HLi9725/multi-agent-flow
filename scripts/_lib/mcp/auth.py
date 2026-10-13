# -*- coding: utf-8 -*-
"""从传输上下文取主体。4A 只映射本地 STDIO 的操作系统用户，不监听 HTTP。"""

from __future__ import annotations

import getpass
from dataclasses import dataclass
from typing import Mapping

from .registry import PrincipalRecord, Registry


@dataclass(frozen=True)
class AuthContext:
    principal_id: str
    org_id: str
    grants: Mapping[str, frozenset]
    disabled: bool


def current_os_user() -> str:
    try:
        name = getpass.getuser()
    except Exception:
        name = ""
    if isinstance(name, str):
        return name.strip()
    return ""


def resolve_stdio_principal(registry: Registry, os_user: str) -> AuthContext | None:
    if not os_user:
        return None
    folded = os_user.casefold()
    for record in registry.principals:
        if record.os_user.casefold() != folded:
            continue
        return _from_record(record)
    return None


def _from_record(record: PrincipalRecord) -> AuthContext:
    return AuthContext(
        principal_id=record.principal_id,
        org_id=record.org_id,
        grants=record.grants,
        disabled=record.disabled,
    )


def scopes_for(auth: AuthContext, project_id: str) -> frozenset:
    scopes = auth.grants.get(project_id)
    if scopes is None:
        return frozenset()
    return scopes
