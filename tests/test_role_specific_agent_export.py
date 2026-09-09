"""角色导出必须保留 YAML 声明的职责、状态和写权限边界。"""

import os
import sys
import tomllib

import pytest
import yaml


ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPTS = os.path.join(ROOT, "scripts")
if SCRIPTS not in sys.path:
    sys.path.insert(0, SCRIPTS)

from verify_and_export_agents import ROLES_MAP, serialize_subagent  # noqa: E402
from _lib.core.validate_transition import validate  # noqa: E402


ANTIGRAVITY_SPEC = {
    "format": "markdown_frontmatter",
    "frontmatter_subagent": True,
}
CODEX_SPEC = {
    "format": "codex_toml",
    "frontmatter_subagent": False,
}
MARKDOWN_PLATFORMS = (
    "antigravity",
    "antigravity_cli",
    "claude_code",
    "cursor",
    "opencode",
    "zcode",
)


def _role(filename):
    with open(os.path.join(ROOT, "agents", filename), "r", encoding="utf-8") as fp:
        return yaml.safe_load(fp), ROLES_MAP[filename]


def _markdown(filename):
    role, meta = _role(filename)
    content = serialize_subagent(
        role, meta, "antigravity", ANTIGRAVITY_SPEC,
        skill_target=".agents/skills/yy-flow",
    )
    _, frontmatter, body = content.split("---", 2)
    return yaml.safe_load(frontmatter), body


def _markdown_for(platform, filename):
    role, meta = _role(filename)
    content = serialize_subagent(
        role,
        meta,
        platform,
        ANTIGRAVITY_SPEC,
        skill_target=f".{platform}/skills/yy-flow",
    )
    _, frontmatter, body = content.split("---", 2)
    return yaml.safe_load(frontmatter), body


def _codex_body(filename):
    role, meta = _role(filename)
    content = serialize_subagent(
        role, meta, "codex", CODEX_SPEC,
        skill_target=".agents/skills/yy-flow",
    )
    return tomllib.loads(content)["developer_instructions"]


@pytest.mark.parametrize("renderer", [_markdown, _codex_body])
def test_reviewer_has_only_reviewer_transitions(renderer):
    rendered = renderer("04-reviewer.yaml")
    body = rendered[1] if isinstance(rendered, tuple) else rendered
    assert "审查中 -> 测试中" in body
    assert "审查中 -> 已退回" in body
    assert "待开始 -> 进行中" not in body
    assert "进行中 -> 审查中" not in body


@pytest.mark.parametrize("renderer", [_markdown, _codex_body])
def test_qa_has_only_qa_transitions(renderer):
    rendered = renderer("05-qa.yaml")
    body = rendered[1] if isinstance(rendered, tuple) else rendered
    assert "测试中 -> 已完成" in body
    assert "测试中 -> 已退回" in body
    assert "待开始 -> 进行中" not in body
    assert "进行中 -> 审查中" not in body


def test_antigravity_permissions_are_role_specific():
    dev_fm, _ = _markdown("03-dev.yaml")
    reviewer_fm, _ = _markdown("04-reviewer.yaml")
    qa_fm, _ = _markdown("05-qa.yaml")

    assert dev_fm["enable_write_tools"] is True
    assert "write_to_file" in dev_fm["tools"]
    for frontmatter in (reviewer_fm, qa_fm):
        assert frontmatter["enable_write_tools"] is False
        assert "write_to_file" not in frontmatter["tools"]
        assert "replace_file_content" not in frontmatter["tools"]
        assert "run_command" in frontmatter["tools"]


@pytest.mark.parametrize("platform", MARKDOWN_PLATFORMS)
def test_all_markdown_platforms_export_role_specific_permissions(platform):
    dev_fm, dev_body = _markdown_for(platform, "03-dev.yaml")
    frontend_fm, _ = _markdown_for(platform, "08-frontend.yaml")
    reviewer_fm, reviewer_body = _markdown_for(platform, "04-reviewer.yaml")
    qa_fm, qa_body = _markdown_for(platform, "05-qa.yaml")

    assert dev_fm["enable_write_tools"] is True
    assert frontend_fm["enable_write_tools"] is True
    assert "进行中 -> 审查中" in dev_body
    for frontmatter, body, expected in (
        (reviewer_fm, reviewer_body, "审查中 -> 测试中"),
        (qa_fm, qa_body, "测试中 -> 已完成"),
    ):
        assert frontmatter["enable_write_tools"] is False
        assert not {tool.lower() for tool in frontmatter["tools"]} & {
            "edit", "write", "replace_file_content", "write_to_file",
        }
        assert expected in body
        assert "待开始 -> 进行中" not in body


@pytest.mark.parametrize("filename", ["04-reviewer.yaml", "05-qa.yaml"])
@pytest.mark.parametrize("renderer", [_markdown, _codex_body])
def test_read_only_roles_return_results_without_mutating_board(filename, renderer):
    rendered = renderer(filename)
    body = rendered[1] if isinstance(rendered, tuple) else rendered
    assert "本角色不得直接调用 transition_task.py" in body
    assert "由 Production Runner 或主协调者核验后执行状态落库" in body
    assert "--from-status" not in body


def test_pm_requires_serial_independent_agents():
    _, body = _markdown("01-pm.yaml")
    assert "DEV 完成并产生候选 Commit 后才允许启动 Reviewer" in body
    assert "Reviewer PASS 后才允许启动 QA" in body
    assert "Reviewer 与 QA 严禁并行" in body
    assert "禁止退化为单对话多角色扮演" in body
    assert "并行调度 DEV/QA/REVIEWER" not in body
    assert "正向验证和反向/异常场景" in body


def test_reviewer_and_qa_exports_require_semantic_quality_evidence():
    _, reviewer_body = _markdown("04-reviewer.yaml")
    _, qa_body = _markdown("05-qa.yaml")

    assert "已有测试为绿" in reviewer_body
    assert "仓库外围调用链" in reviewer_body
    assert "完整验收覆盖矩阵" in qa_body
    assert "反向场景" in qa_body
    assert "零未覆盖风险" in qa_body


def test_dev_reviewer_qa_contracts_are_not_identical():
    bodies = {
        _markdown(filename)[1]
        for filename in ("03-dev.yaml", "04-reviewer.yaml", "05-qa.yaml")
    }
    assert len(bodies) == 3


def test_export_fails_closed_without_declared_transitions():
    role, meta = _role("04-reviewer.yaml")
    role.pop("allowed_transitions")
    with pytest.raises(ValueError, match="缺少 allowed_transitions"):
        serialize_subagent(role, meta, "antigravity", ANTIGRAVITY_SPEC)


def test_export_fails_closed_without_state_write_permission():
    role, meta = _role("04-reviewer.yaml")
    role["boundaries"].pop("can_transition_task")
    with pytest.raises(ValueError, match="缺少 boundaries.can_transition_task"):
        serialize_subagent(role, meta, "antigravity", ANTIGRAVITY_SPEC)


def test_all_roles_explicitly_declare_state_write_ownership():
    for filename in ROLES_MAP:
        role, _ = _role(filename)
        assert isinstance(role["boundaries"].get("can_transition_task"), bool), filename


@pytest.mark.parametrize("role", ["PM", "REVIEWER", "QA"])
def test_a_class_task_cannot_be_started_by_non_development_roles(role):
    assert not validate(
        role=role,
        from_status="待开始",
        to_status="进行中",
        assignee=role,
        end_time="",
        active_dev_count=0,
        task_type="A",
    )


def test_independent_reviewer_and_qa_short_chain_start_remains_supported():
    assert validate("REVIEWER", "待开始", "进行中", "REVIEWER", "", 0, task_type="B")
    assert validate("QA", "待开始", "进行中", "QA", "", 0, task_type="C")


def test_skill_permission_matrix_matches_export_contract():
    with open(os.path.join(ROOT, "SKILL.md"), "r", encoding="utf-8") as fp:
        skill = fp.read()

    reviewer_row = next(line for line in skill.splitlines() if "`@flow-reviewer`" in line)
    qa_row = next(line for line in skill.splitlines() if "`@flow-qa`" in line)
    pm_row = next(line for line in skill.splitlines() if "`@flow-pm`" in line)

    assert "只读 + run_command（不得直接写看板）" in reviewer_row
    assert "只读 + run_command（不得直接写看板）" in qa_row
    assert "完整读写" not in reviewer_row + qa_row
    assert "待开始->进行中" not in pm_row
    assert "已完成->已验收 / 已完成->已退回" in pm_row


def test_reference_contracts_distinguish_runner_and_independent_tasks():
    anti_error = open(
        os.path.join(ROOT, "references", "03-Anti-Error-Mechanism.md"),
        encoding="utf-8",
    ).read()
    handover = open(
        os.path.join(ROOT, "references", "06-Inter-Agent-Handover-Protocol.md"),
        encoding="utf-8",
    ).read()
    assert "REVIEWER/QA 不得领取 A 类待开始任务" in anti_error
    assert "B/C/D/F/G 独立专项短链不属于该限制" in anti_error
    assert "REVIEWER 只返回结论；Runner/主协调者核验后执行" in handover
    assert "QA 只返回结论；Runner/主协调者核验后执行" in handover
