#!/usr/bin/env python3
"""
敏感凭证与硬编码密钥安全扫描脚本 (Check Secrets CLI)
"""
import os
import sys
import re
import argparse
import subprocess
from typing import List, Tuple

SECRET_PATTERNS = [
    (re.compile(r'cli_[a-zA-Z0-9]{16,}', re.IGNORECASE), "飞书 App ID / Token"),
    (re.compile(r'(app_secret|appsecret|secret)\s*:\s*["\']?[a-zA-Z0-9]{20,}', re.IGNORECASE), "硬编码 App Secret"),
    (re.compile(r'ghp_[a-zA-Z0-9]{36}', re.IGNORECASE), "GitHub Personal Access Token"),
    (re.compile(r'ATATT3[a-zA-Z0-9_\-=]{40,}', re.IGNORECASE), "Jira API Token"),
    (re.compile(r'-----BEGIN ' + r'PRIVATE KEY-----'), "RSA 私钥凭证")
]

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, SCRIPT_DIR)
WORKFLOW_ROOT = os.path.abspath(os.path.join(SCRIPT_DIR, ".."))

def scan_file(file_path: str) -> List[Tuple[int, str, str]]:
    findings = []
    # 跳过对检查器脚本自身的匹配
    if os.path.basename(file_path) == "check_secrets.py":
        return findings

    try:
        with open(file_path, 'r', encoding='utf-8', errors='ignore') as f:
            for line_num, line in enumerate(f, 1):
                clean_line = line.strip()
                if clean_line.startswith('#'):
                    continue
                for pattern, desc in SECRET_PATTERNS:
                    match = pattern.search(line)
                    if match:
                        matched_str = match.group(0)
                        # 如果匹配内容纯粹且完全是规范的变量占位符如 ${FEISHU_BASE_TOKEN}，则视为符合规范
                        if re.match(r'^\$\{[A-Za-z0-9_:-]+\}$', matched_str.strip()):
                            continue
                        findings.append((line_num, desc, clean_line))
    except Exception as e:
        print(f"[WARN] 无法读取文件 {file_path}: {e}")
    return findings


def scan_text(text: str) -> List[Tuple[int, str, str]]:
    """Scan immutable candidate content without checking out or editing it."""
    findings = []
    for line_num, line in enumerate(text.splitlines(), 1):
        clean_line = line.strip()
        if clean_line.startswith('#'):
            continue
        for pattern, desc in SECRET_PATTERNS:
            match = pattern.search(line)
            if match and not re.match(r'^\$\{[A-Za-z0-9_:-]+\}$', match.group(0).strip()):
                findings.append((line_num, desc, clean_line))
    return findings


def scan_candidate(project_root: str, baseline: str, candidate: str) -> int:
    """Scan added/modified text at an immutable Git candidate."""
    sha_re = re.compile(r"^[0-9a-fA-F]{40}$")
    if not sha_re.fullmatch(baseline or "") or not sha_re.fullmatch(candidate or ""):
        print("[ERROR] baseline and candidate must be full 40-character Git SHAs")
        return 2
    project_root = os.path.realpath(os.path.abspath(project_root))
    try:
        names = subprocess.check_output(
            ["git", "diff", "--name-only", "--diff-filter=ACMR", "-z", baseline, candidate, "--"],
            cwd=project_root,
        ).decode("utf-8", errors="surrogateescape").split("\0")
    except (OSError, subprocess.CalledProcessError) as exc:
        print(f"[ERROR] unable to enumerate candidate files: {exc}")
        return 2

    total_issues = 0
    scanned = 0
    for relative_path in (name for name in names if name):
        if os.path.basename(relative_path) == "check_secrets.py":
            continue
        try:
            blob = subprocess.check_output(
                ["git", "show", f"{candidate}:{relative_path}"],
                cwd=project_root,
                stderr=subprocess.STDOUT,
            )
        except subprocess.CalledProcessError as exc:
            print(f"[ERROR] unable to read candidate file {relative_path}: {exc}")
            return 2
        if b"\x00" in blob:
            continue
        scanned += 1
        findings = scan_text(blob.decode("utf-8", errors="ignore"))
        if findings:
            print(f"[FAILED] candidate {candidate} contains a credential risk in [{relative_path}]:")
            for line_num, desc, line_content in findings:
                print(f"   - line {line_num} [{desc}]: {line_content[:60]}...")
                total_issues += 1
    if total_issues:
        print(f"[FAILED] scanned_files={scanned} findings={total_issues} candidate={candidate}")
        return 1
    print(f"[SUCCESS] scanned_files={scanned} findings=0 candidate={candidate}")
    return 0

def main():
    parser = argparse.ArgumentParser(description="Scan yy-flow sources or an immutable Git candidate")
    parser.add_argument("--project-root")
    parser.add_argument("--baseline")
    parser.add_argument("--candidate")
    args = parser.parse_args()
    candidate_args = (args.project_root, args.baseline, args.candidate)
    if any(candidate_args):
        if not all(candidate_args):
            parser.error("--project-root, --baseline and --candidate must be provided together")
        sys.exit(scan_candidate(args.project_root, args.baseline, args.candidate))

    print("========================================================================")
    print("        [GUARD]   Multi-Agent Workflow · 敏感凭证与硬编码密钥安全扫描")
    print("========================================================================")

    total_issues = 0
    # 技能代码目录（只读正本）+ 宿主数据根的 user_data 与 docs（真实配置/报告可能含凭证）
    import paths as _paths
    data_root = _paths.resolve_data_root()
    scan_dirs = [
        os.path.join(WORKFLOW_ROOT, d) for d in ["config", "scripts", "agents", "docs", "kanban"]
    ]
    if os.path.abspath(data_root) != os.path.abspath(WORKFLOW_ROOT):
        scan_dirs += [
            os.path.join(data_root, "user_data"),
            _paths.docs_root(),
        ]

    for s_dir in scan_dirs:
        if not os.path.exists(s_dir):
            continue
        for root, _, files in os.walk(s_dir):
            for file in files:
                if file.endswith(('.yaml', '.json', '.py', '.sh')):
                    f_path = os.path.join(root, file)
                    rel_path = os.path.relpath(f_path, WORKFLOW_ROOT)
                    if not os.path.abspath(f_path).startswith(os.path.abspath(WORKFLOW_ROOT)):
                        rel_path = os.path.relpath(f_path, data_root)
                    findings = scan_file(f_path)
                    if findings:
                        print(f"\n[FAILED]  在 [{rel_path}] 中发现高风险硬编码凭证:")
                        for line_num, desc, line_content in findings:
                            print(f"   - 行 {line_num} [{desc}]: {line_content[:60]}...")
                            total_issues += 1

    print("\n========================================================================")
    if total_issues > 0:
        print(f"[WARN]  扫描完成: 共发现 {total_issues} 处明文凭证风险！请使用 ${{ENV_VAR}} 替代敏感信息。")
        sys.exit(1)
    else:
        print("[SUCCESS]  安全扫描完成: 未发现硬编码明文凭证与密钥泄露风险！")
        sys.exit(0)

if __name__ == "__main__":
    main()
