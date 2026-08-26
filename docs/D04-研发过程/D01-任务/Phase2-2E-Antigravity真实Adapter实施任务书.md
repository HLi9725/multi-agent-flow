---
title: 第二阶段2E Antigravity真实Adapter实施任务书
module: host-adapter
stage: Phase-2
type: task
status: active
author: 李文通
updated_at: 2026-08-26
tags: [Antigravity, Adapter, 权限, Sandbox, 自动化, E2E]
---

# 第二阶段 2E Antigravity 真实 Adapter 实施任务书

## 1. 目标与前置条件

2E 在已验收的 2A～2D-1 基础设施上实现真实 `AntigravityAdapter`，取得 Windows 宿主的真实 session、invocation、workspace 和 Evidence 身份链。macOS 当前保持 `static_only`，Linux 按实际入口标记 `static_only` 或 `unsupported`。

2E 同时解决日常开发中大量重复终端确认的问题：对工作区内、稳定、可审计的命令使用项目级最小权限预授权；对网络、工作区外写入、费用、凭证、破坏性操作、外部发布和最终验收继续强制询问。减少重复审批不等于绕过 Antigravity 权限系统。

前置门禁：2D-2 必须先完成独立审查、QA 和用户验收。本文档是范围合同，不构成提前开工授权。

## 2. 宿主入口与真实性

实施前必须重新核查 Antigravity IDE、Desktop、CLI 当前公开能力，选择能够稳定创建独立 Agent、返回真实 invocation 并支持超时/取消的 surface。不同 surface 分别注册和验证，不得用 IDE 的静态路径替代 CLI E2E，也不得用模型自述冒充宿主证据。

至少实现：

1. 创建、等待、取消真实独立 Agent 会话；
2. `inherit`、`branch`、`share` workspace 模式的明确映射，写任务默认使用受控 `branch`/worktree；
3. project、task、role、adapter instance、session、invocation、workspace、baseline/result commit 的完整绑定；
4. 超时、取消、权限拒绝、额度不足、宿主退出和部分结果的统一异常映射；
5. Artifact SHA-256、EvidenceRecord 与 EvidenceGate 的真实校验；
6. 使用当前 Antigravity 账户、模型、权限与额度边界，不保存凭证，不推断无法取得的 Token 用量；
7. Windows 真实 E2E，macOS 静态契约测试，Linux 能力声明。

## 3. 权限与审批优化契约

### 3.1 五类权限档

| 档位 | 典型操作 | 默认策略 |
|---|---|---|
| `safe_local` | 工作区内读取、稳定状态查询、受控测试、Git 只读检查 | 项目级 Allow，可自动执行 |
| `controlled_external` | 网络、依赖下载、工作区外精确目录访问 | Ask，逐次或按精确范围批准 |
| `destructive` | 删除、清理 worktree、reset/clean/rebase、覆盖用户修改 | Deny 或每次 Ask |
| `billing` | API Key、付费 API、模型/额度扩张 | 必须用户单独确认 |
| `acceptance` | Push、Merge main、发布、最终验收 | 必须用户明确确认 |

Adapter 必须记录权限决策来源、作用域、Sandbox 状态和拒绝原因，但不得记录 Token、Cookie、认证缓存或完整敏感命令输出。

### 3.2 稳定命令入口

日常工作流必须优先复用仓库入口：

```text
python scripts/heartbeat.py
python scripts/quick_task.py ...
python scripts/transition_task.py ...
python scripts/check_stage_gate.py ...
python -m pytest ...
git status / diff / rev-parse / show / log / worktree list
```

禁止把常规查询拆成不断变化的 `python -c`、动态 heredoc、临时 Patch 脚本或字符串拼接 Shell。若现有 CLI 缺少必要的只读查询能力，应在本批次内提出稳定、参数化、可测试的入口；不得用宽泛放行任意 Python 代替。

### 3.3 项目级最小权限

默认建议保持 Antigravity `Request Review` 或 Sandbox 下的等价安全策略，只对精确命令族建立项目级 Allow List。以下规则不得自动安装，必须先展示给用户并取得确认：

```text
Allow: yy-flow 稳定 CLI、pytest、Git 只读检查
Ask: 网络、依赖安装、外部目录、git commit、权限扩张
Deny/Ask: git reset/clean/push/merge/rebase、删除和任意内联代码
```

不得默认启用 `Always Proceed`、`--dangerously-skip-permissions`、`command(*)`、`command(python)` 或 `command(python -c.*)`。Deny/Ask 必须优先于 Allow，规则冲突时 Fail-Closed。

如果权威看板与代码 worktree 位于两个目录，应让用户把两个精确目录加入同一 Antigravity Project Folders；不得通过开放任意非工作区访问解决多根目录问题。

## 4. Adapter 行为要求

1. 能力探测必须零写入、零会话、零费用、零权限弹窗；
2. 调度前根据任务所需权限与宿主策略做预检，缺少权限时返回结构化 `approval_required`，不得先启动后卡死；
3. 可安全复用的批准仅限相同项目、相同 Adapter instance、相同命令族和相同权限边界；禁止跨项目、跨账号或跨 worktree 继承；
4. 用户拒绝后立即停止相关调用，不得换用另一账户、另一 Adapter 或付费 API；
5. 等待用户批准不计作成功，也不得推进真实看板状态；
6. Builder、Reviewer、QA 使用各自独立 session；Reviewer 默认只读，QA 仅取得测试所需命令；
7. 2E 只提供真实 Adapter，不负责 2F 的完整自动编排。

## 5. 测试与真实 E2E

除通用 Adapter 合规套件和 2A～2D 全量回归外，至少增加：

1. 已批准的稳定安全命令连续执行两次，第二次不得出现重复审批；
2. 参数改变但仍属于批准命令族时，必须保持在声明能力内；
3. `python -c`、任意 Shell、网络、外部目录和破坏性 Git 不得被安全规则误放行；
4. Ask、Deny、用户拒绝和无交互 headless 场景均返回正确结构化状态；
5. Sandbox 开启时，工作区外文件、注册表、网络和子进程逃逸被拦截；
6. 权限缓存不得跨项目、账号、session、Adapter 或 worktree 串用；
7. 多 Agent 并发时，不得把一个窗口的批准应用到另一个不同权限任务；
8. 权威看板和代码 worktree 双根目录配置可工作，未配置时 Fail-Closed；
9. `Always Proceed` 仅可作为用户显式选择的高风险对照测试，不得成为通过条件；
10. 日志与 Evidence 中无凭证、Cookie、Authorization、API Key 或用户级配置内容。

真实 E2E 报告必须记录：Antigravity surface/版本、验证等级、Terminal Execution Policy、Sandbox 状态、Project Folders、规则作用域、实际提示次数、session/invocation、workspace、权限拒绝用例、命令退出码和 Evidence 引用。

## 6. 允许、禁止与停止条件

允许：真实 Antigravity Adapter、权限策略探测、项目级规则建议、稳定 CLI seam、合规测试、Windows E2E 和必要文档。

禁止：自动改写用户全局 Antigravity 设置、读取凭证、跳过权限、UI 坐标自动点击、使用 Fake 作为 E2E、实现 2F、自动验收、Push/Merge main、发布、依赖升级或写入未经批准的全局目录。

遇到以下情况立即停止并请求用户：需要修改全局设置、需要非工作区访问、需要网络/付费能力、真实 invocation 无法取得、权限拒绝无法结构化、必须修改冻结契约、需要依赖升级或 E2E 只能靠 UI 自动化。

## 7. 交付与停止位置

完成后在 `PHASE2_IMPLEMENTATION_REPORT.md` 追加：授权原文、实际 surface、权限策略、规则作用域、修改文件、测试命令/退出码/数量、真实 session/invocation、Evidence、Git 基线/候选、风险和未实施范围。

实施方只能提交“2E 待独立审查”，不得自行宣布 QA、用户验收或开始 2F。Reviewer 与 QA 必须使用不同于 Builder 的真实 session。
