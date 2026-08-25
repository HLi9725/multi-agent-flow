---
title: 第二阶段2D-1通用Adapter基础设施实施任务书
module: host-adapter
stage: Phase-2
type: task
status: active
author: 李文通
updated_at: 2026-08-25
tags: [AdapterRegistry, AdapterManifest, 验证等级, 能力解析, 合规测试]
---

# 第二阶段 2D-1 通用 Adapter 基础设施实施任务书

## 1. 批次定位

2D-1 将以下五项作为一个不可拆散的通用基础批次实施和验收：

1. `AdapterManifest`；
2. `AdapterRegistry`；
3. Adapter 验证等级；
4. 能力解析与确定性选择；
5. 通用 Adapter 合规测试套件。

本批次不实现、不调用、不验证任何真实 Codex、Antigravity 或其他平台 Adapter。Codex 参考 Adapter 属于 2D-2，Antigravity 真实 Adapter 属于 2E；二者只有在 2D-1 独立验收后才能开工。

2D-1 的价值是让核心编排只依赖开放契约。未来接入 Claude Code、Cursor、AutoLaw、OpenCode 或其他平台时，应只增加 Adapter、Manifest 和平台 E2E，不修改 Evidence Gate、WorktreeManager 或核心状态机。

## 2. 开工前置门禁

以下条件必须全部满足，否则保持未开工：

1. 2C 已完成 Reviewer、QA 和用户终态验收；
2. 2C 验收提交已经持久化，目标开发分支工作区干净；
3. 本任务书及 2D 总任务书已合入 2D-1 的实际开发基线；
4. 已创建独立 2D-1 开发分支和 New Worktree；
5. 已记录基线分支、完整 40 位 SHA、worktree 绝对路径和 `git status --short`；
6. 已创建新的 A 类看板任务，状态为“进行中”，不得复用 2C 或已验收的文档卡；
7. 未检测到未说明的用户修改；如存在，必须停止并请求用户决定；
8. 不需要依赖升级、网络访问、API Key、客户端登录、全局目录写入或付费调用。

推荐分支名：`feature/phase2d-1-generic-adapters`。实际名称可以调整，但开发、审核和报告必须使用同一个明确分支与候选 SHA。

## 3. 范围冻结

### 3.1 允许实施

- 不可变 `AdapterManifest` Schema 与严格校验；
- 开放字符串 `adapter_id` 的显式注册、查询、列举和解析；
- 验证等级与可信使用规则；
- Required Capabilities 的确定性解析和结构化决策；
- 通用、可复用、平台无关的 Adapter 合规测试套件；
- Fake/Static 测试 Adapter 和负面测试夹具；
- 与 2A `BaseHostAdapter`、2B Evidence Context、2C Worktree Descriptor 的只读兼容接缝；
- 必要的配置示例、实施报告和测试报告更新。

### 3.2 明确禁止

- 实现或调用 `CodexNativeAdapter`、`CodexCliAdapter`、`OpenAIResponsesAdapter`；
- 实现或调用 `AntigravityAdapter`；
- 启动真实客户端会话、子 Agent、CLI、MCP 或 API；
- 读取、复制、写入或提交 Token、Cookie、API Key、登录缓存；
- 写入用户级 `.codex`、`.gemini`、`.agents` 或其他全局配置目录；
- 产生外部费用或网络调用；
- 把 Registry 注册成功、静态配置存在、Fake 或模型自述升级为真实 Host 证据；
- 修改 2A～2C 已冻结外部契约，除非用户对精确兼容性变更另行授权；
- 自动合并、自动清理 worktree、删除分支、stash、reset、覆盖用户文件；
- 开始 2D-2、2E、2F、第三阶段或第四阶段；
- 自动宣布用户验收。

## 4. 建议文件边界

允许按现有包结构最小调整，推荐新增：

```text
scripts/_lib/core/adapter_manifest.py
scripts/_lib/core/adapter_registry.py
scripts/_lib/core/adapter_conformance.py
tests/test_adapter_manifest.py
tests/test_adapter_registry.py
tests/test_adapter_conformance.py
```

允许最小修改：

```text
scripts/_lib/core/__init__.py
scripts/_lib/core/agent_schema.py       # 仅兼容性扩展，禁止破坏2A契约
scripts/_lib/core/host_adapter.py       # 仅必要的抽象接缝，禁止真实平台逻辑
PHASE2_IMPLEMENTATION_REPORT.md
MULTI_CLIENT_MODERNIZATION_PLAN.zh-CN.md
```

禁止在通用核心文件中出现针对 `codex`、`antigravity`、`chatgpt` 等客户端名称的条件分支。

## 5. AdapterManifest 契约

`AdapterManifest` 必须是深度不可变、可确定序列化的数据对象。至少包含：

- `schema_version`；
- `adapter_id`；
- `display_name`；
- `implementation_version`；
- `host_surface`；
- `verification_level`；
- `capabilities`；
- `workspace_modes`；
- `identity_fields`；
- `auth_boundary` 和 `billing_boundary`，只描述类别，不保存凭证；
- `platform_version_constraint`；
- `conformance_suite_version`；
- `verified_at`；
- `e2e_evidence_refs`；
- `extra`，必须深度冻结并通过敏感信息检查。

最低校验：

1. `adapter_id` 是开放字符串，但必须满足稳定白名单格式、长度限制和大小写规范；
2. 所有字符串字段严格校验类型，拒绝 bool、容器、空白字符串和控制字符；
3. 版本、时间、能力、workspace 和身份字段必须使用已声明 Schema；
4. 未知字段、缺字段、错误类型和非法枚举 Fail-Closed；
5. `static_only`/`unsupported` 不得携带可推进真实 Evidence Gate 的声明；
6. verified 等级必须具有对应非空 E2E 引用；2D-1 测试夹具不得伪造 verified；
7. Manifest 与 Adapter 实例的 `adapter_id`、Host 身份和能力必须一致；
8. 序列化不得包含秘密、原始 Authorization Header 或登录缓存路径。

## 6. 验证等级

统一值：

```text
native_verified
cli_verified
mcp_verified
static_only
unsupported
```

这些值表示不同验证路径，不定义可随意比较的数值高低。解析请求应声明允许的验证等级集合，而不是使用模糊的“大于等于”。

规则：

- `native_verified`：真实宿主原生入口 E2E，身份链可核验；
- `cli_verified`：真实 CLI E2E，机器可读结果、退出语义与身份链可核验；
- `mcp_verified`：真实 MCP E2E，认证边界和调用身份可核验；
- `static_only`：仅配置、路径、格式、导出或人工检查；
- `unsupported`：不支持、不可用或无法证明。

`static_only`、`unsupported`、Fake、Mock、Test、Simulate 和人工复制结果不得作为真实状态流转证据。验证等级不得由 Adapter 自述直接升级，必须由后续平台批次的真实 E2E 和用户批准完成。

## 7. AdapterRegistry 契约

Registry 至少提供：

```text
register(adapter, manifest)
get(adapter_id)
list_manifests()
resolve(resolution_request)
```

2D-1 不要求提供运行时物理删除或自动卸载接口。

不变量：

1. 仅显式注册；禁止 import 时注册和扫描全局目录自动执行未知程序；
2. 重复 `adapter_id`、Manifest/实现身份不一致立即拒绝；
3. Registry 实例不跨项目、用户、认证或权限上下文复用；
4. 返回对象深度不可变，调用方不能修改 Registry 内部状态；
5. 精确 `adapter_id` 请求优先，指定项不可用时 Fail-Closed；
6. 禁止静默切换到另一账户、另一计费边界或另一平台；
7. 无匹配项返回结构化 `unsupported` 决策，不得返回 Fake 成功；
8. 多个候选同时满足且没有明确确定性优先规则时，返回 `ambiguous`，不得依赖注册顺序随机选择；
9. 查询、列举和解析为纯内存操作，不创建文件、锁、日志、会话或计费；
10. 并发注册/解析不得出现部分可见状态或跨实例污染。

## 8. 能力解析契约

建议定义不可变的 `AdapterResolutionRequest` 与 `AdapterResolutionDecision`。

请求至少包含：

- 可选的精确 `adapter_id`；
- `required_capabilities`；
- `allowed_verification_levels`；
- `required_workspace_modes`；
- `project_id` 和权限/认证上下文的非秘密标识；
- 是否允许人工降级；
- 明确的确定性选择策略。

决策至少包含：

- `selected_adapter_id` 或空值；
- `decision_status`：`selected`、`unsupported`、`ambiguous`、`manual_fallback`；
- 满足和缺失的能力；
- 实际验证等级；
- 选择或拒绝理由；
- auth/billing/workspace 边界摘要；
- `is_real_host`，2D-1 测试结果必须为 false；
- 不包含凭证的审计上下文。

解析顺序必须确定：Schema 校验 → 精确 ID 过滤 → 验证等级过滤 → 全部能力过滤 → workspace/边界过滤 → 唯一性判定 → 结构化决策。任何 `UNKNOWN` 能力不得当成 `SUPPORTED`。

## 9. 通用合规测试套件

合规套件必须通过 Adapter 工厂/夹具复用同一组断言，不允许各平台复制后弱化。2D-1 仅用 Fake、Static 和恶意测试 Adapter 验证套件本身。

最低测试矩阵：

1. Manifest 深度不可变、严格类型、版本和敏感信息拒绝；
2. 重复 ID、未知 ID、未知能力和 Manifest/实现不一致 Fail-Closed；
3. 精确选择、零匹配、多匹配歧义和显式人工降级；
4. `UNKNOWN != SUPPORTED`，能力必须全部满足；
5. 验证等级使用允许集合，不做错误的数值排序；
6. capability detection、Registry 查询和 resolve 零文件写入、零锁、零日志、零会话、零计费；
7. Handle 归属、防伪、跨实例和跨 Adapter 拒绝；
8. session/invocation ID 缺失、复用或来源不一致拒绝；
9. 超时、取消、部分结果和异常统一映射；
10. Builder/Reviewer/QA 的 workspace 和权限请求不得越权；
11. 与 2B EvidenceValidationContext 的 Adapter、capabilities、workspace、session、invocation 绑定一致；
12. 与 2C WorktreeDescriptor 的 project、branch、path、baseline 一致；
13. Token、Cookie、API Key、Authorization、连接串和登录缓存信息不得进入 Manifest、Decision 或 Evidence；
14. Fake/Static/Test/Mock/Simulate 无法升级为 verified；
15. 不可用时不得静默调用付费 API 或替换账户；
16. 并发注册、查询、解析不串项目、不串上下文；
17. 合规单测通过不等于真实平台 E2E；
18. 套件必须能对故意违规 Adapter 产生预期失败，证明测试不是空壳。

零副作用测试应拦截 `open`、目录创建、文件锁、网络、子进程和真实 Adapter dispatch，而不只是比较测试目录树。

## 10. 执行顺序

1. 开始前检查并记录基线；
2. 定义验证等级、Manifest、Resolution Schema；
3. 完成 Schema 定向测试；
4. 实现 Registry 的显式注册与不可变查询；
5. 完成重复、并发、身份不一致和上下文隔离测试；
6. 实现能力解析和结构化决策；
7. 完成精确选择、歧义、降级和 Fail-Closed 测试；
8. 实现通用合规套件及故意违规 Adapter 夹具；
9. 验证 2A/2B/2C 兼容接缝，不改变冻结行为；
10. 运行定向、关联和全量测试；
11. 更新实施报告并创建本地候选提交；
12. 提交独立 Reviewer；通过后进入 QA；
13. QA 通过后停止在“2D-1 待用户验收”；
14. 用户确认前不得开始 2D-2 或 2E。

## 11. 测试和交付证据

至少运行：

```text
python -m pytest tests/test_adapter_manifest.py -q -rs
python -m pytest tests/test_adapter_registry.py -q -rs
python -m pytest tests/test_adapter_conformance.py -q -rs
python -m pytest tests/test_host_adapter.py tests/test_evidence.py tests/test_worktree_manager.py -q -rs
python -m pytest tests -q -rs
git diff --check
```

如果实际文件名不同，报告必须记录真实命令。每条命令记录退出码、passed/failed/skipped 数量和耗时。

测试前后必须核对权威 `board.json` 哈希。测试产生的污染卡只能通过合法 CLI 软取消；禁止直接编辑、物理删除或用旧快照覆盖看板。测试不得删除、弱化断言或添加无理由 skip。

交付报告至少包含：

- 授权原文和范围对账；
- 基线分支、基线 SHA、候选 SHA 和 worktree；
- 修改文件与真实 `git diff --stat`；
- Manifest/Registry/Resolver/Conformance 契约；
- 全部测试命令、退出码、数量和耗时；
- 零副作用与恶意 Adapter 对抗结果；
- auth/billing/workspace 边界；
- 未实现的真实平台；
- 风险、偏差和后续建议；
- 明确状态“2D-1 待用户验收”。

## 12. 验收标准

全部满足才可 PASS：

1. 五项通用基础能力作为一个整体可用；
2. 核心无具体客户端条件分支；
3. Manifest 和决策对象深度不可变、严格校验；
4. Registry 显式、确定、上下文隔离且零副作用；
5. 能力解析不会把 UNKNOWN、Static 或 Fake 当成真实支持；
6. 多候选歧义、指定 Adapter 不可用和边界不匹配均 Fail-Closed；
7. 合规套件既能放行合规测试 Adapter，也能拦截故意违规 Adapter；
8. 2A、2B、2C 回归测试和全量测试通过；
9. 无秘密、外部调用、费用、全局目录写入和用户文件覆盖；
10. 实施报告与 Git、测试和看板证据一致；
11. 未实施 Codex、Antigravity、2F、第三阶段或第四阶段；
12. 最终停在用户验收门前。

## 13. 后续批次关系

```text
2C 用户验收
  -> 合入2D文档并建立干净基线
  -> 2D-1 通用基础（本任务书）
  -> 2D-1 用户验收
      -> 2D-2 Codex参考Adapter
      -> 2E Antigravity真实Adapter
  -> 两个平台分别取得真实验证证据
  -> 2F 独立Reviewer/QA编排与最终验收
```

2D-2 与 2E 都依赖 2D-1 的稳定契约。两者不得在同一工作树并行写；如后续并行开发，必须使用不同分支和 worktree，并在进入 2F 前由 DevOps 做受控合流与回归测试。

## 14. 可直接交给开发窗口的批准提示词

只有第 2 节门禁全部满足后，才可使用：

```text
请阅读仓库根目录 MULTI_CLIENT_MODERNIZATION_PLAN.zh-CN.md，以及：
docs/D04-研发过程/D01-任务/Phase2-2D-1-通用Adapter基础设施实施任务书.md。

我仅批准实施第二阶段 2D-1：AdapterManifest、AdapterRegistry、验证等级、能力解析和通用合规测试套件。
禁止实施任何真实 Codex/Antigravity Adapter，禁止进入 2D-2、2E、2F、第三阶段和第四阶段。

开始前先输出：分支/worktree/完整基线SHA、预计修改文件、测试计划、风险、外部调用/权限/费用、未提交文件和停止位置。门禁不满足时停止。

使用独立分支和 New Worktree；保留用户修改；不得 reset、clean、覆盖、升级依赖、Push、发布或写用户全局目录。每项完成后运行定向测试，最后运行关联回归、全量测试和 git diff --check。记录真实命令、退出码、数量和看板哈希。

完成后创建本地候选提交，更新 PHASE2_IMPLEMENTATION_REPORT.md，流转至独立审查并立即停止。不得自行宣布用户验收。
```

独立 Reviewer 必须基于固定候选 SHA 审查，重点验证核心无客户端硬编码、选择确定性、验证等级不可伪造、零副作用、上下文隔离和合规套件能拦截故意违规 Adapter。Reviewer 不得修改源码或替开发者补丁。

## 15. 可直接交给独立审核窗口的提示词

开发窗口完成本地候选提交并给出完整 SHA 后，将下方 `<BASELINE_SHA>` 和 `<CANDIDATE_SHA>` 替换为真实值：

```text
你是第二阶段 2D-1 的独立 Reviewer。你未参与本轮开发，不得采信开发者自述，不得修改源码或代替开发者修复。

请阅读：
- MULTI_CLIENT_MODERNIZATION_PLAN.zh-CN.md
- docs/D04-研发过程/D01-任务/Phase2-2D-1-通用Adapter基础设施实施任务书.md
- PHASE2_IMPLEMENTATION_REPORT.md

审核基线：<BASELINE_SHA>
候选提交：<CANDIDATE_SHA>

开始前核对分支、worktree、工作区清洁度、祖先关系、看板状态、候选提交和报告 diff_stat。候选 SHA 不一致、工作区存在未说明修改或 2C 未验收时立即停止。

逐行审查 Manifest、Registry、Resolver、Conformance 及测试。重点独立验证：
1. 核心不存在 Codex、Antigravity 或其他客户端名称分支；
2. Manifest 深度不可变且严格拒绝缺字段、错误类型、未知字段和秘密；
3. Registry 仅显式注册，重复/跨实例/跨上下文 Fail-Closed；
4. 精确ID、能力全集、验证等级集合、workspace和边界按确定顺序解析；
5. UNKNOWN 不等于 SUPPORTED，多候选无明确规则时返回 ambiguous；
6. 指定 Adapter 失败时不静默切换平台、账户或计费边界；
7. 查询/解析/能力探测零文件、锁、日志、子进程、网络、会话和计费；
8. Fake/Static/Test/Mock/Simulate 不可升级为 verified 或真实 Evidence；
9. 合规套件能主动拦截故意违规 Adapter，而不是只有 happy path；
10. 2A/2B/2C 契约和全量测试无回归；
11. 未实现或调用任何真实 Codex/Antigravity Adapter；
12. 报告、Git、测试和看板证据一致。

复跑任务书要求的定向测试、关联回归、全量测试和 git diff --check，记录实际命令、退出码、数量和耗时。测试污染卡只能合法软取消，不得编辑或快照覆盖 board.json。

存在任一 P1/P2、测试失败、范围越界、报告失真或无法证明的安全边界时判定 REJECT，并在原任务打回；全部满足才判定 PASS 并合法流转至 QA。不得 Commit、Push、合并、进入 2D-2/2E/2F 或宣布用户验收。
```
