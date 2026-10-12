#!/usr/bin/env python3
"""在临时 Git 仓库里用真实 CLI 演练 Profile 闭环。不调用真实模型宿主。"""
import json
import os
import shutil
import subprocess
import sys
import tempfile

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
PYTHON = sys.executable


def run(cmd, cwd):
    env = os.environ.copy()
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONUTF8"] = "1"
    proc = subprocess.run(
        cmd,
        cwd=cwd,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        env=env,
    )
    return {
        "cmd": cmd,
        "cwd": cwd,
        "exit_code": proc.returncode,
        "stdout": proc.stdout,
        "stderr": proc.stderr,
    }


def git(repo, *args):
    result = run(["git", *args], repo)
    if result["exit_code"] != 0:
        raise RuntimeError(result["stderr"] or result["stdout"])
    return result


def write(repo, name, text):
    path = os.path.join(repo, name)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="\n") as handle:
        handle.write(text)
    return path


def select(data_root, task_id, profile, task_type, files, requirement="", root=None):
    cmd = [
        PYTHON, os.path.join(ROOT, "scripts", "select_workflow_profile.py"),
        "--data-root", data_root,
        "--task-id", task_id,
        "--profile", profile,
        "--task-type", task_type,
        "--requirement", requirement,
        "--requirement-hash", "req-drill",
        "--acceptance-hash", "ac-drill",
        "--root", root or data_root,
    ]
    for item in files:
        cmd.extend(["--file", item])
    return run(cmd, ROOT)


def validate(role, source, target, assignee, task_type, remarks, end_time="", extra=None):
    cmd = [
        PYTHON, os.path.join(ROOT, "scripts", "_lib", "core", "validate_transition.py"),
        "--role", role,
        "--from-status", source,
        "--to-status", target,
        "--assignee", assignee,
        "--type", task_type,
        "--remarks", remarks,
    ]
    if end_time:
        cmd.extend(["--end-time", end_time])
    cmd.extend(extra or [])
    return run(cmd, ROOT)


def head(repo):
    return git(repo, "rev-parse", "HEAD")["stdout"].strip()


def main():
    workspace = tempfile.mkdtemp(prefix="yy-flow-profile-drill-")
    repo = os.path.join(workspace, "repo")
    data_root = os.path.join(workspace, "data")
    os.makedirs(repo)
    os.makedirs(data_root)
    evidence_dir = os.path.join(data_root, "evidence")
    calls = []
    try:
        git(repo, "init")
        git(repo, "config", "user.name", "Drill")
        git(repo, "config", "user.email", "drill@example.com")
        write(repo, "README.md", "# drill\n")
        git(repo, "add", ".")
        git(repo, "commit", "-m", "init")
        baseline = head(repo)

        write(repo, os.path.join("docs", "guide.md"), "---\ntitle: 演练说明\ntype: guide\n---\n\n低风险说明。\n")
        doc_select = select(data_root, "T9001", "docs-short", "C", ["docs/guide.md"], root=repo)
        calls.append(doc_select)
        write(repo, "app.py", "def ready():\n    return True\n")
        git(repo, "add", ".")
        git(repo, "commit", "-m", "low risk code")
        success_sha = head(repo)
        lite_select = select(data_root, "T9002", "lite", "A", ["app.py"], "实现 ready", root=repo)
        calls.append(lite_select)
        lite_policy = json.loads(lite_select["stdout"])["policy"]
        from _lib.core.evidence_store import EvidenceStore
        from _lib.core.evidence_schema import EvidenceMetadata, EvidenceRecord, EvidenceType
        store = EvidenceStore(evidence_dir)
        evidence_id = "evi_verifier_t9002_drill"
        store.append(EvidenceRecord(
            evidence_id=evidence_id,
            evidence_type=EvidenceType.TASK_TRANSITION,
            baseline_commit=baseline,
            result_commit=success_sha,
            artifacts=(),
            metadata=EvidenceMetadata(
                project_id="drill",
                task_id="T9002",
                actor_role="VERIFIER",
                host_id="drill-host",
                adapter="drill-adapter",
                host_session_id="sess_verifier_t9002",
                host_invocation_id="inv_verifier_drill",
                is_real_host=False,
                workspace_mode="workspace_read",
                transition_from="BUILDING",
                transition_to="PENDING_USER_ACCEPTANCE",
                created_at=1.0,
                extra={"workflow_profile_id": "lite", "verifier_decision": "PASS"},
            ),
        ))
        policy_path = json.loads(lite_select["stdout"])["policy_path"]
        success_validate = validate(
            "VERIFIER", "进行中", "已完成", "严经理", "A",
            "备注不能授予通过", "2026-10-12 12:00:00",
            extra=[
                "--policy-file", policy_path,
                "--evidence-root", evidence_dir,
                "--evidence-id", evidence_id,
                "--candidate-commit", success_sha,
                "--builder-session", "sess_builder_t9002",
                "--change-root", repo,
                "--change-file", "app.py",
            ],
        )
        calls.append(success_validate)

        write(repo, "pay.py", "def charge():\n    payment = True\n    return payment\n")
        risk_select = select(data_root, "T9003", "lite", "A", ["pay.py"], root=repo)
        calls.append(risk_select)

        fail_validate = validate(
            "VERIFIER", "进行中", "已完成", "严经理", "A",
            "WF_POLICY profile=lite;version=3.0.0;risk=low;checks=1;verifier=not-an-evidence",
            "2026-10-12 12:10:00",
        )
        calls.append(fail_validate)
        reject_validate = validate("VERIFIER", "进行中", "已退回", "李开发", "A", "Verifier 退回修复")
        calls.append(reject_validate)
        repair = write(repo, "app.py", "def ready():\n    return True\n\n")
        git(repo, "add", ".")
        git(repo, "commit", "-m", "repair")
        repair_sha = head(repo)
        repair_evidence = "evi_verifier_t9002_repair"
        store.append(EvidenceRecord(
            evidence_id=repair_evidence,
            evidence_type=EvidenceType.TASK_TRANSITION,
            baseline_commit=success_sha,
            result_commit=repair_sha,
            artifacts=(),
            metadata=EvidenceMetadata(
                project_id="drill",
                task_id="T9002",
                actor_role="VERIFIER",
                host_id="drill-host",
                adapter="drill-adapter",
                host_session_id="sess_verifier_t9002_repair",
                host_invocation_id="inv_verifier_repair",
                is_real_host=False,
                workspace_mode="workspace_read",
                transition_from="BUILDING",
                transition_to="PENDING_USER_ACCEPTANCE",
                created_at=2.0,
                extra={"workflow_profile_id": "lite", "verifier_decision": "PASS"},
            ),
        ))
        repaired = dict(lite_policy)
        repaired["verifier_evidence_id"] = repair_evidence
        repaired["budget"] = {"budget_consumed": 2, "max_total_attempts": 6}
        repair_validate = validate(
            "VERIFIER", "进行中", "已完成", "严经理", "A",
            "修复后重新验证", "2026-10-12 12:20:00",
            extra=[
                "--policy-file", policy_path,
                "--evidence-root", evidence_dir,
                "--evidence-id", repair_evidence,
                "--candidate-commit", repair_sha,
                "--builder-session", "sess_builder_t9002",
                "--change-root", repo,
                "--change-file", "app.py",
            ],
        )
        calls.append(repair_validate)

        from _lib.core.workflow_policy import resume_policy
        resume = resume_policy(
            repaired,
            requirement_hash=lite_policy.get("requirement_hash") or "",
            acceptance_criteria_hash=lite_policy.get("acceptance_criteria_hash") or "",
            candidate_commit=repair_sha,
            files=[{"path": "app.py", "content": open(os.path.join(repo, "app.py"), encoding="utf-8").read()}],
            budget_consumed=2,
        )
        report = {
            "workspace": workspace,
            "baseline": baseline,
            "success_sha": success_sha,
            "repair_sha": repair_sha,
            "evidence_dir": evidence_dir,
            "evidence_ids": [evidence_id, repair_evidence],
            "calls": calls,
            "resume": resume,
            "real_model_hosts": "unverified",
            "note": "演练使用真实 Git、策略 CLI、状态校验和证据文件；没有调用 Cursor、Codex 或 Antigravity 模型。",
        }
        encoded = json.dumps(report, ensure_ascii=True, indent=2)
        sys.stdout.buffer.write(encoded.encode("utf-8"))
        sys.stdout.buffer.write(b"\n")
        failed = [item for item in calls if item["cmd"][1].endswith("select_workflow_profile.py") and item["exit_code"] not in (0, 2)]
        if reject_validate["exit_code"] != 0 or repair_validate["exit_code"] != 0 or doc_select["exit_code"] != 0:
            return 1
        if failed or success_validate["exit_code"] != 0 or fail_validate["exit_code"] == 0 or risk_select["exit_code"] != 2:
            return 1
        if resume.get("budget_consumed") != 2:
            return 1
        return 0
    finally:
        shutil.rmtree(workspace, ignore_errors=True)


if __name__ == "__main__":
    sys.path.insert(0, os.path.join(ROOT, "scripts"))
    sys.exit(main())
