# -*- coding: utf-8 -*-
"""项目接入编排。默认预览；只有 --apply 才写入。协议自检不是真实客户端连接。"""

from __future__ import annotations

import argparse
import getpass
import hashlib
import json
import os
import subprocess
import sys
import tempfile

from scripts._lib.mcp.registry import RegistryError, load_registry

from .clients import (
    canonical_entry,
    managed_toml_block,
    mcp_entry,
    merge_mcp_json,
    merge_toml,
    strip_managed_toml,
    strip_mcp_json,
)
from .common import (
    CLIENT_CONFIGS,
    CLIENTS,
    CREDENTIAL_ENV,
    EXPORT_DIRS,
    FORBIDDEN_FIELDS,
    FORBIDDEN_SCOPES,
    SERVER_NAME,
    OnboardError,
    read_bytes,
    sha256_bytes,
    within,
)
from .layout import Layout, new_project_files, registry_document, resolve_layout
from .self_check import run_self_check, scrub_text
from . import transaction as txn


def main(argv: list[str] | None = None) -> int:
    parser = _parser()
    args = parser.parse_args(argv)
    try:
        result = execute(args)
    except OnboardError as exc:
        payload = {
            "ok": False,
            "code": exc.code,
            "message": exc.message,
            "diff": exc.diff,
            "client_connection_verified": False,
            "ac14_live_client_verified": False,
            "verification": "not_run",
        }
        _emit(payload, args.json if hasattr(args, "json") else False, stream=sys.stderr)
        return exc.exit_code
    _emit(result, args.json, stream=sys.stdout)
    return 0 if result.get("ok") else 2


def execute(args: argparse.Namespace) -> dict:
    if args.apply and args.dry_run:
        raise OnboardError("USAGE", "--apply 与 --dry-run 不能同时使用")
    selected = [item for item in (args.diagnose, args.remove, args.recover) if item]
    if len(selected) > 1:
        raise OnboardError("USAGE", "诊断、移除和恢复只能选择一个")
    if args.diagnose and (args.apply or args.dry_run or args.skip_self_check):
        raise OnboardError("USAGE", "诊断是只读操作")
    if args.skip_self_check and not args.apply:
        raise OnboardError("USAGE", "--skip-self-check 只能与 --apply 一起使用，且不等于现场验证")
    if args.diagnose:
        return diagnose(args.project_root)
    if args.remove:
        return remove_managed(args, write=bool(args.apply) and not args.dry_run)
    if args.recover:
        return recover(args.project_root, write=bool(args.apply) and not args.dry_run)
    return setup(args, write=bool(args.apply) and not args.dry_run)


def setup(args: argparse.Namespace, write: bool) -> dict:
    layout = resolve_layout(args.project_root, args.skill_root)
    clients = _clients(args.clients)
    python = probe_python(args.python or sys.executable)
    os_user = getpass.getuser().strip()
    revision = code_version(layout.skill_root)
    document = registry_document(layout, os_user, revision)
    registry_text = json.dumps(document, ensure_ascii=False, indent=2) + "\n"
    _assert_registry(layout, registry_text.encode("utf-8"))
    existing_state = _state(layout)
    entry_hashes = (existing_state or {}).get("entry_hashes", {})
    progress = txn.load_json(txn.progress_path(layout.data_root)) if os.path.isdir(layout.data_root) else None
    if txn.incomplete(progress) and not write:
        raise OnboardError("INCOMPLETE", "上次接入写入未完成。请先查看 --diagnose，再用 --apply 恢复后重试")
    if not write:
        exports = _export_bytes(python, layout, clients)
        planned = _build_plan(
            layout, clients, python, registry_text.encode("utf-8"), exports, entry_hashes, link_label="pending",
            recorded_files=(existing_state or {}).get("files") or {},
        )
        return _public(
            ok=True,
            action="preview",
            layout=layout,
            clients=clients,
            python=python,
            revision=revision,
            files=sorted(os.path.relpath(item["path"], layout.project_root) for item in planned),
            verification="not_run",
            message="预览未写入。确认后追加 --apply",
        )
    handle = txn.acquire(layout.data_root)
    try:
        progress = txn.load_json(txn.progress_path(layout.data_root))
        if txn.incomplete(progress):
            txn.rollback(progress, layout.project_root)
            txn.save_json(txn.progress_path(layout.data_root), progress)
            existing_state = _state(layout)
            entry_hashes = (existing_state or {}).get("entry_hashes", {})
        link_label = _ensure_skill_link(layout)
        mount_label, missing_mounts = _ensure_platform_mounts(layout, clients)
        if mount_label == "absolute_entry":
            link_label = "absolute_entry"
        exports = _export_bytes(python, layout, clients)
        if missing_mounts:
            exports = _rewrite_exports_for_absolute(exports, missing_mounts, layout.skill_root)
        planned = _build_plan(
            layout, clients, python, registry_text.encode("utf-8"), exports, entry_hashes, link_label,
            recorded_files=(existing_state or {}).get("files") or {},
        )
        for item in planned:
            _guard_path(layout, item["path"])
            _assert_clean(item["data"])
        records = []
        backup_dir = os.path.join(layout.data_root, "user_data", "onboarding", "backups", "current")
        for item in planned:
            if item["role"] == "state":
                continue
            records.append(
                txn.file_record(item["path"], item["data"], item["role"], item["config_index"], item["entry_hash"], backup_dir)
            )
        progress = txn.new_progress(layout.data_root, records)
        txn.save_json(txn.progress_path(layout.data_root), progress)
        fail_at = _hook_index("YY_FLOW_ONBOARDING_FAIL_CONFIG")
        interrupt_after = _hook_index("YY_FLOW_ONBOARDING_INTERRUPT_AFTER_CONFIG")
        if fail_at and interrupt_after:
            raise OnboardError("USAGE", "不能同时注入写入失败和进程中断")
        for item in planned:
            if item["role"] == "state":
                continue
            if item["role"] == "config" and fail_at and item["config_index"] == fail_at:
                progress["phase"] = "failed"
                txn.save_json(txn.progress_path(layout.data_root), progress)
                raise OnboardError("WRITE_FAILED", f"第 {fail_at} 个客户端配置写入前失败，接入未完成")
            txn.mark_status(progress, item["path"], "replacing")
            txn.save_json(txn.progress_path(layout.data_root), progress)
            current = read_bytes(item["path"])
            if current != item["data"]:
                try:
                    txn.atomic_write(item["path"], item["data"])
                except OSError as exc:
                    progress["phase"] = "failed"
                    txn.save_json(txn.progress_path(layout.data_root), progress)
                    raise OnboardError("WRITE_FAILED", "写入失败，接入未完成") from exc
            txn.mark_written(progress, item["path"])
            txn.save_json(txn.progress_path(layout.data_root), progress)
            if item["role"] == "config" and interrupt_after and item["config_index"] == interrupt_after:
                progress["phase"] = "writing"
                progress["complete"] = False
                txn.save_json(txn.progress_path(layout.data_root), progress)
                os._exit(86)
        if not args.skip_self_check:
            try:
                verification = run_self_check(python, layout.script_path, layout.registry_path, layout.project_id, layout.data_root)
            except OnboardError:
                progress["phase"] = "self_check_failed"
                progress["complete"] = False
                txn.save_json(txn.progress_path(layout.data_root), progress)
                raise
        else:
            verification = {
                "verification": "skipped",
                "client_connection_verified": False,
                "ac14_live_client_verified": False,
                "note": "--skip-self-check 只用于故障注入，不是真实客户端验证，也不是 AC-14 通过",
            }
        state = _state_document(
            layout, clients, python, revision, planned, link_label, verification, existing_state,
        )
        state_bytes = json.dumps(state, ensure_ascii=False, indent=2).encode("utf-8") + b"\n"
        _assert_clean(state_bytes)
        txn.atomic_write(txn.state_path(layout.data_root), state_bytes)
        progress["phase"] = "committed"
        progress["complete"] = True
        txn.save_json(txn.progress_path(layout.data_root), progress)
        return _public(
            ok=True,
            action="apply",
            layout=layout,
            clients=clients,
            python=python,
            revision=revision,
            files=sorted(os.path.relpath(item["path"], layout.project_root) for item in planned),
            verification=verification["verification"],
            message=verification.get("note") or "接入完成。协议自检已通过，真实客户端尚未现场验证",
            extra=verification,
        )
    finally:
        from scripts._lib.core.file_lock import release_lock

        release_lock(handle)


def diagnose(project_root: str) -> dict:
    layout = resolve_layout(project_root, None) if _can_resolve(project_root) else None
    if layout is None:
        try:
            layout = resolve_layout(project_root, None)
        except OnboardError as exc:
            return {
                "ok": True,
                "action": "diagnose",
                "complete": False,
                "code": exc.code,
                "message": exc.message,
                "client_connection_verified": False,
                "ac14_live_client_verified": False,
                "verification": "not_run",
            }
    progress = txn.load_json(txn.progress_path(layout.data_root))
    state = _state(layout)
    complete = bool(state) and not txn.incomplete(progress) and (progress or {}).get("phase") in {None, "committed", "rolled_back"}
    if progress and progress.get("phase") == "committed":
        complete = True
    if txn.incomplete(progress):
        complete = False
    return {
        "ok": True,
        "action": "diagnose",
        "complete": complete,
        "phase": (progress or {}).get("phase", "absent"),
        "install_mode": layout.mode,
        "project_id": layout.project_id,
        "code_version": (state or {}).get("code_version", ""),
        "server_name": SERVER_NAME,
        "client_connection_verified": False,
        "ac14_live_client_verified": False,
        "verification": (state or {}).get("verification", "not_run"),
        "message": "诊断不写入。未完成的事务可用 --apply 恢复，或用 --recover --apply 只恢复",
    }


def recover(project_root: str, write: bool) -> dict:
    layout = resolve_layout(project_root, None)
    progress = txn.load_json(txn.progress_path(layout.data_root))
    if not txn.incomplete(progress):
        return _public(
            ok=True,
            action="recover",
            layout=layout,
            clients=[],
            python="",
            revision="",
            files=[],
            verification="not_run",
            message="没有未完成的接入事务",
        )
    if not write:
        return _public(
            ok=True,
            action="recover-preview",
            layout=layout,
            clients=[],
            python="",
            revision="",
            files=[item["path"] for item in progress.get("files", []) if item.get("status") == "written"],
            verification="not_run",
            message="恢复预览未写入。追加 --apply 才恢复，且不会覆盖事务后的用户修改",
        )
    handle = txn.acquire(layout.data_root)
    try:
        progress = txn.load_json(txn.progress_path(layout.data_root))
        restored = txn.rollback(progress, layout.project_root) if progress else []
        if progress:
            txn.save_json(txn.progress_path(layout.data_root), progress)
        return _public(
            ok=True,
            action="recover",
            layout=layout,
            clients=[],
            python="",
            revision="",
            files=[os.path.relpath(path, layout.project_root) for path in restored],
            verification="not_run",
            message="已恢复未完成事务中仍与计划一致的文件",
        )
    finally:
        from scripts._lib.core.file_lock import release_lock

        release_lock(handle)


def remove_managed(args: argparse.Namespace, write: bool) -> dict:
    layout = resolve_layout(args.project_root, args.skill_root)
    clients = _clients(args.clients)
    state = _state(layout)
    if not state:
        raise OnboardError("NOT_MANAGED", "没有受管接入记录，拒绝删除")
    entry_hashes = state.get("entry_hashes", {})
    planned = []
    for client in clients:
        path = os.path.join(layout.project_root, *CLIENT_CONFIGS[client].split("/"))
        current = read_bytes(path)
        if current is None:
            continue
        text = current.decode("utf-8")
        recorded = entry_hashes.get(client, "")
        if client == "codex":
            updated = strip_managed_toml(text, recorded or None)
        else:
            label = "Cursor 配置" if client == "cursor" else "Antigravity 配置"
            updated = strip_mcp_json(text, recorded or None, label)
        data = updated.encode("utf-8")
        planned.append({"path": path, "data": data, "role": "config", "config_index": 0, "entry_hash": ""})
    if not write:
        return _public(
            ok=True,
            action="remove-preview",
            layout=layout,
            clients=clients,
            python=state.get("python", ""),
            revision=state.get("code_version", ""),
            files=[os.path.relpath(item["path"], layout.project_root) for item in planned],
            verification="not_run",
            message="移除预览未删除。只准备移除仍与受管哈希一致的 yy-flow 条目；追加 --apply 才写入",
        )
    handle = txn.acquire(layout.data_root)
    try:
        fresh = _state(layout) or state
        entry_hashes = fresh.get("entry_hashes", {})
        removed_clients = []
        removed_files = []
        conflicts = []
        for client in clients:
            path = os.path.join(layout.project_root, *CLIENT_CONFIGS[client].split("/"))
            current = read_bytes(path)
            if current is None:
                removed_clients.append(client)
                continue
            try:
                updated = _strip_client(client, current.decode("utf-8"), entry_hashes.get(client, ""))
            except OnboardError as exc:
                if exc.code != "CONFLICT":
                    raise
                conflicts.append(exc)
                continue
            data = updated.encode("utf-8")
            if data != current:
                txn.atomic_write(path, data)
            removed_clients.append(client)
            removed_files.append(os.path.relpath(path, layout.project_root))
        if removed_clients:
            kept = dict(fresh)
            kept["clients"] = [client for client in fresh.get("clients", []) if client not in removed_clients]
            hashes = dict(entry_hashes)
            for client in removed_clients:
                hashes.pop(client, None)
            kept["entry_hashes"] = hashes
            kept["verification"] = "removed"
            txn.atomic_write(
                txn.state_path(layout.data_root),
                json.dumps(kept, ensure_ascii=False, indent=2).encode("utf-8") + b"\n",
            )
        if conflicts:
            raise OnboardError("CONFLICT", conflicts[0].message, diff=conflicts[0].diff)
        return _public(
            ok=True,
            action="remove",
            layout=layout,
            clients=clients,
            python=fresh.get("python", ""),
            revision=fresh.get("code_version", ""),
            files=removed_files,
            verification="removed",
            message="已移除未改动的受管 yy-flow 条目。运行数据和其他服务器保留",
        )
    finally:
        from scripts._lib.core.file_lock import release_lock

        release_lock(handle)


def probe_python(python_path: str, runner=None) -> str:
    if not isinstance(python_path, str) or not python_path.strip():
        raise OnboardError("PYTHON", "缺少 Python 解释器", exit_code=5)
    if not os.path.isabs(python_path):
        raise OnboardError("PYTHON", "Python 必须使用绝对路径", exit_code=5)
    command_runner = runner or _run_probe
    version = command_runner([python_path, "-c", "import sys; raise SystemExit(0 if sys.version_info >= (3, 11) else 1)"])
    if version != 0:
        raise OnboardError("PYTHON", "需要 Python 3.11 或更高版本，或解释器无法启动", exit_code=5)
    deps = command_runner([python_path, "-c", "import tomllib, json, pathlib, yaml"])
    if deps != 0:
        raise OnboardError("PYTHON", "解释器缺少运行本项目所需的 tomllib 或 PyYAML", exit_code=5)
    return os.path.abspath(python_path)


def code_version(skill_root: str) -> str:
    try:
        proc = subprocess.run(
            ["git", "-C", skill_root, "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=15,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        proc = None
    if proc is not None and proc.returncode == 0:
        sha = (proc.stdout or "").strip()
        if len(sha) in {40, 64} and all(char in "0123456789abcdef" for char in sha.lower()):
            return sha
    target = os.path.join(skill_root, "scripts", "mcp_server.py")
    raw = read_bytes(target) or b""
    return hashlib.sha256(raw).hexdigest()


def _build_plan(layout, clients, python, registry_bytes, exports, entry_hashes, link_label: str, recorded_files: dict | None = None) -> list[dict]:
    recorded_files = recorded_files or {}
    planned = []
    for path, data in new_project_files(layout).items():
        role = "arch" if path.endswith(".yaml") else "board"
        planned.append(_item(path, data, role, 0, ""))
    _reject_managed_edit(layout, layout.registry_path, registry_bytes, recorded_files, "项目登记")
    planned.append(_item(layout.registry_path, registry_bytes, "registry", 0, sha256_bytes(registry_bytes)))
    pointer = {
        "managed": True,
        "mode": layout.mode,
        "skill_root": layout.skill_root,
        "script": layout.script_path,
        "link": link_label,
        "server_name": SERVER_NAME,
    }
    pointer_bytes = json.dumps(pointer, ensure_ascii=False, indent=2).encode("utf-8") + b"\n"
    _reject_managed_edit(layout, layout.pointer_path, pointer_bytes, recorded_files, "技能指向")
    planned.append(_item(layout.pointer_path, pointer_bytes, "pointer", 0, ""))
    entry = mcp_entry(python, layout.script_path, layout.registry_path)
    entry_hash = sha256_bytes(canonical_entry(entry).encode("utf-8"))
    block = managed_toml_block(python, layout.script_path, layout.registry_path)
    block_hash = sha256_bytes(block.encode("utf-8"))
    config_index = 0
    for client in clients:
        config_index += 1
        path = os.path.join(layout.project_root, *CLIENT_CONFIGS[client].split("/"))
        current = _read_existing(path)
        text = None if current is None else current.decode("utf-8")
        recorded = entry_hashes.get(client) or None
        if client == "codex":
            updated = merge_toml(text, block, recorded)
            item_hash = block_hash
        else:
            label = "Cursor 配置" if client == "cursor" else "Antigravity 配置"
            updated = merge_mcp_json(text, entry, recorded, label)
            item_hash = entry_hash
        planned.append(_item(path, updated.encode("utf-8"), "config", config_index, item_hash))
    for relative, data in sorted(exports.items()):
        path = os.path.join(layout.project_root, relative)
        current = _read_existing(path)
        planned.append(_item(path, data, "export", 0, sha256_bytes(data)))
        if current is not None and current != data:
            previous = entry_hashes.get(_export_key(relative))
            current_hash = sha256_bytes(current)
            if previous is None or current_hash != previous:
                from .common import limited_diff

                raise OnboardError(
                    "CONFLICT",
                    "生成文件已存在或已被修改，拒绝覆盖：" + relative,
                    diff=limited_diff(current.decode("utf-8", errors="replace"), data.decode("utf-8", errors="replace")),
                )
    return planned


def _read_existing(path: str) -> bytes | None:
    try:
        return read_bytes(path)
    except OSError as exc:
        raise OnboardError("WRITE_FAILED", "目标文件无法访问，拒绝写入", exit_code=2) from exc


def _reject_managed_edit(layout: Layout, path: str, planned: bytes, recorded_files: dict, label: str) -> None:
    """用户改过的受管生成文件拒绝覆盖。仍与上次受管哈希一致时允许相同输入重入。"""
    current = _read_existing(path)
    if current is None or current == planned:
        return
    relative = os.path.relpath(path, layout.project_root).replace("\\", "/")
    recorded = recorded_files.get(relative) if isinstance(recorded_files, dict) else None
    recorded_hash = recorded.get("sha256") if isinstance(recorded, dict) else ""
    if isinstance(recorded_hash, str) and recorded_hash and sha256_bytes(current) == recorded_hash:
        return
    from .common import limited_diff

    raise OnboardError(
        "CONFLICT",
        label + " 已修改，拒绝覆盖",
        diff=limited_diff(current.decode("utf-8", errors="replace"), planned.decode("utf-8", errors="replace")),
    )


def _item(path: str, data: bytes, role: str, config_index: int, entry_hash: str) -> dict:
    return {
        "path": os.path.abspath(path),
        "data": data,
        "role": role,
        "config_index": config_index,
        "entry_hash": entry_hash,
    }


def _export_bytes(python: str, layout: Layout, clients: list[str]) -> dict[str, bytes]:
    script = os.path.join(layout.skill_root, "scripts", "verify_and_export_agents.py")
    env = os.environ.copy()
    for key in CREDENTIAL_ENV:
        env.pop(key, None)
    env.pop("YY_FLOW_ONBOARDING_FAIL_CONFIG", None)
    env.pop("YY_FLOW_ONBOARDING_INTERRUPT_AFTER_CONFIG", None)
    data_root = layout.data_root if os.path.isdir(layout.data_root) else layout.project_root
    env["YY_FLOW_PROJECT_ROOT"] = data_root
    with tempfile.TemporaryDirectory(prefix="yy-flow-export-") as staging:
        proc = subprocess.run(
            [python, script, "--target-project-dir", staging, "--platforms", ",".join(clients)],
            cwd=staging,
            env=env,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=180,
            check=False,
        )
        if proc.returncode != 0:
            detail = scrub_text((proc.stdout or "") + "\n" + (proc.stderr or ""))[-1500:]
            raise OnboardError("EXPORT", "角色规则导出失败\n" + detail)
        found: dict[str, bytes] = {}
        for client in clients:
            for relative_root in EXPORT_DIRS[client]:
                root = os.path.join(staging, *relative_root.split("/"))
                if not os.path.isdir(root):
                    raise OnboardError("EXPORT", "未导出所选客户端目录：" + relative_root)
                for dirpath, dirnames, filenames in os.walk(root):
                    dirnames[:] = [
                        name for name in dirnames
                        if not os.path.islink(os.path.join(dirpath, name))
                        and not _is_reparse_point(os.path.join(dirpath, name))
                    ]
                    for name in filenames:
                        full = os.path.join(dirpath, name)
                        if os.path.islink(full) or _is_reparse_point(full):
                            continue
                        relative = os.path.relpath(full, staging)
                        blob = read_bytes(full)
                        if blob is None:
                            raise OnboardError("EXPORT", "导出文件无法读取")
                        found[relative] = blob
        return found


def _ensure_skill_link(layout: Layout) -> str:
    if layout.mode == "legacy":
        return "absolute_entry"
    link = os.path.join(layout.project_root, ".yy-flow", "skill")
    if os.path.lexists(link):
        if os.path.normcase(os.path.realpath(link)) == os.path.normcase(layout.skill_root):
            return "symlink" if os.path.islink(link) else "project_directory"
        raise OnboardError("SKILL_ROOT", "项目内 .yy-flow/skill 已指向其他位置。共享模式不会整份复制，请先处理该路径")
    if layout.mode == "project":
        return "project_directory"
    if os.environ.get("YY_FLOW_ONBOARDING_DENY_LINKS", "").strip() == "1":
        return "absolute_entry"
    try:
        os.makedirs(os.path.dirname(link), exist_ok=True)
        os.symlink(layout.skill_root, link, target_is_directory=True)
        return "symlink"
    except OSError:
        return "absolute_entry"


def _ensure_platform_mounts(layout: Layout, clients: list[str]) -> tuple[str, list[str]]:
    """按平台声明把技能根挂进项目。链接失败时不复制源码，改记绝对入口。"""
    missing = []
    for relative, link, source in _platform_mounts(layout, clients):
        if os.path.lexists(link):
            if _same_tree(source, link) and (_is_reparse_point(link) or os.path.islink(link)):
                continue
            raise OnboardError("SKILL_ROOT", "平台技能挂载已指向其他位置：" + relative)
        if not _try_link(source, link):
            missing.append(relative)
    return ("absolute_entry" if missing else "symlink"), missing


def _platform_mounts(layout: Layout, clients: list[str]) -> list[tuple[str, str, str]]:
    path = os.path.join(layout.skill_root, "config", "agent_platforms.yaml")
    try:
        import yaml

        with open(path, "r", encoding="utf-8") as handle:
            document = yaml.safe_load(handle)
    except (OSError, UnicodeError) as exc:
        raise OnboardError("EXPORT", "平台配置无法读取") from exc
    if not isinstance(document, dict):
        raise OnboardError("EXPORT", "平台配置无法解析")
    skill_name = document.get("default_skill_name") or "yy-flow"
    platforms = document.get("platforms") or {}
    found = []
    seen = set()
    for client in clients:
        spec = platforms.get(client) or {}
        template = spec.get("skill_target")
        if not isinstance(template, str) or not template.strip():
            continue
        relative = template.format(skill_name=skill_name).replace("\\", "/")
        link = os.path.join(layout.project_root, *relative.split("/"))
        key = os.path.normcase(os.path.abspath(link))
        if key in seen:
            continue
        seen.add(key)
        found.append((relative, link, layout.skill_root))
    return found


def _try_link(source: str, link: str) -> bool:
    if os.environ.get("YY_FLOW_ONBOARDING_DENY_LINKS", "").strip() == "1":
        return False
    os.makedirs(os.path.dirname(link), exist_ok=True)
    if os.path.lexists(link):
        return False
    target = os.path.abspath(source)
    try:
        os.symlink(target, link, target_is_directory=os.path.isdir(target))
    except OSError:
        if not (os.name == "nt" and os.path.isdir(target)):
            return False
        try:
            import _winapi

            _winapi.CreateJunction(target, link)
        except OSError:
            return False
    return _same_tree(target, link)


def _rewrite_exports_for_absolute(exports: dict[str, bytes], prefixes: list[str], skill_root: str) -> dict[str, bytes]:
    absolute = os.path.abspath(skill_root).replace("\\", "/")
    rewritten = {}
    for relative, data in exports.items():
        text = data.decode("utf-8")
        for prefix in prefixes:
            text = text.replace(prefix.replace("\\", "/"), absolute)
        rewritten[relative] = text.encode("utf-8")
    return rewritten


def _same_tree(root: str, path: str) -> bool:
    root_real = os.path.normcase(os.path.realpath(root))
    path_real = os.path.normcase(os.path.realpath(path))
    if path_real == root_real:
        return True
    try:
        return os.path.commonpath([root_real, path_real]) == root_real
    except ValueError:
        return False


def _is_reparse_point(path: str) -> bool:
    if os.path.islink(path):
        return True
    if os.name != "nt" or not os.path.lexists(path):
        return False
    import ctypes

    get_attrs = ctypes.windll.kernel32.GetFileAttributesW
    get_attrs.argtypes = [ctypes.c_wchar_p]
    get_attrs.restype = ctypes.c_uint32
    attrs = get_attrs(path)
    if attrs == 0xFFFFFFFF:
        return False
    return bool(attrs & 0x400)


def _strip_client(client: str, text: str, recorded: str) -> str:
    if client == "codex":
        return strip_managed_toml(text, recorded or None)
    label = "Cursor 配置" if client == "cursor" else "Antigravity 配置"
    return strip_mcp_json(text, recorded or None, label)


def _state_document(layout, clients, python, revision, planned, link_label, verification: dict, existing_state: dict | None = None) -> dict:
    previous = existing_state or {}
    files = dict(previous.get("files") or {})
    entry_hashes = dict(previous.get("entry_hashes") or {})
    selected = set(clients)
    for client in selected:
        entry_hashes.pop(client, None)
        files.pop(CLIENT_CONFIGS[client], None)
        for key in list(entry_hashes):
            if not key.startswith("export:"):
                continue
            relative = key[len("export:"):]
            if _client_owns_export(client, relative):
                entry_hashes.pop(key, None)
                files.pop(relative, None)
    for item in planned:
        relative = os.path.relpath(item["path"], layout.project_root).replace("\\", "/")
        files[relative] = {"sha256": sha256_bytes(item["data"]), "role": item["role"]}
        if item["role"] == "config":
            client = _client_for_path(relative)
            if client:
                entry_hashes[client] = item["entry_hash"]
        elif item["role"] == "export":
            entry_hashes[_export_key(relative)] = sha256_bytes(item["data"])
    merged_clients = [item for item in CLIENTS if item in selected.union(previous.get("clients") or [])]
    return {
        "managed": True,
        "server_name": SERVER_NAME,
        "install_mode": layout.mode,
        "project_id": layout.project_id,
        "skill_root": layout.skill_root,
        "link": link_label,
        "code_version": revision,
        "python": python,
        "clients": merged_clients,
        "files": files,
        "entry_hashes": entry_hashes,
        "write_enabled": False,
        "grants": ["read"],
        "verification": verification.get("verification", "protocol_self_check"),
        "client_connection_verified": False,
        "ac14_live_client_verified": False,
    }


def _client_owns_export(client: str, relative: str) -> bool:
    normalized = relative.replace("\\", "/")
    for root in EXPORT_DIRS[client]:
        if normalized == root or normalized.startswith(root + "/"):
            return True
    return False


def _client_for_path(relative: str) -> str:
    for client, path in CLIENT_CONFIGS.items():
        if relative.replace("\\", "/") == path:
            return client
    return ""


def _export_key(relative: str) -> str:
    return "export:" + relative.replace("\\", "/")


def _state(layout: Layout) -> dict | None:
    path = txn.state_path(layout.data_root)
    if not os.path.isfile(path):
        return None
    data = txn.load_json(path)
    return data


def _assert_registry(layout: Layout, raw: bytes) -> None:
    _assert_clean(raw)
    with tempfile.TemporaryDirectory(prefix="yy-flow-registry-") as directory:
        path = os.path.join(directory, "mcp_registry.json")
        with open(path, "wb") as handle:
            handle.write(raw)
        try:
            registry = load_registry(path)
        except RegistryError as exc:
            raise OnboardError("REGISTRY", "登记文件未能通过现有解析器") from exc
    project = registry.get_project(layout.project_id)
    if project is None or project.write_enabled or project.board_kind != "offline":
        raise OnboardError("REGISTRY", "登记权限或看板类型不符合只读契约")
    if registry.credentials:
        raise OnboardError("SECRET", "登记不得包含凭据")
    scopes = set()
    for principal in registry.principals:
        for granted in principal.grants.values():
            scopes.update(granted)
    if scopes != {"read"} or set(FORBIDDEN_SCOPES).intersection(scopes):
        raise OnboardError("REGISTRY", "登记权限超出 read")


def _assert_clean(raw: bytes) -> None:
    text = raw.decode("utf-8", errors="replace")
    for field in FORBIDDEN_FIELDS:
        if field in text:
            raise OnboardError("SECRET", "生成内容包含禁止字段")
    for key in CREDENTIAL_ENV:
        value = os.environ.get(key, "")
        if isinstance(value, str) and value and value in text:
            raise OnboardError("SECRET", "生成内容包含进程环境中的凭据")


def _guard_path(layout: Layout, path: str) -> None:
    if not within(layout.project_root, path):
        raise OnboardError("PATH", "写入路径跳出项目根，已停止")
    if within(layout.skill_root, path) and os.path.normcase(os.path.realpath(layout.skill_root)) != os.path.normcase(layout.project_root):
        raise OnboardError("PATH", "共享正本不能因当前项目接入写入运行数据")
    if layout.mode == "shared" and within(layout.skill_root, path):
        raise OnboardError("PATH", "共享正本不能因当前项目接入写入运行数据")


def _clients(raw: str) -> list[str]:
    items = [part.strip() for part in (raw or "").split(",") if part.strip()]
    unknown = [item for item in items if item not in CLIENTS]
    if not items or unknown or len(items) != len(set(items)):
        raise OnboardError("USAGE", "clients 只允许 cursor、codex、antigravity，且不能重复")
    return [item for item in CLIENTS if item in set(items)]


def _hook_index(name: str) -> int:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return 0
    if not raw.isdigit() or raw == "0":
        raise OnboardError("USAGE", "故障注入编号无效")
    return int(raw)


def _run_probe(command: list[str]) -> int:
    try:
        proc = subprocess.run(
            command,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=30,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return 1
    return proc.returncode


def _can_resolve(project_root: str) -> bool:
    try:
        resolve_layout(project_root, None)
    except OnboardError:
        return False
    return True


def _public(ok: bool, action: str, layout: Layout, clients, python: str, revision: str, files, verification: str, message: str, extra: dict | None = None) -> dict:
    payload = {
        "ok": ok,
        "action": action,
        "server_name": SERVER_NAME,
        "install_mode": layout.mode,
        "project_id": layout.project_id,
        "project_root": layout.project_root,
        "skill_root": layout.skill_root,
        "data_root": layout.data_root,
        "registry": layout.registry_path,
        "python": python,
        "clients": list(clients),
        "code_version": revision,
        "files": list(files),
        "verification": verification,
        "client_connection_verified": False,
        "ac14_live_client_verified": False,
        "message": message,
    }
    if extra:
        payload["self_check"] = {key: value for key, value in extra.items() if key != "tools"}
        payload["self_check"]["tools"] = extra.get("tools", [])
    return payload


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="准备项目角色、规则和 Cursor、Codex、Antigravity 的项目级 MCP。默认只预览。")
    parser.add_argument("--project-root", required=True)
    parser.add_argument("--skill-root", default=None)
    parser.add_argument("--clients", default="cursor,codex,antigravity")
    parser.add_argument("--python", default=None)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--diagnose", action="store_true", help="只读查看接入状态")
    parser.add_argument("--remove", action="store_true", help="预览受管移除；与 --apply 一起才删除未改动的自身条目")
    parser.add_argument("--recover", action="store_true", help="预览未完成事务的恢复；与 --apply 一起才恢复")
    parser.add_argument("--skip-self-check", action="store_true", help="跳过协议自检。只用于故障注入，不等于真实客户端已连接或 AC-14 通过")
    return parser


def _emit(payload: dict, as_json: bool, stream) -> None:
    if as_json:
        stream.write(json.dumps(payload, ensure_ascii=False, indent=2) + "\n")
    else:
        stream.write(payload.get("message", "") + "\n")
        if payload.get("code"):
            stream.write(payload["code"] + "\n")
        if payload.get("diff"):
            stream.write(payload["diff"] + "\n")
        if payload.get("files"):
            for item in payload["files"]:
                stream.write(str(item) + "\n")
    stream.flush()
