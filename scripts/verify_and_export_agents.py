#!/usr/bin/env python3
"""
通用多 Agent 平台技能挂载与子代理导出引擎 (Universal Agent Mounting & Export Engine)
按声明式配置驱动，自动完成技能目录挂载、规则注册与 8 大专家子代理格式序列化，
并在导出后执行本地格式断言 (Fail-Closed: 解析失败/角色不齐全即 exit(1))。
"""

import os
import sys
import yaml

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, SCRIPT_DIR)

import paths as _paths
from _lib.core.agent_tech_overlay import load_arch_data, apply_tech_stack_to_role

PROJECT_ROOT = _paths.skill_root()
AGENTS_DIR = os.path.join(PROJECT_ROOT, "agents")
CONFIG_PLATFORMS_FILE = os.path.join(PROJECT_ROOT, "config", "agent_platforms.yaml")
TARGET_PROJECT_DIR = _paths.project_root()

ROLES_MAP = {
    "01-pm.yaml": {"id": "flow-pm", "role_code": "pm", "name": "严经理 (项目经理)"},
    "02-architect.yaml": {"id": "flow-architect", "role_code": "architect", "name": "钱架构 (系统架构师)"},
    "03-dev.yaml": {"id": "flow-dev", "role_code": "dev", "name": "李开发 (开发工程师)"},
    "04-reviewer.yaml": {"id": "flow-reviewer", "role_code": "reviewer", "name": "周审查 (代码审查专家)"},
    "05-qa.yaml": {"id": "flow-qa", "role_code": "qa", "name": "章测试 (测试工程师)"},
    "06-docs.yaml": {"id": "flow-docs", "role_code": "docs", "name": "李文通 (文档工程师)"},
    "07-devops.yaml": {"id": "flow-devops", "role_code": "devops", "name": "吕改特 (运维管理员)"},
    "08-frontend.yaml": {"id": "flow-frontend", "role_code": "frontend", "name": "马前端 (前端开发工程师)"},
}

# 各平台官方工具名映射: 导出给对应平台的 subagent 必须使用该平台认识的工具标识
PLATFORM_TOOLS = {
    "claude_code": ["Bash", "Edit", "Read", "Write", "Grep", "Glob"],
    "antigravity": ["run_command", "replace_file_content", "write_to_file", "view_file", "list_dir", "grep_search"],
    "cursor": ["Read", "Edit", "Write", "Bash", "Grep", "Glob"],
    "opencode": ["bash", "edit", "read", "write", "grep", "glob"],
    "zcode": ["run_command", "replace_file_content", "write_to_file", "view_file", "list_dir", "grep_search"],
}
DEFAULT_TOOLS = ["run_command", "replace_file_content", "write_to_file", "view_file", "list_dir", "grep_search"]
WRITE_TOOL_NAMES = {
    "edit", "write", "replace_file_content", "write_to_file",
}
RUNNER_MANAGED_AGENT_IDS = {
    "BUILDER": "flow-runner-builder",
    "REVIEWER": "flow-runner-reviewer",
    "QA": "flow-runner-qa",
}
RUNNER_BUILDER_ID = RUNNER_MANAGED_AGENT_IDS["BUILDER"]


def serialize_runner_builder(platform_key, subagent_spec):
    """Antigravity-only managed Builder profile with no terminal capability."""
    tools = [tool for tool in PLATFORM_TOOLS.get(platform_key, DEFAULT_TOOLS)
             if tool.lower() != "run_command"]
    body = """# Production Runner 托管 Builder

这是 flow-dev 的 Runner 专用执行配置，不是第九个业务角色。

- 只读取和修改分配工作区内的业务源码与测试文件。
- 不得调用 run_command、Shell、Git、测试命令、包管理器或子进程。
- 不得建卡、修改看板、生成 Evidence 或执行 Reviewer/QA/用户验收。
- 完成文件修改后直接返回修改摘要；Git 检查、测试及候选 Commit 由 Runner 受控执行。
- 若文件工具被拒绝，立即返回阻断，不得修改全局权限或尝试绕过。
"""
    fm = yaml.dump({
        "name": RUNNER_BUILDER_ID,
        "description": "yy-flow Production Runner 专用无终端 Builder",
        "tools": tools,
        "enable_write_tools": True,
        "subagent": True,
    }, allow_unicode=True, sort_keys=False)
    return f"---\n{fm}---\n\n{body}"


def serialize_runner_readonly_role(platform_key, role_code, role_data):
    """Antigravity Runner-managed Reviewer/QA profile without a terminal.

    The profile is derived from the corresponding business-role YAML so its
    professional duties remain aligned, while orchestration and command
    execution stay exclusively owned by Production Runner.
    """
    role_code = role_code.upper()
    if role_code not in ("REVIEWER", "QA"):
        raise ValueError(f"Unsupported managed read-only role: {role_code}")
    agent_id = RUNNER_MANAGED_AGENT_IDS[role_code]
    # Runner already supplies a complete diff/impact bundle. Managed reviewers
    # only need deterministic reads of files explicitly named by that bundle;
    # broad directory/search tools can wander into user-global configuration.
    tools = [] if role_code == "QA" else [
        tool for tool in PLATFORM_TOOLS.get(platform_key, DEFAULT_TOOLS)
        if tool.lower() in {"view_file", "read"}
    ]
    responsibility_items = list(role_data.get("responsibilities") or [])
    if role_code == "QA":
        responsibility_items = [
            str(item).replace("测试用例执行", "核验 Runner 提供的测试用例执行结果")
            for item in responsibility_items
        ]
    duties = _as_bullets(responsibility_items)
    audit_rules = []
    for item in list(role_data.get("orchestration_rules") or []):
        text = str(item)
        if "check_secrets.py" in text:
            audit_rules.append("必须核对 Runner 提供的候选级安全扫描命令、固定 SHA、退出码和输出摘要")
        elif any(token in text for token in ("transition_task.py", "run_command", "--end-time")):
            continue
        else:
            if role_code == "QA":
                text = text.replace(
                    "PASS 必须返回完整验收覆盖矩阵、受控命令结果、反向场景和零未覆盖风险",
                    "PASS 必须核验 Runner 受控命令结果，并返回完整验收覆盖矩阵、反向场景和零未覆盖风险",
                )
            audit_rules.append(text)
    redlines = _as_bullets(audit_rules)
    verdict = "PASS/REJECT" if role_code == "REVIEWER" else "PASS/FAIL"
    stage = "审查" if role_code == "REVIEWER" else "测试"
    if role_code == "QA":
        access_rules = """- QA 的全部差异、测试、构建、安全扫描和验收矩阵证据均由 Runner 内联提供；不得调用任何工具。
- 输出只包含 decision、acceptance_coverage、negative_scenarios、uncovered_risks、defects、summary 六个判断字段；会话身份、候选 SHA 和受控测试记录由 Runner 绑定，不能自行编造。
- 每条验收项和负向场景都必须显式填写 status 为 PASS 或 FAIL；不得用 COVERED/OK 代替结论，不得省略负向场景状态。证据引用具体测试或文件，避免重复长篇说明；evidence 建议不超过 600 字符，summary 不超过 400 字符。
- 不得自行读取文件、列举目录、发起搜索或扩大证据范围；证据不足时直接返回否定结论。
- 不得修改任何文件。"""
        denial_rule = "- 若内联证据不足，返回结构化 FAIL；不得请求权限、修改全局配置或尝试绕过。"
    else:
        access_rules = """- 只能只读查看 Runner 明确指定且位于本次工作区根目录内的候选文件或完整差异工件；不得修改任何文件。
- 所有读取必须使用工作区内相对路径；禁止绝对路径、`..`、符号链接/目录联接逃逸，以及读取用户主目录、`.gemini`、`.codex`、其他仓库或任何工作区外路径。
- 不得自行列举目录或发起全局搜索。证据不足时直接返回否定结论，不得通过扩大读取范围补证。"""
        denial_rule = "- 若读取工具被拒绝，立即返回阻断；不得修改全局权限、创建诊断脚本或尝试绕过。"
    body = f"""# Production Runner 托管 {role_code}

这是 `{role_data.get('name', role_code)}` 的 Runner 专用只读执行配置，不是新增业务角色。

## 专业职责
{duties}

## 角色约束
- 只对 Runner 固定的 baseline SHA、candidate SHA、契约快照和证据包执行{stage}判断。
{access_rules}
- 不得调用 run_command、Shell、Git、测试命令、包管理器、浏览器或子进程。
- 不得建卡、修改看板、写入 Evidence 或推进状态；这些动作由 Runner 原子执行。
- Runner 提供的安全扫描、测试、构建或差异检查未执行、失败或证据不足时，必须拒绝通过。
- 只返回符合 Runner JSON Schema 的 {verdict} 结构化结论；不得用自然语言包装 JSON。
{denial_rule}

## 业务角色审计要求（命令执行条款已转换为证据核验）
{redlines}
"""
    fm = yaml.dump({
        "name": agent_id,
        "description": f"yy-flow Production Runner 专用无终端 {role_code}",
        "tools": tools,
        "enable_write_tools": False,
        "subagent": True,
    }, allow_unicode=True, sort_keys=False)
    return f"---\n{fm}---\n\n{body}"


def _as_bullets(value):
    """把 YAML 列表或标量稳定渲染为 Markdown 列表。"""
    if isinstance(value, list):
        return "\n".join(f"- {item}" for item in value)
    if value in (None, ""):
        return "- （未声明）"
    return f"- {value}"


def _role_contract(role_data, role_meta, py_cmd, script_prefix):
    """仅依据角色 YAML 生成职责、权限和状态契约，禁止通用 SOP 覆盖角色边界。"""
    role_code = str(
        role_data.get("role_code")
        or role_data.get("role")
        or role_meta.get("role_code", "")
    ).upper()
    transitions = role_data.get("allowed_transitions")
    if not isinstance(transitions, list) or not transitions:
        raise ValueError(f"{role_meta['id']} 缺少 allowed_transitions，拒绝导出")

    boundaries = role_data.get("boundaries")
    if not isinstance(boundaries, dict):
        raise ValueError(f"{role_meta['id']} 缺少 boundaries，拒绝导出")

    transition_lines = []
    command_lines = []
    for transition in transitions:
        text = str(transition).strip()
        if "->" not in text:
            raise ValueError(f"{role_meta['id']} 存在非法状态流转声明: {text}")
        from_status, remainder = (part.strip() for part in text.split("->", 1))
        to_status = remainder.split("(", 1)[0].strip()
        if not from_status or not to_status:
            raise ValueError(f"{role_meta['id']} 存在非法状态流转声明: {text}")
        transition_lines.append(f"- {text}")
        command_lines.append(
            f"- `{py_cmd} {script_prefix}/transition_task.py --role {role_code} "
            f"--from-status {from_status} --to-status {to_status} "
            "--task-id <TASK_ID> --assignee <下一处理人>`"
        )

    if "can_transition_task" not in boundaries:
        raise ValueError(f"{role_meta['id']} 缺少 boundaries.can_transition_task，拒绝导出")
    can_transition_task = boundaries["can_transition_task"]
    if not isinstance(can_transition_task, bool):
        raise ValueError(f"{role_meta['id']} 的 boundaries.can_transition_task 必须是布尔值")
    permission_lines = [
        f"- 可运行 CLI：{bool(boundaries.get('can_run_cli', False))}",
        f"- 可写领域文件：{bool(boundaries.get('can_write_domain_files', False))}",
        f"- 可修改业务代码：{bool(boundaries.get('can_modify_business_code', False))}",
        f"- 可执行用户验收：{bool(boundaries.get('can_approve', False))}",
        f"- 可自行领取任务：{bool(boundaries.get('can_self_claim', False))}",
        f"- 可直接落库任务状态：{can_transition_task}",
    ]
    if can_transition_task:
        transition_execution = (
            "获得任务状态锁后，可使用以下 CLI 模板执行允许的流转：\n"
            + "\n".join(command_lines)
        )
    else:
        transition_execution = (
            "本角色不得直接调用 transition_task.py 或其他看板写入命令。"
            "只返回绑定 task_id、候选 SHA、会话身份和 PASS/REJECT（或 PASS/FAIL）的结构化结论；"
            "由 Production Runner 或主协调者核验后执行状态落库。"
        )
    return (
        role_code,
        transitions,
        "\n".join(transition_lines),
        transition_execution,
        "\n".join(permission_lines),
    )


def _platform_tools(platform_key, can_write):
    tools = list(PLATFORM_TOOLS.get(platform_key, DEFAULT_TOOLS))
    if can_write:
        return tools
    return [tool for tool in tools if tool.lower() not in WRITE_TOOL_NAMES]

def load_platforms_config():
    """读取声明式平台配置"""
    if os.path.exists(CONFIG_PLATFORMS_FILE):
        with open(CONFIG_PLATFORMS_FILE, "r", encoding="utf-8") as fp:
            return yaml.safe_load(fp)
    return {"default_skill_name": "yy-flow", "platforms": {}}

def safe_symlink(source_dir, target_link_path, relative=True):
    """
    跨平台安全软链接创建：
    具备 Windows OSError (WinError 1314 权限不足) 优雅降级回退机制
    relative=False 用于全局挂载（跨卷绝对链更稳健）
    """
    parent = os.path.dirname(target_link_path)
    os.makedirs(parent, exist_ok=True)

    if os.path.islink(target_link_path) or os.path.exists(target_link_path):
        if os.path.islink(target_link_path):
            os.unlink(target_link_path)
        else:
            return False  # 已存在实体目录/文件，跳过

    try:
        link_source = os.path.relpath(source_dir, parent) if relative else os.path.abspath(source_dir)
        os.symlink(link_source, target_link_path)
        return True
    except (OSError, NotImplementedError):
        # Windows / 受限环境回退：如果无法创建 symlink，创建 Junction 或提示
        if sys.platform == "win32":
            cmd = f'mklink /J "{target_link_path}" "{source_dir}"'
            if os.system(cmd) == 0:
                return True
        return False

def detect_active_platforms(platforms_config, global_mode=False):
    """动态感知当前被激活的 Agent 平台。
    global_mode: 用各平台用户级目录（global_detect_dirs，如 ~/.claude）探测，
    且仅保留声明了 global_skill_target 的平台。"""
    env = os.environ
    active = []

    for platform_key, spec in platforms_config.get("platforms", {}).items():
        if global_mode:
            g_dirs = spec.get("global_detect_dirs", [])
            if not spec.get("global_skill_target"):
                continue  # 无全局挂载目标的平台不参与全局模式
            if any(os.path.exists(os.path.expanduser(d)) for d in g_dirs):
                active.append(platform_key)
            continue

        # Universal 始终作为通用标准兜底激活
        if platform_key == "universal":
            active.append(platform_key)
            continue

        detect_dirs = spec.get("detect_dirs", [])
        is_dir = any(os.path.exists(os.path.join(TARGET_PROJECT_DIR, d)) for d in detect_dirs)
        is_env = any(k in env for k in [f"{platform_key.upper()}_ENV", f"{platform_key.upper()}_CLI"])

        if is_dir or is_env:
            active.append(platform_key)

    return list(dict.fromkeys(active))

def serialize_subagent(role_data, role_meta, platform_key, subagent_spec, skill_target=None):
    """根据目标平台格式要求，将角色定义序列化为对应格式的子代理指令文件"""
    agent_id = role_meta["id"]
    agent_name = role_meta["name"]
    role_code = role_data.get("role_code") or role_data.get("role") or role_meta.get("role_code", "")
    fmt = subagent_spec.get("format", "markdown_frontmatter")
    use_frontmatter = subagent_spec.get("frontmatter_subagent", True)

    core_duties = role_data.get("core_duties") or role_data.get("responsibilities", [])
    duty_str = _as_bullets(core_duties)
    redlines = role_data.get("redlines") or role_data.get("orchestration_rules", [])
    redline_str = _as_bullets(redlines)

    # 动态确定脚本执行路径前缀，确保宿主项目下直接执行有效
    if skill_target:
        script_prefix = f"{skill_target}/scripts"
    elif platform_key == "antigravity":
        script_prefix = ".agents/skills/yy-flow/scripts"
    elif platform_key == "claude_code":
        script_prefix = ".claude/skills/yy-flow/scripts"
    elif os.path.basename(_paths.resolve_data_root()) == ".yy-flow":
        script_prefix = ".yy-flow/skill/scripts"
    else:
        script_prefix = "scripts"

    py_cmd = "python" if sys.platform == "win32" else "python3"

    role_code, _, transition_str, transition_execution, permission_str = _role_contract(
        role_data, role_meta, py_cmd, script_prefix
    )
    boundaries = role_data["boundaries"]
    can_write = bool(boundaries.get("can_write_domain_files", False))
    tools = _platform_tools(platform_key, can_write)

    # 1. 角色专属状态机 SOP。所有内容来自角色 YAML，不再给各角色套用同一三步闭环。
    sop_prompt = f"""# 角色定义：{agent_name} ({agent_id})

## 核心职责
{duty_str}

## 协作规约与红线
{redline_str}

## 权限边界
{permission_str}

## 角色专属状态流转 SOP
本角色只允许执行角色源文件声明的以下流转：
{transition_str}

状态落库所有权：
{transition_execution}

PM 建卡规则：
{f'- `{py_cmd} {script_prefix}/transition_task.py --role PM --create --task-name "<任务名称>" --assignee <负责人>`' if role_code == 'PM' else '- 本角色不得代替 PM 创建 A 类开发任务。'}

执行铁律：
1. 开始工作前必须读取任务当前状态；状态不匹配上述任一来源状态时立即 Fail-Closed。
2. 仅执行本角色核心职责，不得在同一会话中改扮其他角色，也不得越过中间角色或并行启动存在先后依赖的角色。
3. 状态流转描述是允许的结果契约，不自动授予状态写入权；必须服从“状态落库所有权”，不得拼接通用状态链或代行用户验收。
4. 文件写入和业务代码修改必须同时满足本节权限边界；只读角色即使可运行测试或审查命令，也不得修改受跟踪文件或创建 Commit。
5. 【完工硬门禁】：L0 纯文本即时问答可免建卡；L1/L2 工作必须在任务卡和角色状态契约内执行，交付后只能推进到本角色允许的目标状态。
"""

    # 2. 格式 A: Codex 官方 TOML 格式
    if fmt == "codex_toml":
        # 转义三引号
        safe_instructions = sop_prompt.replace('"""', '\\"\\"\\"')
        toml_content = f"""name = "{agent_id}"
description = "multi-agent-flow 中的 {agent_name} 专家子代理"
developer_instructions = \"\"\"
{safe_instructions}
\"\"\"
"""
        return toml_content

    # 3. 格式 B: 标准 Markdown + YAML Frontmatter 格式
    if use_frontmatter:
        fm_dict = {
            "name": agent_id,
            "description": f"multi-agent-flow 中的 {agent_name} 专家子代理",
            "tools": tools,
            "enable_write_tools": can_write,
            "subagent": True if platform_key == "antigravity" else None,
        }
        # 移除 None
        fm_dict = {k: v for k, v in fm_dict.items() if v is not None}
        fm_yaml = yaml.dump(fm_dict, allow_unicode=True, sort_keys=False)
        return f"---\n{fm_yaml}---\n\n{sop_prompt}"
    else:
        return sop_prompt

def verify_exported_agent(out_abs_path, fmt):
    """
    本地格式断言 (Fail-Closed):
    - markdown_frontmatter: frontmatter 必须可被 yaml.safe_load 解析, 且 name/description 非空
    - codex_toml: 必须可被 tomllib 解析, 且 name/description 非空
    返回 (ok: bool, err: str)
    """
    try:
        with open(out_abs_path, "r", encoding="utf-8") as fp:
            content = fp.read()
    except Exception as e:
        return False, f"读取失败: {e}"

    if fmt == "codex_toml":
        try:
            import tomllib
        except ImportError:
            # Python < 3.10 无 tomllib: 降级为关键行存在性断言
            has_name = any(line.startswith("name =") for line in content.splitlines())
            has_desc = any(line.startswith("description =") for line in content.splitlines())
            if has_name and has_desc:
                return True, ""
            return False, "TOML 缺少 name/description 声明行"
        try:
            data = tomllib.loads(content)
        except Exception as e:
            return False, f"TOML 解析失败: {e}"
        if not data.get("name") or not data.get("description"):
            return False, "TOML 缺少 name/description 字段"
        return True, ""

    # markdown + frontmatter
    if not content.startswith("---"):
        return False, "缺少 YAML frontmatter 起始标头"
    parts = content.split("---", 2)
    if len(parts) < 3:
        return False, "frontmatter 结构不完整"
    try:
        fm = yaml.safe_load(parts[1])
    except Exception as e:
        return False, f"frontmatter YAML 解析失败: {e}"
    if not isinstance(fm, dict):
        return False, "frontmatter 不是合法映射"
    if not fm.get("name") or not fm.get("description"):
        return False, "frontmatter 缺少 name/description 字段"
    return True, ""

def export_platform_assets(platforms_config, active_platforms, global_mode=False):
    """执行跨平台 Skill 挂载与 Subagent 导出，返回导出成败统计。

    导出时把项目技术栈（user_data/project_architecture.config.yaml）覆盖到
    内存中的角色定义——agents/*.yaml 模板保持只读。

    global_mode=True: 挂载各平台用户级全局技能目录（global_skill_target，绝对链）；
    Subagent 导出走 user_pattern（用户级）；技术栈覆盖在全局模式下禁用
    （项目个性化产物不得跨项目泄漏）。
    """
    skill_name = platforms_config.get("default_skill_name", "yy-flow")
    platforms = platforms_config.get("platforms", {})
    missing_agents = []
    verify_failures = []
    total_exported = 0

    arch_data = None if global_mode else load_arch_data()
    if global_mode:
        print("[GLOBAL] 全局共享安装模式：Subagent 导出为通用版（不含项目技术栈，防跨项目泄漏）")
        legacy_agent_dir = os.path.expanduser("~/.gemini/config/skills-agents")
        if os.path.exists(legacy_agent_dir):
            print(f"[MIGRATION] 检测到旧版全局 Agent 路径: {legacy_agent_dir}")
            print("请手动清理或迁移，系统不静默删除用户文件。")
    elif arch_data:
        proj = (arch_data.get("project") or {}).get("name", "未知")
        print(f"[SYNC]  检测到已初始化架构配置，导出时合并项目技术栈: 【{proj}】")
    else:
        print("[NOTE]  架构配置未初始化，导出使用通用模板职责（首次 init 属正常，Step 6 会重导出）")

    print(f"[SCAN]  已自动侦测到当前激活/兼容平台: {active_platforms}")

    for p_key in active_platforms:
        if p_key not in platforms:
            continue
        spec = platforms[p_key]
        p_name = spec.get("name", p_key)

        # 1. 执行 Skill 目录挂载（全局模式消费 global_skill_target）
        if global_mode:
            g_target_tpl = spec.get("global_skill_target")
            if g_target_tpl:
                abs_target = os.path.expanduser(g_target_tpl.format(skill_name=skill_name))
                if safe_symlink(PROJECT_ROOT, abs_target, relative=False):
                    print(f"[SUCCESS]  [{p_name}] 全局挂载 Skill -> {abs_target}")
                else:
                    print(f"[WARN]  [{p_name}] 全局挂载跳过 (目标已存在实体路径或无法创建软链) -> {abs_target}")
        elif "skill_target" in spec:
            rel_target = spec["skill_target"].format(skill_name=skill_name)
            abs_target = os.path.join(TARGET_PROJECT_DIR, rel_target)
            if safe_symlink(PROJECT_ROOT, abs_target):
                print(f"[SUCCESS]  [{p_name}] 成功挂载 Skill 发现路径 -> {rel_target}")
            else:
                print(f"[WARN]  [{p_name}] Skill 挂载跳过 (目标已存在实体路径或无法创建软链) -> {rel_target}")

        # 2. 执行 Cursor MDC 规则挂载（仅项目级模式有意义）
        if not global_mode and spec.get("mount_type") == "cursor_mdc" and "rule_target" in spec:
            rel_rule = spec["rule_target"].format(skill_name=skill_name)
            abs_rule = os.path.join(TARGET_PROJECT_DIR, rel_rule)
            os.makedirs(os.path.dirname(abs_rule), exist_ok=True)
            with open(abs_rule, "w", encoding="utf-8") as fp:
                fp.write(
                    "---\n"
                    "description: yy-flow 多专家协同研发工作流规则\n"
                    "globs: [\"*\"]\n"
                    "alwaysApply: true\n"
                    "---\n"
                    "# YY-Flow Multi-Agent Workflow\n\n"
                    "在 Cursor 会话中开发或解决任务时，主 Agent 必须担任调度中枢，遵循以下流转闭环：\n"
                    "1. L1/L2 研发任务强制开工门禁：通过 `python scripts/quick_task.py create` 建立任务卡，并推至【进行中】；\n"
                    "2. 串行调度对应专家子代理（`.cursor/agents/`）：\n"
                    "   - 开发实现：`@flow-dev` (李开发)\n"
                    "   - 凭证扫描与审查：`@flow-reviewer` (周审查，只读)\n"
                    "   - 测试验证与矩阵覆盖：`@flow-qa` (章测试，只读)\n"
                    "   - 交付与验收推进：`@flow-pm` (严经理)\n"
                    "3. 状态落库必须经由 `python scripts/transition_task.py` 严格校验；\n"
                    "4. 详见 `.cursor/rules/yy-flow-orchestrator.mdc` 与 `skills/multi-agent-flow/SKILL.md`。\n"
                )
            print(f"[SUCCESS]  [{p_name}] 成功创建 MDC 规则 -> {rel_rule}")

            orch_rule = os.path.join(TARGET_PROJECT_DIR, ".cursor", "rules", "yy-flow-orchestrator.mdc")
            with open(orch_rule, "w", encoding="utf-8") as fp:
                fp.write(
                    "---\n"
                    "description: yy-flow 多专家全自动协同流转编排规约 (Cursor Autonomous Multi-Agent Orchestration)\n"
                    "globs: [\"*\"]\n"
                    "alwaysApply: true\n"
                    "---\n\n"
                    "# yy-flow Cursor 对话自主多专家流转编排规约 (Autonomous Orchestrator)\n\n"
                    "当用户在 Cursor 会话中提出任务、需求、缺陷修复或功能开发请求时，当前主 Agent 自动担任全局编排主控 (Autonomous Orchestrator)，遵循本规约无人值守自主推进全流程。\n\n"
                    "## 一、核心原则：中枢调度与自主推进\n"
                    "1. 中枢受控调度 (Hub-and-Spoke)：Cursor 当前子代理机制不支持 P2P 点对点自主转交。主 Agent 必须作为唯一的中枢调度器，按照看板状态机顺序串行派发任务给对应专家子代理（或以专家身份执行），严禁跳过审查与测试节点。\n"
                    "2. 闭环推进不中断 (Autonomous Continuation)：除非遇到不可解决的冲突、致命报错或最终需要人类用户验收，主 Agent 必须自动连续执行各阶段动作，严禁在中间阶段向人类询问“是否继续”。\n\n"
                    "## 二、任务分级与开工门禁\n"
                    "- L0 咨询/只读排查：无需建卡，直接回答或使用只读工具分析。\n"
                    "- L1/L2 研发任务（代码变更、Bug 修复、新功能、重构）：强制开工门禁：\n"
                    "  1. 动态识别任务类型与责任人：后端/通用为 A 类/李开发，前端为 A 类/马前端，架构为 B 类/钱架构，文档为 C 类/李文通，运维为 D 类/吕改特；\n"
                    "  2. 检查 `user_data/board.json` 是否已有对应卡片；若无卡片通过 CLI 建卡：\n"
                    "     `python scripts/quick_task.py create --name \"<任务名称>\" --role PM --assignee <负责人> --type <类型> --force`\n"
                    "  3. 获取新建任务 ID，并推进至【进行中】：\n"
                    "     `python scripts/transition_task.py --role DEV --from-status 待开始 --to-status 进行中 --task-id <TASK_ID> --assignee <负责人>`\n\n"
                    "## 三、标准流转流水线\n"
                    "1. 开发实现：由分派的专家子代理实施修改并确认 diff；\n"
                    "2. 代码审查 (REVIEWER - @flow-reviewer)：状态推至【审查中】；运行 `python scripts/check_secrets.py`，只读审查 diff；若有缺陷通过 `transition_task.py` 退回至合法状态【已退回】（禁止使用不存在的状态），PASS 则流转至【测试中】；\n"
                    "3. 测试验证 (QA - @flow-qa)：运行 `pytest`，核验验收矩阵；若失败同样退回至【已退回】，PASS 则流转至【已完成】；\n"
                    "4. 完工交付 (PM - @flow-pm)：核验交付物并总结，停在【已完成】等待用户人工进行【已验收】核验。\n"
                )
            print(f"[SUCCESS]  [{p_name}] 成功创建 MDC 编排规约 -> .cursor/rules/yy-flow-orchestrator.mdc")

        # 3. 执行 Subagent 导出（全局模式：仅 user_pattern；项目模式：pattern）
        subagent_spec = spec.get("subagent_export")
        pattern = None
        if global_mode:
            pattern = (subagent_spec or {}).get("user_pattern")
            if not pattern:
                print(f"[SKIP]  [{p_name}] 无用户级 Subagent 导出路径声明，跳过全局导出")
                continue
        elif subagent_spec and subagent_spec.get("pattern"):
            pattern = subagent_spec["pattern"]

        if not pattern:
            continue

        fmt = (subagent_spec or {}).get("format", "markdown_frontmatter")
        exported_count = 0

        for yaml_file, role_meta in sorted(ROLES_MAP.items()):
            yaml_path = os.path.join(AGENTS_DIR, yaml_file)
            if not os.path.exists(yaml_path):
                missing_agents.append(f"[{p_key}] {yaml_file}")
                continue

            with open(yaml_path, "r", encoding="utf-8") as fp:
                role_data = yaml.safe_load(fp)

            # 导出时覆盖项目技术栈（纯内存操作，agents/*.yaml 不落盘不改写）
            role_key = role_meta.get("role_code", "")
            role_data = apply_tech_stack_to_role(role_data, arch_data, role_key)

            st_val = spec.get("skill_target", "").format(skill_name=skill_name) if spec.get("skill_target") else None
            out_content = serialize_subagent(role_data, role_meta, p_key, subagent_spec or {}, skill_target=st_val)
            out_rel_path = pattern.format(agent_id=role_meta["id"])
            if global_mode:
                out_abs_path = os.path.expanduser(out_rel_path)
            else:
                out_abs_path = os.path.join(TARGET_PROJECT_DIR, out_rel_path)

            os.makedirs(os.path.dirname(out_abs_path), exist_ok=True)
            with open(out_abs_path, "w", encoding="utf-8") as fp:
                fp.write(out_content)
            exported_count += 1
            total_exported += 1

            # 导出后本地格式断言
            ok, err = verify_exported_agent(out_abs_path, fmt)
            if not ok:
                verify_failures.append(f"[{p_key}] {out_rel_path}: {err}")

        # Managed execution profiles are not additional business roles. They
        # exist only on Antigravity surfaces, where headless terminal calls may
        # be elevated before a user can approve them.
        if p_key in ("antigravity", "antigravity_cli"):
            managed_contents = {
                RUNNER_BUILDER_ID: serialize_runner_builder(p_key, subagent_spec or {}),
            }
            for yaml_file, role_code in (("04-reviewer.yaml", "REVIEWER"), ("05-qa.yaml", "QA")):
                with open(os.path.join(AGENTS_DIR, yaml_file), "r", encoding="utf-8") as fp:
                    managed_role_data = yaml.safe_load(fp)
                managed_contents[RUNNER_MANAGED_AGENT_IDS[role_code]] = serialize_runner_readonly_role(
                    p_key, role_code, managed_role_data
                )
            for managed_id, managed_content in managed_contents.items():
                runner_rel = pattern.format(agent_id=managed_id)
                runner_abs = os.path.expanduser(runner_rel) if global_mode else os.path.join(TARGET_PROJECT_DIR, runner_rel)
                os.makedirs(os.path.dirname(runner_abs), exist_ok=True)
                with open(runner_abs, "w", encoding="utf-8") as fp:
                    fp.write(managed_content)
                total_exported += 1
                ok, err = verify_exported_agent(runner_abs, fmt)
                if not ok:
                    verify_failures.append(f"[{p_key}] {runner_rel}: {err}")

        print(f"[SUCCESS]  [{p_name}] 成功导出专家子代理 ({exported_count}/8) -> 模式: `{pattern}`")

    return missing_agents, verify_failures, total_exported

def main():
    import argparse
    parser = argparse.ArgumentParser(description="跨平台 Skill 挂载与 Subagent 导出引擎")
    parser.add_argument("--global", dest="global_mode", action="store_true",
                        help="全局共享安装模式：挂载各宿主用户级技能目录（global_skill_target）")
    parser.add_argument("--target-project-dir", default=None,
                        help="宿主项目目录（默认: 当前工作目录）")
    args = parser.parse_args()

    global TARGET_PROJECT_DIR
    if args.target_project_dir:
        TARGET_PROJECT_DIR = os.path.abspath(args.target_project_dir)

    platforms_config = load_platforms_config()
    active_platforms = detect_active_platforms(platforms_config, global_mode=args.global_mode)

    mode_label = "全局共享安装 (user-level mount)" if args.global_mode else "项目级挂载"
    print("==============================================================================")
    print(f"[START] [Multi-Agent Flow] 跨平台 Skill 自动挂载与 Subagent 导出 · {mode_label}")
    print("==============================================================================")

    missing_agents, verify_failures, total_exported = export_platform_assets(
        platforms_config, active_platforms, global_mode=args.global_mode)

    if missing_agents:
        for m in missing_agents:
            print(f"[ERROR] 角色定义文件缺失: {m}")
    if verify_failures:
        for f_ in verify_failures:
            print(f"[ERROR] 导出格式断言失败: {f_}")

    # Fail-Closed 计数：global 模式只对声明了 user_pattern 的平台有导出预期
    if args.global_mode:
        exportable = [p for p in active_platforms
                      if (platforms_config.get("platforms", {}).get(p, {})
                          .get("subagent_export", {}) or {}).get("user_pattern")]
    else:
        exportable = [p for p in active_platforms
                      if platforms_config.get("platforms", {}).get(p, {}).get("subagent_export")]

    # global 模式下零平台被探测到 = 没有任何宿主已安装 → Fail-Closed（静默成功更糟）
    if args.global_mode and not active_platforms:
        print("==============================================================================")
        print("[FAILED] 全局模式未探测到任何已安装的 Agent 宿主（各平台用户级目录均不存在）！")
        print("         请先安装至少一个宿主（Claude Code / Codex / Antigravity），或用项目级模式。")
        print("==============================================================================")
        sys.exit(1)

    if missing_agents or verify_failures or (exportable and total_exported == 0):
        print("==============================================================================")
        print("[FAILED] Subagent 导出未通过完整性校验，初始化阻断 (Fail-Closed)！")
        print("==============================================================================")
        sys.exit(1)

    print("==============================================================================")
    print(f"[SUCCESS] 全平台 Skill 挂载与专家子代理序列化就绪 (共导出 {total_exported} 份，本地格式断言全部通过)！")
    print("==============================================================================")

if __name__ == "__main__":
    main()
