# -*- coding: utf-8 -*-
"""Versioned workflow profiles, independent from host managed-role config."""
from __future__ import annotations

import ast
import copy
import json
import os
import re
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence

try:
    from .rule_source import PROFILE_VERSION, gates_text, source_version
except ImportError:  # python path/to/validate_transition.py
    from rule_source import PROFILE_VERSION, gates_text, source_version

PROFILE_STANDARD = "standard"
PROFILE_LITE = "lite"
PROFILE_DOCS = "docs-short"
SUPPORTED_PROFILES = (PROFILE_STANDARD, PROFILE_LITE, PROFILE_DOCS)

RISK_PATTERNS = (
    ("auth", re.compile(
        r"(?i)(password\s*=|api[_-]?key|secret[_-]?key|oauth|authorization:\s*bearer|私钥|认证绕过)"
    )),
    ("permission", re.compile(
        r"(?i)(is_admin\s*=\s*true|grant\s+all|chmod\s+777|privilege\s+escalat|越权|权限提升|rbac)"
    )),
    ("funds", re.compile(
        r"(?i)(payment|billing|refund|stripe|金额|支付|转账|账务|balance\s*[-+]=)"
    )),
    ("privacy", re.compile(
        r"(?i)(身份证|privacy|pii|\bssn\b|手机号|个人敏感|user\.phone|email\s*=\s*[^\n]*phone)"
    )),
    ("migration", re.compile(
        r"(?i)(alembic|flyway|alter\s+table|数据库迁移|schema\s+migration|drop_table)"
    )),
    ("deploy", re.compile(
        r"(?i)(kubectl\s+apply|terraform\s+apply|生产部署|production\s+deploy|helm\s+upgrade)"
    )),
)

_TRANSIENT = (
    ("CAPACITY", re.compile(r"\b503\b|service unavailable|(?:\bunavailable\b).*(?:capacity|temporar|server)|no capacity available", re.I)),
    ("RATE_LIMIT", re.compile(r"(?:\b429\b|rate[ -]?limit|too many requests|resource_exhausted)", re.I)),
    ("BAD_GATEWAY", re.compile(r"\b502\b|bad gateway", re.I)),
    ("GATEWAY_TIMEOUT", re.compile(r"\b504\b|gateway timeout", re.I)),
    ("TRANSIENT_NETWORK", re.compile(r"connection (?:reset|aborted)|temporar(?:y|ily) unavailable|i/o timeout", re.I)),
)
_AUTH = re.compile(
    r"permission|denied_actions|access denied|unauthori[sz]ed|forbidden|authentication",
    re.I,
)
_PROTOCOL = re.compile(r"invalid json|json decode|schema mismatch|protocol error|malformed", re.I)
_BUDGET = re.compile(r"budget|quota (?:exhausted|exceeded)|max total attempts|wall clock", re.I)
_TOKEN = re.compile(r"WF_POLICY\s+(\S+)")


class PolicyError(RuntimeError):
    pass


@dataclass
class FailureDisposition:
    kind: str
    retryable: bool
    terminal_state: Optional[str]
    forge_business_decision: bool = False
    delay_seconds: float = 0.0
    message: str = ""


@dataclass
class PolicyDecision:
    profile_id: str
    profile_version: str
    risk_level: str
    risk_signals: List[Dict[str, str]] = field(default_factory=list)
    checks_passed: bool = False
    check_errors: List[str] = field(default_factory=list)
    blocked: bool = False
    message: str = ""
    required_roles: List[str] = field(default_factory=list)
    gates: List[str] = field(default_factory=list)
    budget: Dict[str, Any] = field(default_factory=dict)
    requirement_hash: str = ""
    acceptance_criteria_hash: str = ""
    rule_source_version: str = ""
    ignored_claims: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "profile_id": self.profile_id,
            "profile_version": self.profile_version,
            "risk_level": self.risk_level,
            "risk_signals": list(self.risk_signals),
            "checks_passed": self.checks_passed,
            "check_errors": list(self.check_errors),
            "blocked": self.blocked,
            "message": self.message,
            "required_roles": list(self.required_roles),
            "gates": list(self.gates),
            "budget": dict(self.budget),
            "requirement_hash": self.requirement_hash,
            "acceptance_criteria_hash": self.acceptance_criteria_hash,
            "rule_source_version": self.rule_source_version,
            "ignored_claims": list(self.ignored_claims),
            "verifier_evidence_id": "",
            "open_defects": [],
        }


_AUTH_ALWAYS_ALLOW = re.compile(
    r"(?ims)^[ \t]*def[ \t]+(can_access|has_permission|is_authorized|authorize|"
    r"check_access|is_allowed|has_access|check_permission)\s*\([^)]*\)\s*:"
    r"(.*?)(?=^[ \t]*def[ \t]+|\Z)"
)


def _material_blob(item: Mapping[str, str]) -> str:
    return "\n".join((
        str(item.get("path") or ""),
        str(item.get("content") or ""),
        str(item.get("diff") or ""),
    ))


def _permission_bypass(item: Mapping[str, str]) -> Optional[Dict[str, str]]:
    """An authorization function that returns True is an explicit permission opening."""
    blob = _material_blob(item)
    match = _AUTH_ALWAYS_ALLOW.search(blob)
    if match is None or not re.search(r"(?i)\breturn\s+True\b", match.group(2)):
        return None
    return {
        "category": "permission",
        "path": str(item.get("path") or ""),
        "excerpt": " ".join(match.group(0).split())[:80],
    }


def scan_risk(materials: Iterable[Mapping[str, str]]) -> List[Dict[str, str]]:
    """Scan path and content. Titles, tags, file counts and user claims are not inputs."""
    signals: List[Dict[str, str]] = []
    for item in materials:
        path = str(item.get("path") or "")
        blob = _material_blob(item)
        for name, pattern in RISK_PATTERNS:
            match = pattern.search(blob)
            if match:
                signals.append({
                    "category": name,
                    "path": path,
                    "excerpt": match.group(0)[:80],
                })
        bypass = _permission_bypass(item)
        if bypass and bypass not in signals:
            signals.append(bypass)
    return signals


def _is_pure_expr(node: ast.AST) -> bool:
    """Local arithmetic and data construction. Boolean gates are not computation."""
    if isinstance(node, ast.Constant):
        return not isinstance(node.value, bool) and isinstance(node.value, (int, float, str, bytes, type(None)))
    if isinstance(node, ast.Name):
        return True
    if isinstance(node, ast.BinOp):
        return _is_pure_expr(node.left) and _is_pure_expr(node.right)
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, (ast.UAdd, ast.USub)):
        return _is_pure_expr(node.operand)
    if isinstance(node, (ast.Tuple, ast.List)):
        return all(_is_pure_expr(item) for item in node.elts)
    return False


def _function_definition_is_pure(stmt: ast.FunctionDef) -> bool:
    """Definition-time sites must be pure. A clean return body is not enough."""
    if stmt.decorator_list:
        return False
    for param in getattr(stmt, "type_params", ()) or ():
        if any(isinstance(child, ast.Call) for child in ast.walk(param)):
            return False
    arguments = stmt.args
    defaults = list(arguments.defaults)
    defaults.extend(item for item in arguments.kw_defaults if item is not None)
    if any(not _is_pure_expr(default) for default in defaults):
        return False
    annotations = [
        arg.annotation
        for arg in (*arguments.posonlyargs, *arguments.args, *arguments.kwonlyargs)
    ]
    if arguments.vararg is not None:
        annotations.append(arguments.vararg.annotation)
    if arguments.kwarg is not None:
        annotations.append(arguments.kwarg.annotation)
    annotations.append(stmt.returns)
    if any(annotation is not None and not _is_pure_expr(annotation) for annotation in annotations):
        return False
    body = stmt.body
    # Only a literal leading docstring is inert; f-strings and other expression
    # statements remain subject to conservative rejection.
    if (body and isinstance(body[0], ast.Expr)
            and isinstance(body[0].value, ast.Constant)
            and isinstance(body[0].value.value, str)):
        body = body[1:]
    if len(body) != 1 or not isinstance(body[0], ast.Return):
        return False
    value = body[0].value
    return value is not None and _is_pure_expr(value)


def _is_pure_python(content: str) -> bool:
    try:
        tree = ast.parse(content)
    except SyntaxError:
        return False
    if not tree.body:
        return False
    for stmt in tree.body:
        if isinstance(stmt, ast.Assign):
            if not stmt.targets or not all(isinstance(target, ast.Name) for target in stmt.targets):
                return False
            if not _is_pure_expr(stmt.value):
                return False
        elif isinstance(stmt, ast.FunctionDef):
            if not _function_definition_is_pure(stmt):
                return False
        elif isinstance(stmt, ast.Expr):
            if not _is_pure_expr(stmt.value):
                return False
        else:
            return False
    return True


def _positively_low(files: Sequence[Mapping[str, str]]) -> bool:
    """Low risk is a positive classification, never the absence of a keyword."""
    if not files:
        return False
    paths = [str(item.get("path") or "") for item in files]
    if all(path.endswith(".md") for path in paths):
        ok, _errors = assess_docs(files)
        return ok
    if all(path.endswith(".py") for path in paths):
        return all(_is_pure_python(str(item.get("content") or "")) for item in files)
    return False


def classify_change(
    files: Optional[Sequence[Mapping[str, str]]] = None,
    requirement_text: str = "",
) -> tuple:
    """Return high, low, unclear, or unspecified.

    unspecified means there is no candidate to classify yet. It is not evidence
    that the change is low risk.
    """
    materials: List[Dict[str, str]] = []
    if requirement_text:
        materials.append({"path": "", "content": requirement_text})
    file_list = [dict(item) for item in (files or [])]
    materials.extend(file_list)
    signals = scan_risk(materials)
    if signals:
        return "high", signals
    if not file_list:
        return "unspecified", []
    if _positively_low(file_list):
        return "low", []
    return "unclear", []


def _secret_findings(text: str) -> List[str]:
    import importlib.util
    script = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "check_secrets.py"))
    spec = importlib.util.spec_from_file_location("yy_flow_check_secrets", script)
    if spec is None or spec.loader is None:
        raise PolicyError("无法加载秘密扫描器")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return [desc for _, _, desc in module.scan_text(text)]


def _path_depth(path: str) -> int:
    parts = [part for part in path.replace("\\", "/").split("/") if part not in ("", ".")]
    return len(parts)


def assess_docs(files: Sequence[Mapping[str, str]]) -> tuple:
    errors: List[str] = []
    if not files:
        return False, ["没有可检查的文档改动"]
    for item in files:
        path = str(item.get("path") or "")
        content = str(item.get("content") or "")
        if not path.endswith(".md"):
            errors.append(f"非文档文件不能走文档短链: {path}")
            continue
        if _path_depth(path) > 3:
            errors.append(f"文档路径超过 3 级: {path}")
        stripped = content.lstrip("\ufeff").lstrip()
        if not stripped.startswith("---"):
            errors.append(f"缺少 YAML frontmatter: {path}")
        else:
            end = stripped.find("\n---", 3)
            if end < 0:
                errors.append(f"frontmatter 未闭合: {path}")
            else:
                import yaml
                try:
                    meta = yaml.safe_load(stripped[3:end])
                except Exception as exc:
                    meta = None
                    errors.append(f"frontmatter 无法解析: {path}: {exc}")
                if not isinstance(meta, dict) or not any(key in meta for key in ("title", "name", "type")):
                    errors.append(f"frontmatter 缺少文档元数据: {path}")
        for desc in _secret_findings(content + "\n" + str(item.get("diff") or "")):
            errors.append(f"秘密扫描未通过: {path}: {desc}")
    return (len(errors) == 0), errors


def _roles_for(profile_id: str) -> List[str]:
    if profile_id == PROFILE_LITE:
        return ["BUILDER", "VERIFIER", "USER"]
    if profile_id == PROFILE_DOCS:
        return ["DOCS", "USER"]
    return ["BUILDER", "REVIEWER", "QA", "USER"]


def _gates_for(profile_id: str) -> List[str]:
    common = ["safety_rules_complete", "user_acceptance_required", "budget_bounded"]
    if profile_id == PROFILE_LITE:
        return common + ["risk_content_scan", "verifier_evidence", "no_forged_reviewer_qa"]
    if profile_id == PROFILE_DOCS:
        return common + ["secret_scan", "doc_metadata", "path_depth"]
    return common + ["independent_review", "independent_qa"]


def _budget(consumed: int = 0, max_attempts: int = 6, transient_used: int = 0, transient_max: int = 3) -> Dict[str, Any]:
    return {
        "max_total_attempts": max_attempts,
        "budget_consumed": consumed,
        "host_transient_max_retries": transient_max,
        "transient_retries_used": transient_used,
    }


def select_profile(
    *,
    task_type: str = "A",
    requested: Optional[str] = None,
    files: Optional[Sequence[Mapping[str, str]]] = None,
    requirement_text: str = "",
    title: str = "",
    tags: Optional[Sequence[str]] = None,
    user_claims_low_risk: bool = False,
    requirement_hash: str = "",
    acceptance_criteria_hash: str = "",
    budget_consumed: int = 0,
) -> PolicyDecision:
    """Choose a profile from content. Title, tags and user claims do not lower risk."""
    level, signals = classify_change(files, requirement_text)
    risk = level
    ignored = []
    if title:
        ignored.append("title")
    if tags:
        ignored.append("tags")
    if user_claims_low_risk:
        ignored.append("user_claim")
    doc_files = [item for item in (files or []) if str(item.get("path") or "").endswith(".md")]
    only_docs = bool(files) and len(doc_files) == len(list(files))
    docs_ok, doc_errors = assess_docs(list(files or [])) if only_docs else (False, [])
    if risk == "high":
        docs_ok = False
    requested_name = (requested or "").strip().lower() or None
    if requested_name in ("", "none"):
        requested_name = None
    if requested_name == "auto":
        if risk == "low" and (only_docs or task_type.upper() == "C") and only_docs:
            chosen = PROFILE_DOCS
            blocked = not docs_ok
            message = "低风险文档" if docs_ok else "文档必要检查未通过"
        elif risk == "low":
            chosen = PROFILE_LITE
            blocked = False
            message = "低风险代码选择 Lite"
        elif risk == "high":
            chosen = PROFILE_STANDARD
            blocked = False
            message = "内容含高风险信号，自动选择 Standard"
        else:
            chosen = PROFILE_STANDARD
            blocked = False
            message = "风险不明确，保持 Standard"
    elif requested_name == PROFILE_LITE:
        chosen = PROFILE_LITE
        if risk == "high":
            blocked = True
            message = "高风险改动不能使用 Lite，请重新计划"
        elif risk == "unclear":
            blocked = True
            message = "风险不明确，保持 Standard，不能使用 Lite"
        else:
            blocked = False
            message = "显式 Lite"
    elif requested_name == PROFILE_DOCS:
        chosen = PROFILE_DOCS
        if not list(files or []) and risk != "high":
            blocked = False
            message = "文档短链，候选生成后复验"
        else:
            blocked = risk in ("high", "unclear") or not docs_ok
            message = "高风险或文档检查未通过，不能短链" if blocked else "文档短链"
    elif requested_name in (None, PROFILE_STANDARD):
        chosen = PROFILE_STANDARD
        blocked = False
        message = "Standard" if requested_name else "未配置，保持 Standard"
    else:
        raise PolicyError(f"未知工作流 Profile: {requested_name}")

    checks_passed = chosen != PROFILE_DOCS or docs_ok
    if chosen == PROFILE_DOCS and risk == "high":
        checks_passed = False
        doc_errors = list(doc_errors) + ["高风险内容不得短链"]
    return PolicyDecision(
        profile_id=chosen,
        profile_version=PROFILE_VERSION,
        risk_level=risk,
        risk_signals=signals,
        checks_passed=checks_passed and not blocked,
        check_errors=doc_errors,
        blocked=blocked,
        message=message,
        required_roles=_roles_for(chosen),
        gates=_gates_for(chosen),
        budget=_budget(consumed=budget_consumed),
        requirement_hash=requirement_hash,
        acceptance_criteria_hash=acceptance_criteria_hash,
        rule_source_version=source_version(),
        ignored_claims=ignored,
    )


def freeze_policy(decision: PolicyDecision, task_id: str) -> Dict[str, Any]:
    if decision.blocked:
        raise PolicyError(decision.message or "策略被阻断，不能固化")
    if not str(decision.requirement_hash or "").strip() or not str(decision.acceptance_criteria_hash or "").strip():
        raise PolicyError("没有需求哈希和验收哈希，拒绝固化")
    payload = decision.to_dict()
    payload["task_id"] = task_id
    payload["gate_text"] = gates_text(decision.profile_id)
    return payload


def policy_token(policy: Mapping[str, Any]) -> str:
    checks = 1 if policy.get("checks_passed") else 0
    verifier = policy.get("verifier_evidence_id") or ""
    return (
        f"WF_POLICY profile={policy.get('profile_id')};version={policy.get('profile_version')};"
        f"risk={policy.get('risk_level')};checks={checks};verifier={verifier}"
    )


def policy_from_remarks(remarks: str) -> Optional[Dict[str, Any]]:
    if not remarks:
        return None
    match = _TOKEN.search(remarks)
    if not match:
        return None
    data: Dict[str, Any] = {}
    for part in match.group(1).split(";"):
        if "=" not in part:
            continue
        key, value = part.split("=", 1)
        data[key] = value
    if "profile" not in data:
        return None
    return {
        "profile_id": data.get("profile"),
        "profile_version": data.get("version") or PROFILE_VERSION,
        "risk_level": data.get("risk") or "low",
        "checks_passed": data.get("checks") == "1",
        "verifier_evidence_id": data.get("verifier") or "",
    }


def merge_policy(file_policy: Optional[Mapping[str, Any]], remarks: str = "") -> Optional[Dict[str, Any]]:
    """Frozen policy comes only from the policy record. Remarks cannot grant a profile or a pass."""
    del remarks
    if not file_policy:
        return None
    return dict(file_policy)


def policy_dir(data_root: str) -> str:
    return os.path.join(data_root, "workflow_policies")


def policy_path(data_root: str, task_id: str) -> str:
    return os.path.join(policy_dir(data_root), f"{task_id}.json")


def save_policy_file(data_root: str, policy: Mapping[str, Any]) -> str:
    task_id = str(policy.get("task_id") or "")
    if not task_id:
        raise PolicyError("策略缺少 task_id")
    destination = policy_path(data_root, task_id)
    os.makedirs(os.path.dirname(destination), exist_ok=True)
    with open(destination, "w", encoding="utf-8") as handle:
        json.dump(policy, handle, ensure_ascii=False, indent=2)
    return destination


def load_policy_file(data_root: str, task_id: str) -> Optional[Dict[str, Any]]:
    path = policy_path(data_root, task_id)
    if not os.path.isfile(path):
        return None
    with open(path, "r", encoding="utf-8") as handle:
        data = json.load(handle)
    if not isinstance(data, dict):
        raise PolicyError("策略文件不是对象")
    if data.get("task_id") not in (None, "", task_id):
        raise PolicyError("策略文件与任务编号不一致")
    return data


def evaluate_candidate_risk(policy: Mapping[str, Any], files: Sequence[Mapping[str, str]], requirement_text: str = "") -> Dict[str, Any]:
    """Re-scan a candidate. Lightweight profiles continue only when risk is positively low."""
    level, signals = classify_change(files, requirement_text)
    lightweight = policy.get("profile_id") in (PROFILE_LITE, PROFILE_DOCS)
    stop = lightweight and level != "low"
    if level == "high":
        message = "风险升级，停止轻量路径并重新计划"
    elif stop:
        message = "风险不明确，停止轻量路径并重新计划"
    else:
        message = ""
    reported = "high" if level == "high" else ("low" if level == "low" else "unclear")
    return {
        "risk_level": reported,
        "risk_signals": signals,
        "stop_for_replan": stop,
        "message": message if lightweight else "",
    }


def resume_policy(
    frozen: Mapping[str, Any],
    *,
    requirement_hash: str,
    acceptance_criteria_hash: str,
    candidate_commit: Optional[str],
    files: Sequence[Mapping[str, str]],
    budget_consumed: int,
    requested_profile: Optional[str] = None,
    downgrade_authorization: str = "",
    open_defects: Optional[Sequence[Mapping[str, Any]]] = None,
    candidate_pending: bool = False,
) -> Dict[str, Any]:
    original_budget = int((frozen.get("budget") or {}).get("budget_consumed") or 0)
    consumed = max(original_budget, int(budget_consumed))
    if consumed < original_budget:
        raise PolicyError("恢复不得清零历史预算")
    if frozen.get("profile_version") != PROFILE_VERSION:
        return {
            "ok": False,
            "state": "NEEDS_USER_INPUT",
            "message": "策略版本不兼容，拒绝恢复",
            "budget_consumed": consumed,
        }
    if not str(frozen.get("requirement_hash") or "").strip() or not str(frozen.get("acceptance_criteria_hash") or "").strip():
        return {
            "ok": False,
            "state": "NEEDS_USER_INPUT",
            "message": "旧策略缺少需求或验收哈希，拒绝恢复",
            "budget_consumed": consumed,
        }
    if not str(requirement_hash or "").strip() or not str(acceptance_criteria_hash or "").strip():
        return {
            "ok": False,
            "state": "NEEDS_USER_INPUT",
            "message": "恢复缺少需求或验收哈希",
            "budget_consumed": consumed,
        }
    if frozen.get("requirement_hash") != requirement_hash:
        return {
            "ok": False,
            "state": "NEEDS_USER_INPUT",
            "message": "需求已变化，拒绝沿用旧策略恢复",
            "budget_consumed": consumed,
        }
    if frozen.get("acceptance_criteria_hash") != acceptance_criteria_hash:
        return {
            "ok": False,
            "state": "NEEDS_USER_INPUT",
            "message": "验收契约已变化，拒绝沿用旧策略恢复",
            "budget_consumed": consumed,
        }
    risk = evaluate_candidate_risk(frozen, files, "")
    # An interrupted first Builder has no immutable candidate yet. Only this
    # explicit lifecycle state may defer scanning; completion still rescans.
    if risk["stop_for_replan"] and not (candidate_pending and not candidate_commit and not files):
        return {
            "ok": False,
            "state": "NEEDS_USER_INPUT",
            "message": "风险升级，停止轻量路径并重新计划",
            "budget_consumed": consumed,
            "risk_signals": risk["risk_signals"],
        }
    target = (requested_profile or frozen.get("profile_id") or PROFILE_STANDARD).lower()
    current = str(frozen.get("profile_id") or PROFILE_STANDARD)
    defects = list(open_defects or frozen.get("open_defects") or [])
    if target != current:
        order = {PROFILE_DOCS: 0, PROFILE_LITE: 1, PROFILE_STANDARD: 2}
        if order.get(target, 2) < order.get(current, 2):
            if not downgrade_authorization.strip():
                return {
                    "ok": False,
                    "state": "NEEDS_USER_INPUT",
                    "message": "Profile 降级需要明确授权",
                    "budget_consumed": consumed,
                }
            if defects:
                return {
                    "ok": False,
                    "state": "NEEDS_USER_INPUT",
                    "message": "不得用降级消除已有缺陷",
                    "budget_consumed": consumed,
                }
        else:
            added = [item for item in _gates_for(target) if item not in _gates_for(current)]
            if not added and target != current:
                added = _gates_for(target)
            return {
                "ok": True,
                "state": "READY",
                "message": "Profile 升级需完成新增检查",
                "profile_id": target,
                "required_roles": _roles_for(target),
                "added_gates": added,
                "budget_consumed": consumed,
                "repeat_completed_stages": False,
            }
    return {
        "ok": True,
        "state": "READY",
        "message": "策略可恢复",
        "profile_id": current,
        "required_roles": list(frozen.get("required_roles") or _roles_for(current)),
        "budget_consumed": consumed,
        "candidate_commit": candidate_commit,
        "repeat_completed_stages": False,
    }


def completion_context(data_root: str, task_id: str) -> Dict[str, Any]:
    """Read the frozen checkpoint context used to re-scan and bind completion evidence."""
    import glob
    matches = glob.glob(os.path.join(data_root, "runner_checkpoints", "*", task_id + ".json"))
    if len(matches) != 1:
        return {}
    with open(matches[0], "r", encoding="utf-8") as handle:
        checkpoint = json.load(handle)
    snapshot = checkpoint.get("execution_spec_snapshot") or {}
    root = snapshot.get("project_root") or ""
    candidate = checkpoint.get("candidate_commit") or ""
    baseline = snapshot.get("baseline_commit") or ""
    files: List[Dict[str, str]] = []
    if root and candidate and baseline and candidate != baseline:
        try:
            names = subprocess_diff(root, ["diff", "--name-only", baseline, candidate])
            for name in [line.strip() for line in names.splitlines() if line.strip()]:
                try:
                    content = subprocess_diff(root, ["show", f"{candidate}:{name}"])
                except Exception:
                    content = ""
                files.append({"path": name, "content": content})
        except Exception:
            files = []
    policy = (checkpoint.get("execution_options") or {}).get("workflow_policy")
    return {
        "policy": policy if isinstance(policy, dict) else None,
        "changed_files": files,
        "candidate_commit": candidate,
        "builder_session_id": checkpoint.get("builder_session_id") or "",
        "evidence_root": os.path.join(root, "user_data", "runner_evidence") if root else "",
    }


def subprocess_diff(repo: str, args: Sequence[str]) -> str:
    import subprocess
    return subprocess.check_output(["git", *args], cwd=repo, encoding="utf-8", errors="replace")


def verify_verifier_evidence(record: Any, *, candidate: str, builder_session_id: str = "") -> None:
    """Completion proof is the stored record, not a remarks token."""
    meta = record.metadata
    if meta.actor_role in ("REVIEWER", "QA"):
        raise PolicyError("审查或测试证据不能顶替 Verifier")
    if meta.actor_role != "VERIFIER":
        raise PolicyError("完成证据的 actor 必须是 VERIFIER")
    if meta.extra.get("verifier_decision") != "PASS":
        raise PolicyError("Verifier 决策不是 PASS")
    session_id = str(meta.host_session_id or "")
    if "verifier" not in session_id.lower():
        raise PolicyError("Verifier 会话不可区分")
    if builder_session_id and session_id == builder_session_id:
        raise PolicyError("Verifier 会话与 Builder 相同")
    if not candidate or record.result_commit != candidate:
        raise PolicyError("证据 result_commit 没有绑定当前候选")


def completion_allowed(policy: Mapping[str, Any], records: Sequence[Mapping[str, Any]], candidate: str) -> None:
    profile = policy.get("profile_id")
    bound = [item for item in records if item.get("candidate") == candidate]
    if profile == PROFILE_LITE:
        verifier_records = [item for item in bound if item.get("actor") == "VERIFIER"]
        if not verifier_records or verifier_records[-1].get("decision") != "PASS":
            raise PolicyError("Lite 无 Verifier PASS 证据，不能完成")
        forged = [item for item in bound if item.get("actor") in ("REVIEWER", "QA") and item.get("used_as_completion")]
        if forged:
            raise PolicyError("不得用审查或测试证据冒充 Lite 完成")
        return
    if profile == PROFILE_DOCS:
        if policy.get("risk_level") == "high" or not policy.get("checks_passed"):
            raise PolicyError("文档短链检查未通过或内容为高风险")
        if not any(item.get("actor") == "DOCS_CHECK" and item.get("decision") == "PASS" for item in bound):
            raise PolicyError("文档短链缺少必要检查证据")
        return
    reviewer = [item for item in bound if item.get("actor") == "REVIEWER" and item.get("decision") == "PASS"]
    qa = [item for item in bound if item.get("actor") == "QA" and item.get("decision") == "PASS"]
    if not reviewer or not qa:
        raise PolicyError("Standard 需要同一候选的审查与测试证据")
    if any(item.get("actor") == "VERIFIER" and item.get("used_as_completion") for item in bound):
        raise PolicyError("Standard 不能把 Lite Verifier 报告当作审查或测试证据")


def rebind_evidence(record: Mapping[str, Any], new_candidate: str) -> None:
    old = str(record.get("candidate") or "")
    if old and old != new_candidate:
        raise PolicyError("旧证据不能转绑到新候选")


def backfill_standard(snapshot: Mapping[str, Any]) -> Dict[str, Any]:
    """Annotate Standard only when the old snapshot proves the full chain.

    The input snapshot is never mutated.
    """
    original = copy.deepcopy(dict(snapshot))
    roles = [str(item).upper() for item in snapshot.get("completed_roles") or []]
    has_chain = "BUILDER" in roles and "REVIEWER" in roles and "QA" in roles
    has_lite = "VERIFIER" in roles or snapshot.get("profile_id") == PROFILE_LITE
    equivalent = has_chain and not has_lite and snapshot.get("profile_id") in (None, "", PROFILE_STANDARD)
    note: Dict[str, Any] = {
        "equivalent": equivalent,
        "annotation": None,
        "differences": [],
    }
    if equivalent:
        note["annotation"] = {
            "profile_id": PROFILE_STANDARD,
            "profile_version": PROFILE_VERSION,
            "backfill": True,
            "budget": copy.deepcopy(snapshot.get("budget") or {}),
        }
    else:
        note["differences"] = [
            "无法证明旧执行语义与 Standard 等价" if not has_chain else "快照含 Lite 或其他非 Standard 语义",
        ]
        if snapshot.get("budget") is not None:
            note["preserved_budget"] = copy.deepcopy(snapshot.get("budget"))
    if dict(snapshot) != original:
        raise PolicyError("补记过程改写了旧快照")
    return note


def classify_failure(text: str, *, attempts: int = 0, max_retries: int = 3, budget_remaining: int = 1) -> FailureDisposition:
    raw = text or ""
    if budget_remaining <= 0 or _BUDGET.search(raw):
        return FailureDisposition("budget", False, "NEEDS_USER_INPUT", False, 0.0, "预算耗尽，停止执行")
    if _AUTH.search(raw):
        return FailureDisposition("auth", False, "APPROVAL_REQUIRED", False, 0.0, "等待授权，不扩大权限")
    if _PROTOCOL.search(raw):
        return FailureDisposition("protocol", False, "NEEDS_USER_INPUT", False, 0.0, "协议错误，不编造业务结论")
    for name, pattern in _TRANSIENT:
        if pattern.search(raw):
            if attempts >= max_retries:
                return FailureDisposition("transient", False, "NEEDS_USER_INPUT", False, 0.0, "暂态错误已达重试上限")
            delay = min(30.0, 5.0 * (2 ** attempts))
            return FailureDisposition("transient", True, None, False, delay, f"暂态宿主错误 {name}，有界退避")
    return FailureDisposition("business", False, "REJECTED", False, 0.0, "业务失败，退回修复")


def collect_live_contract() -> Dict[str, Any]:
    import sys
    scripts = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
    if scripts not in sys.path:
        sys.path.insert(0, scripts)
    import enums
    from .validate_transition import ROLE_BASE_PERMISSIONS
    source = open(os.path.join(scripts, "quick_task.py"), encoding="utf-8").read()
    match = re.search(r'p_create\.add_argument\("--type", default="([A-G])"', source)
    if not match:
        raise PolicyError("无法读取 CLI 默认任务类型")
    return {
        "statuses": enums.TaskStatus.all_values(),
        "task_types": enums.TaskType.all_values(),
        "cli_default_task_type": match.group(1),
        "role_permissions": {key: list(value) for key, value in ROLE_BASE_PERMISSIONS.items()},
    }


def compare_contract(recorded: Mapping[str, Any], live: Optional[Mapping[str, Any]] = None) -> List[str]:
    current = dict(live or collect_live_contract())
    differences = []
    if list(recorded.get("statuses") or []) != list(current.get("statuses") or []):
        differences.append("状态枚举与已记录契约不一致")
    if recorded.get("cli_default_task_type") != current.get("cli_default_task_type"):
        differences.append("CLI 默认任务类型与已记录契约不一致")
    recorded_perms = recorded.get("role_permissions") or {}
    live_perms = current.get("role_permissions") or {}
    if recorded_perms != live_perms:
        differences.append("角色权限与已记录契约不一致")
    if list(recorded.get("task_types") or []) != list(current.get("task_types") or []):
        differences.append("任务类型与已记录契约不一致")
    return differences


def refuse_contract_overwrite(existing: Mapping[str, Any], proposed: Mapping[str, Any]) -> None:
    differences = []
    # Compare the two snapshots directly so a silent rewrite is rejected.
    if existing.get("statuses") != proposed.get("statuses"):
        differences.append("状态枚举")
    if existing.get("role_permissions") != proposed.get("role_permissions"):
        differences.append("角色权限")
    if existing.get("cli_default_task_type") != proposed.get("cli_default_task_type"):
        differences.append("CLI 默认任务类型")
    if existing.get("task_types") != proposed.get("task_types"):
        differences.append("任务类型")
    if differences:
        raise PolicyError("拒绝静默覆盖契约: " + ", ".join(differences))


def install_contract_snapshot(path: str, payload: Mapping[str, Any], *, allow_overwrite: bool = False) -> None:
    if os.path.isfile(path):
        with open(path, "r", encoding="utf-8") as handle:
            existing = json.load(handle)
        try:
            refuse_contract_overwrite(existing, payload)
        except PolicyError:
            if not allow_overwrite:
                raise
    directory = os.path.dirname(path)
    if directory:
        os.makedirs(directory, exist_ok=True)
    if os.path.isfile(path) and not allow_overwrite:
        return
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=2)
