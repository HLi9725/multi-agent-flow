# -*- coding: utf-8 -*-
"""第三阶段工作流 Profile：文档短链、Lite、Standard 与恢复门禁。"""
import json
import os
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPTS = os.path.join(ROOT, "scripts")
if SCRIPTS not in sys.path:
    sys.path.insert(0, SCRIPTS)

from _lib.core.rule_source import RuleSourceError, build_static_bundle, load_rule_bundle, load_safety_text, source_version
from _lib.core.validate_transition import validate
from _lib.core.workflow_policy import (
    PROFILE_VERSION,
    backfill_standard,
    classify_failure,
    collect_live_contract,
    compare_contract,
    completion_allowed,
    freeze_policy,
    rebind_evidence,
    refuse_contract_overwrite,
    resume_policy,
    select_profile,
)
from _lib.core.production_runner import _transient_host_failure_kind
from build_agent_context import build_context


DOC = """---
title: 说明
type: guide
---

只补充使用说明。
"""


def test_docs_short_chain_after_required_checks():
    decision = select_profile(
        task_type="C",
        requested="docs-short",
        files=[{"path": "docs/guide.md", "content": DOC}],
        requirement_hash="req-docs",
        acceptance_criteria_hash="ac-docs",
    )
    assert decision.profile_id == "docs-short"
    assert decision.checks_passed is True
    assert decision.blocked is False
    policy = freeze_policy(decision, "T100")
    assert validate(
        "DOCS", "进行中", "已完成", "严经理", "2026-10-12 12:00:00", 0,
        task_type="C", workflow_policy=policy,
        changed_files=[{"path": "docs/guide.md", "content": DOC}],
    )


def test_docs_short_chain_rejects_secret_and_high_risk():
    secret = DOC + "\n-----BEGIN PRIVATE KEY-----\n"
    decision = select_profile(task_type="C", requested="docs-short", files=[{"path": "docs/guide.md", "content": secret}])
    assert decision.blocked is True
    risky = DOC + "\npayment = 1\n"
    decision = select_profile(task_type="C", requested="auto", files=[{"path": "docs/guide.md", "content": risky}])
    assert decision.profile_id == "standard"
    assert decision.risk_level == "high"
    assert validate(
        "DOCS", "进行中", "已完成", "严经理", "2026-10-12 12:00:00", 0,
        task_type="C",
        workflow_policy={"profile_id": "docs-short", "risk_level": "high", "checks_passed": False},
    ) is False


def test_low_risk_code_selects_lite_and_standard_stays_full_chain():
    lite = select_profile(
        task_type="A",
        requested="lite",
        files=[{"path": "app.py", "content": "def ready():\n    return True\n"}],
        title="低风险",
        user_claims_low_risk=True,
        requirement_hash="req-lite",
        acceptance_criteria_hash="ac-lite",
    )
    assert lite.profile_id == "lite"
    assert lite.risk_level == "low"
    assert "title" in lite.ignored_claims
    policy = freeze_policy(lite, "T101")
    policy["verifier_evidence_id"] = "evi_verifier_ok"
    assert validate(
        "VERIFIER", "进行中", "已完成", "严经理", "2026-10-12 12:00:00", 0,
        task_type="A", workflow_policy=policy,
    ) is False
    assert validate(
        "DEV", "进行中", "审查中", "周审查", "", 0,
        task_type="A", workflow_policy=policy,
    ) is False
    standard = select_profile(task_type="A", requested=None, files=[{"path": "app.py", "content": "x = 1\n"}])
    assert standard.profile_id == "standard"
    assert validate("DEV", "进行中", "审查中", "周审查", "", 0, task_type="A")
    assert validate("DEV", "进行中", "已完成", "严经理", "2026-10-12 12:00:00", 0, task_type="A") is False


def test_small_high_risk_change_cannot_downgrade():
    decision = select_profile(
        task_type="A",
        requested="lite",
        files=[{"path": "one.py", "content": "def migrate():\n    sql = 'ALTER TABLE users'\n"}],
        title="小改动",
        tags=["lite"],
        user_claims_low_risk=True,
    )
    assert decision.blocked is True
    assert decision.risk_level == "high"
    assert len(decision.risk_signals) >= 1


def test_requirement_change_risk_upgrade_and_incompatible_resume():
    frozen = freeze_policy(select_profile(
        task_type="A", requested="lite",
        files=[{"path": "app.py", "content": "x = 1\n"}],
        requirement_hash="req-1", acceptance_criteria_hash="ac-1", budget_consumed=2,
    ), "T102")
    changed = resume_policy(
        frozen, requirement_hash="req-2", acceptance_criteria_hash="ac-1",
        candidate_commit="a" * 40, files=[{"path": "app.py", "content": "x = 1\n"}], budget_consumed=2,
    )
    assert changed["ok"] is False
    upgraded = resume_policy(
        frozen, requirement_hash="req-1", acceptance_criteria_hash="ac-1",
        candidate_commit="a" * 40,
        files=[{"path": "app.py", "content": "payment = 1\n"}],
        budget_consumed=2,
    )
    assert upgraded["ok"] is False
    assert "重新计划" in upgraded["message"]
    assert upgraded["budget_consumed"] == 2
    stale = dict(frozen)
    stale["profile_version"] = "0.0.1"
    incompatible = resume_policy(
        stale, requirement_hash="req-1", acceptance_criteria_hash="ac-1",
        candidate_commit="a" * 40, files=[], budget_consumed=2,
    )
    assert incompatible["ok"] is False
    kept = resume_policy(
        frozen, requirement_hash="req-1", acceptance_criteria_hash="ac-1",
        candidate_commit="b" * 40, files=[{"path": "app.py", "content": "x = 1\n"}],
        budget_consumed=1,
    )
    assert kept["budget_consumed"] == 2


def test_downgrade_cannot_erase_defects_and_old_evidence_cannot_rebind():
    frozen = freeze_policy(select_profile(
        task_type="A", requested="standard", files=[{"path": "a.py", "content": "x=1\n"}],
        requirement_hash="req-std", acceptance_criteria_hash="ac-std",
    ), "T103")
    frozen["open_defects"] = [{"defect_id": "DEF-1"}]
    denied = resume_policy(
        frozen, requirement_hash="req-std", acceptance_criteria_hash="ac-std", candidate_commit=None,
        files=[], budget_consumed=0, requested_profile="lite", downgrade_authorization="USER-OK",
        open_defects=[{"defect_id": "DEF-1"}],
    )
    assert denied["ok"] is False
    with pytest.raises(Exception):
        rebind_evidence({"candidate": "a" * 40, "actor": "REVIEWER"}, "b" * 40)


def test_missing_or_forged_evidence_and_failure_kinds():
    policy = {"profile_id": "lite"}
    with pytest.raises(Exception):
        completion_allowed(policy, [], "abc")
    with pytest.raises(Exception):
        completion_allowed(policy, [{"actor": "REVIEWER", "decision": "PASS", "candidate": "abc", "used_as_completion": True}], "abc")
    completion_allowed(policy, [{"actor": "VERIFIER", "decision": "PASS", "candidate": "abc"}], "abc")
    with pytest.raises(Exception):
        completion_allowed({"profile_id": "standard"}, [{"actor": "VERIFIER", "decision": "PASS", "candidate": "abc", "used_as_completion": True}], "abc")
    protocol = classify_failure("invalid json")
    assert protocol.kind == "protocol"
    assert protocol.forge_business_decision is False
    auth = classify_failure("authentication failed")
    assert auth.kind == "auth" and auth.terminal_state == "APPROVAL_REQUIRED"
    budget = classify_failure("quota exhausted")
    assert budget.kind == "budget" and budget.retryable is False
    first = classify_failure("503 service unavailable", attempts=0, max_retries=2)
    assert first.retryable is True
    stopped = classify_failure("503 service unavailable", attempts=2, max_retries=2)
    assert stopped.retryable is False
    assert classify_failure("connection reset", attempts=0).kind == "transient"
    assert _transient_host_failure_kind("connection reset") is not None
    assert _transient_host_failure_kind("authentication failed") is None


def test_historical_backfill_does_not_overwrite_snapshot():
    equivalent = {"completed_roles": ["BUILDER", "REVIEWER", "QA"], "budget": {"budget_consumed": 4}}
    before = json.dumps(equivalent, sort_keys=True)
    note = backfill_standard(equivalent)
    assert note["equivalent"] is True
    assert json.dumps(equivalent, sort_keys=True) == before
    assert note["annotation"]["budget"]["budget_consumed"] == 4
    unknown = {"completed_roles": ["BUILDER"], "budget": {"budget_consumed": 1}}
    diff = backfill_standard(unknown)
    assert diff["equivalent"] is False
    assert diff["differences"]
    assert unknown["budget"]["budget_consumed"] == 1


def test_rule_bundle_is_complete_and_static_host_matches():
    safety = load_safety_text(ROOT)
    dynamic = load_rule_bundle("DEV", action="submit", host="cursor", profile_id="lite", root=ROOT)
    static = build_static_bundle("codex", root=ROOT)
    assert safety.strip() in dynamic
    assert safety.strip() in static
    assert source_version(ROOT) in dynamic
    assert "不得用来消除已经存在的缺陷" in dynamic
    with pytest.raises(RuleSourceError):
        load_safety_text(os.path.join(ROOT, "tests"))


def test_contract_drift_refuses_silent_overwrite():
    live = collect_live_contract()
    assert compare_contract(live, live) == []
    changed = dict(live)
    changed["cli_default_task_type"] = "C"
    with pytest.raises(Exception):
        refuse_contract_overwrite(live, changed)


def test_context_keeps_safety_and_dispatch_questions():
    text = build_context("PM", "dispatch")
    assert "三问" in text
    assert "公共安全规则" in text
    assert "不得为了节省 token" in text


def test_windows_utf8_roundtrip(tmp_path):
    path = tmp_path / "说明.md"
    path.write_text(DOC + "中文内容\n", encoding="utf-8")
    decision = select_profile(task_type="C", requested="docs-short", files=[{"path": "docs/说明.md", "content": path.read_text(encoding="utf-8")}])
    assert decision.checks_passed is True
    assert decision.profile_version == PROFILE_VERSION
