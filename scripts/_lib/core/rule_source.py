# -*- coding: utf-8 -*-
"""Single rule source: safety + role delta + host delta.

Selected modules are loaded in full. Safety text is never truncated.
"""
from __future__ import annotations

import hashlib
import os
from typing import Dict, Iterable, Optional

PROFILE_VERSION = "3.0.0"
SAFETY_FILENAME = "safety-core.md"

HOST_DELTAS: Dict[str, str] = {
    "cursor": (
        "宿主 Cursor 支持按任务动态加载上下文。"
        "动态加载仍必须读完公共安全规则，门禁与静态包一致。"
    ),
    "codex": (
        "宿主 Codex 使用静态规则包。行为、状态权限和证据门禁与动态宿主一致，"
        "不得因为缺少动态加载而省略安全规则。"
    ),
    "antigravity": (
        "宿主 Antigravity 使用静态规则包。托管角色配置 flow-runner-builder、"
        "flow-runner-reviewer、flow-runner-qa 只描述宿主执行能力，不是工作流 Profile。"
    ),
    "static": (
        "当前宿主不支持动态加载，使用静态规则包。门禁与支持动态加载的宿主保持一致。"
    ),
}

ROLE_DELTAS: Dict[str, str] = {
    "PM": "项目经理建卡、分配和收口，不代替用户验收。",
    "ARCHITECT": "架构师负责设计与专项审计，不代替开发领取 A 类实现任务。",
    "DEV": "开发工程师实现任务并在 Standard 下提交独立审查。",
    "FRONTEND": "前端工程师实现界面任务，状态权限与开发工程师的实现链一致。",
    "REVIEWER": "审查专家只在 Standard 下独立审查，不得被 Lite 的 Verifier 证据冒充。",
    "QA": "测试工程师只在 Standard 下独立验证，不得被 Lite 的 Verifier 证据冒充。",
    "DOCS": "文档工程师处理低风险文档。短链前必须通过秘密扫描和文档元数据/路径检查。",
    "DEVOPS": "运维工程师处理环境与发布。生产部署内容本身属于高风险，不能自动降级。",
    "VERIFIER": "Verifier 只服务 Lite。调用身份必须与 Builder 区分，失败或无证据不能完成。",
    "USER": "用户保留最终验收。模型不能代替用户。",
}

PROFILE_GATES: Dict[str, str] = {
    "standard": "必要角色 BUILDER、REVIEWER、QA、USER。完成需要同一候选上的审查 PASS 与测试 PASS。",
    "lite": "必要角色 BUILDER、VERIFIER、USER。完成只接受 Verifier PASS，不接受审查或测试证据顶替。",
    "docs-short": "低风险文档在秘密扫描、frontmatter 和路径深度检查通过后短链。高风险改动不得短链。",
}


class RuleSourceError(RuntimeError):
    """Safety rules are missing or a bundle would drop them."""


def safety_path(root: Optional[str] = None) -> str:
    here = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", ".."))
    base = root or here
    return os.path.join(base, "rules", SAFETY_FILENAME)


def load_safety_text(root: Optional[str] = None) -> str:
    path = safety_path(root)
    if not os.path.isfile(path):
        raise RuleSourceError(f"公共安全规则缺失，拒绝启动: {path}")
    with open(path, "r", encoding="utf-8") as handle:
        text = handle.read()
    if not text.strip():
        raise RuleSourceError("公共安全规则为空，拒绝启动")
    return text


def source_version(root: Optional[str] = None) -> str:
    payload = (PROFILE_VERSION + "\n" + load_safety_text(root)).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def ensure_safety_rules_present(root: Optional[str] = None) -> str:
    text = load_safety_text(root)
    version = source_version(root)
    if version not in hashlib.sha256((PROFILE_VERSION + "\n" + text).encode("utf-8")).hexdigest():
        raise RuleSourceError("规则源版本计算失败")
    return version


def gates_text(profile_id: str) -> str:
    return PROFILE_GATES.get(profile_id, PROFILE_GATES["standard"])


def load_rule_bundle(
    role: str,
    action: str = "general",
    host: str = "cursor",
    profile_id: str = "standard",
    root: Optional[str] = None,
) -> str:
    """Return the full safety text plus only the selected role and host deltas."""
    safety = load_safety_text(root)
    role_key = (role or "DEV").upper()
    host_key = (host or "static").lower()
    if host_key not in HOST_DELTAS:
        host_key = "static"
    profile_key = profile_id if profile_id in PROFILE_GATES else "standard"
    parts = [
        safety.rstrip(),
        "",
        f"## 规则源版本\n{source_version(root)}",
        "",
        f"## 工作流门禁 ({profile_key})\n{gates_text(profile_key)}",
        "",
        f"## 角色职责差异 ({role_key})\n{ROLE_DELTAS.get(role_key, ROLE_DELTAS['DEV'])}",
        "",
        f"## 宿主能力差异 ({host_key})\n{HOST_DELTAS[host_key]}",
        "",
        f"## 当前动作\n{action}",
        "",
    ]
    rendered = "\n".join(parts)
    if safety.strip() not in rendered:
        raise RuleSourceError("安全规则被截断，拒绝返回上下文")
    return rendered


def build_static_bundle(host: str, roles: Optional[Iterable[str]] = None, root: Optional[str] = None) -> str:
    """Static package for hosts that cannot load rules per task."""
    safety = load_safety_text(root)
    selected = [item.upper() for item in (roles or ROLE_DELTAS.keys())]
    role_block = "\n".join(f"- {name}: {ROLE_DELTAS.get(name, '')}" for name in selected)
    host_key = host.lower() if host and host.lower() in HOST_DELTAS else "static"
    rendered = "\n".join([
        safety.rstrip(),
        "",
        f"## 规则源版本\n{source_version(root)}",
        "",
        "## 工作流门禁",
        gates_text("standard"),
        gates_text("lite"),
        gates_text("docs-short"),
        "",
        "## 角色职责差异",
        role_block,
        "",
        "## 宿主能力差异",
        HOST_DELTAS[host_key],
        "",
    ])
    if safety.strip() not in rendered:
        raise RuleSourceError("静态包缺少完整安全规则")
    return rendered
