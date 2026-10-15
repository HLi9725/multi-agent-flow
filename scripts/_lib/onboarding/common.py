# -*- coding: utf-8 -*-
"""接入常量。三客户端受管 MCP 服务名固定为 yy-flow。"""

from __future__ import annotations

import hashlib
import os
import re

SERVER_NAME = "yy-flow"
CLIENTS = ("cursor", "codex", "antigravity")
MANAGED_BEGIN = "# BEGIN YY-FLOW MANAGED yy-flow"
MANAGED_END = "# END YY-FLOW MANAGED yy-flow"
ORG_ID = "local"
READ_SCOPE = "read"
FORBIDDEN_SCOPES = ("task.transition", "evidence.submit", "acceptance", "admin")
FORBIDDEN_FIELDS = ("github_credential", "cursor_secret", "token_secret")
CREDENTIAL_ENV = ("GITHUB_TOKEN", "GH_TOKEN", "GITHUB_CREDENTIAL", "GH_PAT")
EMPTY_BOARD = b"[]\n"
ARCH_MARKER = 'meta:\n  initialized: false\nproject:\n  name: ""\n'
CLIENT_CONFIGS = {
    "cursor": ".cursor/mcp.json",
    "codex": ".codex/config.toml",
    "antigravity": ".agents/mcp_config.json",
}
EXPORT_DIRS = {
    "cursor": (".cursor/rules", ".cursor/agents"),
    "codex": (".codex/agents",),
    "antigravity": (".agents/agents",),
}


class OnboardError(Exception):
    def __init__(self, code: str, message: str, *, diff: str = "", exit_code: int = 2) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.diff = diff
        self.exit_code = exit_code


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def raw_has_parent(value: str) -> bool:
    return any(part == ".." for part in re.split(r"[\\/]", value or ""))


def within(root: str, candidate: str) -> bool:
    root_real = os.path.normcase(os.path.realpath(root))
    path_real = os.path.normcase(os.path.realpath(candidate))
    try:
        return os.path.commonpath([root_real, path_real]) == root_real
    except ValueError:
        return False


def symlink_escape(path: str) -> bool:
    absolute = os.path.abspath(path)
    real = os.path.realpath(absolute)
    return os.path.normcase(os.path.normpath(absolute)) != os.path.normcase(os.path.normpath(real))


def read_bytes(path: str) -> bytes | None:
    if not os.path.lexists(path) or os.path.isdir(path):
        return None
    with open(path, "rb") as handle:
        return handle.read()


def limited_diff(before: str, after: str, limit: int = 40) -> str:
    import difflib

    lines = list(difflib.unified_diff(before.splitlines(), after.splitlines(), lineterm="", n=1))
    if not lines and before != after:
        lines = ["--- current", "+++ planned", "line endings differ"]
    if len(lines) > limit:
        lines = lines[:limit] + ["... diff truncated"]
    return "\n".join(lines)
