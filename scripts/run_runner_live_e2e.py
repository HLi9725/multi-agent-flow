# -*- coding: utf-8 -*-
"""
scripts/run_runner_live_e2e.py
Phase 2F-PROD: Windows 真实任意任务 Universal Production Orchestration Runner E2E 验证脚本。
调用真实 Codex Builder、真实 Antigravity Reviewer、真实 Codex QA，
验证全生命周期自动编排：
  读取权威任务 (从Board) -> 隔离 Worktree -> Codex Builder -> 真实候选校验 ->
  Antigravity Reviewer (严格 JSON Schema) -> Codex QA (源码不可变性) ->
  EvidenceGate 逐级 1:1 强校验 -> 经合法状态机停在 PENDING_USER_ACCEPTANCE / 已完成。
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

import paths
from _lib.boards.offline_board_adapter import OfflineBoardAdapter
from _lib.core.adapter_registry import AdapterRegistry
from _lib.core.evidence_gate import EvidenceGate
from _lib.core.evidence_store import EvidenceStore
from _lib.core.production_runner import ProductionRunner, create_default_registry
from _lib.core.runner_checkpoint_store import RunnerCheckpointStore
from _lib.core.runner_schema import RunnerState, TaskExecutionSpec
from _lib.core.task_spec_loader import load_task_execution_spec
from _lib.hosts.antigravity_adapter import AntigravityAdapter, create_antigravity_manifest
from _lib.hosts.codex_cli_adapter import CodexCliAdapter, create_codex_cli_manifest


def mask_sensitive(text: str) -> str:
    """脱敏 conversation_id、token 等敏感字符串"""
    if not text:
        return ""
    if len(text) <= 8:
        return "***"
    return f"{text[:4]}...{text[-4:]} (hash:{hashlib.sha256(text.encode()).hexdigest()[:8]})"


def ensure_authoritative_test_task(authority_root: str, task_id: str) -> None:
    """在权威看板中确保存在待测试的任意合法任务 (非固定 fixture，非 T0054)"""
    board_file = os.path.join(authority_root, "user_data", "board.json")
    os.makedirs(os.path.dirname(board_file), exist_ok=True)
    adapter = OfflineBoardAdapter(board_file=board_file)

    rec = adapter.get_record(task_id)
    if not rec:
        task_data = {
            "task_id": task_id,
            "task_name": "实现通用字符串哈希与校验工具库",
            "type": "A",
            "status": "进行中",
            "owner": "李开发",
            "handler": "李开发",
            "process": (
                "需求: 在 string_utils.py 中实现 calculate_sha256(text: str) -> str 与 verify_sha256(text: str, expected_hash: str) -> bool 两个函数，并编写 tests/test_string_utils.py 单元测试。"
                "验收标准: 单元测试 100% 通过，对空字符串与标准文本的 SHA256 哈希计算精确无误。"
            ),
        }
        adapter.create_record(task_data)
        print(f"[BOARD] 权威看板已建卡/初始化任务: {task_id} (进行中)")
    else:
        fields = rec.get("fields", {}) if "fields" in rec else rec
        fields["status"] = "进行中"
        fields["handler"] = "李开发"
        fields["owner"] = "李开发"
        adapter.update_record(task_id, fields)
        print(f"[BOARD] 权威看板任务 {task_id} 已就绪 (进行中)")


def run_live_e2e(authority_root: str, test_task_id: str = "T0063") -> Dict[str, Any]:
    print("=" * 70)
    print(f"[2F-PROD LIVE E2E] 启动 Windows 真实任意任务 E2E 编排验证: {test_task_id}")
    print("=" * 70)

    # 1. 准备权威任务卡片
    ensure_authoritative_test_task(authority_root, test_task_id)

    # 2. 创建真实独立宿主注册表
    registry = create_default_registry(context_id="live_prod_runner")
    ag_adapter = registry.get("antigravity")
    cdx_adapter = registry.get("codex_cli")

    if not ag_adapter or not cdx_adapter:
        raise RuntimeError("Failed to resolve live Antigravity or Codex CLI adapters.")

    ag_caps = ag_adapter.detect_capabilities()
    cdx_caps = cdx_adapter.detect_capabilities()
    print(f"[HOST DETECTION] Antigravity: is_real_host={ag_caps.is_real_host}, binary={ag_caps.extra.get('cli_binary')}")
    print(f"[HOST DETECTION] Codex CLI: is_real_host={cdx_caps.is_real_host}, binary={cdx_caps.extra.get('cli_binary')}")

    # 3. 准备受控测试 Git 仓库环境
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

    # 4. 创建 Evidence 存储与 Checkpoint 存储
    evidence_dir = os.path.join(temp_repo_dir, "user_data", "runner_evidence")
    os.makedirs(evidence_dir, exist_ok=True)
    evidence_store = EvidenceStore(root_dir=evidence_dir)
    evidence_gate = EvidenceGate(store=evidence_store, project_root=temp_repo_dir)
    checkpoint_store = RunnerCheckpointStore(data_root=temp_repo_dir, project_root=temp_repo_dir, project_id="live_project_arbitrary")

    runner = ProductionRunner(
        registry=registry,
        evidence_store=evidence_store,
        evidence_gate=evidence_gate,
        checkpoint_store=checkpoint_store,
    )

    # 5. 从权威看板动态加载 TaskExecutionSpec (DEF-T0061-4)
    spec = load_task_execution_spec(
        project_root=temp_repo_dir,
        task_id=test_task_id,
        authority_root=authority_root,
        overrides={
            "project_id": "live_project_arbitrary",
            "workspace_mode": "branch",
            "test_command": f'"{sys.executable}" -m pytest tests/ -q',
            "builder_timeout_seconds": 300,
            "reviewer_timeout_seconds": 300,
            "qa_timeout_seconds": 300,
        },
    )
    print(f"[SPEC LOADED] 任务: {spec.task_id} ({spec.task_name})")
    print(f"[SPEC LOADED] 验收标准: {spec.acceptance_criteria}")
    print(f"[SPEC LOADED] 验收标准哈希: {spec.acceptance_criteria_hash}")

    # 6. 启动 ProductionRunner 执行真实多阶段编排
    start_time = time.time()
    result = runner.start(spec)
    elapsed = time.time() - start_time

    print("=" * 70)
    print(f"[RESULT] 编排完成，耗时: {elapsed:.2f}s")
    print(f"[RESULT] Success: {result.success}")
    print(f"[RESULT] State: {result.state}")
    print(f"[RESULT] Candidate Commit: {result.candidate_commit}")
    print(f"[RESULT] Evidence Count: {len(result.evidence_ids)}")
    print(f"[RESULT] Confirmation Request ID: {result.confirmation_request_id}")
    print(f"[RESULT] Message: {result.message}")
    print("=" * 70)

    # 7. 逐级验证 Evidence 完整性
    for evi_id in result.evidence_ids:
        rec = evidence_store.get(evi_id)
        if rec:
            print(f"[EVIDENCE AUDIT] ID={rec.evidence_id}, Type={rec.evidence_type.value}, Actor={rec.metadata.actor_role}, Host={rec.metadata.host_id}, Invocation={mask_sensitive(rec.metadata.host_invocation_id)}, Session={mask_sensitive(rec.metadata.host_session_id)}")

    # 8. 核验权威看板最终状态
    adapter = OfflineBoardAdapter(board_file=os.path.join(authority_root, "user_data", "board.json"))
    final_rec = adapter.get_record(test_task_id)
    final_status = final_rec.get("fields", {}).get("status") if final_rec else "UNKNOWN"
    final_handler = final_rec.get("fields", {}).get("handler") if final_rec else "UNKNOWN"
    print(f"[FINAL BOARD AUDIT] 任务 {test_task_id} 最终状态: {final_status} (处理人: {final_handler})")

    report = {
        "task_id": test_task_id,
        "task_name": spec.task_name,
        "success": result.success,
        "state": result.state,
        "elapsed_seconds": round(elapsed, 2),
        "baseline_commit": baseline_commit,
        "candidate_commit": result.candidate_commit,
        "confirmation_request_id": result.confirmation_request_id,
        "evidence_ids": list(result.evidence_ids),
        "final_board_status": final_status,
        "final_board_handler": final_handler,
    }
    return report


if __name__ == "__main__":
    authority_root_arg = sys.argv[1] if len(sys.argv) > 1 else r"C:\Users\user\Desktop\user\multi-agent-flow-phase2-real-agents"
    task_id_arg = sys.argv[2] if len(sys.argv) > 2 else "T0063"
    report_data = run_live_e2e(authority_root=authority_root_arg, test_task_id=task_id_arg)
    print(json.dumps(report_data, indent=2, ensure_ascii=False))
