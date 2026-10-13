# -*- coding: utf-8 -*-
"""旧 CLI 与 Production Runner 在未安装 mcp、未配置 MCP 时仍可导入和收集。"""
import os
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _env_without_mcp():
    env = os.environ.copy()
    env.pop("YY_FLOW_MCP_REGISTRY", None)
    env.pop("YY_FLOW_MCP_ACCEPTANCE_CONFIRMED", None)
    env["PYTHONPATH"] = os.pathsep.join(
        [ROOT, os.path.join(ROOT, "scripts"), env.get("PYTHONPATH", "")]
    )
    return env


def test_transition_and_runner_import_without_mcp_package():
    code = (
        "import importlib.util, sys\n"
        "import transition_task\n"
        "import scripts._lib.core.production_runner as runner\n"
        "assert importlib.util.find_spec('mcp') is None\n"
        "assert 'mcp' not in sys.modules\n"
        "assert runner.ProductionRunner is not None\n"
        "assert callable(transition_task.transition_task_pipeline)\n"
    )
    completed = subprocess.run(
        [sys.executable, "-c", code],
        cwd=ROOT,
        env=_env_without_mcp(),
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert completed.returncode == 0, completed.stderr


def test_workflow_profile_collects_without_mcp_config():
    completed = subprocess.run(
        [sys.executable, "-m", "pytest", "--collect-only", "-q", "tests/test_workflow_profile.py"],
        cwd=ROOT,
        env=_env_without_mcp(),
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert completed.returncode == 0, completed.stderr + completed.stdout
