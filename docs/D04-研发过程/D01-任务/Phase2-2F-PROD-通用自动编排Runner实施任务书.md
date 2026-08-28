---
title: 第二阶段2F-PROD通用自动编排Runner实施任务书
module: orchestration-runner
stage: Phase-2
type: task
status: active
author: 李文通
updated_at: 2026-08-28
tags: [Runner, Codex, Antigravity, 自动编排, Worktree, EvidenceGate, 用户验收]
---

# 第二阶段 2F-PROD 通用自动编排 Runner 实施任务书

## 1. 批次定位与纠偏声明

`2F-PROD` 是第二阶段重新开启后的收尾批次，不属于第三阶段新需求。

2A～2F-LIVE 已证明 Host Adapter、Worktree、Evidence、EvidenceGate、独立 Session 和双宿主 L2 在受控验收场景中可工作，但尚未交付可供任意项目日常使用的通用生产 Runner：

- `scripts/auto_task.py` 被第一阶段安全要求强制为纯模拟，`--run` 尚未启用；
- `Orchestrator` 目前是类库，没有持续运行的通用任务入口；
- `scripts/run_phase2_live_e2e.py` 是固定 T0054 与 fixture 的验收 Harness，不是生产 Runner；
- 用户仍需在客户端窗口之间复制交接包，未达到第二阶段原定 `verified_automatic` 使用体验。

因此第二阶段整体状态修正为：**基础设施已验收，生产自动编排待 2F-PROD 验收**。2F-PROD 通过独立 Reviewer、QA、真实任意任务 E2E 和用户终态验收之前，不得再次宣称第二阶段完整产品化。

## 2. 用户可见目标

用户只提交一次任务并确认启动，Runner 自动执行：

```text
读取权威任务
  -> 创建受控 Worktree
  -> Codex Builder 开发
  -> 校验并取得候选提交
  -> Antigravity Reviewer 独立审核
  -> 审核退回则自动回到 Codex Builder 修复（复用原任务）
  -> 审核通过后 Codex QA 独立测试
  -> EvidenceStore 持久化并由 EvidenceGate 逐阶段验证
  -> 停在 PENDING_USER_ACCEPTANCE / 已完成
```

正常执行期间不得要求用户复制 Prompt、候选 SHA 或交接包。只有以下情况可以暂停并请求用户：

1. 首次未包含在原始授权中的写入或权限扩张；
2. 网络、依赖安装、费用、凭证、工作区外访问；
3. 破坏性操作、Push、合并 main、发布或清理 Worktree；
4. 需求歧义导致实现方向发生实质变化；
5. 超过配置的 Reviewer/QA 退回次数；
6. 最终业务验收。

## 3. 开工门禁

以下条件全部满足才可编码：

1. 基线为本任务书及纠偏状态已提交的明确 40 位 SHA；
2. 使用独立分支与 New Worktree，推荐分支 `feature/phase2f-prod-runner`；
3. 创建新的 A 类看板任务并先合法领取到“进行中”；
4. 开发 Worktree 干净，不覆盖任何用户修改；
5. 2A～2F-LIVE 既有测试在基线可运行；
6. Windows 上 `codex` 与 `agy` Adapter 保持 `cli_verified`；macOS/Linux 仍为 `static_only`；
7. 不需要升级依赖、写全局目录或修改用户客户端安全设置；
8. 明确 authority root、代码仓库 root、Runner 状态根和受控 Worktree 根，禁止依赖模糊 CWD。

开始前必须输出：当前分支、HEAD、基线祖先关系、Git 状态、预计修改文件、测试计划、真实 Host 调用计划、权限/代理/费用风险和停止位置。

## 4. 允许范围与禁止事项

### 4.1 允许实施

- 通用 `run_task.py` CLI 与生产 Runner 服务类；
- 任务读取、验证、锁定和确定性执行计划；
- 复用 `AdapterRegistry`、Codex/Antigravity Adapter、`Orchestrator`、`WorktreeManager`、`EvidenceStore` 和 `EvidenceGate`；
- Runner Checkpoint、幂等恢复、状态查询、取消与超时；
- 自动 Builder/Reviewer/QA 调度和原任务退回重做；
- `/yy-flow run`、`/yy-flow run-status`、`/yy-flow resume`、`/yy-flow cancel` 的 Skill 契约；
- 新增单元、集成、对抗和真实 Windows E2E；
- 更新 README、SKILL、计划与实施报告。

### 4.2 明确禁止

- 重新启用或隐藏恢复旧 `auto_task.py` 的真实写入路径；
- 把 `run_phase2_live_e2e.py` 改名或包一层后冒充通用 Runner；
- 硬编码 T0054、fixture 路径、固定任务内容、固定测试数量或固定候选 SHA；
- 直接编辑 `board.json`、跳过合法状态流转或另建影子任务；
- Reviewer/QA 复用 Builder Session、Invocation 或可写权限；
- 模型自述 PASS、Fake/Mock/Static 结果推进真实状态；
- 自动用户验收、自动合并 main、Push、Tag、Release 或 Worktree 清理；
- `--dangerously-skip-permissions`、`danger-full-access`、宽泛命令白名单或 UI 坐标点击；
- 凭证、Cookie、Token、Authorization、完整代理认证信息进入日志、Checkpoint 或 Evidence；
- 进入第三、第四阶段。

## 5. 必须交付的生产入口

### 5.1 CLI

至少支持：

```text
python scripts/run_task.py start  --project-root <repo> --task-id <Txxxx>
python scripts/run_task.py status --project-root <repo> --task-id <Txxxx>
python scripts/run_task.py resume --project-root <repo> --task-id <Txxxx>
python scripts/run_task.py cancel --project-root <repo> --task-id <Txxxx>
```

`start` 允许显式选择：

```text
--builder-adapter codex_cli
--reviewer-adapter antigravity
--qa-adapter codex_cli
--max-review-cycles <n>
--max-qa-cycles <n>
--timeout-seconds <n>
--test-command <受控命令族>
```

不得使用 `--auto-approve` 绕过宿主权限。若提供非交互运行模式，权限不足必须返回结构化 `approval_required` 并安全暂停。

### 5.2 Slash Command

完成实现后，`SKILL.md` 和 `README.md` 必须明确：

- `/yy-flow auto`：仅模拟状态链，零写入；
- `/yy-flow run`：启动真实生产 Runner；
- `/yy-flow run-status`：查询任务、Session、Evidence 和暂停原因；
- `/yy-flow resume`：在权限/外部条件满足后从 Checkpoint 恢复；
- `/yy-flow cancel`：取消未完成 Host 调用并合法停止，不删除任务和证据。

## 6. 权威任务读取契约

Runner 的第一步必须读取权威看板中的现有任务，不能仅使用聊天 Prompt 作为事实来源。

### 6.1 TaskExecutionSpec

建议建立不可变 `TaskExecutionSpec`，至少包含：

- `project_id`、`project_root`、`authority_root`；
- `task_id`、任务名称、需求正文、验收标准、任务类型和当前状态；
- owner、当前 handler、允许的角色链；
- baseline branch、baseline commit；
- builder/reviewer/qa Adapter 精确 ID；
- workspace 模式和受控 Worktree 根；
- 测试计划或受控测试命令；
- 权限、auth、billing、proxy 的非秘密边界标识；
- 最大退回次数、超时、取消策略；
- 是否禁止 Push/Merge/发布/自动验收（默认全部禁止）。

### 6.2 读取不变量

1. `--task-id` 必须精确命中一条真实任务；不存在、重复、已取消或已验收时 Fail-Closed；
2. 任务的项目身份必须与 `--project-root`、Git top-level 和 authority root 一致；
3. 只允许从稳定 Board Adapter 读取，不得直接解析/改写 `board.json` 绕过适配层；
4. 缺少需求、验收标准、角色或测试边界时进入 `NEEDS_USER_INPUT`，不得让模型自行补造；
5. Runner 不得为了执行已有任务创建新任务；退回修复始终复用原 `task_id`；
6. 若显式支持 `--requirement` 新建任务，必须先通过 `quick_task.py` 合法建卡，取得真实 task_id 后再进入同一读取路径；
7. 读取和 `status` 必须零写入、零锁副作用、零 Host 调用；
8. 开始执行前以原子锁绑定 `project_id + task_id + candidate generation`，防止两个 Runner 同时执行同一任务。

## 7. 完整自动执行状态机

### 7.1 启动与 Worktree

1. 校验任务状态、项目 Git 仓库、工作区清洁度和基线；
2. 使用 `WorktreeManager` 创建唯一受控 Worktree 与功能分支；
3. Worktree Descriptor 必须绑定 task、project、repo identity、branch、path 和 baseline SHA；
4. 创建失败必须结构化回滚本次 Registry，不删除既有分支、目录或用户文件；
5. 将任务合法推进到“进行中”，写入 Runner/Worktree 身份。

### 7.2 Codex Builder

1. 通过 `codex_cli` Adapter 创建独立真实 Builder Session；
2. Prompt 必须来自 `TaskExecutionSpec`，包含需求、验收标准、允许/禁止范围和 Worktree；
3. Builder 只能写自己的 Worktree；
4. Builder 必须执行定向测试、提交候选 Commit，并返回真实 thread/item 标识；
5. Runner 重新读取 Git HEAD、工作区、diff、测试输出和 Artifact，不相信模型自述；
6. 候选必须是 baseline 后代，tracked 工作区符合策略，提交内容不得越界；
7. 生成 Builder Evidence，经 Gate 通过后才能推进“审查中”。

### 7.3 Antigravity Reviewer

1. 使用与 Builder 不同的真实 Session 和 Invocation；
2. Reviewer 默认只读候选 Worktree，不得修改源码、测试或提交；
3. 审查输入绑定精确 baseline..candidate、需求和验收标准；
4. 只有机器可解析的明确 `PASS:` 才能放行；普通完成文本、空结果或模糊结论按拒绝处理；
5. `REJECT` 必须形成 `DEF-<task_id>-<n>` 结构化缺陷和 Reviewer Evidence；
6. Runner 将原任务合法退回“已退回/进行中”，重新创建 Codex Builder Session 修复；
7. 修复后产生新候选 generation，旧候选 Evidence 保留但不可用于新候选准出；
8. 超过最大审查循环后进入 `NEEDS_USER_INPUT`，不得无限消耗 Token。

### 7.4 Codex QA

1. Reviewer PASS 后创建第三个独立 Codex QA Session；
2. QA 固定在通过审核的候选提交，只取得测试需要的只读/执行权限；
3. QA 不得修改代码、测试或通过追加提交让测试变绿；
4. 测试命令来自项目配置、任务字段或用户明确参数，并经过命令族与路径安全校验；
5. Runner 独立核对退出码、passed/failed/skipped、日志哈希和 HEAD 不变；
6. QA 失败形成结构化缺陷，原任务退回 Builder；修复后必须重新走 Reviewer，不得直接回 QA；
7. QA PASS Evidence 通过 Gate 后，任务只能推进到“已完成/等待用户验收”。

### 7.5 EvidenceGate 与最终停点

每次真实推进必须持久化并验证：

- task/project/role/transition；
- adapter、host、真实 session 和 invocation；
- workspace/worktree；
- baseline/result commit；
- capabilities 与验证等级；
- Artifact 路径、SHA-256、测试退出码；
- auth/billing/permission 非秘密边界。

Runner 完成后必须：

```text
orchestrator_state = PENDING_USER_ACCEPTANCE
board_status       = 已完成
handler            = 严经理
```

只生成不可预测且绑定任务/候选的 `confirmation_request_id`。没有用户显式确认时不得调用 `confirm_user_acceptance()`，不得进入“已验收”。

## 8. Checkpoint、恢复和进程模型

1. `start` 可以前台持续运行到暂停/终态，也可由受控服务托管；两种方式使用同一 Runner 内核；
2. 每个状态变更后原子保存 Checkpoint，包含状态、generation、Adapter Handle 引用、Evidence 引用和重试计数，不保存凭证；
3. `resume` 必须重新验证 Git HEAD、任务状态、Worktree、Host Handle 和 Evidence，拒绝过期候选；
4. 重复事件、重复 `resume` 和进程崩溃恢复必须幂等；
5. Runner 重启后不能把仍在运行的旧 Host 会话误判为成功，也不能重复启动相同角色；
6. `cancel` 只取消当前 Runner/Host 调用并合法记录，不执行 worktree remove、branch delete、reset 或 clean；
7. 同一项目可并行不同任务，但不得共享 Worktree、Session、Invocation、Checkpoint、权限缓存或 Evidence；
8. `status` 输出必须脱敏，并可明确显示当前角色、候选 generation、暂停原因和用户下一步。

## 9. 权限、代理与客户端边界

- Runner 调用 CLI Adapter，不依赖用户手工打开两个桌面窗口；
- 使用各宿主当前登录账号、额度和模型，不转移或集中保存 Token；
- 只向子进程传递明确允许的代理环境变量，敏感代理认证值不得写入 Evidence；
- `safe_local` 可复用项目级明确批准；网络、依赖、外部目录、费用、破坏性操作和 acceptance 必须 Ask/Deny；
- 用户拒绝权限后状态为 `APPROVAL_REQUIRED` 或 `PERMISSION_DENIED`，不得静默切换 Adapter/账号；
- Windows 完成真实 E2E；macOS/Linux 继续 `static_only`，不得进入 `verified_automatic`。

## 10. 建议代码边界

推荐新增：

```text
scripts/run_task.py
scripts/_lib/core/production_runner.py
scripts/_lib/core/runner_schema.py
scripts/_lib/core/task_spec_loader.py
scripts/_lib/core/runner_checkpoint_store.py
tests/test_task_spec_loader.py
tests/test_production_runner.py
tests/test_runner_recovery.py
tests/test_runner_security.py
tests/test_runner_live_e2e.py
```

允许最小修改：

```text
scripts/_lib/core/orchestrator.py
scripts/_lib/core/orchestrator_schema.py
scripts/_lib/core/worktree_manager.py
scripts/_lib/core/evidence_gate.py
scripts/_lib/hosts/codex_cli_adapter.py
scripts/_lib/hosts/antigravity_adapter.py
scripts/paths.py
SKILL.md
README.md
MULTI_CLIENT_MODERNIZATION_PLAN.zh-CN.md
PHASE2_IMPLEMENTATION_REPORT.md
```

任何冻结契约变更都必须说明兼容性原因并增加回归测试，不能为了 Runner 方便弱化既有门禁。

## 11. 测试矩阵

### 11.1 任务读取和安全测试

- 任意真实 task_id 读取、项目/authority root 对齐；
- 不存在、重复、终态、缺字段任务 Fail-Closed；
- `status` 零写入；
- 同任务双 Runner 互斥；
- 路径遍历、符号链接/Junction、跨仓库、跨项目拒绝；
- Prompt、测试命令和任务字段注入防御。

### 11.2 编排测试

- Builder PASS → Reviewer PASS → QA PASS → PENDING_USER_ACCEPTANCE；
- Reviewer REJECT → Builder 新 Session 修复 → Reviewer 新 Session复审；
- QA FAIL → Builder 修复 → Reviewer 复审 → QA 重测；
- 超时、取消、权限拒绝、Host EOF、部分结果、无 Invocation；
- 最大循环限制、断点恢复、重复事件和并发隔离；
- 任一 Evidence 缺失、伪造、哈希不符或上下文错配均阻断。

### 11.3 真实 Windows E2E

必须在临时或专用测试 Git 项目中创建一个**非 T0054、非固定 fixture** 的新任务，例如“新增一个小型纯函数及其测试”，然后只提交一次 `run_task.py start`：

1. Runner 从看板读取任务；
2. 自动创建 Worktree；
3. 真实 Codex Builder 修改代码并提交；
4. 真实 Antigravity Reviewer 独立审查；
5. 至少额外执行一次受控 Reviewer 退回路径，证明可自动重新调度 Builder；
6. 真实 Codex QA 执行项目测试；
7. 三类 Evidence 均经 Gate 通过；
8. 最终停在 `PENDING_USER_ACCEPTANCE`，任务为“已完成”；
9. 全程用户不复制交接包、不手工提供候选 SHA；
10. 未自动验收、合并、Push、发布或清理。

E2E 报告必须记录脱敏后的 Session/Invocation、候选 generation、Worktree、每轮退回原因、测试命令/退出码、Evidence ID/SHA、权限提示次数和最终状态。

### 11.4 回归

至少运行：

```text
python -m pytest tests/test_task_spec_loader.py -q -rs
python -m pytest tests/test_production_runner.py tests/test_runner_recovery.py tests/test_runner_security.py -q -rs
python -m pytest tests/test_orchestrator.py tests/test_phase2_live_e2e.py -q -rs
python -m pytest tests/test_codex_cli_adapter.py tests/test_antigravity_adapter.py -q -rs
python -m pytest tests/test_host_adapter.py tests/test_evidence.py tests/test_worktree_manager.py -q -rs
python -m pytest tests -q -rs
git diff --check
```

记录实际命令、退出码、passed/failed/skipped 数量和耗时。禁止删除测试、弱化断言或添加无理由 skip。

## 12. 验收标准

以下全部满足才可 PASS：

1. 对任意合法现有任务执行，不依赖 T0054/fixture/固定 SHA；
2. 一次启动后无需用户复制交接包即可走完 Builder→Reviewer→QA；
3. 自动创建隔离 Worktree，并取得真实候选提交；
4. Reviewer 退回和 QA 失败均在原任务自动回到 Builder，且修复后重新审核；
5. 三个角色使用独立真实 Session/Invocation，零串线；
6. 每次推进有真实 Evidence，并由 EvidenceGate 1:1 验证；
7. Checkpoint 恢复、幂等、取消、超时和最大循环生效；
8. 用户权限/费用/破坏性边界保持 Ask/Deny；
9. 最终严格停在 `PENDING_USER_ACCEPTANCE` / “已完成”；
10. 不自动验收、合并 main、Push、发布或清理；
11. `/yy-flow auto` 与 `/yy-flow run` 文档和行为不再矛盾；
12. Windows 真实任意任务 E2E 通过，macOS/Linux 未经 E2E 保持 `static_only`；
13. 全量测试和 `git diff --check` 通过；
14. 实施报告、Git、看板、Evidence 与测试数字一致。

## 13. 交付报告与停止条件

开发完成后必须：

1. 更新 `PHASE2_IMPLEMENTATION_REPORT.md`，追加 2F-PROD 章节；
2. 输出基线、候选、分支、Worktree、真实 diff stat；
3. 输出完整测试与 E2E 证据；
4. 创建本地候选 Commit，外部提交信息不得含内部任务号或虚拟人名；
5. 将任务合法推进到“审查中”并停止；
6. 不自行执行 Reviewer、QA、用户验收、main 合流或 Push；
7. 遇到依赖升级、全局目录写入、真实费用、无法取得真实 Invocation、必须修改宿主安全策略或 E2E 只能靠 UI 点击时，先请求用户确认。

## 14. 可直接交给 Antigravity 开发窗口的提示词

```text
请阅读仓库根目录：
- MULTI_CLIENT_MODERNIZATION_PLAN.zh-CN.md
- PHASE2_IMPLEMENTATION_REPORT.md
- docs/D04-研发过程/D01-任务/Phase2-2F-PROD-通用自动编排Runner实施任务书.md

我仅批准实施第二阶段收尾批次 2F-PROD：通用自动编排 Runner。
这是第二阶段纠偏，不是第三阶段。禁止进入第三、第四阶段。

目标是交付可用于任意项目和任意合法现有任务的真实生产 Runner，并确保一次启动后自动完成：
读取权威任务 → 创建受控 Worktree → Codex Builder 开发 → 取得真实候选提交 → Antigravity Reviewer 独立审核 → 审核退回则在原任务自动重新调度 Codex 修复 → 审核通过后 Codex QA 独立测试 → EvidenceStore/EvidenceGate 逐阶段验证 → 停在 PENDING_USER_ACCEPTANCE/已完成。

严格要求：
1. 使用独立分支 feature/phase2f-prod-runner 和 New Worktree；基线必须是包含本任务书的最新 main 提交。
2. 开工前创建新的 A 类任务并合法领取；禁止复用已验收任务。
3. 先输出分支、Worktree、完整基线 SHA、Git 状态、预计文件、测试计划、真实 Host 计划、权限/代理/费用风险和停止位置。
4. 实现 scripts/run_task.py 的 start/status/resume/cancel，以及可测试的 Production Runner 内核、TaskSpec Loader 和 Checkpoint Store。
5. Runner 必须通过稳定 Board Adapter 读取 --task-id 对应的真实任务；不得直接编辑 board.json，不得另建影子任务，不得只相信聊天 Prompt。
6. 禁止重新启用 auto_task.py 真实写入；禁止把 run_phase2_live_e2e.py 政名或包裹后冒充通用 Runner；禁止硬编码 T0054、fixture、固定 SHA、测试数量或任务内容。
7. Builder/Reviewer/QA 必须是独立真实 Session/Invocation；Reviewer 默认只读，QA 不得修改代码。
8. Reviewer REJECT 和 QA FAIL 必须复用原任务自动回到 Builder；修复后必须重新 Reviewer，设置最大循环，禁止无限消耗。
9. 所有真实状态推进必须持久化 Evidence 并通过 EvidenceGate 1:1 校验；模型自述、Fake、Mock、Static 不得放行。
10. 实现原子 Checkpoint、幂等 resume、并发互斥、超时、取消、权限等待和跨项目/Worktree/Session 隔离。
11. /yy-flow auto 保持纯模拟；新增 /yy-flow run、run-status、resume、cancel 的准确文档契约。
12. 运行任务书全部定向、关联和全量测试；不得删除测试、弱化断言或添加无理由 skip。
13. 完成一个非 T0054、非固定 fixture 的 Windows 真实任意任务 E2E，证明用户无需复制交接包或候选 SHA，最终严格停在等待用户验收。
14. 不得自动用户验收、合并 main、Push、Tag、Release、删除 Worktree、升级依赖、修改全局配置或绕过权限。
15. 完成后更新 PHASE2_IMPLEMENTATION_REPORT.md，创建本地候选 Commit，将任务推进至“审查中”后立即停止，等待独立 Reviewer。

任何门禁不满足、范围不明、需要依赖升级/费用/全局目录写入/宿主安全策略变更或无法取得真实 Invocation 时，立即停止并请求确认。
```
