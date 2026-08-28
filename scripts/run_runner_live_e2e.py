# -*- coding: utf-8 -*-
"""
scripts/run_runner_live_e2e.py
Phase 2F-PROD: Windows 真实任意任务 Universal Production Orchestration Runner E2E 验证脚本。
调用真实 Codex Builder、真实 Antigravity Reviewer、真实 Codex QA，
验证全生命周期自动编排：
  读取任务 Spec -> 隔离 Worktree -> Codex Builder -> Antigravity Reviewer (JSON Schema) ->
  Codex QA (源码不可变性) -> EvidenceGate 逐级 1:1 强校验 -> 停在 PENDING_USER_ACCEPTANCE / 已完成。
"""
import hashlib
import json
import os
import subprocess
import sys
import tempfile
import time
from typing import Any, Dict

_SCRIPTS_ROOT = os.path.abspath(os.path.dirname(__file__))
if _SCRIPTS_ROOT not in sys.path:
    sys.path.insert(0, _SCRIPTS_ROOT)

from _lib.core.adapter_registry import AdapterRegistry
from _lib.core.evidence_gate import EvidenceGate
from _lib.core.evidence_store import EvidenceStore
from _lib.core.production_runner import ProductionRunner, create_default_registry
from _lib.core.runner_checkpoint_store import RunnerCheckpointStore
from _lib.core.runner_schema import RunnerState, TaskExecutionSpec
from _lib.hosts.antigravity_adapter import AntigravityAdapter, create_antigravity_manifest
from _lib.hosts.codex_cli_adapter import CodexCliAdapter, create_codex_cli_manifest


def mask_sensitive(text: str) -> str:
    """脱敏 conversation_id、token 等敏感字符串"""
    if not text:
        return ""
    if len(text) <= 8:
        return "***"
    return f"{text[:4]}...{text[-4:]} (hash:{hashlib.sha256(text.encode()).hexdigest()[:8]})"


def run_live_e2e(authority_root: str, test_task_id: str = "T0063") -> Dict[str, Any]:
    print("=" * 70)
    print(f"[2F-PROD LIVE E2E] 启动 Windows 真实任意任务 E2E 编排验证: {test_task_id}")
    print("=" * 70)

    # 1. 创建真实独立宿主注册表
    registry = create_default_registry(context_id="live_prod_runner")
    ag_adapter = registry.get("antigravity")
    cdx_adapter = registry.get("codex_cli")

    if not ag_adapter or not cdx_adapter:
        raise RuntimeError("Failed to resolve live Antigravity or Codex CLI adapters.")

    ag_caps = ag_adapter.detect_capabilities()
    cdx_caps = cdx_adapter.detect_capabilities()
    print(f"[HOST DETECTION] Antigravity: is_real_host={ag_caps.is_real_host}, binary={ag_caps.extra.get('cli_binary')}")
    print(f"[HOST DETECTION] Codex CLI: is_real_host={cdx_caps.is_real_host}, binary={cdx_caps.extra.get('cli_binary')}")

    # 2. 准备受控测试 Git 仓库环境
    temp_repo_dir = tempfile.mkdtemp(prefix="live_runner_repo_")
    print(f"[SETUP] 创建独立测试仓库: {temp_repo_dir}")
    subprocess.run(["git", "init"], cwd=temp_repo_dir, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.name", "LiveRunnerBuilder"], cwd=temp_repo_dir, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "builder@multi-agent-flow.local"], cwd=temp_repo_dir, check=True, capture_output=True)

    readme = os.path.join(temp_repo_dir, "README.md")
    with open(readme, "w", encoding="utf-8") as f:
        f.write("# Live Runner Arbitrary Task Target Repo\n")
    subprocess.run(["git", "add", "README.md"], cwd=temp_repo_dir, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "chore: initial commit"], cwd=temp_repo_dir, check=True, capture_output=True)

    baseline_commit = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=temp_repo_dir, text=True).strip()
    print(f"[SETUP] 基线提交 Baseline SHA: {baseline_commit}")

    # 3. 创建 Evidence 存储与 Checkpoint 存储
    evidence_dir = os.path.join(temp_repo_dir, "user_data", "runner_evidence")
    os.makedirs(evidence_dir, exist_ok=True)
    evidence_store = EvidenceStore(root_dir=evidence_dir)
    evidence_gate = EvidenceGate(store=evidence_store, project_root=temp_repo_dir)
    checkpoint_store = RunnerCheckpointStore(data_root=temp_repo_dir)

    runner = ProductionRunner(
        registry=registry,
        evidence_store=evidence_store,
        evidence_gate=evidence_gate,
        checkpoint_store=checkpoint_store,
    )

    # 4. 构建真实任意任务 Spec (非固定 fixture，非 T0054)
    spec = TaskExecutionSpec(
        project_id="live_project_arbitrary",
        project_root=temp_repo_dir,
        authority_root=authority_root,
        task_id=test_task_id,
        task_name="实现通用字符串哈希与校验工具库",
        requirement_text="在 string_utils.py 中实现 calculate_sha256(text: str) -> str 与 verify_sha256(text: str, expected_hash: str) -> bool 两个函数，并编写 tests/test_string_utils.py 单元测试。",
        acceptance_criteria="验收标准: 单元测试 100% 通过，对空字符串与标准文本的 SHA256 哈希计算精确无误。",
        acceptance_criteria_hash=hashlib.sha256("验收标准: 单元测试 100% 通过".encode()).hexdigest(),
        task_version="1.0",
        status_at_read="进行中",
        owner="李开发",
        handler="李开发",
        baseline_commit=baseline_commit,
        workspace_mode="inherit",
        test_command="python -m pytest tests/test_string_utils.py -v",
        max_review_cycles=3,
        max_qa_cycles=3,
        max_total_attempts=5,
    )

    print(f"[RUNNER START] 启动 ProductionRunner.start() 执行真实流水线...")
    result = runner.start(spec)

    print("-" * 70)
    print(f"[RUNNER RESULT] success={result.success}, state={result.state}")
    print(f"[RUNNER RESULT] message={result.message}")
    print(f"[RUNNER RESULT] candidate_commit={result.candidate_commit}")
    print(f"[RUNNER RESULT] evidence_ids={result.evidence_ids}")
    print(f"[RUNNER RESULT] confirmation_request_id={result.confirmation_request_id}")
    print("-" * 70)

    # 5. 验证约束与终态
    assert result.success is True, f"Runner failed: {result.message}"
    assert result.state == RunnerState.PENDING_USER_ACCEPTANCE.value, f"Expected state PENDING_USER_ACCEPTANCE, got {result.state}"
    assert result.confirmation_request_id is not None, "Missing confirmation_request_id"
    assert len(result.evidence_ids) >= 3, f"Expected at least 3 evidence records, got {len(result.evidence_ids)}"

    # 6. 验证 Checkpoint 状态查询
    status_query = runner.status(project_root=temp_repo_dir, task_id=test_task_id)
    print(f"[STATUS QUERY] {json.dumps(status_query, indent=2, ensure_ascii=False)}")
    assert status_query["has_checkpoint"] is True
    assert status_query["state"] == RunnerState.PENDING_USER_ACCEPTANCE.value

    # 7. 验证工作区源码存在且测试通过
    test_run = subprocess.run(
        ["python", "-m", "pytest", "tests/test_string_utils.py", "-v"],
        cwd=temp_repo_dir,
        capture_output=True,
        text=True,
    )
    print(f"[POST TEST] 外部独立复核测试输出:\n{test_run.stdout.strip()}")
    assert test_run.returncode == 0, f"Post-verification test failed: {test_run.stderr}"

    print("=" * 70)
    print("[2F-PROD LIVE E2E SUCCESS] 真实任意任务 Windows E2E 编排验证 100% 成功！")
    print("=" * 70)

    return {
        "success": True,
        "task_id": test_task_id,
        "state": result.state,
        "candidate_commit": result.candidate_commit,
        "evidence_ids": list(result.evidence_ids),
        "confirmation_request_id": result.confirmation_request_id,
    }


if __name__ == "__main__":
    auth_root = r"C:\Users\user\Desktop\user\multi-agent-flow-phase2-real-agents"
    if len(sys.argv) > 1:
        auth_root = sys.argv[1]
    res = run_live_e2e(authority_root=auth_root)
    print(json.dumps(res, indent=2, ensure_ascii=False))
