---
title: YY-Flow单角色与完整自动编排使用指南
module: workflow
stage: Phase-4
type: guide
status: active
updated_at: 2026-10-16
tags: [单角色, 自动编排, Profile, 使用指南]
---

# YY-Flow 单角色与完整自动编排使用指南

以当前项目本地 Skill、公开 CLI 和实际策略记录为准。请核对项目级 Skill 的分支、HEAD 与导出文件，不能把工具仓库尚未发布或同步的能力当作业务项目已经安装的能力。第四阶段仍有真实 GitHub 沙箱等独立未验证项，不代表整体结项。

## 1. 四个选择字段与通用关键词

关键词用于表达意图，不是绕过门禁的口令。主协调者必须将意图落实为真实角色调用、公开命令和策略记录。

| 字段 | 可填写内容 | 含义 |
| --- | --- | --- |
| 执行方式 | 单角色 / 自动编排 | 单角色只做指定职责；编排串行衔接必要角色 |
| 操作 | 新需求 / 恢复原任务 / 只读检查 | 新需求先查重建卡；恢复沿用任务和历史；只读不修改 |
| 实施范围 | 仅后端 / 仅前端 / 前后端 / 仅文档 | 限制改动范围，不自动决定风险 |
| 流程策略 | standard / auto / lite / docs-short / 沿用原策略 | 决定质量链；恢复默认沿用 |

常用组合：

- `YY-Flow 自动编排，新需求，仅后端，策略 auto`。
- `YY-Flow 自动编排，恢复原任务 Txxxx，沿用原范围和策略`。
- `YY-Flow 单角色，Reviewer，只读审查 Txxxx 的固定候选`。
- `YY-Flow 单角色，QA，执行指定测试，不推进用户验收`。
- `YY-Flow 自动编排，仅文档，请求 docs-short，必要检查不通过则停止`。

“小项目”“简单修改”“HOTFIX”“DOCS”或任务备注不能作为免检依据。L0/L1/L2 是任务管理分级，A–G 是职责类型，Profile 是质量链；三者不能互相代替。

## 2. 谁实现，谁验证

八个业务角色按职责参与，不要求每个任务调用全部八个：PM 规划与协调；Architect 设计；DEV 后端/通用开发；Frontend 前端；Docs 文档；DevOps 运维；Reviewer 审查；QA 测试。

| Profile | 实际质量链 | 仍须满足的条件 |
| --- | --- | --- |
| standard | Builder → 独立 Reviewer → Runner 受控测试 → 独立 QA → EvidenceGate → 用户验收 | 当前候选、冻结契约、检查与阶段证据一致 |
| lite | Builder → Runner 必要检查 → 独立 Verifier → 用户验收 | 明确低风险；差异、安全扫描与配置测试通过；完整 AC 覆盖与检查引用 |
| docs-short | 文档实现 → 必要检查 → 用户验收 | 低风险 Markdown；秘密扫描、frontmatter、路径深度通过 |

Runner 内的文档产出由 Builder 阶段承接；原生编排由 Docs 专家实施。Verifier 是轻量链专属独立验证者，不是开发者改名自审，也不伪造 Reviewer/QA 证据。

托管 Reviewer、QA、Verifier 负责独立判断；Runner 负责受控命令与状态持久化。单角色能力与托管配置不要混用。权限、资金、隐私、数据库结构和生产部署等高风险不能走 Lite；风险不明确也保持 Standard。

## 3. 自动选择的真实边界

- 省略 `--profile`：保持 Standard。
- `--profile auto`：基于需求及当时可取得的候选材料选择策略；低风险纯文档可短链，明确低风险代码可 Lite，其他情况 Standard。
- `--profile lite` / `docs-short`：请求短链，但候选内容仍须通过资格与必要检查。不能强制免检。
- **Runner 启动时绑定并固化策略**。新任务没有候选差异时，`auto` 会保守选择 Standard；不是“先开发，然后自动改成 Lite”。已经固化的 Standard 也不会因后续文件少而自动降级。
- 显式 Lite 或文档短链可以在尚无候选时进入待复验状态；生成候选后仍会检查，高风险或不明确会暂停，不能凭最初请求完成。
- 恢复保留需求/验收哈希、策略、候选与历史预算。策略降级需独立明确授权和受支持入口，不能靠关键词或手改文件实现。

纯函数等低风险正向识别目前较保守，不保证所有语言或小改动都能缩减。Compliance 尚未实现，不得请求不存在的 Profile。

## 4. 两种执行入口

### 4.1 Production Runner

适用于已验证可用的 Host Adapter。用项目级 `run_task.py start/resume`，独立角色真实身份由 session/invocation 与 Evidence 核验。公开参数名称是 **`--profile`，不是 `--workflow-profile`**。

从业务项目根目录启动示意（先建卡并补全可执行验收标准；替换占位项）：

```powershell
python .yy-flow/skill/scripts/run_task.py start `
  --task-id Txxxx `
  --project-root "C:\项目路径" `
  --authority-root "C:\项目路径\.yy-flow" `
  --builder-adapter codex_cli `
  --reviewer-adapter codex_cli `
  --qa-adapter codex_cli `
  --profile auto `
  --test-command "python -m pytest -q" `
  --approve
```

Adapter 必须与本次实际宿主能力匹配；Antigravity 使用 `antigravity`。测试命令必须属于项目真实测试入口并通过 Runner 安全校验；不能把示例 pytest 套用于所有项目。`--approve` 仅表示已授权本轮受控工作区操作，不绕过 Host 权限。

恢复一次并等待退出：

```powershell
python .yy-flow/skill/scripts/run_task.py resume `
  --task-id Txxxx `
  --project-root "C:\项目路径" `
  --authority-root "C:\项目路径\.yy-flow" `
  --approve
```

恢复默认不附新 Profile、Adapter 或测试参数，沿用历史。仅明确授权后提高累计上限：`--max-total-attempts <累计次数>`、`--total-wall-clock-timeout-seconds <累计秒数>`；历史计数不清零。`--timeout-seconds` 是单次角色派发限制，不能代替累计预算。

### 4.2 Cursor 原生对话

主对话真实派发 `.cursor/agents/` 独立子代理，不依赖外部 `cursor-sdk`，也不自动等同于 Runner。读取项目导出的规则，按实际职责串行执行，用官方状态 CLI 落库。

短链必须经过实际策略选择、持久化与证据验证，不接受口头“已选择 Lite”。公开选择入口是 `select_workflow_profile.py`，要求真实需求哈希、验收哈希及完整相关文件；仅返回策略不代表检查、Verifier 或完成门禁已经通过。无法落实短链证据时保持 Standard，不能自造 PASS 或 WF_POLICY 备注替代策略文件。

Cursor 原生流程不虚构 Runner Checkpoint、EvidenceGate 或 confirmation_request_id。有 Runner Checkpoint 的任务不能未经迁移确认就切为原生模式恢复。当前第三阶段真实 Runner 演练已验证 Codex CLI；不据此宣称 Cursor、Antigravity 的新短链均已实测。

## 5. 一份跨宿主通用提示词

复制此模板，替换占位项。不要保留不适用的 AC，也不要只填“全部正常”。此模板统一表达需求，宿主执行入口仍有区别。

```text
使用 YY-Flow 执行下面的任务。
项目地址：<绝对路径>
宿主：<Cursor / Codex / Antigravity>
执行入口：<Cursor 原生编排 / Production Runner>
执行方式：<自动编排 / 单角色：PM、Architect、DEV、Frontend、Reviewer、QA、Docs、DevOps>
操作：<新需求 / 恢复原任务 Txxxx / 只读检查>
实施范围：<仅后端 / 仅前端 / 前后端 / 仅文档；列出允许和禁止改动目录>
流程策略：<auto / standard / lite / docs-short；恢复填“沿用原策略”>

需求：<目标、业务入口、接口/数据/页面影响；恢复沿用冻结契约>
验收标准：
AC-01：<输入、操作和可观察结果>
AC-02：<失败、边界、权限或并发结果>
AC-03：<兼容要求与禁止变化>
AC-04：<本项目必要测试及差异检查通过>
测试命令：<按实际项目逐条列出；恢复沿用原命令>

先读取项目本地 .yy-flow/skill/SKILL.md 与适用规则，核对实际 Skill 版本、
分支、HEAD、工作区、看板、已有任务、存活进程和锁。
新需求查重后通过官方入口建卡，补齐需求与可执行 AC，领取后实施。
恢复沿用原 task_id、候选、契约、Evidence、预算；不手改持久化文件。
已处于等待用户验收的任务不再次 start/resume。

自动编排按实际策略决定必要角色，不机械调用全部八角色：
Standard：Builder → Reviewer → 受控测试 → QA → EvidenceGate → 用户验收；
Lite：Builder → 必要检查 → 独立 Verifier → 用户验收；
文档短链：文档实现 → 必要检查 → 用户验收。
auto 以程序当时可核实的内容判断，新任务无候选时可能保持 Standard。
不得因关键词、项目规模或文件数量擅自降级。
单角色只履行指定职责，不自动扩展成完整流程，不代行其他质量角色。

Runner 模式明确使用实际可用的 Adapter 与公开 --profile 参数；
恢复不擅自覆盖策略。当前主对话只启动、监控和交接，不模拟角色编码。
Cursor 原生模式真实调用项目导出的独立子代理，通过公开 CLI 落库；
短链必须有真实策略与合法证据，不能虚构 Runner 凭据；能力不足则报告。

本次授权在上述范围内实施非破坏性改动、补充测试和生成本地候选提交；
只读检查及只读角色不获得代码写权限。既有用户改动不得覆盖。
业务缺陷在同一任务内有界修复，新候选重新检查；
环境、权限、协议和宿主暂态问题按机制分类处理，不伪装成通过。
等待实际命令退出，展示有限脱敏进度，不自动重复 start/resume。
APPROVAL_REQUIRED、NEEDS_USER_INPUT、FAILED 或预算耗尽后停止报告；
不修改全局权限、代理或凭据，不使用通配放行，不泄露完整连接串。

完成停在【已完成】等待我验收，不自动验收、Push、Merge、Tag 或发布。
报告 task_id、实际入口/Profile及理由、候选 SHA、真实角色调用标识、
检查命令与退出码、所需 Evidence、最终状态；Runner 附最终 JSON 与验收编号。
没有调用的角色写“不适用”，没有实测的能力写“未验证”。
```

单角色 Reviewer/QA 只给绑定候选的结论；看板推进由有权限的协调者核验后执行。单角色开发只交付候选，不能把未经过质量链的实现称为完整编排通过。

## 6. 初始化、更新与版本边界

新项目先安装 Skill，再执行一次初始化；已有项目不重复 `/yy-flow start`。跨前后端仓库可在共同项目根建立总计划与项目级 Skill，但必须明确子仓库根、目录范围和各自候选，不能假定父目录就是一个 Git 仓库。

```powershell
# 仅适用于尚未安装的项目
git clone --branch main --depth 1 https://github.com/HLi9725/multi-agent-flow.git .yy-flow/skill

# 已安装项目：先检查 Skill 干净；若有改动停止，不执行强制覆盖
git -C .yy-flow/skill status --short
git -C .yy-flow/skill switch main
git -C .yy-flow/skill pull --ff-only origin main
python .yy-flow/skill/scripts/verify_and_export_agents.py --target-project-dir "C:\项目路径"
git -C .yy-flow/skill rev-parse HEAD
```

main 未包含新功能时，上述更新不会取得第三阶段短链。功能分支试用须明确分支和固定 SHA，不能误称正式发布。重新导出后新开对话加载；保留 user_data、Checkpoint 和历史 Evidence。直接在工具源码仓库使用 `scripts/`，业务安装使用 `.yy-flow/skill/scripts/`。Windows 用实际 Python 解释器，Linux/macOS 通常用 python3。

## 7. 停止、验收与退回

运行中只监控，不重复启动。业务失败正常进入有界修复；权限拒绝停在 APPROVAL_REQUIRED，环境或预算等问题可能停在 NEEDS_USER_INPUT。心跳不是正在修改某文件的证据。

Runner 任一 Profile 完成后均停在 PENDING_USER_ACCEPTANCE，使用返回的 confirmation_request_id 对同一候选执行 accept/reject。新候选应换号，旧编号无效；reject 后 resume 原任务。不能拿 Standard 的三阶段证据要求 Lite，也不能拿 Lite 的 Verifier 代替 Standard 的 QA。

Cursor 原生验收核对真实子代理记录、策略、候选、检查与官方状态历史，不能调用不存在的 Runner accept。最终验收及 Git 发布均需独立明确授权。

验收提示词：

```text
请只读复核任务 Txxxx 的实际入口、Profile、候选、冻结 AC 与对应质量证据，
按该 Profile 重新核验必要检查。没有执行的阶段不得补造记录。
发现缺陷给出可复现原因和原任务退回建议；全部通过先报告，
未得到我的明确验收授权不推进【已验收】，不自动合并或推送。
```

`/yy-flow auto` 是模拟状态链，不等于真实自动开发。真正 Runner 用 start/resume；不承诺任何宿主永不超时、永不中断。

## 8. 高效执行，不降低质量

- 开发阶段先跑原缺陷反例和受影响回归；代码冻结后，由 QA（原生编排）或 Runner（托管模式）跑一次契约要求的最终检查。Reviewer 不兼任 QA，主协调者不因角色切换再次跑相同全量。
- 新源码、依赖、环境或契约变化会使相应结果失效；同 SHA 不足以证明可以复用。独立验收仍可补独立反例；禁止跨候选复用、删验收命令或伪造测试缓存。
- 子代理只加载完整公共安全规则、当前角色与动作、任务契约、原缺陷和实际调用链。无需反复读所有专家、历史看板和全套文档；安全规则不得截断。
- 使用 `build_agent_context.py --role <角色> --action <动作> --profile <已固化Profile>` 显式绑定上下文。该参数不是策略选择或降级授权，不能传尚未解析的 auto 代替真实策略。
- Runner 的 Standard 受控测试和 Lite 测试等待中每 15 秒发布命令心跳，完成后保留耗时。心跳只证明仍在等待，不证明用例通过；原始日志仍先脱敏，不流式泄露输出。原生 Cursor 应由主协调者显示命令/模型/权限等待原因及超时。
- 不默认安装 pytest-xdist 或全量并行：当前部分用例共享进程环境、端口和导出路径，须证明隔离后才能并行。没有承诺固定提速比例。

可在第 5 节模板追加：

```text
按 YY-Flow 效率契约执行：开发先反例及影响回归；冻结后由约定执行方
跑一次完整准出检查。只读审查不重做 QA；托管 QA 不重复 Runner 命令。
长命令报告开始、等待类别、超时和退出耗时，不将沉默等同卡死。
源码、环境、依赖或契约变化后重新验证；不删测试、不跨候选复用证据。
```

细节与测量基线见 `docs/D01-项目管理/12-工作流效率治理.md`。

## 9. 共享安装与项目接入

接入入口只有 `scripts/setup_project.py`。默认预览，追加 `--apply` 才写入项目级角色、规则和 MCP 配置。受管服务名是 `yy-flow`。登记只给当前操作系统用户的 `read`，不会自动打开建卡、流转或验收。

已实现的是下面这些命令，以及 apply 结束前的本地 STDIO 协议自检。协议自检会做 initialize、tools/list、项目和任务读取、未知项目拒绝、写入拒绝。它不是 Cursor、Codex 或 Antigravity 的真实连接。2026-10-16 Windows 隔离项目已完成三个客户端的真实工具发现及 `get_project`、`list_tasks` 只读查询；该结果不自动适用于其他项目、系统或业务写入权限。`--skip-self-check` 只用于故障注入，同样不是现场验证。

Antigravity 当前 App 按项目插件发现 MCP：生成 `.agents/plugins/yy-flow/plugin.json` 和同目录 `mcp_config.json`，不再只生成会被该安装版忽略的 `.agents/mcp_config.json`。更新时只迁移旧受管且未被用户改动的 `yy-flow` 条目，保留其他服务器；未知同名插件拒绝接管，用户修改过的清单或服务器条目拒绝覆盖。插件两文件和旧条目迁移共用事务日志，中断后使用下面的恢复命令。

配置生成后，在客户端打开项目根目录（不是子目录）。Cursor 从 Customize → MCPs 启用项目 `yy-flow`；Antigravity 从 Customizations → Installed 确认项目 `yy-flow` 插件。如未发现，先重开项目或新建会话；不通过增加 MCP Allow 通配规则或改全局配置来解决发现问题。然后真实调用 `get_project` 和 `list_tasks` 核对登记项目。生成器只声明协议自检，不替用户记为现场通过。移除只去掉自身 MCP 条目，保留插件清单、角色、第三方服务器和业务数据；空插件不再提供本工具。

Antigravity 的项目插件还应通过 `/plugin` 内置指南中的正式 `GetAllPlugins` 查询核对，并传入当前项目的 `workspaceUris`，只输出目标插件的名称、路径、disabled、isGlobal 和 MCP 名称，不输出认证令牌或完整技能正文。全局 Installed 页面或 CLI 空列表不是项目插件不存在的充分证据。本次项目插件的实际服务器名是 `yy-flow_yy-flow`，而声明名仍是 `yy-flow`；以真实工具列表的命名空间调用，不能写死无前缀服务器名。只看到插件或工具列表还不算真实查询通过。

首次安装共享正本（Windows，默认源 `HLi9725/multi-agent-flow`，默认入口 `main`）：

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\install_global.ps1
python "$env:USERPROFILE\agent-skills\multi-agent-flow\scripts\setup_project.py" `
  --project-root "D:\work\demo" `
  --skill-root "$env:USERPROFILE\agent-skills\multi-agent-flow" `
  --clients cursor,codex,antigravity
python "$env:USERPROFILE\agent-skills\multi-agent-flow\scripts\setup_project.py" `
  --project-root "D:\work\demo" `
  --skill-root "$env:USERPROFILE\agent-skills\multi-agent-flow" `
  --clients cursor,codex,antigravity `
  --apply
```

已有项目更新受管配置，或正本更新后再接入。正本目录有本地改动、运行看板或 `.yy-flow-running` 时，安装器会拒绝，不会自动 stash，也不会结束用户进程。

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\install_global.ps1 -Ref main
python .yy-flow\skill\scripts\setup_project.py --project-root . --clients cursor,codex,antigravity --apply
python .yy-flow\skill\scripts\setup_project.py --project-root . --diagnose
```

Linux / macOS 的 `scripts/install_global.sh` 提供同样的参数边界。当前没有在这些系统上完成一次真实安装，不能把脚本存在或语法检查写成安装成功。

诊断、只恢复未完成事务、移除仍与受管哈希一致的 `yy-flow` 条目：

```powershell
python scripts\setup_project.py --project-root "D:\work\demo" --diagnose
python scripts\setup_project.py --project-root "D:\work\demo" --recover --apply
python scripts\setup_project.py --project-root "D:\work\demo" --remove
python scripts\setup_project.py --project-root "D:\work\demo" --remove --apply
```

适用边界：只生成项目内配置；保留其他 MCP、TOML 注释和运行数据；同名非受管条目或用户改过的受管文件会拒绝覆盖。不写 `github_credential`，不把进程里的令牌写入生成文件，不修改用户全局客户端配置。
