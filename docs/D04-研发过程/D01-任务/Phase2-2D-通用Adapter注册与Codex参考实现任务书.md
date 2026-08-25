---
title: 第二阶段2D通用Adapter注册与Codex参考实现任务书
module: host-adapter
stage: Phase-2
type: task
status: draft
author: 李文通
updated_at: 2026-08-25
tags: [Adapter, Codex, 可移植性, 合规测试]
---

# 第二阶段 2D 通用 Adapter 注册与 Codex 参考实现任务书

## 1. 目标与定位

2D 不再定义为“把核心系统改造成 Codex 专用”，而是拆成两个连续准入批次：

1. **2D-1 通用 Adapter 基础设施**：实现 `AdapterRegistry`、`AdapterManifest`、能力驱动选择、验证等级和统一合规测试套件；
2. **2D-2 Codex 参考 Adapter**：在通用基础设施上实现并真实验证 `CodexNativeAdapter`。

Codex 是首个参考实现，不是核心层依赖。未来增加 Claude Code、Cursor、AutoLaw、OpenCode 或其他平台时，只新增平台 Adapter、Manifest 和 E2E，不重写看板、Evidence Gate、WorktreeManager 或编排器。

2D 只有在 2C 正式验收后才能开工。本任务书当前为设计草案，不构成 2D 开工授权。

## 2. 三档使用与降级语义

| 平台接入状态 | 可用能力 | 用户体验 |
|---|---|---|
| 没有 yy-flow Adapter | Skill/CLI、看板、人工角色切换、手工调用证据和 worktree | 接近旧版单 Agent，用户手工切换角色或复制提示词 |
| 平台有原生 Agent，但没有合规 Adapter | 平台自身可手工启动多个 Agent | yy-flow 不能自动派发、等待、取证或推进可信状态 |
| Adapter 已实现并通过合规与真实 E2E | 自动派发、独立会话、worktree、证据门禁和状态联动 | 第二阶段完整多 Agent 体验 |

没有 Adapter 时允许明确降级，但必须向用户展示降级等级；禁止把同一会话角色切换、静态 Agent 文件导出或手工复制提示词宣传为真实自动多 Agent。

## 3. 架构不变量

1. 核心编排器只依赖 `BaseHostAdapter` 和通用 Schema，不导入 `CodexNativeAdapter`、`AntigravityAdapter` 等具体类；
2. Builder、Reviewer、QA 分别通过配置的 `adapter_id` 解析，可使用相同或不同平台；
3. 核心状态机、Evidence Store/Gate 和 WorktreeManager 中不得出现客户端名称分支；
4. Adapter ID 是开放字符串并由 Registry 验证，不使用封闭的 Codex/Antigravity 枚举；
5. 注册必须显式、确定、无导入时副作用；不得扫描全局目录后自动把未知程序标记为可用；
6. 指定 Adapter 不可用时默认 Fail-Closed，不得静默换成其他计费、权限或数据边界不同的 Adapter；
7. 允许用户显式选择人工降级，但结果必须标记 `static_only` 或 `unsupported`，不能产生真实 Host 证据；
8. 每个 Adapter 使用自己的客户端账户、认证、权限、配额和 Token 计量，核心不得伪报来源；
9. 真实 Adapter 必须返回可核验的 session/thread、invocation、workspace 和 Adapter instance 身份；
10. 平台内部实现变化只能影响对应 Adapter，不得迫使核心状态机改代码。

## 4. 通用注册与验证模型

建议新增：

```text
scripts/_lib/core/adapter_manifest.py
scripts/_lib/core/adapter_registry.py
scripts/_lib/core/adapter_conformance.py
scripts/_lib/hosts/codex_native_adapter.py
tests/test_adapter_registry.py
tests/test_adapter_conformance.py
tests/test_codex_native_adapter.py
```

实际布局可以按仓库现状最小调整，但不得大规模迁移冻结模块。

建议验证等级：

```text
native_verified  # 宿主原生入口真实 E2E 已验证
cli_verified     # 稳定 CLI、机器可读结果和身份链真实验证
mcp_verified     # MCP 调用链、认证和结果身份真实验证
static_only      # 仅配置路径/格式或静态导出验证
unsupported      # 不支持或能力未知
```

`AdapterManifest` 至少描述：

- `adapter_id`、实现版本和宿主类型；
- 验证等级及验证时间；
- 支持的能力集合；
- session/invocation 身份来源；
- workspace 模式；
- 认证和计费边界类型，不保存凭证；
- 平台版本约束；
- 合规套件版本；
- 真实 E2E 证据引用。

`AdapterRegistry` 至少支持：

- 显式注册、查询、列举和按能力解析；
- 重复 ID、Manifest/实现不匹配和未知能力 Fail-Closed；
- 精确 Adapter 选择优先于自动选择；
- 自动选择必须满足全部 required capabilities，并返回选择理由；
- 禁止实例跨项目、跨用户或跨权限上下文复用；
- 无匹配项时返回结构化“不支持”，不得伪造 Fake 成功。

## 5. 通用配置目标

编排器的最终输入应类似：

```yaml
agents:
  builder:
    adapter_id: codex_native
    required_capabilities: [real_subagents, isolated_context, worktree]
  reviewer:
    adapter_id: autolaw_cli
    required_capabilities: [isolated_context, read_only_workspace]
  qa:
    adapter_id: cursor_native
    required_capabilities: [isolated_context, command_execution]
```

以上只是开放配置契约示例，不代表这些 Adapter 已实现。未通过验证的 ID 必须显示 `static_only` 或 `unsupported`。

## 6. Adapter 合规测试套件

所有平台 Adapter 必须复用同一套测试，不允许各写一套宽松标准。最低覆盖：

1. Manifest 必填字段、不可变性和版本兼容；
2. 注册重复、未知 ID、未知能力和能力不满足时 Fail-Closed；
3. capability detection 零写入、零会话、零计费；
4. Handle 归属、防伪、跨实例和跨 Adapter 拒绝；
5. session/invocation ID 缺失或复用拒绝；
6. 超时、取消、部分结果和进程异常统一语义；
7. Builder/Reviewer/QA 权限与 workspace 请求不越权；
8. 结果与 2B EvidenceValidationContext 逐项绑定；
9. 与 2C WorktreeDescriptor 的仓库、分支、路径和基线一致；
10. Token、Cookie、API Key、登录缓存和原始敏感输出不落 Evidence；
11. `static_only`、Fake 和手工输入不能升级为 verified；
12. 平台不可用时不会静默使用付费 API 或另一账户；
13. 合规单测通过不能代替真实平台 E2E；
14. 每个 verified Adapter 至少保存一个可复核的真实 E2E 证据包。

## 7. Codex 参考 Adapter 边界

Codex 路线必须明确分开：

| 路线 | 身份与用量 | 2D 默认状态 |
|---|---|---|
| Codex 当前宿主暴露的原生任务/子 Agent 能力 | 当前 Codex/ChatGPT 客户端账户、权限和用量规则 | 只有取得真实 thread/session 与调用证据后才能 `native_verified` |
| Codex CLI 可编程入口 | 本机 Codex CLI 的登录、沙箱、批准和版本规则 | 只有机器可读输出、退出语义和会话身份真实验证后才能 `cli_verified` |
| OpenAI Responses API | 独立 API Key、API 数据边界和 API 计费 | 2D 默认禁止，必须另行批准 |

三条路线不能相互冒充。检测到 `.codex/`、Agent TOML、可执行文件或当前桌面客户端，不等于已经能够从 Python 控制现有桌面会话。

2D-2 开工前必须重新查证官方 OpenAI 文档和本机可调用入口；不得依赖本任务书中的历史命令或未公开内部函数。官方资料将 Codex、API 与 Plugin/MCP 作为不同扩展面，因此每条路线必须分别认证。

## 8. 2D-1 允许与禁止范围

允许：Registry、Manifest、验证等级、能力选择、合规套件、Fake/静态 Adapter 的负面测试和必要文档。

禁止：

- 调用真实 Codex、Antigravity 或其他客户端；
- 创建外部会话、使用 API Key 或产生费用；
- 修改 2A～2C 已冻结契约，除非用户单独批准兼容性扩展；
- 把 Registry 注册成功作为真实 Host 证据；
- 自动发现并执行用户机器上的任意 CLI；
- 开始 2D-2、2E 或 2F。

2D-1 完成后必须独立复审和用户验收，再批准 2D-2。

## 9. 2D-2 允许与禁止范围

允许：Codex 参考 Adapter、真实能力探测、受控调度、超时/取消/结果归一、Evidence/Worktree 对接 seam、合规测试和真实手工 E2E 清单。

禁止：

- 未经批准使用 Responses API、API Key 或付费外部调用；
- 读取、复制或提交登录凭证；
- 写用户级 Codex 全局配置，除非用户批准精确路径；
- 依赖未公开内部函数或 UI 自动点击冒充稳定 Adapter；
- 用 Fake、mock、静态路径或模型自述作为真实 E2E；
- 自动验收、开始 2E/2F、合并 main 或发布。

如果当前 Codex 宿主没有可供 Python/CLI 稳定调用并返回真实身份的接口，2D-2 必须如实停止为 `static_only/unsupported`，不得为了完成计划伪造实现。

## 10. 真实 E2E 最低证据

Codex 参考 Adapter 的真实 E2E 至少记录：

- Adapter ID、实现版本和验证等级；
- Codex surface 与可验证版本；
- project/task/role；
- 真实 session/thread 和 invocation 标识；
- 2C worktree 路径、分支、baseline/result commit；
- 权限和沙箱模式；
- 超时、取消和失败场景至少各一次；
- Artifact SHA-256 和 2B Gate 判定；
- 调用使用的账户/认证类别，不记录秘密；
- 实际命令或宿主动作、退出码、测试数量和时间；
- 无法获取的用量数据明确标为 unknown，不推断 Token。

## 11. 多平台扩展准入

未来增加平台时只需要：

1. 实现 `BaseHostAdapter`；
2. 提供 `AdapterManifest`；
3. 通过通用合规测试；
4. 完成对应平台真实 E2E；
5. 由用户批准注册为 verified；
6. 不修改核心编排和其他 Adapter。

若平台没有 CLI、API、MCP 或宿主原生可调用入口，只能保持人工模式；用户仍可使用看板和角色规则，但体验等同旧版单 Agent 手工切换。

## 12. 停止条件与交付

出现以下情况立即停止：平台接口不确定、无法取得真实身份、需要付费 API/全局目录/凭证、需要修改冻结范围、依赖升级、候选分支不干净或真实 E2E 无法证明。

每个子批次必须记录实际修改、测试命令/退出码/数量、验证等级、真实或模拟标识、权限/计费边界、Git 基线/结果、风险和未实现平台。实施方只能提交“待独立复审”，不能自行宣布用户验收。
