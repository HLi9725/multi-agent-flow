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


ANTIGRAVITY_SPEC = {
    "format": "markdown_frontmatter",
    "frontmatter_subagent": True,
}
CODEX_SPEC = {
    "format": "codex_toml",
    "frontmatter_subagent": False,
}


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
