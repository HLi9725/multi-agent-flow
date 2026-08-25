---
title: Multi-Agent Flow 多客户端可信化与生态改造实施方案
module: multi-agent-flow
stage: Phase-2
type: design-spec
status: active
author: User / Codex
updated_at: 2026-08-24
tags: [多客户端, 多Agent, 可信化, 实施计划]
---

# Multi-Agent Flow 多客户端可信化与生态改造实施方案

> 文档状态：Active / 第一阶段已验收，第二阶段待分批批准
> 适用仓库：`YuanYii/multi-agent-flow`  
> 目标客户端：ChatGPT、OpenAI Codex、Google Antigravity  
> 制定日期：2026-08-24  
> 实施原则：先可信、再真实多 Agent、再降复杂度、最后扩生态

## 1. 文档目的

本方案用于指导 `multi-agent-flow` 从“以提示词和本地状态机为主的流程 Skill”演进为：

1. 默认安全、可审计、不会伪造完成状态的工作流工具；
2. 能通过宿主原生能力运行真实子 Agent 的多客户端适配层；
3. 能根据任务风险选择 Lite、Standard、Compliance 流程；
4. 全局安装能力、按项目隔离数据，不在不同项目间串任务；
5. 后续可通过远程 MCP Server 和 Plugin/App 接入 ChatGPT Web、Codex、Antigravity及外部系统。

本文是实施依据，不代表所有阶段已经完成。第一阶段已于 2026-08-24 经用户明确验收并冻结；第二至第四阶段进入编码前仍需用户按批次确认，最终进入“已验收”状态必须再次由用户确认。

## 2. 当前基线

截至本文制定时，仓库大约包含 7,410 行 Python 代码和 217 个测试函数。当前测试无法完成收集，直接原因是 `scripts/start_kanban_server.py` 使用了 `Any`、`Optional`，但没有从 `typing` 导入：

```text
NameError: name 'Any' is not defined
```

当前架构已有以下基础：

- 状态流转、角色权限、审计、离线看板和外部看板 Adapter；
- `YY_FLOW_PROJECT_ROOT`、`.yy-flow` 与 CWD 路径解析；
- 全局共享 Skill 与项目运行数据分离的初步设计；
- Codex、Antigravity等平台的 Agent 定义导出；
- L0/L1/L2 与 A-G 类型流程；
- 阶段门禁和 Git 工作区检查。

当前关键缺口：

- `/auto` 能生成总结并推进状态，但没有执行真实开发、审查和测试；
- 子 Agent 调用写死或隐含依赖特定宿主能力；
- Reviewer、QA 的“角色不同”尚不等于“独立上下文真实运行”；
- 状态变更没有统一、可校验的证据对象；
- 项目隔离仍可能静默回退 CWD，缺少稳定的 `project_id`；
- 看板删除为物理删除；
- ChatGPT Web 不能直接使用本地脚本，需要 Plugin + 远程 MCP；
- 现有部分全局 Skill/Agent 路径与客户端现行规范不一致。

## 3. 官方能力边界与兼容目标

### 3.1 OpenAI ChatGPT 与 Codex

根据 [OpenAI Build skills 文档](https://learn.chatgpt.com/docs/build-skills)，ChatGPT 和 Codex 都可以显式或隐式激活 Skill；Codex 会从项目 `.agents/skills` 和用户 `$HOME/.agents/skills` 等位置发现本地 Skill。可复用分发及连接器场景应优先使用 Plugin。

根据 [OpenAI Subagents 文档](https://learn.chatgpt.com/docs/agent-configuration/subagents)，ChatGPT Work 与 Codex 支持子 Agent 工作流；Codex 的个人自定义 Agent 位于 `~/.codex/agents/`，项目自定义 Agent 位于 `.codex/agents/`。每个子 Agent 会进行独立模型和工具工作，因此不能把“角色文本切换”当成真实子 Agent。

根据 [OpenAI Plugin architecture 文档](https://developers.openai.com/plugins/concepts/plugins)，Plugin 可以组合 Skill、MCP Server 和可选 UI。ChatGPT Web/Work 的跨设备接入应在第四阶段通过该机制完成，不能承诺仅安装本地 Python Skill 后即可在所有 ChatGPT Web 对话中运行。

### 3.2 Google Antigravity

根据 [Antigravity Skills 文档](https://antigravity.google/docs/skills/)，工作区 Skill 位于 `.agents/skills/<skill>/`，IDE/Desktop 全局 Skill 位于 `~/.gemini/config/skills/<skill>/`。

根据 [Antigravity Subagents 文档](https://antigravity.google/docs/subagents)，自定义 Agent 的工作区路径为 `.agents/agents/<name>.md` 或 `.agents/agents/<name>/agent.md`，全局路径为 `~/.gemini/config/agents/<name>.md` 或相应目录形式。子 Agent 可使用 `inherit`、`branch` 或 `share` 工作区模式，其中 `branch` 可创建隔离 Git worktree。

Antigravity CLI 可能使用独立的 CLI 全局 Skill 目录。实现时必须按宿主 surface 探测并分别验证，不能用一个未经检测的硬编码路径覆盖 IDE、Desktop 和 CLI。

### 3.3 分阶段兼容承诺

| 阶段 | Codex 本地客户端 | Antigravity 本地客户端 | ChatGPT Desktop | ChatGPT Web/Work |
|---|---|---|---|---|
| 第一阶段 | 本地 Skill/脚本可信运行 | 本地 Skill/脚本可信运行 | 仅支持已暴露的本地能力 | 不承诺本地脚本执行 |
| 第二阶段 | 原生子 Agent Adapter | 原生 `invoke_subagent` Adapter | 依赖可用的 Codex/Plugin 能力 | 仅宿主已提供子 Agent 时可用 |
| 第三阶段 | Lite/Standard/Compliance | Lite/Standard/Compliance | Profile 规则可复用 | Profile 规则可复用 |
| 第四阶段 | 本地或远程 MCP/Plugin | 本地或远程 MCP/Plugin | Plugin + MCP | Plugin/App + 远程 MCP |

## 4. 目标架构

```text
用户请求
  │
  ▼
Skill Router
  ├── 判断项目身份
  ├── 判断 L0/L1/L2 与 Profile
  ├── 生成执行计划
  └── 等待用户确认
  │
  ▼
Host Adapter
  ├── CodexAdapter
  ├── AntigravityAdapter
  └── ChatGPTAdapter / RemoteMcpAdapter
  │
  ▼
Execution Core
  ├── 项目隔离与 worktree
  ├── 任务状态机
  ├── 证据门禁
  ├── Reviewer/QA 独立性校验
  └── 用户最终验收
  │
  ▼
Board Adapter
  ├── Local JSON
  ├── Remote MCP Board
  ├── GitHub Projects
  ├── Jira
  └── Feishu Base
```

建议逐步形成以下代码结构，并保留现有 `scripts/*.py` 作为向后兼容 CLI 包装器：

```text
multi-agent-flow/
├── SKILL.md
├── skills/
│   ├── router/SKILL.md
│   ├── task-flow/SKILL.md
│   ├── review/SKILL.md
│   └── qa/SKILL.md
├── yy_flow/
│   ├── core/
│   ├── evidence/
│   ├── execution/
│   ├── hosts/
│   ├── projects/
│   └── boards/
├── scripts/                 # 兼容入口
├── agents/                  # 中立角色源定义
├── config/
├── tests/
└── docs/
```

## 5. 全局能力与项目数据隔离

### 5.1 基本原则

全局安装只共享代码、Skill、通用角色模板和 Adapter，不共享项目任务数据、技术栈覆盖、会话绑定和锁文件。

```text
全局共享：
  yy-flow Skill / Python Core / Host Adapters / 通用角色模板

项目私有：
  .yy-flow/project.json
  .yy-flow/user_data/board.json
  .yy-flow/user_data/evidence/
  .yy-flow/user_data/logs/
  .yy-flow/user_data/worktrees.json
  .yy-flow/user_data/session-bindings/
```

### 5.2 项目身份文件

每个项目首次启用时生成 `.yy-flow/project.json`：

```json
{
  "schema_version": 1,
  "project_id": "licenseplate-7f31c2",
  "project_name": "LicensePlate",
  "root_realpath": "C:\\Users\\user\\Desktop\\Project\\LicensePlate",
  "vcs": "git",
  "remote_fingerprint": "sha256:...",
  "created_at": "2026-08-24T10:00:00+08:00"
}
```

`project_id` 一经生成不得因项目改名而改变。任务外部标识使用：

```text
<project_id>:<task_id>
licenseplate-7f31c2:T0001
```

### 5.3 防串项目门禁

任何写操作前执行：

1. 解析显式 `--project-root`；
2. 解析 Git 顶层目录；
3. 读取 `.yy-flow/project.json`；
4. 比较 realpath、`project_id` 和远端指纹；
5. 确认任务所属 `project_id`；
6. 不一致则 Fail-Closed，禁止静默退回其他 CWD；
7. 用户明确确认“切换项目”后才建立新的会话绑定。

注意事项：

- 全局目录中出现 `board.json` 必须视为安装污染并阻断；
- 环境变量 `YY_FLOW_PROJECT_ROOT` 只能作为显式覆盖，必须与项目身份文件交叉校验；
- 同一对话操作多个仓库时，每个写调用都要携带 `project_id`，不能依赖“上一条消息”；
- 远程 MCP 的数据主键必须至少包含 `tenant_id + user_id + project_id + task_id`；
- 全局 Agent 只能使用通用模板，项目技术栈在每次调用时动态加载，不能写回全局 Agent 文件。

## 6. 用户确认与自动执行策略

### 6.1 两次确认

```text
请求 → 分级与计划 → 用户确认开始 → 执行/审查/测试 → 用户确认验收
```

第一次确认控制“是否开始产生变更”，第二次确认控制“是否进入已验收终态”。

建议配置：

```yaml
execution_policy:
  default: selective
  plan_before_execute: true
  require_start_confirmation: true
  final_acceptance: human
  allow_auto_accept: false
  prefer_single_agent: true
  max_subagents: 2
```

若用户的原始命令已经明确包含“执行、修改、修复并测试”，可以视为第一次确认；高风险、破坏性、外部发布和权限扩张仍必须单独请求确认。最终验收不得从原始执行授权中推导。

### 6.2 任务分级

| 等级 | 使用场景 | 默认执行 |
|---|---|---|
| L0 | 解释、只读查询、无文件交付 | 主 Agent 直接处理，不建卡 |
| L1 | 小范围、低风险、易验证修改 | Lite，Builder + 独立 Verifier |
| L2 | 多模块、高风险、长周期或外部影响 | Standard/Compliance，多角色与严格证据 |

### 6.3 执行委托合同

总体方案不能直接等同于一次执行授权。每次交给 Antigravity、Codex 或其他宿主实施前，必须生成一份范围冻结的执行委托合同。合同至少包含：执行阶段、允许事项、禁止事项、Git 基线、权限边界、交付物、停止条件和复审方。

第一阶段建议合同如下：

```yaml
delegation_contract:
  contract_version: 1
  execution_batch_id: phase-1-trust
  plan_document: MULTI_CLIENT_MODERNIZATION_PLAN.zh-CN.md
  executor_host: antigravity
  reviewer_host: codex
  status: approved_to_execute

  scope:
    phase: phase-1
    allowed_items:
      - "7.1"
      - "7.2"
      - "7.3"
      - "7.4"
      - "7.5"
      - "7.6"
    forbidden_phases:
      - phase-2
      - phase-3
      - phase-4

  repository:
    base_branch: main
    base_commit: "<执行前运行 git rev-parse HEAD 获取>"
    expected_clean_worktree: true
    require_new_branch: true
    prefer_new_worktree: true
    allow_git_push: false
    allow_force_push: false
    allow_release: false

  permissions:
    allow_workspace_read: true
    allow_workspace_write: true
    allow_dependency_install: false
    allow_global_directory_write: false
    allow_external_system_write: false
    allow_destructive_git: false

  delivery:
    implementation_report: PHASE1_IMPLEMENTATION_REPORT.md
    require_test_evidence: true
    require_diff_summary: true
    require_known_issues: true
    final_state: awaiting_user_acceptance
```

合同执行规则：

1. 实施 Agent 只能执行 `allowed_items`；
2. 发现必须修改范围外内容时，先停止并请求合同扩展；
3. 第一阶段完成后立即停止，不得提前实现 Host Adapter、MCP、Plugin 或第三方连接器；
4. 实施 Agent 不得把阶段状态改为“已验收”；
5. 审查方可以读取所有变更和证据，但默认不直接修改；
6. 用户明确批准修复审查问题后，才进入下一轮修改；
7. 合同变更必须留下时间、原因和批准人记录。

#### 6.3.1 执行前置检查

实施前必须执行并记录：

```text
git status --short
git branch --show-current
git rev-parse HEAD
python --version
python -m pytest -q
```

当前方案文档在首次生成时是 Git 未跟踪文件。若使用 Antigravity `New Worktree` 或子 Agent `branch` 模式，新 worktree 不会自动包含未跟踪文件。因此执行前必须满足下列任一条件：

1. 用户先把方案文档加入 Git 并提交；
2. 用户将文档明确复制到新 worktree；
3. 用户把完整执行合同和对应阶段内容附加到 Antigravity 请求中。

不允许实施 Agent自行提交、移动或删除用户的未跟踪文件来“清理工作区”。若基线工作区不干净，应列出文件并等待用户决定。

Antigravity 临时 worktree 在子 Agent 被终止时可能被自动清理。终止或清理前必须确保代码提交、补丁或实施报告已经持久化到用户确认的位置；不得让未提交成果只存在于临时 worktree。

### 6.4 禁止事项

以下行为在所有阶段默认禁止，除非用户通过新的书面合同逐项授权。

#### 6.4.1 测试与证据红线

禁止为了“测试全绿”而：

- 删除原有测试；
- 大量降低断言强度；
- 增加无明确原因的 `skip`、`xfail` 或条件绕过；
- 捕获并吞掉本应暴露的异常；
- 把真实功能改成固定返回成功；
- 只修改测试去迎合错误实现；
- 伪造测试数量、命令、退出码、日志或客户端验证结果；
- 把合成文本总结作为开发、审查、测试或验收证据；
- 未运行完整测试却声称“全量测试通过”。

测试确需调整时，实施报告必须解释：原行为、规范依据、测试为什么错误、新断言验证什么，并由独立 Reviewer 审查。

#### 6.4.2 文件、依赖和环境红线

默认禁止：

- 未经批准安装、升级或移除依赖；
- 写入用户主目录、全局 Skill/Agent 目录或系统配置；
- 修改真实 API Key、OAuth Token、SSH Key 或凭证文件；
- 把秘密写入代码、配置、测试夹具、日志或实施报告；
- 修改与本执行批次无关的业务文件；
- 覆盖、移动或删除用户已有未提交修改；
- 启动面向公网或局域网的未认证服务；
- 调用真实 Jira、GitHub、飞书等外部系统执行写操作；
- 创建计划外的后台常驻服务。

#### 6.4.3 Git 和发布红线

默认禁止：

- `git reset --hard`、强制 checkout 覆盖、清理整个工作区；
- 改写已有提交历史；
- force push；
- 未经批准 push、创建 PR、合并或打 Tag；
- 发布包、部署服务或修改生产环境；
- 把多个阶段混入同一提交；
- 为了获得干净状态而删除未跟踪文件。

允许创建本地任务分支和提交，但必须由合同明确指定。若合同未明确允许 commit，则实施方只能保留工作区 diff 和实施报告。

### 6.5 交付报告

每个执行批次必须在仓库根目录生成独立、可跟踪的实施报告。第一阶段固定使用：

```text
PHASE1_IMPLEMENTATION_REPORT.md
```

报告模板：

```markdown
# 第一阶段实施报告

## 1. 执行身份
- 执行宿主：
- 主 Agent / 子 Agent：
- 执行批次：phase-1-trust
- 开始时间：
- 结束时间：

## 2. Git 基线
- 仓库绝对路径：
- 起始分支：
- 起始提交：
- 工作分支：
- worktree 路径：
- 开始前 git status：

## 3. 范围
- 已执行条目：
- 未执行条目：
- 获批的范围变更：

## 4. 文件变更
| 文件 | 修改目的 | 对应方案条目 | 风险 |
|---|---|---|---|

## 5. 行为变化
- Any/Optional：
- 监听地址：
- 软删除与恢复：
- Antigravity 路径：
- /auto 模拟：

## 6. 测试证据
| 命令 | 工作目录 | Python | 退出码 | 结果 | 日志/哈希 |
|---|---|---|---:|---|---|

## 7. 客户端验证
| 客户端/Surface | 验证方式 | 状态 | 证据 | 未验证原因 |
|---|---|---|---|---|

## 8. Git 结果
- 最终提交：
- 提交列表：
- git diff --stat：
- 最终 git status：
- 未跟踪文件：

## 9. 已知问题与风险
- 阻断问题：
- 非阻断问题：
- 后续建议：

## 10. 回滚方式
- 代码回滚：
- 数据迁移回滚：
- 路径迁移回滚：

## 11. 声明
- 未执行第二至第四阶段；
- 未自动标记用户验收；
- 未伪造或推断未运行的验证结果。
```

报告要求：

- 空白项必须写“未执行/不适用及原因”，不能删除；
- 测试结果必须来自真实命令；
- 日志含敏感信息时保存脱敏副本并记录脱敏规则；
- 报告自身必须出现在最终 `git status` 或提交列表中；
- 报告只描述事实，不能用“应该通过”替代“已经通过”。

### 6.6 停止条件

发生下列任一情况，实施 Agent 必须停止修改、保存当前证据并请求用户确认：

1. 需要安装、升级或移除依赖；
2. 需要修改合同未列出的阶段或大范围文件；
3. 发现方案与现有 API 兼容要求、测试或数据格式冲突；
4. 需要写入用户目录、系统目录、全局配置或外部服务；
5. 需要执行删除、批量移动、重置历史、强制覆盖等破坏性操作；
6. 工作区存在来源不明的用户修改；
7. 测试出现与本阶段无关且无法解释的失败；
8. 无法验证软删除没有造成变相物理删除；
9. 无法在真实 Antigravity surface 验证路径发现；
10. 子 Agent 或命令持续循环、超时或请求超出合同的权限；
11. worktree 产生冲突或成果可能在清理时丢失；
12. 需要 push、创建 PR、合并、发布或连接生产账号；
13. 证据文件、基线提交或项目身份无法确定；
14. 预计实现与计划的工作量、风险明显不一致。

停止后的输出必须包括：已完成事项、未完成事项、当前 diff、最后成功测试、失败信息、需要用户决定的问题。停止不等于失败，也不得为了避免停止而静默扩大权限。

### 6.7 自动测试与真实客户端验证边界

| 内容 | 自动测试 | 本地冒烟 | 真实客户端验证 | 第一阶段判定 |
|---|---|---|---|---|
| `Any/Optional` 导入 | 必须 | 可选 | 不需要 | 自动测试通过即可 |
| `127.0.0.1` 监听 | 必须 | 必须 | 可选 | socket 与进程验证 |
| 软删除/恢复 | 必须 | 必须 | 看板 UI 建议验证 | API、文件和 UI 证据 |
| `/auto` 零副作用 | 必须 | 必须 | CLI 建议验证 | 前后哈希一致 |
| Antigravity Skill 路径 | 路径测试 | 导出测试 | 必须 | 未真实发现不得写“已验证” |
| Antigravity Agent 路径 | 格式测试 | 导出测试 | 必须 | 至少发现一个自定义 Agent |
| Codex 兼容性 | 静态/格式测试 | Codex 侧复审 | Codex E2E 后续执行 | 第一阶段不夸大结论 |
| ChatGPT Web | 不适用 | 不适用 | 第四阶段 Plugin/MCP | 第一阶段明确未验证 |

客户端验证状态只能使用：

```text
verified      已在指定真实客户端和 surface 验证
static_only   仅完成路径、格式或契约静态验证
not_run       未执行
blocked       因账户、权限、环境或功能不可用而阻塞
```

禁止把 `static_only` 转换成 `verified`。IDE、Desktop、CLI 属于不同 surface，应分别记录版本和结果。

### 6.8 交给 Antigravity 的第一阶段执行指令

可将下面内容与本方案一起交给 Antigravity：

```text
请完整阅读仓库根目录的
MULTI_CLIENT_MODERNIZATION_PLAN.zh-CN.md。

我批准你仅执行“第一阶段：先保证可信”的 7.1～7.6，
并遵守 6.3～6.7 的执行委托合同、禁止事项、交付报告和停止条件。
禁止实施第二、第三、第四阶段。

执行要求：
1. 开始前输出 Git 基线、未提交文件、预计修改文件、测试计划和风险，等待我确认。
2. 使用独立分支；复杂修改优先使用 New Worktree。
3. 不得 reset、覆盖、移动或删除用户现有修改和未跟踪文件。
4. 不得删除测试、弱化断言、添加无理由 skip 或伪造测试结果。
5. 不得安装/升级依赖、修改全局目录、写外部系统、push 或发布。
6. 每项修改先运行相关测试，最后运行全量测试并记录命令和退出码。
7. Antigravity 路径必须区分 static_only 和真实 verified。
8. /auto 必须是零写入纯模拟，不得推进真实状态。
9. 完成后在仓库根目录生成 PHASE1_IMPLEMENTATION_REPORT.md。
10. 第一阶段完成后立即停止，状态保持 awaiting_user_acceptance。
11. 遇到 6.6 任一停止条件时，保存证据并向我请求决定。

开始修改前只提交执行前检查结果和计划；我确认后再修改。
```

如果用户在发送本指令时已经明确说“确认执行第一阶段”，可以视为实施开始确认；它不包含最终验收，也不授权范围扩展、破坏性操作或外部写入。

### 6.9 Codex 独立复审交接

Antigravity 完成后，交给 Codex 的材料必须包括：

- 本方案文档；
- `PHASE1_IMPLEMENTATION_REPORT.md`；
- 起始提交和最终提交；
- 工作分支和 worktree 信息；
- 完整 `git status --short`；
- `git diff --stat` 和可审查的完整 diff；
- 全部测试命令、退出码和失败日志；
- 未跟踪文件列表；
- 未解决问题、范围变更和用户批准记录；
- Antigravity 真实客户端验证记录。

推荐给 Codex 的复审指令：

```text
Antigravity 已完成第一阶段。请以
MULTI_CLIENT_MODERNIZATION_PLAN.zh-CN.md
和 PHASE1_IMPLEMENTATION_REPORT.md 为依据进行独立审查。

本轮默认只读：不要直接修复。
请检查完整 Git diff、实施范围、软删除语义、/auto 零副作用、
默认监听安全、Antigravity 路径兼容、测试真实性和报告一致性。
重新运行必要测试，并按阻断/重要/一般问题输出证据。
最后给出：建议验收、修改后验收或拒绝验收。
不要代替用户执行最终验收。
```

Codex 复审流程：

1. 先确认基线、当前分支和工作区状态；
2. 检查是否越过第一阶段范围；
3. 逐文件审查，不只阅读实施报告；
4. 独立重跑受影响测试和全量测试；
5. 检查报告声明与实际 diff、命令结果是否一致；
6. 将问题按优先级列出，并给出文件和复现证据；
7. 默认不修改代码；用户批准修复后再进入修复批次；
8. 即使无问题，也只能“建议用户验收”，不能自行进入已验收。

### 6.10 执行批次完成定义

一个执行批次只有同时满足以下条件，才可以标记为“待用户验收”：

- 合同范围内事项全部完成，范围外事项未实施；
- 相关测试和全量测试按计划运行，失败均被如实记录；
- 实施报告完整且与实际 Git 状态一致；
- 没有遗失在临时 worktree 的未提交成果；
- 没有未披露的依赖、权限、全局目录或外部系统修改；
- 实施方完成自检；
- 独立 Reviewer 可以获得完整 diff 和证据。

“待用户验收”不等于“已验收”。只有用户在阅读独立复审结论后明确确认，才能推进最终状态。

## 7. 第一阶段：先保证可信

### 7.1 修复 `Any/Optional` 导入

目标：恢复测试收集并消除运行时类型注解错误。

涉及文件：

- `scripts/start_kanban_server.py`
- `tests/test_kanban_server.py`
- `tests/test_kanban_api_v2.py`

执行流程：

1. 在 `start_kanban_server.py` 增加 `from typing import Any, Optional`；
2. 运行两个看板测试文件，确认可以完成收集；
3. 运行全量测试；
4. 检查其他运行时注解是否存在未导入名称；
5. 增加“模块可独立 import”测试，防止同类问题再次发生。

注意事项：

- 不要通过 `from __future__ import annotations` 单独掩盖所有缺失导入；若运行时使用类型对象，仍应明确导入；
- 测试必须使用 Python 3.11，并在 CI 增加至少一个更新版本验证。

验收标准：

- `python -m pytest tests/test_kanban_server.py tests/test_kanban_api_v2.py -q` 能完成；
- 全量测试不再在收集阶段失败。

### 7.2 默认监听改为 `127.0.0.1`

目标：默认不向局域网暴露未认证看板服务。

涉及文件：

- `scripts/start_kanban_server.py`
- `kanban/start.ps1`、`kanban/start.sh`、`kanban/start.bat`
- 看板服务相关测试和 README

执行流程：

1. 将 `start_server()` 和 CLI `--host` 默认值统一改为 `127.0.0.1`；
2. `print_kanban_urls()` 仅在实际绑定非回环地址时显示局域网 URL；
3. 增加显式 `--allow-remote`；未提供时拒绝 `0.0.0.0` 或非回环地址；
4. 远程监听时输出醒目安全警告；
5. 增加监听地址单元测试和真实 socket 集成测试。

注意事项：

- 当前端口探测在 `127.0.0.1` 进行，但默认启动随后会重建为 `0.0.0.0`，两处都必须校验；
- 第一阶段没有完善认证，因此不应把远程监听作为默认功能；
- CSRF/Origin 检查不能替代身份认证。

验收标准：

- 无参数启动仅监听 `127.0.0.1`；
- 未加 `--allow-remote` 时不能绑定 `0.0.0.0`；
- 现有本机看板功能不回归。

### 7.3 删除 API 改为软删除

目标：删除操作可恢复、可审计，不破坏任务历史和编号链。

涉及文件：

- `scripts/start_kanban_server.py`
- `scripts/_lib/boards/offline_board_adapter.py`
- `kanban/js/board.js`、`kanban/js/app.js`、`kanban/js/data.js`
- API 测试、离线看板测试、审计测试

建议字段：

```json
{
  "is_deleted": true,
  "deleted_at": "2026-08-24T10:00:00+08:00",
  "deleted_by": "user-or-agent-id",
  "delete_reason": "duplicate",
  "delete_version": 1
}
```

执行流程：

1. `DELETE /api/tasks/{id}` 不再移除数组元素，只写软删除元数据；
2. 单条和批量删除共用一个领域服务，避免两套行为；
3. 默认列表、统计和重复任务检查排除软删除记录；
4. 增加 `include_deleted=true` 的受控查询；
5. 增加 `POST /api/tasks/{id}/restore` 恢复接口；
6. UI 文案从“删除”改为“移入回收站”，增加回收站和恢复；
7. 审计记录删除前后版本、操作者和原因；
8. 物理清理单独设计为管理员维护命令，不对普通 Agent 暴露。

注意事项：

- 不建议把状态直接改成“已删除”，否则会破坏原状态历史；软删除应为正交元数据；
- 兼容旧客户端的批量删除路由也必须软删除；
- `POST /board.json` 全量覆盖可能变相物理删除未提交的卡片，应弃用或改为 merge + version 校验；
- 恢复后应保留原任务 ID，不重新编号。

验收标准：

- 删除后原记录仍存在于数据文件；
- 默认 UI 不显示、回收站可显示并恢复；
- 删除和恢复都有审计证据；
- 并发版本冲突返回 409。

### 7.4 修正 Antigravity 全局 Agent 路径

目标：按不同 Antigravity surface 输出正确的 Skill 和 Agent 位置。

涉及文件：

- `config/agent_platforms.yaml`
- `scripts/verify_and_export_agents.py`
- `scripts/install_global.ps1`、`scripts/install_global.sh`
- 路径和导出测试

执行流程：

1. 保留 IDE/Desktop 全局 Skill：`~/.gemini/config/skills/yy-flow/`；
2. 把全局 Agent 从错误的 `~/.gemini/config/skills-agents/...` 改为 `~/.gemini/config/agents/...`；
3. 工作区 Agent 统一使用 `.agents/agents/<name>.md`；
4. 对 Antigravity CLI 单独声明和探测 CLI Skill 目录，不与 IDE 目录混为一谈；
5. 导出前检测宿主、版本和目标目录；无法确认时只打印建议，不猜测写入；
6. 为旧路径提供一次性迁移检测，禁止静默删除用户文件；
7. 增加导出后格式解析和宿主发现测试。

注意事项：

- Skill 路径和 Agent 路径是两个概念；
- 不要把项目技术栈写入全局 Agent；
- Windows Junction、symlink 权限失败时必须报告降级结果，不能显示假成功；
- 同名实体目录存在时不得覆盖。

验收标准：

- Antigravity 能发现全局 Skill；
- 能发现至少一个导出的全局自定义 Agent；
- 工作区 Agent 不污染其他项目；
- 路径测试覆盖 Windows、POSIX 和 `~` 展开。

### 7.5 `/auto` 改为纯模拟模式

目标：杜绝没有真实执行就自动进入审查、测试、完成或验收。

涉及文件：

- `scripts/auto_task.py`
- `SKILL.md`
- `README.md`
- `tests/test_workflow_v2.py`

执行流程：

1. `/auto` 和 `auto_task.py` 默认只输出计划，不写看板；
2. 移除或禁用自动生成“已完成审查/测试”的合成总结；
3. 输出内容明确标记 `SIMULATION`；
4. 禁止模拟结果作为证据提交；
5. 如保留 `--simulate`，将其设为兼容别名；
6. 原来的真实执行入口更名为 `run`，但在第二阶段 Host Adapter 完成前保持不可用；
7. 已验收自动链测试改成“不得改变数据”的断言。

注意事项：

- 模拟可计算预计状态链，但不能创建任务、修改状态或写审计为成功；
- 不能用 `--force` 绕过证据门禁；
- CLI 输出和返回码要区分“模拟成功”和“业务执行成功”。

验收标准：

- 默认运行 `/auto` 前后 `board.json` 哈希不变；
- 输出不包含未经执行的成功事实；
- 只有第二阶段真实 Runner 可以提交状态变更。

### 7.6 测试重新全绿

目标：建立第一阶段可信基线。

执行流程：

1. 先运行受影响测试；
2. 再运行全量 217 个现有测试函数；
3. 新增监听、软删除、恢复、模拟无副作用、路径迁移测试；
4. 增加 Windows 与 Linux CI；
5. 测试中不得依赖真实外部 Jira、GitHub 或飞书凭证；
6. 记录测试命令、退出码、测试数量和日志哈希。

注意事项：

- 不能通过删除失败测试、放宽断言或统一 skip 达到“全绿”；
- 测试通过只是第一阶段完成条件之一，还要进行本机 API 冒烟验证；
- 测试产生的数据必须位于临时项目目录，不能写入全局 Skill 正本。

第一阶段退出条件：

- 全量测试通过；
- 默认仅本机监听；
- 删除可恢复；
- `/auto` 零副作用；
- Antigravity 全局路径通过真实宿主验证；
- 用户确认第一阶段验收。

### 7.7 第一阶段验收与冻结记录

验收结论：**通过**。

| 项目 | 记录 |
|---|---|
| 用户确认日期 | 2026-08-24 |
| 验收范围 | 7.1～7.6 |
| 基线提交 | `43156b0710005ad80cf610fbc456cb503eb2d0ca` |
| 第一阶段交付提交 | `b83741b25ad567eb44f085bdea7bc3ac9537e840` |
| 独立全量测试 | `226 passed`，退出码 `0` |
| `/auto` 零写入 | 已通过目录树前后对比验证 |
| Antigravity 路径结论 | 静态验证通过；未获得真实客户端证据的项继续标记 `static_only` |
| 最终状态 | 第一阶段正式结束，保持只读基线 |

冻结规则：

1. 后续批次不得顺带重构或补改 7.1～7.6；发现第一阶段回归时，停止第二阶段并单独申请 Hotfix；
2. 不得改写第一阶段测试证据、实施报告或验收结论；允许追加更正说明，但必须保留原始记录；
3. 第二阶段必须从已验收提交或其经验证的 `main` 合流提交开始，不能从旧 `main` 基线另起开发；
4. 第一阶段 worktree 在合流和主分支复验前保留，不自动清理。

## 8. 第二阶段：建立真实多 Agent

### 8.0 阶段准入、基线与 worktree 处置

第二阶段尚未因第一阶段验收而自动获得实施授权。进入开发前必须同时满足：

1. 用户单独批准某一个第二阶段子批次；
2. Git 基线包含第一阶段已验收成果；
3. 主工作区和目标 worktree 的未提交文件已披露并得到妥善保留；
4. 输出预计修改文件、测试计划、风险点和宿主能力检测结果；
5. 未触发依赖升级、全局目录写入、外部系统写入或付费 API 调用。

这些目录不是多个项目，而是同一 Git 仓库的多个 worktree。第二阶段交付后的布局为：

```text
multi-agent-flow                     -> main
multi-agent-flow-phase1-trust        -> phase-transition-plan（过渡/文档分支，目录名保留历史名称）
multi-agent-flow-phase2-real-agents  -> phase-2-real-agents（第二阶段唯一开发 worktree）
phase-1-trust                        -> 冻结分支 b83741b，不再承载新修改
```

禁止手工复制或“合并文件夹”。推荐的 Git 合流顺序为：

1. 保留 `phase-1-trust` 作为第一阶段可审计来源；
2. 单独处理 `main` worktree 中与受控文件同名的未跟踪文件，不得覆盖或删除；
3. 经用户批准后，由 DevOps 将 `phase-1-trust` 合入 `main`；若 `main` 仍为其祖先，可使用 fast-forward；
4. 在更新后的 `main` 上重新运行全量测试；
5. 从复验通过的 `main` 新建 `phase-2-real-agents` 分支和 `multi-agent-flow-phase2-real-agents` worktree；
6. 第二阶段只在新 worktree 开发，不复用第一阶段冻结 worktree；
7. 合并和清理 worktree 均是独立 Git 操作，必须另行批准。

若暂不合流，技术上可以直接从 `b83741b` 创建第二阶段分支，但会使 `main` 长期落后、验收链复杂化，因此不作为默认方案。

### 8.0.1 第一阶段合流与第二阶段交接记录

2026-08-24 实际执行记录：

| 项目 | 实际结果 |
|---|---|
| 原 `main` | `43156b0710005ad80cf610fbc456cb503eb2d0ca` |
| 第一阶段冻结分支 | `phase-1-trust`，`b83741b25ad567eb44f085bdea7bc3ac9537e840` |
| 过渡分支 | `phase-transition-plan` |
| 第一轮合流提交 | `4273e21`，fast-forward 合入 `main` |
| 全量测试命令 | `python -m pytest tests -q -rs` |
| 首次复验 | 退出码 `1`：`4 failed, 169 passed, 53 errors`；原因是新 worktree 不携带 Git 忽略的本地配置和数据文件 |
| 环境修复 | 从已验收 worktree 复制无凭证的 `config/workflow.config.yaml` 本地快照；不提交、不升级依赖、不改代码 |
| 第二次复验 | 退出码 `0`：`226 passed in 51.78s` |
| 旧未跟踪方案 | 已移至仓库外 `C:\Users\user\Desktop\user\multi-agent-flow-backups\MULTI_CLIENT_MODERNIZATION_PLAN.zh-CN.pre-phase1-untracked.md`，SHA-256 `B3FDF2EF0269F67F02C796CCA208AFDEF004FDA81A49DFB206BC80CCE9F10378` |
| 第二阶段分支 | `phase-2-real-agents`，初始分支点 `79513e1b10d33ef2c193c3e64d02402b0e170ae2`，随后仅同步本交接文档 |
| 第二阶段 worktree | `C:\Users\user\Desktop\user\multi-agent-flow-phase2-real-agents` |
| 第二阶段 worktree 复验 | `python -m pytest tests -q -rs`，退出码 `0`：`226 passed in 56.97s`；测试后已恢复权威看板快照并校验一致 |
| 远端分支 | 用户已确认并推送 `origin/main`、`origin/phase-1-trust`、`origin/phase-2-real-agents`；本次同步前第二阶段本地/远端差异为 `0/0` |
| 2A 当前状态 | 已验收并冻结；代码提交 `9c59b14`，定向测试 `11 passed`，全量测试 `237 passed` |
| 2B 当前状态 | 已于 2026-08-25 完成 AutoLaw 独立复审、Codex QA 准出和用户授权终态验收；候选提交 `f6b79e9`，Codex 定向复验 `25 passed`、全量复验 `251 passed`；2B 已冻结 |
| 2C 交接 | 实施任务书见 `docs/D04-研发过程/D01-任务/Phase2-2C-Worktree隔离实施任务书.md`；仅允许 Worktree 隔离，不得进入真实 Host Adapter |

本地运行数据交接规则：

1. `config/workflow.config.yaml` 和 `user_data/` 被 `.gitignore` 排除，Git 分支/合并/worktree 不会自动携带；
2. 创建第二阶段 worktree 后，必须复制已验收的无凭证配置和最终看板快照，并校验 SHA-256；
3. 第二阶段开始后，以 `multi-agent-flow-phase2-real-agents` 下的数据根为唯一权威写入源；`main`、过渡 worktree 和历史 worktree 只读，禁止多份看板并行推进；
4. 测试必须使用隔离数据根；若既有全量测试仍在 `user_data/` 生成污染卡，只能通过合法状态 CLI 软取消并记录，禁止复制快照覆盖或直接编辑权威 `board.json`；
5. Antigravity 开工前仍须执行 `git rev-parse HEAD`、`git status --short` 和全量测试，不得仅依据本文中的历史结果。

### 8.0.2 第二阶段执行委托合同

**允许范围：**仅实施用户当次明确批准的 2A～2F 子批次；允许在独立分支/worktree 内修改、运行本地测试和生成交付报告。

**禁止事项：**

- 禁止实施第三、第四阶段，包括 Profile 拆分、MCP Server、ChatGPT Plugin/App、远程看板、认证和第三方连接器；
- 禁止升级或新增依赖、推送远端、发布版本、打 Tag、自动合并或自动清理 worktree；
- 禁止写入用户级 Codex/Antigravity 全局目录，除非用户对精确路径另行批准；
- 禁止调用付费 API、使用 API Key、创建外部资源或写外部系统，除非用户另行批准调用范围和费用边界；
- 禁止 `reset`、自动 `stash`、覆盖未提交文件、删除用户文件或伪造 Host/session/evidence；
- 禁止 Fake Adapter、模拟输出或单会话角色切换满足“真实多 Agent”验收条件。

**停止条件：**出现范围不明、基线不一致、工作区不干净、依赖变更、全局目录写入、外部认证/计费、破坏性操作、测试失败无法在本批次内解释、宿主能力无法确定、拿不到真实 session/invocation ID，或需要修改已冻结第一阶段时，立即停止并请求用户确认。

**交付要求：**每个子批次生成变更摘要、实际文件、测试命令/退出码/数量、Git 状态、风险和未完成项；第二阶段最终生成 `PHASE2_IMPLEMENTATION_REPORT.md`。实施方只能提交“待独立复审”，不能自行宣布用户验收。

### 8.0.3 2A 开工交接不变量

Antigravity 执行 2A 时必须满足：

1. 唯一工作目录为 `C:\Users\user\Desktop\user\multi-agent-flow-phase2-real-agents`，唯一开发分支为 `phase-2-real-agents`；
2. 开工前执行 `git fetch origin`，确认 `HEAD` 与 `origin/phase-2-real-agents` 的关系；若存在远端新提交、分叉或用户未提交修改，停止并报告，不自行 rebase、merge、reset 或覆盖；
3. 2A 只定义 Host 契约、纯只读能力探测、`FakeHostAdapter` 和契约测试，不接入真实 Codex/Antigravity，不创建 worktree，不接入状态机和证据存储；
4. `FakeHostAdapter` 的所有结果必须携带 `is_real_host=false`，其 session/invocation ID 必须带 fake/test 命名空间，且不能通过任何真实状态门禁；
5. 能力探测不得以创建文件、目录、锁、日志、会话或外部请求验证能力；未确认能力一律返回 unsupported/unknown；
6. 如果现有源码布局不适合计划中的 `yy_flow/hosts/`，先在开工报告中提出最小布局方案，用户未确认前不得进行大规模包迁移；
7. 只允许新增或修改 2A 直接相关代码、测试、`PHASE2_IMPLEMENTATION_REPORT.md` 和必要索引说明；第一阶段代码与报告保持冻结；
8. 完成后工作区必须干净、允许本地 commit，但不得 push；状态停止在“2A 待用户验收”。

### 8.0.4 2A 准出结论与 2B 开工不变量

2A 于 2026-08-25 完成最终修复和 Codex 准出验证，交付基线为代码提交 `9c59b14` 及其后的报告/交接文档提交。最终契约具备以下不变量：

1. 契约对象及其常见嵌套容器递归冻结，调用方不能原地污染 Adapter 的能力、请求或结果；
2. Fake session、Adapter 实例和 invocation token 分属独立命名空间；Handle 明确采用不可猜测 token 的 bearer-capability 模型；
3. Fake 结果始终为 `is_real_host=false`，不支持交互确认，也不能成为真实状态证据；
4. 超时基于 `time.monotonic()`、请求默认值、显式覆盖值和剩余执行时间计算；
5. 能力探测测试封锁文件、临时目录、子进程和网络等副作用入口；
6. 定向测试 `11 passed`，全量回归 `237 passed`，测试后权威看板已恢复并完成哈希核验；
7. `PHASE2_IMPLEMENTATION_REPORT.md` 是 2A 的最终交付与风险说明。

2B 开工时必须满足：

1. 2B 只实现 Evidence Schema、canonical serializer、受控的 append-only Evidence Store、SHA-256 完整性校验和纯门禁判定；
2. 2B 不调用真实 Codex/Antigravity，不创建 worktree，不实现 Reviewer/QA 独立运行，不推进 2C～2F；
3. Evidence Store 只能写项目内或显式配置的受控证据根，必须阻断绝对路径逃逸、`..`、符号链接/联接点逃逸、覆盖、删除和更新既有证据；
4. 落盘采用 canonical UTF-8 JSON、确定字段顺序、原子创建和 SHA-256 校验；同一 `evidence_id` 不得覆盖；
5. `is_real_host=false`、Fake session、缺失 Host/session/invocation ID、哈希不符、基线不符或 transition 不匹配时，门禁必须 Fail-Closed；
6. invocation token、API Key、访问令牌、环境变量秘密和未脱敏命令输出不得进入证据；2A Handle 的 invocation token 仅供进程内校验；
7. 用户验收证据只能来自显式用户确认，Fake confirmation 或模型自述不得生成用户确认凭据；
8. 2B 测试只能在 `tmp_path` 或受控临时根写证据，不得污染权威 `user_data/`；
9. 2B 只提供门禁服务和最小集成 seam，不改变第一阶段既有 CLI 的默认流转行为；真正接入实际多 Agent 状态链留到后续获批批次；
10. 完成后停止在“2B 待用户验收”，不得自行进入 2C。

### 8.0.5 2B 最终准出与 2C 开工不变量

2B 于 2026-08-25 以候选提交 `f6b79e903fb39d8724bfe303e817d7a0748cd7b6` 完成最终验收。Codex 独立复验结果为：`tests/test_evidence.py tests/test_host_adapter.py` 共 `25 passed in 0.96s`，全量 `251 passed in 41.08s`，`git diff --check` 退出码 0。原工单已完成 QA 与 PM 终态流转，2B 正式冻结。

2C 开工必须遵循：

1. 仅实现 Worktree Schema、`WorktreeManager`、受控路径/仓库身份/并发保护、只读核验和测试；
2. 不实现真实 Codex/Antigravity Adapter，不改 Reviewer/QA 编排和现有业务状态链；
3. 禁止自动合并、删除、prune、reset、stash、clean、push 或写全局目录；
4. 实际物理清理不属于 2C；2C 只能返回清理计划，后续需用户对精确目标另行授权；
5. 详细执行、对抗测试、开发交付和独立复审合同以 `docs/D04-研发过程/D01-任务/Phase2-2C-Worktree隔离实施任务书.md` 为准；
6. 完成后停止在“2C 待用户验收”，不得自行进入 2D。

### 8.1 增加 Host Adapter

目标：把业务流程与宿主原生 Agent 调用解耦。

建议接口：

```python
class HostAdapter(Protocol):
    def detect(self) -> HostCapabilities: ...
    def build_dispatch(self, request: AgentRequest) -> DispatchPlan: ...
    def spawn(self, plan: DispatchPlan) -> AgentHandle: ...
    def wait(self, handle: AgentHandle) -> AgentResult: ...
    def cancel(self, handle: AgentHandle) -> None: ...
    def request_confirmation(self, request: ConfirmationRequest) -> ConfirmationResult: ...
```

建议文件：

```text
yy_flow/hosts/base.py
yy_flow/hosts/detect.py
yy_flow/hosts/codex.py
yy_flow/hosts/antigravity.py
yy_flow/hosts/chatgpt.py
yy_flow/hosts/manifest.py
```

执行流程：

1. 定义标准能力：子 Agent、并行、独立上下文、worktree、权限审批、MCP、用量信息；
2. 定义标准请求和结果 Schema；
3. 先做 capability detection，再选择 Adapter；
4. 不支持真实子 Agent 时 Fail-Closed 或降级单 Agent，不得模拟多个 Agent；
5. Adapter 返回宿主 session/thread ID，供独立性和审计校验；
6. 统一超时、取消、失败和部分结果语义。

注意事项：

- 很多“子 Agent 工具”属于宿主会话能力，不能假设 Python 子进程可直接调用；
- Adapter 应分为“生成标准调度计划”和“由宿主执行原生工具”两层；
- 宿主工具名、模型名和路径不能散落在核心状态机中。

验收标准：

- FakeHostAdapter 可完成全部契约测试；
- 不同 Adapter 的结果都能归一化为同一 `AgentResult`；
- 未检测到宿主时不会伪装运行成功。

### 8.2 实现 Codex Adapter

Codex 必须区分两条执行路线：

| Adapter | 执行位置 | 凭证与用量 | 第二阶段定位 |
|---|---|---|---|
| `CodexNativeAdapter` | ChatGPT Desktop、Codex CLI 或 IDE 当前宿主会话 | 使用该客户端当前账户、模型、权限和用量规则 | 默认路线 |
| `OpenAIResponsesAdapter` | Python 通过 OpenAI Responses API 调用 | 使用单独 API Key，并按 API Token/工具用量计费 | 可选路线，必须单独授权 |

两者不能相互冒充。Python Core 可以生成调度计划和验证结果，但只有宿主实际暴露的原生子 Agent 能力才能创建 Codex 客户端线程；不得假设存在未公开的本地 Python 函数可直接操纵 Codex Desktop。OpenAI 官方文档确认当前 Codex 客户端可运行独立子 Agent 线程，并从 `~/.codex/agents/*.toml` 或项目 `.codex/agents/*.toml` 加载自定义 Agent；Responses API 的 Multi-agent 则是独立的 API 能力。

执行流程：

1. 探测 Codex 多 Agent 是否启用以及并发上限；
2. 使用 Codex 原生子 Agent 工作流，不在 Python 中伪造角色；
3. 按官方 TOML Schema 从 `~/.codex/agents/*.toml` 或项目 `.codex/agents/*.toml` 加载角色，至少包含 `name`、`description` 和 `developer_instructions`；
4. Builder 使用 workspace-write，Reviewer 默认 read-only，QA 仅获得测试所需权限；
5. 保存父任务、子 Agent 线程、模型、权限模式和结果摘要；
6. 子 Agent 失败时不得自动提交下一状态；
7. 增加真实 Codex 手工 E2E 清单，并由宿主返回可检查的 Agent thread ID；
8. 只有用户明确批准 API Key、模型、费用上限和数据边界后，才实现或启用 `OpenAIResponsesAdapter`。

注意事项：

- Codex 子 Agent 会增加 Token，应受 Profile 和并发上限约束；
- 子 Agent 会继承部分父会话设置，必须在调度前检查权限；
- 不要依赖未公开、易变化的内部函数名；优先使用宿主暴露的能力和指令契约。
- 客户端订阅/额度与 OpenAI API 账单不是同一授权面；报告必须记录实际使用的 Adapter，不推断或伪报 Token 来源。

官方依据：

- [Codex Subagents](https://learn.chatgpt.com/docs/agent-configuration/subagents)
- [OpenAI Responses API Multi-agent](https://developers.openai.com/api/docs/guides/responses-multi-agent)

### 8.3 实现 Antigravity Adapter

执行流程：

1. 探测 `invoke_subagent` 与自定义 Agent 能力；
2. 按 `.agents/agents` 或全局 `.gemini/config/agents` 加载角色；
3. 映射工作区模式：只读探索可 `inherit`，写入实现优先 `branch`，明确共享才使用 `share`；
4. 映射 Antigravity 工具权限、命令策略、MCP Server 和模型层级；
5. 保存 invocation ID、workspace 模式和结果；
6. 对无子 Agent 权限或额度不足提供明确降级提示。

注意事项：

- `share` 会让多个写 Agent 操作同一目录，默认禁止并行写；
- `inherit` 不等于独立代码工作区；上下文独立与文件隔离是两个维度；
- IDE、Desktop、CLI 的发现路径和能力应分别做契约测试。

### 8.4 增加 worktree 隔离

目标：避免并行 Agent 在同一工作目录互相覆盖。

执行流程：

1. 校验当前目录是干净 Git 仓库；
2. 为任务生成分支：`yy-flow/<project-id>/<task-id>/<role>`；
3. 在项目外的受控目录创建 worktree；
4. 写入 `worktrees.json`，记录路径、分支、基线提交、Agent 和状态；
5. Agent 只获得自己 worktree 的写权限；
6. Reviewer 对提交差异做只读审查；
7. QA 在候选提交上测试；
8. 合并前检测冲突并请求用户确认；
9. 用户验收后再清理 worktree；失败时保留以便诊断。
10. worktree 根目录必须为配置的受控绝对路径；创建、合并、移除前分别校验解析后的目标仍在受控根内；
11. Builder 仅写自己的 worktree，Reviewer 默认只读，QA 固定在候选提交测试；
12. 不允许 Adapter 自动合入 `main`，也不允许因任务完成自动删除分支或目录。

注意事项：

- 不允许对未提交用户修改自动 stash、reset 或覆盖；
- Windows 路径长度、文件锁和杀毒软件占用需专项测试；
- 非 Git 项目必须显式降级为单写者临时副本，不可假装具备 worktree 隔离；
- 清理属于破坏性操作，必须精确校验路径在受控 worktree 根内。

### 8.5 状态流转必须提交真实证据

建议证据 Schema：

```json
{
  "evidence_id": "ev-uuid",
  "project_id": "licenseplate-7f31c2",
  "task_id": "T0001",
  "transition": "测试中->已完成",
  "type": "test_result",
  "actor_role": "QA",
  "adapter": "codex_native",
  "is_real_host": true,
  "host": "codex",
  "host_session_id": "...",
  "host_invocation_id": "...",
  "workspace_mode": "worktree",
  "baseline_commit": "...",
  "command": "python -m pytest -q",
  "exit_code": 0,
  "artifact_uri": ".yy-flow/user_data/evidence/...json",
  "artifact_sha256": "...",
  "result_commit": "...",
  "created_at": "..."
}
```

门禁矩阵：

| 流转 | 最低证据 |
|---|---|
| 待开始 → 进行中 | 用户开始确认、项目身份、Agent/worktree ID |
| 进行中 → 审查中 | Git diff/commit、修改摘要、开发自测结果 |
| 审查中 → 测试中 | 独立 Reviewer session、审查报告、结论 |
| 测试中 → 已完成 | 独立 QA session、命令、退出码、日志哈希 |
| 已完成 → 已验收 | 用户确认 ID、确认时间、验收说明 |

注意事项：

- 纯文本总结不是证据；
- 文件路径必须在项目或受控证据目录内；
- 命令输出应做敏感信息脱敏；
- Git commit 不是所有任务的唯一证据，文档、设计和研究任务使用相应 artifact 类型；
- 外部看板只保存引用和摘要，原始证据应有可验证存储。
- `is_real_host=false`、缺少宿主 ID、无法核对基线/结果提交的证据只能用于契约测试，不能推进真实任务状态；
- Host Adapter 的能力声明、调用结果和证据对象必须交叉校验，不能仅相信 Adapter 自报 `success`。

### 8.6 Reviewer 和 QA 独立运行

独立性最低要求：

- Reviewer/QA 的 `host_session_id` 不得等于 Builder；
- Reviewer 默认不能修改实现代码；
- QA 不能修改代码让测试通过；
- Reviewer 与 QA 必须从实际 diff/commit 和测试环境重新获取事实；
- Lite 可以合并 Reviewer + QA 为一个 Verifier，但 Verifier 仍必须独立于 Builder；
- Compliance 下 Reviewer、QA、Builder 三者必须分离。

执行流程：

1. Builder 提交候选提交和开发证据；
2. Reviewer 在独立 session 审查；
3. 有缺陷则生成 DEF 并退回，不进入测试；
4. Reviewer 通过后，QA 在候选提交上独立运行；
5. QA 失败则退回并附失败证据；
6. QA 通过后只能进入“已完成/待用户验收”。

### 8.7 最终验收默认要求用户确认

执行流程：

1. 汇总修改、风险、审查、测试和未解决事项；
2. 生成验收请求，状态保持“已完成”；
3. 用户明确回复确认后写入 `user_acceptance` 证据；
4. 状态机验证证据后进入“已验收”；
5. 超时不自动验收，只提醒或保持等待。

注意事项：

- PM Agent 不能代替用户；
- “测试通过”不等于“业务验收”；
- 用户拒绝时应进入退回或阻塞，而不是修改历史证据。

### 8.8 第二阶段子批次与逐批授权

第二阶段拆为六个可独立验收的子批次。默认一次只批准一个，上一批达到“待用户验收”且经用户明确确认后，下一批才可开工。

| 批次 | 目标 | 允许的核心产出 | 本批次禁止 |
|---|---|---|---|
| 2A | Host 契约与能力探测 | Schema、`HostAdapter`、`FakeHostAdapter`、契约测试 | 真实客户端调用、worktree 写入、状态推进 |
| 2B | 证据存储与状态门禁 | Evidence Schema、存储、哈希、门禁测试 | 把 Fake/模拟证据当真实证据 |
| 2C | worktree 隔离 | `WorktreeManager`、受控路径、冲突/清理保护测试 | 自动合并、自动清理、stash/reset |
| 2D | Codex 真实 Adapter | `CodexNativeAdapter`、真实线程证据、Codex E2E | 未经批准的 Responses API/API Key 使用 |
| 2E | Antigravity 真实 Adapter | `AntigravityAdapter`、真实 invocation 证据、Antigravity E2E | 写全局目录、用静态路径测试代替真实宿主 |
| 2F | 独立 Reviewer/QA 与验收 | 独立 session 门禁、真实 L2 双宿主验证、验收请求 | 自动用户验收、进入第三阶段 |

每批开始前必须输出：

- 基线分支、提交和 worktree；
- 预计修改文件；
- 相关测试与全量测试计划；
- 风险点、外部调用、权限和费用；
- 是否存在未提交或未跟踪文件；
- 本批次完成后停止位置。

每批完成后必须在 `PHASE2_IMPLEMENTATION_REPORT.md` 追加：

1. 批次、授权原文和范围对账；
2. 实际修改文件与 `git diff --stat`；
3. 每条测试的实际命令、退出码、通过/失败/跳过数量和必要日志哈希；
4. Host/Adapter、真实或模拟标识、session/invocation ID、权限与 workspace 模式；
5. 基线提交、结果提交、工作区状态和未提交文件；
6. 已知限制、风险、偏差和后续建议；
7. 明确写明“未实施的后续批次”和“待用户验收”。

2A 的完成不代表真实多 Agent 已建立；2D、2E 未分别取得真实宿主证据前，产品文案必须保持“契约/静态能力”，不能宣传为“已支持真实 Codex/Antigravity 多 Agent”。

第二阶段退出条件：

- Codex 和 Antigravity各完成至少一个真实 L2 任务；
- Builder、Reviewer、QA session 独立可证明；
- worktree 无交叉写入；
- 无证据不能流转；
- 用户确认才能验收。

## 9. 第三阶段：控制复杂度

### 9.1 拆分 Skill

目标：使用渐进式加载，避免一个 Skill 装载所有规则。

建议拆分：

```text
yy-flow-router       # 项目识别、分级、Profile、确认
yy-flow-task         # 建卡与状态流转
yy-flow-review       # 独立审查
yy-flow-qa           # 测试证据
yy-flow-release      # 合并、发布和最终验收
yy-flow-admin        # 初始化、迁移、诊断
```

执行流程：

1. 统计 SKILL.md 重复规则；
2. 把通用不变量放入短小核心参考；
3. 每个 Skill 只负责一个明确任务；
4. Router 根据任务选择加载；
5. 宿主不支持动态 Skill 时，由 Adapter 合成最小上下文；
6. 增加触发/不触发提示词测试。

### 9.2 增加 Lite / Standard / Compliance

推荐角色：

| Profile | 角色组合 | 独立性 |
|---|---|---|
| Lite | Builder + Verifier + User | Verifier 独立于 Builder |
| Standard | Coordinator + Builder + Reviewer + QA + User | Reviewer、QA 独立 |
| Compliance | PM、Architect、Dev/Frontend、Reviewer、QA、Docs、DevOps、User | 严格职责分离 |

选择规则：

- Lite：少文件、低风险、无生产/权限/数据迁移影响；
- Standard：多模块、接口变更、常规业务功能；
- Compliance：安全、财务、隐私、生产发布、不可逆数据操作或监管要求；
- 用户可以向上提升 Profile；向下降级必须给出风险并确认。

注意事项：

- 小项目角色合并不能把实现者与验证者合并；
- Profile 控制最大 Agent 数，不要求每次都启动到上限；
- L0 不应因为安装了 Skill 就自动建卡。

### 9.3 缩短重复规则

执行流程：

1. 建立唯一术语表和状态机来源；
2. 角色文件只声明差异，不复制完整流程；
3. 公共门禁从代码或 reference 引用；
4. 删除同义重复和硬编码人物姓名依赖；
5. 文案与可执行规则分离，规则由 Schema 校验。

验收标准：

- 相同门禁只有一个权威定义；
- 修改状态规则不需要同步八份 Agent 文件；
- Skill 主入口控制在可快速理解的长度。

### 9.4 按需加载参考文件

执行流程：

1. 为每个参考文件建立触发条件；
2. Router 先加载目录和摘要，不加载全文；
3. 仅在当前步骤需要时加载对应 reference；
4. 记录本次实际加载的参考文件；
5. 不同客户端分别测量上下文占用。

注意事项：

- “按需加载”不能变成只读半个必需规则文件；选中一个规则文件后必须完整读取；
- 安全门禁和项目隔离规则属于常驻最小核心；
- 客户端无法动态读取时，使用构建期生成的精简 bundle。

### 9.5 单 Agent 与多 Agent 真实评测

评测任务至少覆盖：

1. 小型单文件修复；
2. 多文件功能；
3. 隐蔽回归 Bug；
4. 安全/权限改动；
5. 文档和配置任务；
6. 外部 API 或 UI 测试任务。

每个任务分别运行：

- 单 Agent；
- Lite；
- Standard；
- 必要时 Compliance。

指标：

```text
正确性：测试通过率、Reviewer 真问题数、回归数
质量：修改范围、可维护性、缺陷逃逸率
效率：总耗时、人工等待时间、并行收益
成本：可获得时记录 Token；否则记录模型调用次数、轮次和子 Agent 数
稳定性：失败恢复、冲突、超时、权限请求数
```

注意事项：

- 不同客户端订阅模式不一定暴露精确 Token，缺失时不得伪造；
- 同一任务使用相同基线提交、相同测试和验收标准；
- 至少重复运行三次，避免单次随机结果决定策略；
- 原始日志脱敏后保存。

第三阶段退出条件：

- Profile 选择可解释；
- Lite 小项目只需 Builder + Verifier；
- 单 Agent 与多 Agent 有真实数据对比；
- 默认策略能在质量与成本间作合理选择。

## 10. 第四阶段：扩展生态

### 10.1 MCP Server

目标：把稳定的领域能力作为受控工具提供给多个客户端。

第一版工具建议：

```text
initialize_project
get_project
classify_task
create_task
get_task
list_tasks
submit_evidence
transition_task
request_acceptance
soft_delete_task
restore_task
```

执行流程：

1. 先从只读工具开始；
2. 为每个工具定义 JSON Schema、幂等键和错误码；
3. 写工具增加用户、项目、权限与证据校验；
4. 本地使用 STDIO，远程使用 Streamable HTTP；
5. 增加 Bearer/OAuth、TLS、限流、审计；
6. 私有部署使用安全隧道或内网，不默认暴露公网；
7. 做 Codex、ChatGPT Plugin、Antigravity MCP 契约测试。

注意事项：

- 不提供“任意 shell”MCP 工具；
- MCP Server 只暴露受约束业务操作；
- `transition_task` 必须服务端再次验证证据，不能信任客户端声明；
- 远程服务不得使用客户端传来的任意文件路径访问服务器文件系统。

### 10.2 ChatGPT Plugin/App

目标：让 ChatGPT Web/Work 能使用远程工作流，而不是依赖用户电脑上的 Python。

执行流程：

1. 用 Skill + MCP Server 组成 Plugin；
2. 为看板、验收和证据摘要提供可选 UI；
3. 实现 OAuth 用户认证和项目授权；
4. 把写操作配置为需要批准；
5. 提供无 UI 的 headless 工具结果，保证 Codex 也能使用；
6. 完成隐私、安全、错误处理和发布测试。

注意事项：

- ChatGPT Plugin 使用远程数据，不自动拥有本地仓库访问权限；
- 若要修改代码，需结合 Codex、本地执行器或远程受控 Runner；
- Plugin 能力可因账户、工作区管理员策略和产品 surface 不同而不同。

### 10.3 远程看板与认证

建议数据隔离键：

```text
tenant_id / user_id / project_id / task_id
```

执行流程：

1. 从本地 JSON 抽象 Repository；
2. 设计数据库迁移和版本字段；
3. OAuth 登录后建立用户与项目授权；
4. 写操作使用乐观锁和幂等键；
5. 证据对象存储与任务记录分离；
6. 审计日志采用追加写，不允许普通用户修改；
7. 提供本地导入、远程导出和灾难恢复。

注意事项：

- 不同用户同名项目不能共享命名空间；
- 删除继续采用软删除和保留策略；
- 认证、授权、审计是三件事，必须分别实现；
- API Token、客户端模型额度和外部连接器凭证必须分开管理。

### 10.4 GitHub / Jira / 飞书等连接器

执行顺序建议：GitHub → Jira → 飞书。

每个连接器统一流程：

1. 定义最小只读能力；
2. 完成 OAuth/最小权限；
3. 映射项目和任务外部 ID；
4. 增加幂等创建与双向同步冲突策略；
5. 写操作默认要求确认；
6. 增加速率限制、重试、退避和失败队列；
7. 完成沙箱账号 E2E；
8. 再开放生产账号。

注意事项：

- 不把第三方记录 ID 当成本地全局唯一 ID；
- 同步必须保存来源和版本，避免回环更新；
- 凭证不得写入项目仓库和审计正文；
- 一个连接器失败不得让本地任务状态显示假成功。

第四阶段退出条件：

- ChatGPT Web/Work 可通过 Plugin + MCP 查看并操作授权项目；
- Codex 与 Antigravity可使用同一远程领域工具；
- 租户和项目隔离测试通过；
- 至少一个外部连接器完成真实 E2E。

## 11. 测试与验证总体策略

### 11.1 测试层级

| 层级 | 内容 |
|---|---|
| Unit | 路径、状态机、证据、软删除、Profile、Schema |
| Contract | Host Adapter、Board Adapter、MCP 工具契约 |
| Integration | Git/worktree、看板服务、恢复、并发版本 |
| Host E2E | Codex、Antigravity、ChatGPT Plugin 各自真实运行 |
| Security | 项目越权、路径穿越、凭证泄漏、未认证写入 |
| Evaluation | 单 Agent 与多 Agent 质量、耗时和成本 |

### 11.2 CI 建议

```text
OS: Windows + Linux，后续补 macOS
Python: 3.11 + 当前稳定版本
模式: local board + fake host + git worktree
门禁: 单测、类型检查、格式检查、安全扫描、文档链接检查
```

真实客户端 E2E 不应依赖普通 PR CI 的个人账户，可采用发布候选人工验证或受控测试账号。

## 12. 工作量、里程碑和依赖

| 阶段 | 预计人日 | 前置依赖 | 建议里程碑 |
|---|---:|---|---|
| 第一阶段 | 2～4 | 无 | `vNext-alpha.1-trust` |
| 第二阶段 | 9～16 | 第一阶段全绿 | `vNext-alpha.2-agents` |
| 第三阶段 | 5～9 | 真实 Adapter 稳定 | `vNext-beta.1-profiles` |
| 第四阶段 | 12～25 | Core API 与项目隔离稳定 | `vNext-beta.2-ecosystem` |

总工作量约 28～54 人日。本地 MVP 建议只承诺第一阶段、第二阶段核心和 Lite/Standard，约 12～20 人日。连接器按产品逐个估算，每个成熟连接器通常另需 2～5 人日。

## 13. 推荐实施顺序与提交策略

每个编号项独立分支、独立测试、独立审查，不在一个提交中混合四个阶段。

每次开始实施前，先按 6.3 生成并冻结执行委托合同。总体方案描述了完整路线，但只有当前合同中 `allowed_items` 列出的条目获得执行授权。实施报告和复审交接分别遵循 6.5 与 6.9。

```text
1. 建立基线标签和测试报告
2. Phase 1.1 import 修复
3. Phase 1.2 本机监听
4. Phase 1.3 软删除
5. Phase 1.4 路径修正
6. Phase 1.5 auto 模拟化
7. Phase 1.6 全量回归
8. 用户确认第一阶段
9. Host Adapter 契约与 Fake Adapter
10. Codex Adapter
11. Antigravity Adapter
12. worktree 与证据门禁
13. 独立 Reviewer/QA 与用户验收
14. 用户确认第二阶段
15. Skill/Profile/评测
16. 用户确认第三阶段
17. MCP/Plugin/远程看板/连接器
```

每个提交必须包含：

- 变更目的；
- 受影响文件；
- 测试证据；
- 兼容性影响；
- 回滚方式；
- 尚未解决的问题。

## 14. 回滚原则

- 第一阶段保留旧 API 路由，但内部改为软删除；
- 路径迁移先创建兼容链接或提示，不自动删除旧目录；
- `auto_task.py` 旧行为不得通过隐藏开关重新启用；需要真实执行时走新 Runner；
- Host Adapter 可按配置禁用并降级单 Agent；
- MCP 和远程看板必须保留本地离线模式；
- 数据 Schema 升级前备份，迁移必须可重复、可审计；
- 回滚代码不能回滚或删除已经产生的审计证据。

## 15. 最终验收清单

### 可信性

- [ ] 全量测试通过且没有删除、skip 原失败测试；
- [ ] 默认仅监听 `127.0.0.1`；
- [ ] 删除可恢复；
- [ ] `/auto` 不产生任何成功状态或数据修改；
- [ ] 没有真实证据不能推进状态。

### 多 Agent

- [ ] Codex 使用真实子 Agent；
- [ ] Antigravity使用真实 `invoke_subagent`；
- [ ] Builder、Reviewer、QA 的 session 和权限可区分；
- [ ] 并行写入使用隔离 worktree；
- [ ] 小项目 Lite 至少保持 Builder/Verifier 分离；
- [ ] 最终验收必须由用户确认。

### 多项目

- [ ] 全局安装不保存项目任务数据；
- [ ] 每个项目有稳定 `project_id`；
- [ ] 任务外部 ID 包含项目命名空间；
- [ ] 错误 CWD、错误项目 ID、错误远端指纹都会阻断写入；
- [ ] 同一对话切换项目需要明确确认。

### 多客户端

- [ ] Codex 当前推荐 Skill 与 Agent 路径验证通过；
- [ ] Antigravity IDE/Desktop/CLI 分别验证；
- [ ] ChatGPT Desktop 的可用能力有明确测试记录；
- [ ] ChatGPT Web/Work 通过 Plugin + 远程 MCP 使用，不依赖本地路径；
- [ ] 各客户端不支持的能力会明确降级或 Fail-Closed。

## 16. 执行批准

第一阶段已由用户在 2026-08-24 明确验收并关闭。本文档的更新、第一阶段验收或 Git 合流均不构成第二阶段实施授权。建议用户按阶段、按子批次批准：

```text
批准第一阶段：仅实施可信化六项，完成后停止并提交测试证据。
批准第二阶段：在第一阶段验收后按 2A～2F 逐批实施真实多 Agent。
批准第三阶段：在两个真实 Host Adapter 验证后实施 Profile 和评测。
批准第四阶段：在 Core API 稳定后实施远程生态。
```

每一阶段结束后，状态保持“待用户验收”；只有用户明确确认后才能标记该阶段完成并进入下一阶段。

批准语句还应同时指定：执行宿主、执行批次、是否允许本地 commit、是否使用新 worktree，以及是否允许本批次访问全局目录或外部系统。未明确的高风险权限一律视为未授权。

第一阶段推荐完整批准语句：

```text
我批准 Antigravity 按本文 6.3 的执行委托合同实施第一阶段 7.1～7.6。
允许创建本地分支和本地提交，优先使用 New Worktree；
不允许 push、发布、安装依赖、写全局目录、写外部系统或执行第二至第四阶段。
完成后生成 PHASE1_IMPLEMENTATION_REPORT.md 并停止在待用户验收。
```

第一阶段关闭记录：

```text
我确认第一阶段 7.1～7.6 验收通过。第一阶段正式结束，不再继续修改第一阶段范围。
```

第二阶段 **2A** 推荐完整批准语句（当前环境已就绪，可直接交给 Antigravity）：

```text
工作目录固定为：
C:\Users\user\Desktop\user\multi-agent-flow-phase2-real-agents

当前开发分支必须为：
phase-2-real-agents

请完整阅读仓库根目录：
MULTI_CLIENT_MODERNIZATION_PLAN.zh-CN.md

我批准你仅执行该文档第二阶段 2A：
Host 契约、纯只读能力探测、FakeHostAdapter 和契约测试。
禁止实施 2B～2F、第三阶段和第四阶段。

执行要求：
1. 修改前执行并原样报告：
   - git fetch origin
   - git branch --show-current
   - git rev-parse HEAD
   - git rev-list --left-right --count origin/phase-2-real-agents...HEAD
   - git status --short
2. 开始前先输出：基线提交、预计修改文件、测试计划、风险点、能力假设、是否存在未提交/未跟踪文件；输出后再开始合同范围内工作。
3. 若当前分支不是 phase-2-real-agents、与远端发生分叉、工作区不干净或基线与文档冲突，立即停止，不得自行 merge、rebase、reset、stash、覆盖或清理。
4. 2A 仅允许实现：
   - HostCapabilities、AgentRequest、DispatchPlan、AgentHandle、AgentResult、ConfirmationRequest/Result 等必要 Schema；
   - HostAdapter Protocol/ABC 及统一错误、超时、取消语义；
   - 无副作用的 Host capability detection；
   - 明确 is_real_host=false 的 FakeHostAdapter；
   - 上述内容的单元测试和契约测试。
5. 2A 禁止实现或调用：
   - 真实 Codex、Antigravity 或 ChatGPT Adapter；
   - OpenAI Responses API、API Key、付费 API 或任何外部请求；
   - Evidence Store/状态流转门禁（2B）；
   - WorktreeManager 或自动分支/合并/清理（2C）；
   - Reviewer/QA 独立运行（2F）；
   - MCP、Plugin/App、远程服务、认证、连接器；
   - 用户级/全局目录写入。
6. FakeHostAdapter 的结果必须带 is_real_host=false；fake/test session ID 不得作为真实 Agent 证据，不得推进任何真实任务状态。
7. 能力探测必须零写入：不得创建目录、文件、锁、日志或会话；不确定的能力返回 unknown/unsupported，禁止猜测为 supported。
8. 不得修改第一阶段 7.1～7.6 的实现、PHASE1_IMPLEMENTATION_REPORT.md 或既有验收证据。若发现回归，停止并单独报告，不在 2A 顺手修复。
9. 不得升级或新增依赖，不得删除/弱化测试、放宽断言或添加无理由 skip。
10. 每项完成后运行相关测试；最后执行：
    python -m pytest tests -q -rs
11. 测试记录必须包含实际命令、退出码、通过/失败/跳过数量；失败必须如实保留。
12. 允许在 phase-2-real-agents 上创建本地 commit；禁止 push、发布、创建 PR、打 Tag、合并 main 或删除 worktree。
13. 创建或追加仓库根目录 PHASE2_IMPLEMENTATION_REPORT.md，记录授权范围、实际文件、diff、测试、Git 状态、限制和未实施批次。
14. 完成后立即停止在“2A 待用户验收”，不要继续 2B，不要自行宣布第二阶段完成或已验收。
15. 遇到范围不明、依赖变更、破坏性操作、外部/全局权限、真实 Host 调用需求或布局需要大规模迁移时，先请求我的确认。
```

第二阶段 **2B** 推荐完整批准语句（2A 验收后可直接交给 Antigravity）：

```text
工作目录固定为：
C:\Users\user\Desktop\user\multi-agent-flow-phase2-real-agents

当前开发分支必须为：
phase-2-real-agents

请完整阅读仓库根目录：
MULTI_CLIENT_MODERNIZATION_PLAN.zh-CN.md
PHASE2_IMPLEMENTATION_REPORT.md

我确认第二阶段 2A 验收通过并冻结 2A 范围。
我批准你仅执行第二阶段 2B：证据存储与状态门禁。
禁止实施 2C～2F、第三阶段和第四阶段。

执行要求：
1. 修改前执行并原样报告：
   - git fetch origin
   - git branch --show-current
   - git rev-parse HEAD
   - git log -5 --oneline
   - git rev-list --left-right --count origin/phase-2-real-agents...HEAD
   - git merge-base --is-ancestor 9c59b14 HEAD
   - git merge-base --is-ancestor f403d74 HEAD
   - git status --short
   预期：两个 merge-base 命令退出码均为 0；left/right 结果的左侧必须为 0。本地领先远端 7 个或更多提交属于当前已知交接状态，不应误判为分叉。
2. 开始前先输出：基线提交、预计修改文件、Schema 草案、存储根设计、门禁矩阵、测试计划、风险点，以及是否存在未提交/未跟踪文件；输出后再开始合同范围内工作。
3. 若分支错误、工作区不干净、远端出现本地尚未包含的新提交、提交历史真正分叉、上述两个基线提交不是 HEAD 祖先、2A 报告与代码不一致或需要修改 2A 已冻结契约，立即停止，不得自行 merge、rebase、reset、stash、覆盖或清理。
4. 2B 仅允许实现：
   - Evidence Schema 与必要枚举/统一异常；
   - canonical UTF-8 JSON serializer；
   - 项目受控根内的 append-only Evidence Store；
   - artifact SHA-256、证据内容哈希和读取时完整性复验；
   - 真实/模拟标记、Host/session/invocation、transition、actor、baseline/result commit 等门禁校验；
   - 不实际写看板的纯 EvidenceGate 判定接口及契约测试；
   - PHASE2_IMPLEMENTATION_REPORT.md 的 2B 追加章节。
5. Evidence Store 必须：
   - 阻断绝对路径逃逸、..、符号链接/Junction 逃逸；
   - 同一 evidence_id 只允许创建一次，禁止覆盖、更新或删除；
   - 使用原子创建，失败不得留下被当成有效证据的半文件；
   - 读取时重新计算哈希，内容或 artifact 被篡改必须 Fail-Closed；
   - 对命令、输出和 metadata 做敏感字段拦截或脱敏。
6. EvidenceGate 必须拒绝：
   - is_real_host=false 或 fake/test session；
   - 缺失 host_session_id/host_invocation_id；
   - transition、task、actor、Host 或 Adapter 能力不匹配；
   - baseline/result commit 无法核对；
   - artifact 不存在、越界或 SHA-256 不匹配；
   - Fake confirmation、模型自述或无显式用户确认 ID 的最终验收证据。
7. 严禁把 AgentHandle.invocation_token、API Key、Token、Cookie、凭证环境变量或未脱敏原始输出写入 Evidence。
8. 2B 测试只能写 pytest tmp_path/受控临时目录，不得写权威 user_data、全局目录或外部系统。
9. 2B 禁止实现或调用：
   - WorktreeManager、自动分支/合并/清理（2C）；
   - Codex、Antigravity、ChatGPT 真实 Adapter 或付费 API（2D/2E）；
   - Reviewer/QA 独立运行或用户自动验收（2F）；
   - MCP、Plugin/App、远程服务、认证和第三方连接器；
   - 现有 transition_task/quick_task 默认真实状态写入链的行为变更。
10. 不得新增/升级依赖，不得删除或弱化既有测试，不得添加无理由 skip。
11. 每项完成后运行相关测试，最后执行：
    python -m pytest tests -q -rs
    git diff --check
12. 测试记录必须包含实际命令、退出码、通过/失败/跳过数量；测试前后核对权威 board.json 哈希。测试必须隔离数据根；如被既有测试污染，只能使用合法状态 CLI 软取消污染卡并记录，禁止快照覆盖或直接编辑 board.json。
13. 允许在 phase-2-real-agents 创建本地 commit；禁止 push、PR、发布、Tag、合并 main 或删除 worktree。
14. 在 PHASE2_IMPLEMENTATION_REPORT.md 追加 2B：授权范围、实际文件、diff、Schema/存储/门禁说明、测试证据、Git 状态、风险和未实施批次。
15. 完成后立即停止在“2B 待用户验收”，不得自行进入 2C，不得宣布第二阶段整体完成。
16. 遇到范围不明、路径安全无法证明、破坏性操作、依赖升级、外部权限、需要修改冻结 2A/第一阶段或真实 Host 调用时，先请求我的确认。
```

后续 2C～2F 必须分别使用同等粒度的批准语句，不能用“继续第二阶段”一次性放行全部子批次。

第二阶段 **2C** 的完整批准语句、开发交付合同和 AutoLaw 独立复审合同见：

`docs/D04-研发过程/D01-任务/Phase2-2C-Worktree隔离实施任务书.md`
