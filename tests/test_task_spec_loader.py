# -*- coding: utf-8 -*-
"""
tests/test_task_spec_loader.py
TaskSpecLoader 单元测试与边界校验。
"""
import os
import json
import pytest
import tempfile
import yaml

from scripts._lib.core.runner_schema import TaskExecutionSpec
from scripts._lib.core.task_spec_loader import (
    load_task_execution_spec,
    verify_optimistic_concurrency,
    TaskSpecError,
    TaskSpecNotFoundError,
    TaskSpecInvalidStatusError,
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
            "process": "需求: 支持 OAuth2 登录与 Token 刷新。验收标准: 单元测试覆盖率达到 100%。",
        },
        {
            "id": "T0002",
            "name": "已完成结项的任务",
            "status": "已验收",
            "assignee": "李开发",
            "owner": "李开发",
            "handler": "严经理",
            "process": "已终态验收",
        },
        {
            "id": "T0003",
            "name": "已取消的任务",
            "status": "已取消",
            "assignee": "李开发",
            "owner": "李开发",
            "handler": "严经理",
            "process": "废弃",
        }
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
