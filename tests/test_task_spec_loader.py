# -*- coding: utf-8 -*-
"""
tests/test_task_spec_loader.py
TaskSpecLoader 单元测试与乐观并发对抗校验 (DEF-T0061-5)。
"""
import hashlib
import json
import os
import pytest
import tempfile
import yaml

import sys
REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPTS = os.path.join(REPO_ROOT, "scripts")
if SCRIPTS not in sys.path:
    sys.path.insert(0, SCRIPTS)

from scripts._lib.boards.offline_board_adapter import OfflineBoardAdapter
from scripts._lib.core.runner_schema import TaskExecutionSpec
from scripts._lib.core.task_spec_loader import (
    load_task_execution_spec,
    verify_optimistic_concurrency,
    TaskSpecError,
    TaskSpecNotFoundError,
    TaskSpecInvalidStatusError,
    TaskSpecIncompleteError,
)


@pytest.fixture
def mock_project_environment(tmp_path):
    proj_dir = tmp_path / "test_repo"
    proj_dir.mkdir()
    config_dir = proj_dir / "config"
    config_dir.mkdir()
    user_data_dir = proj_dir / "user_data"
    user_data_dir.mkdir()

    workflow_cfg = {
        "board": {
            "provider": "local",
            "board_file": str(user_data_dir / "board.json"),
            "fields": {
                "task_id": "id",
                "task_name": "name",
                "status": "status",
                "assignee": "assignee",
                "owner": "owner",
                "handler": "handler",
                "remarks": "remarks",
                "process": "process",
            }
        }
    }
    with open(config_dir / "workflow.config.yaml", "w", encoding="utf-8") as f:
        yaml.dump(workflow_cfg, f)

    tasks_data = [
        {
            "id": "T0001",
            "name": "实现用户身份认证模块",
            "status": "进行中",
            "assignee": "李开发",
            "owner": "李开发",
            "handler": "李开发",
            "process": (
                "需求: 支持 OAuth2 登录。\n验收标准:\n"
                "- 登录成功后返回访问令牌。\n"
                "- 未认证请求返回 401。\n"
                "- 无效令牌不得访问受保护资源。\n\n"
                "【开发计划】实现认证服务与接口。"
            ),
            "updated_at": "1787890000",
        },
        {
            "id": "T0002",
            "name": "已完成结项的任务",
            "status": "已验收",
            "assignee": "李开发",
            "owner": "李开发",
            "handler": "严经理",
            "process": "已终态验收",
            "updated_at": "1787890000",
        },
        {
            "id": "T0003",
            "name": "已取消的任务",
            "status": "已取消",
            "assignee": "李开发",
            "owner": "李开发",
            "handler": "严经理",
            "process": "废弃",
            "updated_at": "1787890000",
        },
        {
            "id": "T0004",
            "name": "缺少验收标准的开发任务",
            "status": "待开始",
            "assignee": "李开发",
            "owner": "李开发",
            "handler": "李开发",
            "type": "A",
            "process": "需求: 实现一个没有验收标准的功能。",
            "updated_at": "1787890000",
        },
        {
            "id": "T0005",
            "name": "实现批量导出",
            "status": "进行中",
            "assignee": "李开发",
            "owner": "李开发",
            "handler": "李开发",
            "type": "A",
            "remarks": (
                "需求: 支持批量导出审计结果。\n验收标准:\n"
                "- 导出文件包含任务编号。\n"
                "- 导出失败返回明确错误。"
            ),
            "process": "[T0005-N01] 待开始 -> 进行中 | 已领取任务",
            "updated_at": "1787890000",
        },
        {
            "id": "T0006",
            "name": "占位验收任务",
            "status": "待开始",
            "assignee": "李开发",
            "owner": "李开发",
            "handler": "李开发",
            "type": "A",
            "remarks": "需求: 实现报表。\n验收标准: 全量测试通过。",
            "updated_at": "1787890000",
        },
    ]
    with open(user_data_dir / "board.json", "w", encoding="utf-8") as f:
        json.dump(tasks_data, f)

    return proj_dir


def test_load_valid_task_execution_spec(mock_project_environment):
    spec = load_task_execution_spec(
        project_root=str(mock_project_environment),
        task_id="T0001",
        authority_root=str(mock_project_environment),
    )
    assert spec.task_id == "T0001"
    assert spec.task_name == "实现用户身份认证模块"
    assert spec.status_at_read == "进行中"
    assert "OAuth2" in spec.requirement_text
    assert spec.acceptance_criteria_hash != ""
    assert [item.criterion_id for item in spec.acceptance_criteria_items] == ["AC-01", "AC-02", "AC-03"]
    assert "未认证请求返回 401" in spec.acceptance_criteria
    assert "开发计划" not in spec.acceptance_criteria


def test_project_local_yy_flow_config_is_used_for_load_and_revalidation(mock_project_environment):
    yy_data = mock_project_environment / ".yy-flow" / "user_data"
    yy_data.mkdir(parents=True)
    source_config = mock_project_environment / "config" / "workflow.config.yaml"
    source_config.replace(yy_data / "workflow.config.yaml")
    source_board = mock_project_environment / "user_data" / "board.json"
    source_board.replace(yy_data / "board.json")
    installed_config = yy_data / "workflow.config.yaml"
    config_data = yaml.safe_load(installed_config.read_text(encoding="utf-8"))
    config_data["board"]["board_file"] = "user_data/board.json"
    installed_config.write_text(yaml.safe_dump(config_data), encoding="utf-8")

    spec = load_task_execution_spec(
        project_root=str(mock_project_environment),
        task_id="T0001",
        authority_root=str(mock_project_environment),
    )
    assert spec.task_id == "T0001"
    assert verify_optimistic_concurrency(spec) is True


def test_a_class_task_without_explicit_acceptance_criteria_fails_closed(mock_project_environment):
    with pytest.raises(TaskSpecIncompleteError, match="no explicit, executable acceptance criteria"):
        load_task_execution_spec(
            project_root=str(mock_project_environment),
            task_id="T0004",
            authority_root=str(mock_project_environment),
        )


def test_a_class_task_with_generic_acceptance_placeholder_fails_closed(mock_project_environment):
    with pytest.raises(TaskSpecIncompleteError, match="non-executable acceptance criteria"):
        load_task_execution_spec(
            project_root=str(mock_project_environment),
            task_id="T0006",
            authority_root=str(mock_project_environment),
        )


def test_stable_requirement_fields_take_precedence_over_transition_log(mock_project_environment):
    spec = load_task_execution_spec(
        project_root=str(mock_project_environment),
        task_id="T0005",
        authority_root=str(mock_project_environment),
    )

    assert "批量导出审计结果" in spec.requirement_text
    assert "已领取任务" not in spec.requirement_text
    assert [item.criterion_id for item in spec.acceptance_criteria_items] == ["AC-01", "AC-02"]

    board_file = str(mock_project_environment / "user_data" / "board.json")
    adapter = OfflineBoardAdapter(board_file=board_file)
    with open(board_file, "r", encoding="utf-8") as f:
        data = json.load(f)

    # Appending workflow history must not invalidate a stable requirement snapshot.
    data[4]["process"] += "\n[T0005-N02] 进行中 -> 审查中 | 候选已提交"
    data[4]["status"] = "审查中"
    data[4]["updated_at"] = "1787890001"
    with open(board_file, "w", encoding="utf-8") as f:
        json.dump(data, f)
    assert verify_optimistic_concurrency(spec, adapter) is False
    assert verify_optimistic_concurrency(
        spec,
        adapter,
        enforce_status=False,
        enforce_version=False,
    ) is True

    # Changing only the requirement body while retaining the same criteria is a conflict.
    data[4]["remarks"] = data[4]["remarks"].replace("支持批量导出审计结果", "支持跨租户批量导出审计结果")
    with open(board_file, "w", encoding="utf-8") as f:
        json.dump(data, f)
    assert verify_optimistic_concurrency(
        spec,
        adapter,
        enforce_status=False,
        enforce_version=False,
    ) is False


def test_load_nonexistent_task_fails_closed(mock_project_environment):
    with pytest.raises(TaskSpecNotFoundError, match="not found in authoritative board"):
        load_task_execution_spec(
            project_root=str(mock_project_environment),
            task_id="T9999",
            authority_root=str(mock_project_environment),
        )


def test_load_terminal_status_task_fails_closed(mock_project_environment):
    with pytest.raises(TaskSpecInvalidStatusError, match="terminal status"):
        load_task_execution_spec(
            project_root=str(mock_project_environment),
            task_id="T0002",
            authority_root=str(mock_project_environment),
        )


def test_load_invalid_task_id_format(mock_project_environment):
    with pytest.raises(TaskSpecError, match="Invalid task_id format"):
        load_task_execution_spec(
            project_root=str(mock_project_environment),
            task_id="INVALID_TASK",
            authority_root=str(mock_project_environment),
        )


def test_optimistic_concurrency_verification(mock_project_environment):
    spec = load_task_execution_spec(
        project_root=str(mock_project_environment),
        task_id="T0001",
        authority_root=str(mock_project_environment),
    )
    board_file = str(mock_project_environment / "user_data" / "board.json")
    adapter = OfflineBoardAdapter(board_file=board_file)

    # 1. 正常未篡改时应通过
    assert verify_optimistic_concurrency(spec, adapter) is True

    # Runner 自己追加的结构化流转节点不属于需求正文，不得产生误冲突。
    with open(board_file, "r", encoding="utf-8") as f:
        data = json.load(f)
    data[0]["process"] += "\n[T0001-N02] 进行中 -> 审查中 | 候选已提交"
    with open(board_file, "w", encoding="utf-8") as f:
        json.dump(data, f)
    assert verify_optimistic_concurrency(spec, adapter) is True

    # 2. 对抗复现：任务内容或验收标准在外部被修改 -> 应识别并发冲突返回 False
    data[0]["process"] = "篡改后的需求: 增加 SAML 支持。验收标准: 不同的验收标准。"
    with open(board_file, "w", encoding="utf-8") as f:
        json.dump(data, f)

    assert verify_optimistic_concurrency(spec, adapter) is False

    # 3. 对抗复现：状态在外部被转变为终态已验收 -> 应返回 False
    data[0]["process"] = (
        "需求: 支持 OAuth2 登录。\n验收标准:\n"
        "- 登录成功后返回访问令牌。\n"
        "- 未认证请求返回 401。\n"
        "- 无效令牌不得访问受保护资源。\n\n"
        "【开发计划】实现认证服务与接口。"
    )
    data[0]["status"] = "已验收"
    with open(board_file, "w", encoding="utf-8") as f:
        json.dump(data, f)

    assert verify_optimistic_concurrency(spec, adapter) is False
