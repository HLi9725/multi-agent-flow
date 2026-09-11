# -*- coding: utf-8 -*-
"""
tests/test_runner_security.py
Runner 缺陷回环、JSON Schema 对抗校验、候选提交校验、权限暂停/恢复与源码不可变性测试。
"""
import hashlib
import json
import os
import shutil
import subprocess
import sys
import time
import pytest
import yaml

from scripts._lib.core.adapter_registry import AdapterRegistry
from scripts._lib.core.agent_schema import (
    AgentHandle,
    AgentPermissionRequiredError,
    AgentResult,
    AgentStatus,
    CapabilitySupport,
    HostCapabilities,
)
from scripts._lib.core.evidence_store import EvidenceStore
from scripts._lib.core.production_runner import (
    QA_PROTOCOL_DEFECT_SUFFIXES,
    REVIEWER_PROTOCOL_DEFECT_SUFFIXES,
    ProductionRunner,
    _build_qa_subprocess_env,
    _extract_real_invocation_id,
    _has_protocol_defect,
    _validate_qa_test_command,
)
from scripts._lib.core.runner_checkpoint_store import RunnerCheckpointStore
from scripts._lib.core.runner_schema import RunnerCheckpoint, RunnerResult, RunnerState, TaskExecutionSpec
from scripts._lib.core.task_spec_loader import load_task_execution_spec
from scripts._lib.hosts.antigravity_adapter import AntigravityAdapter, create_antigravity_manifest
from scripts._lib.hosts.codex_cli_adapter import CodexCliAdapter, create_codex_cli_manifest


def test_protocol_defects_are_distinct_from_business_defects():
    reviewer_schema = [{"defect_id": "DEF-T0012-SCHEMA-VIOLATION"}]
    reviewer_business = [{"defect_id": "DEF-T0012-ATOMICITY"}]
    qa_schema = [{"defect_id": "DEF-T0012-QA-SCHEMA-VIOLATION"}]
    qa_business = [{"defect_id": "DEF-T0012-QA-CONCURRENCY"}]

    assert _has_protocol_defect(reviewer_schema, REVIEWER_PROTOCOL_DEFECT_SUFFIXES)
    assert not _has_protocol_defect(reviewer_business, REVIEWER_PROTOCOL_DEFECT_SUFFIXES)
    assert _has_protocol_defect(qa_schema, QA_PROTOCOL_DEFECT_SUFFIXES)
    assert not _has_protocol_defect(qa_business, QA_PROTOCOL_DEFECT_SUFFIXES)


def test_qa_subprocess_environment_removes_host_credentials(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "secret-value")
    monkeypatch.setenv("HTTPS_PROXY", "http://credential@example.invalid")
    monkeypatch.setenv("SAFE_TEST_SETTING", "kept")

    clean_env = _build_qa_subprocess_env()

    assert "OPENAI_API_KEY" not in clean_env
    assert "HTTPS_PROXY" not in clean_env
    assert clean_env["SAFE_TEST_SETTING"] == "kept"
    assert clean_env["CI"] == "1"
    assert clean_env["NPM_CONFIG_AUDIT"] == "false"


@pytest.mark.skipif(shutil.which("npm") is None, reason="npm is not installed on this host")
def test_installed_npm_resolves_to_an_absolute_executable(tmp_path):
    worktree_dir = str(tmp_path / "worktree")
    os.makedirs(worktree_dir, exist_ok=True)

    ok, error, args = _validate_qa_test_command("npm test", worktree_dir)

    assert ok is True, error
    assert os.path.isabs(args[0])
    assert os.path.basename(args[0]).lower() in {"npm", "npm.cmd", "npm.exe"}
    assert args[1:] == ["test"]
    probe = subprocess.run(
        [args[0], "--version"],
        cwd=worktree_dir,
        env=_build_qa_subprocess_env(),
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert probe.returncode == 0, probe.stderr


@pytest.fixture
def mock_git_repo(tmp_path):
    repo_dir = tmp_path / "repo"
    repo_dir.mkdir()
    subprocess.run(["git", "init"], cwd=repo_dir, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.name", "TestDev"], cwd=repo_dir, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "test@test.com"], cwd=repo_dir, check=True, capture_output=True)

    readme = repo_dir / "README.md"
    readme.write_text("# Test Repo\n", encoding="utf-8")
    subprocess.run(["git", "add", "README.md"], cwd=repo_dir, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "initial commit"], cwd=repo_dir, check=True, capture_output=True)

    head_sha = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=repo_dir, text=True).strip()

    config_dir = repo_dir / "config"
    config_dir.mkdir()
    user_data_dir = repo_dir / "user_data"
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
            },
        }
    }
    with open(config_dir / "workflow.config.yaml", "w", encoding="utf-8") as f:
        yaml.dump(workflow_cfg, f)

    tasks_data = [
        {
            "id": "T0033",
            "name": "权限测试任务",
            "status": "进行中",
            "assignee": "李开发",
            "owner": "李开发",
            "handler": "李开发",
            "process": "需求: 需要网络权限。验收标准: 权限通过。",
            "updated_at": "1.0",
        },
        {
            "id": "T0055",
            "name": "无提交测试",
            "status": "进行中",
            "assignee": "李开发",
            "owner": "李开发",
            "handler": "李开发",
            "process": "需求: 无提交测试。验收标准: Builder 必须生成不同于基线的候选提交。",
            "updated_at": "1.0",
        },
        {
            "id": "T0066",
            "name": "只读测试验证",
            "status": "进行中",
            "assignee": "李开发",
            "owner": "李开发",
            "handler": "李开发",
            "process": "需求: 只读测试。验收标准: QA 修改候选提交时 Runner 必须阻断准出。",
            "updated_at": "1.0",
        },
        {
            "id": "T0077",
            "name": "安全加固模块",
            "status": "进行中",
            "assignee": "李开发",
            "owner": "李开发",
            "handler": "李开发",
            "process": "需求: 实现严格安全加固。验收标准: 零漏洞。",
            "updated_at": "1.0",
        },
    ]
    with open(user_data_dir / "board.json", "w", encoding="utf-8") as f:
        json.dump(tasks_data, f)

    return repo_dir, head_sha


def test_reviewer_schema_adversarial_rejections(mock_git_repo, tmp_path):
    """
    DEF-T0061-1 对抗测试：
    1. 普通文本 PASS: looks good 必须被拒绝；
    2. 残缺 JSON（缺失必填身份字段）必须被拒绝；
    3. 身份/提交哈希不匹配必须被拒绝；
    4. PASS 携带缺陷列表必须被拒绝。
    """
    repo_dir, baseline_sha = mock_git_repo
    data_root = tmp_path / "data_root"
    data_root.mkdir()

    runner = ProductionRunner(
        checkpoint_store=RunnerCheckpointStore(data_root=str(data_root), project_root=str(repo_dir)),
    )

    # 1. 普通文本 "PASS: looks good"
    res1 = runner._parse_reviewer_structured_json(
        raw_output="PASS: looks good",
        task_id="T0077",
        baseline_commit=baseline_sha,
        candidate_commit="b" * 40,
        session_id="sess_r_1",
        invocation_id="inv_r_1",
        review_request_id="rev_req_1",
    )
    assert res1.decision == "REJECT"
    assert "DEF-T0077-SCHEMA-VIOLATION" in res1.defects[0]["defect_id"]

    # 2. 残缺 JSON（仅含 decision/defects/summary，缺失必填身份字段）
    incomplete_json = '{"decision": "PASS", "defects": [], "summary": "looks good"}'
    res2 = runner._parse_reviewer_structured_json(
        raw_output=incomplete_json,
        task_id="T0077",
        baseline_commit=baseline_sha,
        candidate_commit="b" * 40,
        session_id="sess_r_1",
        invocation_id="inv_r_1",
        review_request_id="rev_req_1",
    )
    assert res2.decision == "REJECT"
    assert "DEF-T0077-SCHEMA-VIOLATION" in res2.defects[0]["defect_id"]

    # 3. 身份/哈希不匹配（task_id 或 commit 伪造）
    mismatched_json = json.dumps({
        "task_id": "T9999_FAKE",
        "baseline_commit": baseline_sha,
        "candidate_commit": "b" * 40,
        "session_id": "sess_r_1",
        "review_request_id": "rev_req_1",
        "decision": "PASS",
        "defects": [],
        "summary": "pass",
    })
    res3 = runner._parse_reviewer_structured_json(
        raw_output=mismatched_json,
        task_id="T0077",
        baseline_commit=baseline_sha,
        candidate_commit="b" * 40,
        session_id="sess_r_1",
        invocation_id="inv_r_1",
        review_request_id="rev_req_1",
    )
    assert res3.decision == "REJECT"
    assert "IDENTITY-MISMATCH" in res3.defects[0]["defect_id"]

    # 4. PASS 携带缺陷列表
    contradictory_json = json.dumps({
        "task_id": "T0077",
        "baseline_commit": baseline_sha,
        "candidate_commit": "b" * 40,
        "session_id": "sess_r_1",
        "review_request_id": "rev_req_1",
        "decision": "PASS",
        "defects": [{"defect_id": "DEF-1", "severity": "P1", "description": "some error"}],
        "summary": "pass with defect",
    })
    res4 = runner._parse_reviewer_structured_json(
        raw_output=contradictory_json,
        task_id="T0077",
        baseline_commit=baseline_sha,
        candidate_commit="b" * 40,
        session_id="sess_r_1",
        invocation_id="inv_r_1",
        review_request_id="rev_req_1",
    )
    assert res4.decision == "REJECT"
    assert "INVALID-PASS" in res4.defects[0]["defect_id"]


def test_qa_schema_and_semantic_coverage_fail_closed():
    runner = object.__new__(ProductionRunner)
    baseline = "a" * 40
    candidate = "b" * 40
    criteria_hash = hashlib.sha256(b"criteria").hexdigest()

    valid = {
        "task_id": "T0077",
        "baseline_commit": baseline,
        "candidate_commit": candidate,
        "session_id": "sess_qa_1",
        "qa_request_id": "qa_req_1",
        "acceptance_criteria_hash": criteria_hash,
        "decision": "PASS",
        "acceptance_coverage": [
            {"criterion_id": "AC-01", "status": "PASS", "evidence": "tests/test_api.py::test_ok"},
            {"criterion_id": "AC-02", "status": "PASS", "evidence": "tests/test_api.py::test_401"},
        ],
        "test_commands": [
            {"command": "python -m pytest -q", "exit_code": 0, "summary": "2 passed"},
        ],
        "negative_scenarios": [
            {"name": "unauthenticated request", "status": "PASS", "evidence": "test_401"},
        ],
        "uncovered_risks": [],
        "defects": [],
        "summary": "All criteria covered.",
    }
    kwargs = {
        "task_id": "T0077",
        "baseline_commit": baseline,
        "candidate_commit": candidate,
        "session_id": "sess_qa_1",
        "invocation_id": "inv_qa_1",
        "qa_request_id": "qa_req_1",
        "acceptance_criteria_hash": criteria_hash,
        "expected_criterion_ids": ["AC-01", "AC-02"],
        "expected_test_commands": ["python -m pytest -q"],
    }

    parsed = runner._parse_qa_structured_json(json.dumps(valid), **kwargs)
    assert parsed.decision == "PASS"

    mixed_output = "QA assessment complete.\n" + json.dumps(valid)
    parsed = runner._parse_qa_structured_json(mixed_output, **kwargs)
    assert parsed.decision == "PASS"

    plain_text = runner._parse_qa_structured_json("all tests passed", **kwargs)
    assert plain_text.decision == "FAIL"
    assert "SCHEMA-VIOLATION" in plain_text.defects[0]["defect_id"]

    missing_criterion = dict(valid)
    missing_criterion["acceptance_coverage"] = valid["acceptance_coverage"][:1]
    parsed = runner._parse_qa_structured_json(json.dumps(missing_criterion), **kwargs)
    assert parsed.decision == "FAIL"
    assert "COVERAGE-GAP" in parsed.defects[0]["defect_id"]

    no_negative = dict(valid)
    no_negative["negative_scenarios"] = []
    parsed = runner._parse_qa_structured_json(json.dumps(no_negative), **kwargs)
    assert parsed.decision == "FAIL"
    assert "NEGATIVE-GAP" in parsed.defects[0]["defect_id"]

    uncovered = dict(valid)
    uncovered["uncovered_risks"] = ["OpenAPI contract was not checked"]
    parsed = runner._parse_qa_structured_json(json.dumps(uncovered), **kwargs)
    assert parsed.decision == "FAIL"
    assert "UNCOVERED-RISK" in parsed.defects[0]["defect_id"]

    extra_command = dict(valid)
    extra_command["test_commands"] = valid["test_commands"] + [
        {"command": "npm run build", "exit_code": 0, "summary": "built"},
    ]
    parsed = runner._parse_qa_structured_json(json.dumps(extra_command), **kwargs)
    assert parsed.decision == "FAIL"
    assert "COMMAND-GAP" in parsed.defects[0]["defect_id"]


def test_task_execution_spec_normalizes_and_deduplicates_test_commands(tmp_path):
    base = {
        "project_id": "project",
        "project_root": str(tmp_path),
        "authority_root": str(tmp_path),
        "task_id": "T0077",
        "task_name": "test",
        "requirement_text": "requirement",
        "acceptance_criteria": "验收标准: pass",
        "acceptance_criteria_hash": hashlib.sha256("验收标准: pass".encode("utf-8")).hexdigest(),
        "task_version": "1",
        "status_at_read": "进行中",
    }
    one = TaskExecutionSpec(**base, test_commands="python -m pytest -q")
    assert one.test_commands == ("python -m pytest -q",)

    duplicate = TaskExecutionSpec(
        **base,
        test_command="python -m pytest -q",
        test_commands=("python -m pytest -q", " npm run build ", "npm run build"),
    )
    assert duplicate.test_commands == ("python -m pytest -q", "npm run build")


def test_agent_result_status_and_identity_validation():
    """
    P1 对抗测试：
    未校验 AgentResult.status/session_id/is_real_host；FAILED 结果携带 invocation 必须被严格拒绝。
    """
    handle = AgentHandle(
        session_id="sess_valid_123",
        host_id="codex_cli",
        status="completed",
        is_real_host=True,
        adapter_instance_id="inst_1",
        invocation_token="tok_12345678901234567890",
    )

    # 1. 状态为 FAILED 但携带有效 partial_results / host_invocation_id
    failed_res = AgentResult(
        session_id="sess_valid_123",
        status=AgentStatus.FAILED,
        output="error occurred",
        partial_results=({"invocation_id": "inv_should_be_rejected"},),
        is_real_host=True,
    )
    assert _extract_real_invocation_id(failed_res, handle) is None

    # 2. Session ID 不匹配
    mismatched_sess_res = AgentResult(
        session_id="sess_foreign_999",
        status=AgentStatus.SUCCESS,
        output="ok",
        partial_results=({"invocation_id": "inv_123"},),
        is_real_host=True,
    )
    assert _extract_real_invocation_id(mismatched_sess_res, handle) is None

    # 3. is_real_host 为 False (假冒真实 Host)
    fake_host_res = AgentResult(
        session_id="sess_valid_123",
        status=AgentStatus.SUCCESS,
        output="ok",
        partial_results=({"invocation_id": "inv_123"},),
        is_real_host=False,
    )
    assert _extract_real_invocation_id(fake_host_res, handle) is None

    # 4. 正常合法真实结果
    valid_res = AgentResult(
        session_id="sess_valid_123",
        status=AgentStatus.SUCCESS,
        output="ok",
        partial_results=({"invocation_id": "inv_valid_real_456"},),
        is_real_host=True,
    )
    assert _extract_real_invocation_id(valid_res, handle) == "inv_valid_real_456"


def test_qa_test_command_security_and_path_boundary_validation(tmp_path, monkeypatch):
    """
    P2 对抗测试：
    QA 测试命令缺少受控命令族与路径边界校验。
    """
    worktree_dir = str(tmp_path / "worktree")
    os.makedirs(worktree_dir, exist_ok=True)

    # 1. 空命令拒绝
    ok, err, _ = _validate_qa_test_command("", worktree_dir)
    assert ok is False
    assert "cannot be empty" in err

    # 2. 危险 shell 操作符拒绝
    for forbidden in ["pytest; rm -rf /", "pytest | echo hack", "pytest && cat secret", "pytest > output.txt"]:
        ok, err, _ = _validate_qa_test_command(forbidden, worktree_dir)
        assert ok is False
        assert "Dangerous shell operator" in err

    # 3. 未白名单的危险命令拒绝
    for forbidden_bin in ["curl http://evil.com", "wget http://evil.com", "bash run.sh", "cmd /c dir"]:
        ok, err, _ = _validate_qa_test_command(forbidden_bin, worktree_dir)
        assert ok is False

    # 4. 路径逃逸拒绝 (.. 逃离工作区)
    ok, err, _ = _validate_qa_test_command("python ../../outside_script.py", worktree_dir)
    assert ok is False
    assert "python -m pytest" in err

    # 5. 合法受控命令通过
    ok, err, args = _validate_qa_test_command("python -m pytest tests/ -q", worktree_dir)
    assert ok is True
    assert args == [os.path.realpath(sys.executable), "-m", "pytest", "tests/", "-q"]

    trusted_tools = tmp_path / "trusted-tools"
    trusted_tools.mkdir()
    npm_executable = trusted_tools / ("npm.cmd" if sys.platform == "win32" else "npm")
    npm_executable.write_text("", encoding="utf-8")
    monkeypatch.setattr(
        "scripts._lib.core.production_runner.shutil.which",
        lambda command: str(npm_executable) if command == "npm" else None,
    )
    ok, err, args = _validate_qa_test_command("npm run build", worktree_dir)
    assert ok is True
    assert args == [os.path.realpath(npm_executable), "run", "build"]

    ok, err, _ = _validate_qa_test_command("tools/npm run build", worktree_dir)
    assert ok is False
    assert "bare allowlisted command" in err

    monkeypatch.setattr("scripts._lib.core.production_runner.shutil.which", lambda command: None)
    ok, err, _ = _validate_qa_test_command("npm test", worktree_dir)
    assert ok is False
    assert err.startswith("[INFRA_TOOL_MISSING]")

    workspace_npm = tmp_path / "worktree" / ("npm.cmd" if sys.platform == "win32" else "npm")
    workspace_npm.write_text("", encoding="utf-8")
    monkeypatch.setattr(
        "scripts._lib.core.production_runner.shutil.which",
        lambda command: str(workspace_npm),
    )
    ok, err, _ = _validate_qa_test_command("npm test", worktree_dir)
    assert ok is False
    assert "resolves inside the candidate workspace" in err

    monkeypatch.setattr(
        "scripts._lib.core.production_runner.shutil.which",
        lambda command: str(npm_executable) if command in {"npm", "npx"} else None,
    )
    ok, err, _ = _validate_qa_test_command("npm test --prefix=../outside", worktree_dir)
    assert ok is False
    assert "escapes worktree boundary" in err

    ok, err, _ = _validate_qa_test_command("npx jest", worktree_dir)
    assert ok is False
    assert "--no-install" in err

    # Windows quoted absolute interpreter is accepted only when it is the
    # currently trusted Python executable.
    ok, err, args = _validate_qa_test_command(
        f'"{sys.executable}" -m pytest tests/ -q',
        worktree_dir,
    )
    assert ok is True
    assert args[0] == sys.executable

    fake_python = tmp_path / "outside" / "python.exe"
    fake_python.parent.mkdir()
    fake_python.write_bytes(b"not-an-interpreter")
    ok, err, _ = _validate_qa_test_command(
        f'"{fake_python}" -m pytest tests/ -q',
        worktree_dir,
    )
    assert ok is False
    assert "not in allowed test runner whitelist" in err

    # 6. Python 任意代码、pytest 外部配置与同名前缀目录逃逸均拒绝
    ok, err, _ = _validate_qa_test_command("python -c print(1)", worktree_dir)
    assert ok is False
    assert "python -m pytest" in err

    ok, err, _ = _validate_qa_test_command("python -m pytest -c ../pytest.ini", worktree_dir)
    assert ok is False
    assert "forbidden" in err

    sibling = os.path.realpath(worktree_dir + "-outside")
    ok, err, _ = _validate_qa_test_command(f"python -m pytest {sibling}", worktree_dir)
    assert ok is False
    assert "escapes worktree boundary" in err


def test_qa_source_immutability_committed_changes_fails_closed(mock_git_repo, tmp_path, monkeypatch):
    """
    P1 对抗测试：
    QA 修改源码后执行 git commit，前后 status 虽干净但 HEAD 改变，必须被 Fail-Closed 拦截。
    """
    repo_dir, baseline_sha = mock_git_repo
    data_root = tmp_path / "data_root"
    data_root.mkdir()

    session_workspaces = {}
    review_requests = {}

    def mock_detect_caps(self):
        return HostCapabilities(
            is_real_host=True,
            supports_real_subagents=CapabilitySupport.SUPPORTED,
            supports_parallelism=CapabilitySupport.SUPPORTED,
            supports_isolated_context=CapabilitySupport.SUPPORTED,
            supports_worktree=CapabilitySupport.SUPPORTED,
            supports_permission_approval=CapabilitySupport.SUPPORTED,
            supports_mcp=CapabilitySupport.SUPPORTED,
            supports_interactive_confirmation=CapabilitySupport.UNSUPPORTED,
            supports_usage_telemetry=CapabilitySupport.SUPPORTED,
            max_concurrent_agents=4,
        )

    def mock_codex_dispatch(self, req):
        session_workspaces[req.session_id] = req.workspace_dir
        return AgentHandle(
            session_id=req.session_id,
            host_id="codex_cli",
            status="completed",
            is_real_host=True,
            adapter_instance_id="inst_c",
            invocation_token="tok_codex_1234567890",
        )

    def mock_codex_wait(self, handle, timeout_seconds=None):
        target_dir = session_workspaces.get(handle.session_id, str(repo_dir))
        if "builder" in handle.session_id:
            dummy_file = os.path.join(target_dir, f"feature_{int(time.time()*1000)}.py")
            with open(dummy_file, "w", encoding="utf-8") as f:
                f.write("def feat(): return True\n")
            subprocess.run(["git", "add", "."], cwd=target_dir, check=True, capture_output=True)
            subprocess.run(["git", "commit", "-m", "feat: new feature"], cwd=target_dir, check=True, capture_output=True)
            return AgentResult(session_id=handle.session_id, status=AgentStatus.SUCCESS, output="ok", partial_results=({"invocation_id": "inv_builder_real"},), is_real_host=True)
        else:
            # QA 恶意修改了源码并执行了 git commit (试图让 working copy 显得干净)
            readme_path = os.path.join(target_dir, "README.md")
            with open(readme_path, "a", encoding="utf-8") as f:
                f.write("\n# QA sneaked a commit!\n")
            subprocess.run(["git", "add", "."], cwd=target_dir, check=True, capture_output=True)
            subprocess.run(["git", "commit", "-m", "qa sneaked commit"], cwd=target_dir, check=True, capture_output=True)
            return AgentResult(session_id=handle.session_id, status=AgentStatus.SUCCESS, output="QA done", partial_results=({"invocation_id": "inv_qa_real"},), is_real_host=True)

    def mock_reviewer_dispatch(self, req):
        session_workspaces[req.session_id] = req.workspace_dir
        review_requests[req.session_id] = req.extra_context["review_request_id"]
        return AgentHandle(session_id=req.session_id, host_id="antigravity", status="completed", is_real_host=True, adapter_instance_id="inst_a", invocation_token="tok_reviewer_1234567890")

    def mock_reviewer_wait(self, handle, timeout_seconds=None):
        target_dir = session_workspaces.get(handle.session_id, str(repo_dir))
        cand_sha = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=target_dir, text=True).strip()
        out_json = {
            "task_id": "T0066",
            "baseline_commit": baseline_sha,
            "candidate_commit": cand_sha,
            "session_id": handle.session_id,
            "review_request_id": review_requests[handle.session_id],
            "decision": "PASS",
            "defects": [],
            "summary": "pass",
        }
        return AgentResult(session_id=handle.session_id, status=AgentStatus.SUCCESS, output=json.dumps(out_json), partial_results=({"invocation_id": "inv_reviewer_real"},), is_real_host=True)

    monkeypatch.setattr(CodexCliAdapter, "detect_capabilities", mock_detect_caps)
    monkeypatch.setattr(AntigravityAdapter, "detect_capabilities", mock_detect_caps)
    monkeypatch.setattr(CodexCliAdapter, "dispatch_agent", mock_codex_dispatch)
    monkeypatch.setattr(CodexCliAdapter, "wait_for_result", mock_codex_wait)
    monkeypatch.setattr(AntigravityAdapter, "dispatch_agent", mock_reviewer_dispatch)
    monkeypatch.setattr(AntigravityAdapter, "wait_for_result", mock_reviewer_wait)

    registry = AdapterRegistry(context_id="test_repo")
    codex_manifest = create_codex_cli_manifest(adapter_id="codex_cli", verified_version="0.149.0")
    codex_adapter = CodexCliAdapter(is_real_host=True)
    registry.register(codex_adapter, codex_manifest)

    ag_manifest = create_antigravity_manifest(adapter_id="antigravity", verified_version="1.1.22")
    ag_adapter = AntigravityAdapter(is_real_host=True)
    registry.register(ag_adapter, ag_manifest)

    evidence_store = EvidenceStore(root_dir=str(data_root / "evidence"))
    checkpoint_store = RunnerCheckpointStore(data_root=str(data_root), project_root=str(repo_dir), project_id="test_repo")

    runner = ProductionRunner(
        registry=registry,
        evidence_store=evidence_store,
        checkpoint_store=checkpoint_store,
    )

    spec = load_task_execution_spec(project_root=str(repo_dir), task_id="T0066", authority_root=str(repo_dir))

    result = runner.start(spec)
    assert result.success is False
    assert result.state == RunnerState.FAILED.value
    assert "QA modified HEAD or committed code illegally" in result.message


def test_qa_immutability_allows_nested_test_caches_but_rejects_source(mock_git_repo):
    repo_dir, _ = mock_git_repo
    subprocess.run(["git", "add", "config"], cwd=repo_dir, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "add config"], cwd=repo_dir, check=True, capture_output=True)
    candidate = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=repo_dir, text=True).strip()
    runner = object.__new__(ProductionRunner)

    nested_cache = repo_dir / "tests" / "__pycache__"
    nested_cache.mkdir(parents=True)
    (nested_cache / "test_value.cpython-311-pytest.pyc").write_bytes(b"cache")
    ok, error = runner._verify_qa_immutability(str(repo_dir), candidate)
    assert ok is True
    assert error is None

    (repo_dir / "tests" / "unexpected_source.py").write_text("VALUE = 1\n", encoding="utf-8")
    ok, error = runner._verify_qa_immutability(str(repo_dir), candidate)
    assert ok is False
    assert "unexpected_source.py" in error


def test_builder_no_commit_or_missing_invocation_fails_closed(mock_git_repo, tmp_path, monkeypatch):
    """
    DEF-T0061-2 对抗测试：
    1. Builder 没有产出新提交 (Candidate == Baseline) 必须 Fail-Closed 终止；
    2. Host 没有返回真实 Invocation 必须 Fail-Closed 终止。
    """
    repo_dir, baseline_sha = mock_git_repo
    data_root = tmp_path / "data_root"
    data_root.mkdir()

    def mock_detect_caps(self):
        return HostCapabilities(
            is_real_host=True,
            supports_real_subagents=CapabilitySupport.SUPPORTED,
            supports_parallelism=CapabilitySupport.SUPPORTED,
            supports_isolated_context=CapabilitySupport.SUPPORTED,
            supports_worktree=CapabilitySupport.SUPPORTED,
            supports_permission_approval=CapabilitySupport.SUPPORTED,
            supports_mcp=CapabilitySupport.SUPPORTED,
            supports_interactive_confirmation=CapabilitySupport.UNSUPPORTED,
            supports_usage_telemetry=CapabilitySupport.SUPPORTED,
            max_concurrent_agents=4,
        )

    def mock_builder_no_commit_wait(self, handle, timeout_seconds=None):
        return AgentResult(
            session_id=handle.session_id,
            status=AgentStatus.SUCCESS,
            output="Builder executed but made no commit",
            partial_results=({"invocation_id": "inv_b_real"},),
            is_real_host=True,
        )

    def mock_builder_dispatch(self, req):
        return AgentHandle(session_id=req.session_id, host_id="codex_cli", status="completed", is_real_host=True, adapter_instance_id="inst_c", invocation_token="tok_b")

    monkeypatch.setattr(CodexCliAdapter, "detect_capabilities", mock_detect_caps)
    monkeypatch.setattr(CodexCliAdapter, "dispatch_agent", mock_builder_dispatch)
    monkeypatch.setattr(CodexCliAdapter, "wait_for_result", mock_builder_no_commit_wait)
    monkeypatch.setattr(AntigravityAdapter, "detect_capabilities", mock_detect_caps)

    registry = AdapterRegistry(context_id="test_repo")
    codex_manifest = create_codex_cli_manifest(adapter_id="codex_cli", verified_version="0.149.0")
    codex_adapter = CodexCliAdapter(is_real_host=True)
    registry.register(codex_adapter, codex_manifest)

    ag_manifest = create_antigravity_manifest(adapter_id="antigravity", verified_version="1.1.22")
    ag_adapter = AntigravityAdapter(is_real_host=True)
    registry.register(ag_adapter, ag_manifest)

    runner = ProductionRunner(
        registry=registry,
        evidence_store=EvidenceStore(root_dir=str(data_root / "evidence")),
        checkpoint_store=RunnerCheckpointStore(data_root=str(data_root), project_root=str(repo_dir)),
    )

    spec = load_task_execution_spec(project_root=str(repo_dir), task_id="T0055", authority_root=str(repo_dir))

    result = runner.start(spec)
    assert result.success is False
    assert result.state == RunnerState.FAILED.value
    assert "Builder produced no new commits" in result.message


def test_permission_approval_required_pause_and_resume(mock_git_repo, tmp_path, monkeypatch):
    """
    DEF-T0061-7 测试：
    权限被拒绝时暂停在 APPROVAL_REQUIRED，并支持通过 resume(--approve) 授权恢复。
    """
    repo_dir, baseline_sha = mock_git_repo
    data_root = tmp_path / "data_root"
    data_root.mkdir()

    call_count = 0

    def mock_detect_caps(self):
        return HostCapabilities(
            is_real_host=True,
            supports_real_subagents=CapabilitySupport.SUPPORTED,
            supports_parallelism=CapabilitySupport.SUPPORTED,
            supports_isolated_context=CapabilitySupport.SUPPORTED,
            supports_worktree=CapabilitySupport.SUPPORTED,
            supports_permission_approval=CapabilitySupport.SUPPORTED,
            supports_mcp=CapabilitySupport.SUPPORTED,
            supports_interactive_confirmation=CapabilitySupport.UNSUPPORTED,
            supports_usage_telemetry=CapabilitySupport.SUPPORTED,
            max_concurrent_agents=4,
        )

    def mock_dispatch(self, req):
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            raise AgentPermissionRequiredError("Operation requires explicit user permission approval for network access.")
        return AgentHandle(session_id=req.session_id, host_id="codex_cli", status="completed", is_real_host=True, adapter_instance_id="inst_c", invocation_token="tok_b")

    def mock_wait(self, handle, timeout_seconds=None):
        target_dir = str(repo_dir)
        dummy_file = os.path.join(target_dir, f"perm_feature_{int(time.time()*1000)}.py")
        with open(dummy_file, "w", encoding="utf-8") as f:
            f.write("def perm_ok(): return True\n")
        subprocess.run(["git", "add", "."], cwd=target_dir, check=True, capture_output=True)
        subprocess.run(["git", "commit", "-m", "feat: perm ok"], cwd=target_dir, check=True, capture_output=True)

        return AgentResult(session_id=handle.session_id, status=AgentStatus.SUCCESS, output="ok", partial_results=({"invocation_id": "inv_b"},), is_real_host=True)

    monkeypatch.setattr(CodexCliAdapter, "detect_capabilities", mock_detect_caps)
    monkeypatch.setattr(CodexCliAdapter, "dispatch_agent", mock_dispatch)
    monkeypatch.setattr(CodexCliAdapter, "wait_for_result", mock_wait)
    monkeypatch.setattr(AntigravityAdapter, "detect_capabilities", mock_detect_caps)

    registry = AdapterRegistry(context_id="test_repo")
    codex_manifest = create_codex_cli_manifest(adapter_id="codex_cli", verified_version="0.149.0")
    codex_adapter = CodexCliAdapter(is_real_host=True)
    registry.register(codex_adapter, codex_manifest)

    ag_manifest = create_antigravity_manifest(adapter_id="antigravity", verified_version="1.1.22")
    ag_adapter = AntigravityAdapter(is_real_host=True)
    registry.register(ag_adapter, ag_manifest)

    checkpoint_store = RunnerCheckpointStore(data_root=str(data_root), project_root=str(repo_dir), project_id="test_repo")
    runner = ProductionRunner(
        registry=registry,
        evidence_store=EvidenceStore(root_dir=str(data_root / "evidence")),
        checkpoint_store=checkpoint_store,
    )

    spec = load_task_execution_spec(project_root=str(repo_dir), task_id="T0033", authority_root=str(repo_dir))

    # 1. 首次启动应触发 APPROVAL_REQUIRED 暂停
    res1 = runner.start(spec)
    assert res1.success is False
    assert res1.state == RunnerState.APPROVAL_REQUIRED.value
    assert "Operation requires explicit user permission approval" in res1.message
    paused = checkpoint_store.load_checkpoint("T0033")
    assert paused is not None
    assert paused.state == RunnerState.APPROVAL_REQUIRED.value
    assert paused.current_role == "BUILDER"
    assert "explicit user permission approval" in (paused.approval_reason or "")

    # 2. 通过 resume 并授权后继续执行
    res2 = runner.resume(project_root=str(repo_dir), task_id="T0033", authority_root=str(repo_dir), pre_granted_approval=True)
    assert res2.state != RunnerState.APPROVAL_REQUIRED.value


def test_runner_failure_result_cannot_leave_building_checkpoint(tmp_path):
    store = RunnerCheckpointStore(
        data_root=str(tmp_path / "checkpoint_data"),
        project_root=str(tmp_path),
        project_id="checkpoint_guard",
    )
    store.save_checkpoint(RunnerCheckpoint(
        task_id="T0088",
        project_id="checkpoint_guard",
        state=RunnerState.BUILDING.value,
        current_role="BUILDER",
    ))
    runner = ProductionRunner(checkpoint_store=store)

    result = runner._synchronize_failed_result_checkpoint(RunnerResult(
        success=False,
        state=RunnerState.NEEDS_USER_INPUT.value,
        task_id="T0088",
        message="Host stopped before returning a valid result.",
    ))

    persisted = store.load_checkpoint("T0088")
    assert result.state == RunnerState.NEEDS_USER_INPUT.value
    assert persisted is not None
    assert persisted.state == RunnerState.NEEDS_USER_INPUT.value
    assert persisted.last_error == "Host stopped before returning a valid result."
