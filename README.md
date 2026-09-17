# Multi-Agent Team Workflow (多专家协同研发工作流 · YY-Flow)

<p align="center">
  <a href="https://yuanyii.github.io/multi-agent-flow/"><img src="https://img.shields.io/badge/🌐_Official_Site-Live_Demo-7C6CF0?style=for-the-badge&logo=googlechrome&logoColor=white" alt="Official Website"></a>
  <a href="https://github.com/YuanYii/multi-agent-flow"><img src="https://img.shields.io/github/stars/YuanYii/multi-agent-flow?style=for-the-badge&logo=github&color=38BDF8" alt="GitHub Stars"></a>
  <a href="https://github.com/YuanYii/multi-agent-flow/blob/main/LICENSE"><img src="https://img.shields.io/badge/License-MIT-10B981?style=for-the-badge" alt="License"></a>
  <a href="https://yuanyii.github.io/multi-agent-flow/"><img src="https://img.shields.io/badge/Theme-Dark%20%2F%20Light-F59E0B?style=for-the-badge" alt="Theme Support"></a>
</p>

<p align="center">
  <b><a href="https://yuanyii.github.io/multi-agent-flow/">👉 点击访问官方互动主页 &amp; 在线看板全景演示 (GitHub Pages) 👈</a></b>
</p>

> **“不要让CV工程师变成YES工程师”** —— 契约驱动的 AI 多角色协同研发工作流技能包，将项目管理、架构、开发、前端、审查、测试、文档、运维拆分为 8 大专家角色，以五层防错门控、阶段准出核验与现代可视化看板，有效防范跨角色越权、打回碎片化与状态悬挂。

---

## 🌟 核心产品特性

- 👥 **8 大虚拟专家协同**：内置 PM（严经理）、架构师（钱架构）、后端开发（李开发）、前端开发（马前端）、审查员（周审查）、测试工程师（章测试）、文档工程师（李文通）、运维管理员（吕改特），各司其职、严守红线。
- 🎯 **L0/L1/L2 任务分级体系**：派单前执行“分级三问”，L0 即时问答无卡直答（免建卡且不产生冗余），L1 轻量任务走短链快速交付验收，L2 核心代码走开发-审查-测试全流程。
- 🚪 **阶段双向 Git 门禁体系 (Stage Start & Close Gates)**：
  - **阶段开工准入**：核验前序阶段结项完结性与 Git 清洁度，输出拉取新阶段特性分支的 Git 指令向导；
  - **阶段结项准出**：执行看板全终态、WBS 双向对账、架构技术总结、PM 复盘报告及 Git 工作区清洁度 5 项硬核验，放行后输出分支合并与打发布 Tag 提醒向导。
- 🔄 **GitHub PR 状态监听与合流自动解阻 (PR Gate)**：提 PR 自动挂起【已阻塞】释放活跃并发；监听检测到 PR Merged 后秒级推进至【已完成】，自动落盘 Merge Commit SHA 审计凭据并向 PM 严经理发送结构化验收通知。
- 📊 **现代交互看板与偏好持久化**：支持纯静态离线与本地 Web 服务双模（默认 32886 端口），支持 PR/Issue 徽标与外链安全渲染、筛选/排序条件持久化保持、卡片字段按需展示、右上角主题切换与按 Tab 智能工具栏联动。
- 🛡️ **五层防错门控与并发安全**：状态与处理人原子绑定、代码层越权硬拦截（Fail-Closed）、打回不拆单（在原卡追加 `DEF-TXXX-N` 缺陷）、过程报告原位追加防孤儿、全局排他锁并发防重号。

---

## ⚡ 核心快捷指令 (Slash Commands)

| 快捷指令 | 功能描述 |
| :--- | :--- |
| **`/yy-flow`** 或 **`/yy-flow start`** | **一键激活工作流**：执行 7 步标准初始化并唤起 PM 严经理进行项目鉴定与编排 |
| **`/yy-flow status`** | **看板全局大盘与健康巡检**：一键输出项目总体进度、交付周期 (Lead Time)、专家负荷与风险阻断告警（【已阻塞】PR / 滞留 / 超限） |
| **`/yy-flow kanban`** | **启动看板 Web 服务**：本地启动可视化看板服务并输出实际访问链接（默认 32886 端口） |
| **`/yy-flow sync-pr`** | **PR 状态监听与合流解阻**：扫描【已阻塞】任务，检测 GitHub PR Merged 自动推进至【已完成】并唤起 PM 验收 |
| **`/yy-flow auto`** | **纯模拟状态链**：零写入演示 A–G 类型流转，不调用真实 Host、不修改看板 |
| **`/yy-flow run`** | **真实自动编排（2F-PROD 通用 Runner）**：读取带逐项验收标准的任务，串行执行 Builder → Reviewer → QA；要求影响面审查、逐项验收覆盖、反向场景和受控命令证据，通过语义 EvidenceGate 后停在等待用户验收 |

> 💡 **业务流转与协同全走自然语言**：需求拆解建卡、阶段开工、阶段结项、认领、提审、测试与打回等日常研发生命周期，直接使用自然语言与 Agent 对话沟通，由对应专家在后台自主调度底层脚本。

---

## 🚀 快速开始（Agent 初始化）

**前置要求**：任一支持 Markdown/Skill 规范的 AI Agent（Antigravity CLI / Codex / Claude Code / Cursor 等）；离线看板模式**无需任何外部依赖或 Token 凭证**。

### 1. 安装技能包到项目 `.yy-flow/skill`

```bash
cd /path/to/your-project

# 方式 A: degit 安装（推荐，Linux / macOS / Windows）
npx -y degit YuanYii/multi-agent-flow /tmp/yy-flow-stage && mkdir -p .yy-flow && mv /tmp/yy-flow-stage .yy-flow/skill

# 方式 B: tarball 安装（无 Node 环境时，Linux / macOS）
mkdir -p .yy-flow/skill && curl -L https://github.com/YuanYii/multi-agent-flow/archive/refs/heads/main.tar.gz | tar xz -C .yy-flow/skill --strip-components=1

# 方式 C: Windows PowerShell 安装
git clone https://github.com/YuanYii/multi-agent-flow.git .yy-flow\skill
powershell -ExecutionPolicy Bypass -File .yy-flow\skill\scripts\init_skill.ps1
```

安装后目录布局（数据与技能同根，升级删 `.yy-flow/skill` 重装不伤数据；`docs/` 是项目交付物留项目根）：
```text
<project>/
├── .yy-flow/            # 工具私有根（建议整体加入 .gitignore）
│   ├── skill/           # 技能代码
│   └── user_data/       # 初始化后生成：board/审计/锁
└── docs/                # 项目工程文档骨架（交付物，提交 git）
```

<details>
<summary><b>多项目共享安装（可选，单份只读正本 + 全局软链）</b></summary>

```bash
# Linux / macOS 一次性全局共享安装：
bash scripts/install_global.sh

# Windows PowerShell 一次性全局共享安装：
powershell -ExecutionPolicy Bypass -File .\scripts\install_global.ps1
```
- **代码共享**：`~/agent-skills/multi-agent-flow` 正本只读；
- **数据隔离**：每个项目的 `user_data/` 自动锚定各自项目根。

</details>

### 2. 在 Agent 对话中触发初始化

输入指令：**`/yy-flow`** 或 **`/yy-flow start`**（或直接说：“*使用 multi-agent-flow 初始化当前项目*”）。

Agent 将自动执行 7 步标准初始化：
1. 敏感凭据安全扫描（`check_secrets.py`）；
2. 导出 8 大专家子代理至 `.agents/agents/`；
3. 扫描项目技术架构识别语言与框架；
4. 生成宿主专属 `user_data/`（看板、工作流配置与审计日志）；
5. 建立 `docs/` 规范目录骨架并镜像归档历史文档；
6. 同步专家团队技术栈；
7. 唤起 PM 严经理输出项目定位与权限矩阵。

---

## 📋 任务流转示例

| 场景 | 示例 Prompt | 预期行为 |
| :--- | :--- | :--- |
| **L0 即时问答** | “解释一下项目架构” / “查找接口契约” | 分级三问判定为 L0 → 直接作答，免建卡且不调用 CLI |
| **L1 轻量任务** | “更新部署说明文档” / “调整数据库配置” | 走短链（B/C/D/F/G）：【待开始】→【进行中】→【已完成】→ PM【已验收】 |
| **L2 标准任务** | “实现用户登录接口与 JWT 鉴权” | 走全链（A 类）：【待开始】→【进行中】→【审查中】→【测试中】→【已完成】→【已验收】 |
| **自领取任务** | “自领取下一个待开始任务” | 核验并发上限（≤3）→ 状态先推【进行中】落库 → 开始编码 |
| **提交审查** | “提交 T0001 代码审查” | 状态推【审查中】，处理人原子移交 Reviewer 周审查 |
| **阶段开工** | “开启阶段 S1” / `/yy-flow start-stage S1` | 自动核验前序阶段结项与 Git 清洁度，输出拉取特性分支向导 |
| **阶段结项** | “结束当前阶段 S1” / `/yy-flow gate S1` | 自动运行 `check_stage_gate.py` 进行 5 项硬核验，全绿后触发 DevOps 合流打 Tag |
| **PR 合流解阻** | “同步 PR 状态” / `/yy-flow sync-pr` | 自动感知 GitHub PR Merged 状态，秒级解除【已阻塞】推至【已完成】并通知 PM 验收 |

### Production Runner 的准出要求

正式 A 类任务不能只写“功能正常”或“全量测试通过”。任务卡必须包含明确的 `验收标准:` 段落，每一项应能对应具体入口、正向结果和异常/反向场景；缺少时 Runner 会停止，不会让 Agent 自行猜测。

Runner 的 Reviewer 会收到固定候选差异、变更文件、公开符号引用、适用影响面提示，以及由 Runner 从 Skill 正确路径执行的候选级安全扫描结果，不能只凭已有测试为绿放行。差异超过内联上限时会生成带 SHA-256 的完整只读工件并明确提示读取，不会静默截断。Runner 针对每个候选 SHA 单次执行全部受控测试命令，并无条件追加 `git diff --check <baseline>..<candidate> --` 格式门禁；再把脱敏、限长的真实结果交给只读 QA。QA 对并发/原子性结论必须核对物理连接隔离、同步点和单胜者计数；SQLite `StaticPool` 共享单连接的多线程不能作为独立 Worker 证据。EvidenceGate 从磁盘 Evidence 重算报告与命令哈希。真实测试或业务缺陷才退回 Builder；Reviewer/QA 协议错误只重试原角色，工具缺失或无法启动等基础设施故障停在原阶段等待恢复。

```powershell
# 双宿主默认链：Codex Builder -> Antigravity Reviewer -> Codex QA
# --approve 只能在用户已明确授权本次非破坏性工作区操作后使用
python scripts/run_task.py start --task-id T0003 --approve `
  --test-command "python -m pytest tests -q"

# 后端、前端等多项门禁：重复传入 --test-command
python scripts/run_task.py start --task-id T0003 --approve `
  --test-command "python -m pytest tests -q" `
  --test-command "npm run build"

# Antigravity 单宿主、三个独立角色会话
python scripts/run_task.py start --task-id T0003 --approve `
  --builder-adapter antigravity `
  --reviewer-adapter antigravity `
  --qa-adapter antigravity `
  --timeout-seconds 900 `
  --test-command "python -m pytest tests -q"
```

`status` 是纯只读查询；`resume` 用于审批、退回修复或故障消除后的断点恢复。旧版已经手工把看板退回、但 Checkpoint 仍为 `PENDING_USER_ACCEPTANCE` 时，使用 `resume --reason "<完整验收缺陷>"` 对账并把缺陷注入 Builder。用户验收必须使用 Runner 返回的不可预测 `confirmation_request_id`：`run_task.py accept --task-id <id> --confirmation-request-id <id>` 或 `run_task.py reject --task-id <id> --confirmation-request-id <id> --reason "<缺陷>"`。`reject` 退回原任务，下次 `resume` 回到 Builder，不新建卡片。Checkpoint 会保存 Adapter、测试命令、超时与循环预算，新候选提交会重置该候选的 Reviewer/QA 轮次。

总尝试预算耗尽后，重复 `resume` 不增加历史次数，也不会派发角色。用户可明确授权 `resume --max-total-attempts 25`：25 是累计上限，不是额外次数；例如已用 21 次则剩 4 次。此正整数参数也适用于 `start`；恢复时省略则继承已有预算。它不清零历史计数，不改变 Reviewer/QA 单独预算、累计耗时上限或质量门禁，不应无限提高预算掩盖持续失败。

累计活跃耗时预算耗尽后同样会在派发角色前停止，且不会消耗一次尝试。用户核实宿主连通性并明确授权后，可使用 `resume --total-wall-clock-timeout-seconds 4500` 覆盖累计秒数上限；该正整数参数也适用于 `start`。覆盖只提高上限并保留历史耗时，恢复后会写回 Checkpoint，省略参数时继续继承；它不会跳过角色、测试、EvidenceGate 或其他安全门禁。

QA Evidence 使用同一份规范化、脱敏后的元数据计算报告哈希、落盘和构造期望上下文；EvidenceGate 仍精确比较磁盘内容并重算哈希，不在校验时忽略差异。校验错误只报告不一致的字段名，不回显可能包含敏感内容的原始报告。错误返回保留当前候选代数和已有有效 Evidence；被拒绝的 QA Evidence ID 单独放入诊断，不冒充有效通过凭证，也不覆盖历史文件。

Checkpoint 还冻结初始需求哈希、验收标准哈希、项目/Authority Root 和 Git 基线；恢复与验收会拒绝契约漂移。修复轮次必须在上一候选之上产生新 Commit（仅有工作区修改时由 Runner 受控固化）；没有新产物会停在 `NEEDS_USER_INPUT`，但保留旧候选、Evidence、会话标识和脱敏诊断。完成、验收、退回、取消均采用写前状态与重启对账，后到的 Host 输出不能覆盖已经持久化的取消。

在 Windows 上，Runner 会将裸 `npm`/`npx` 确定性解析为 PATH 中工作区外的真实 `.cmd/.exe`，并把 `python`/`pytest` 固定到启动 Runner 的可信解释器。测试子进程不会继承 Token、密码、认证头或代理凭据；找不到工具时返回 `NEEDS_USER_INPUT`，不会伪装成代码测试失败或触发无效 Builder 修复。

Antigravity Prompt 通过官方 stdin `stream-json` 协议传输，不进入 Windows 命令行参数，因此完整 Reviewer diff 不受约 32 KiB 的 `CreateProcess` 命令行上限影响。适配器会把最终 `structured_output` 与 Planner/进度消息隔离，并兼容过程文本后附带的裸 JSON；运行期间 stderr 会持续输出 `[YY-FLOW]` JSONL 事件，明确显示 `BUILDER -> REVIEWER -> QA -> PENDING_USER_ACCEPTANCE`。Reviewer/QA 协议重试达到上限时，Runner 会先原子保存 `NEEDS_USER_INPUT` Checkpoint，再返回最终结果，确保状态查询与恢复依据一致。

如果 Antigravity GUI 已登录但 `agy` 无头 CLI 仍提示 OAuth，需先在运行 Runner 的同一 Windows 用户/终端上下文完成 CLI 登录。Runner 不会绕过该认证，也不会把登录失败伪装成 Reviewer 或 QA 成功。

Host 权限拒绝属于显式暂停条件：Runner 会原子保存 `APPROVAL_REQUIRED`，释放运行锁并要求用户在 Runner 外部处理授权。对明确可恢复的宿主基础设施错误（模型容量不足/503、429 限流、502/504 网关错误和连接重置），Runner 默认执行最多 3 次有界指数退避重试；每次使用新的宿主 Session，写入 `host_attempt_history`，但不消耗 Builder/Reviewer/QA 业务轮次。Builder 只有在 Git HEAD 与完整工作区状态均未变化时才允许自动重试，防止不确定的部分写入被重复执行。权限、认证、配额耗尽、协议错误、业务失败及未知错误绝不按瞬时故障重试；重试耗尽后才持久化为 `NEEDS_USER_INPUT`，不会遗留假的 `BUILDING`、`REVIEWING` 或 `QA_TESTING`。可通过 `--host-transient-max-retries`、`--host-transient-retry-base-seconds` 和 `--host-transient-retry-max-seconds` 收紧或关闭该机制，但不能用它绕过质量门禁。Runner 和角色 Agent 均不得修改用户全局 Antigravity/agy/Codex 设置、创建绕权限诊断脚本，或使用 `command(*)`、`unsandboxed(*)`、`--dangerously-skip-permissions` 等通配/跳过规则。检测到这类危险全局配置时将 Fail-Closed 停止，且 `--approve` 不能绕过安全门禁。

Builder 瞬时失败若已留下部分文件修改，Runner 不会自动重放、删除或归属这些修改。恢复时默认以 `PREEXISTING_BUILDER_CHANGES` 停止并只列出相对路径；Checkpoint 当前角色为 Builder 且上次错误属于明确瞬时宿主故障，或暂存/提交失败并保留真实 Builder session/invocation 时，可在一次 `resume` 中显式传入 `--recover-partial-builder-changes`。托管 Builder 随后必须检查完整现有差异、修正或补全后交回，Runner 才能受控提交；该授权不持久化到后续恢复，也不允许接纳来源不明的普通脏工作区。缺少授权的暂停保留恢复资格，不因错误文案更新丢失恢复依据。

候选暂存按实际变更的字面路径与 NUL 分隔清单执行，兼容忽略的控制目录、已暂存改动、删除、重命名及中文/空格路径；不使用 `git add -f`，不把排除目录作为显式 add 目标。如果控制文件已在用户索引中，停止而不擅自取消暂存或混入候选。暂存/提交失败标记为 `CANDIDATE_FINALIZATION_FAILED`，与无代码产出的 `BUILDER_NO_CANDIDATE` 分开。宿主重试采用内容指纹（含索引和未跟踪文件），不能仅凭相同的 `M` 状态重放写操作；取消失败不启动重叠会话，活跃耗时从本轮固定起点累计，避免重试重复计费。

QA 受控命令采用明确的三态路由：只有命令不存在、测试运行器缺失、超时、磁盘/内存、网络连通性、认证或 TLS 等可确定宿主故障才暂停等待环境处理；pytest setup/teardown、SQL 表结构、编译、断言及其他非零退出作为候选证据交给独立 QA，由 QA 自动判定并在需要时退回 Builder。未知非零退出不再被武断标为基础设施故障，也不会在语义 QA 之前反复要求人工介入。
Runner 拥有的非零命令证据不可被模型 PASS 覆盖：由此产生的 `QA-COMMAND-GAP` 属于确定性业务门禁失败，直接进入原任务 Builder 修复闭环，不消耗协议格式重试；只有 JSON、身份和语义自相矛盾等真正的角色交接问题才使用有界协议重试。
当测试已经失败或实际执行 0 项测试时，QA Host 即使返回截断、重复 JSON 或其他非法载荷，Runner 也会保留真实 session/invocation 与脱敏协议诊断，使用自身不可变的命令证据生成 `RUNNER_COMMAND_GATE` FAIL Evidence 并自动退回 Builder。只有所有受控命令通过后，QA 的非法报告才会阻断准出并进行有界协议修复，防止以格式降级绕过独立质量判断。

Antigravity 的 Production Runner 使用 `flow-runner-builder`、`flow-runner-reviewer`、`flow-runner-qa` 三份托管执行配置（不是新增业务角色）：Builder 只有项目内文件读写能力，Reviewer/QA 只保留 Runner 明确指定的工作区内文件定点读取能力，不提供目录遍历、全局搜索或 `run_command`。每次托管派发都会注入不可被自定义提示覆盖的规范化工作区根边界，禁止绝对路径、`..`、链接逃逸、用户目录、`.gemini`、`.codex`、其他仓库及任何工作区外读取。Git、候选 Commit、安全扫描、测试、构建与格式门禁均由 Runner 确定性执行；角色只负责专业修改或独立判断。独立使用 `flow-dev`、`flow-reviewer`、`flow-qa` 时仍保留各自 YAML 声明的原有能力，不受托管配置影响。

Runner 成功只停在【已完成】/`PENDING_USER_ACCEPTANCE`，不会替用户验收、合并、Push 或创建 Tag。

QA 失败诊断：Runner 对输出逐行脱敏，并整体遮蔽多行私钥，保留安全错误上下文；Evidence 的 `test_diagnostics` 保存每条命令最后 12000 字符及截断标记。返工 Builder 同时收到原需求、验收标准、候选 SHA 和该诊断。能够识别的失败测试进入修复；依赖缺失、超时、未收集到测试及无法分类的执行错误暂停等待诊断。旧任务缺少日志时，恢复会在固定、干净候选上重跑原配置的受控测试以补齐诊断；这不是重放历史 PASS。空回复不能证明没有编辑，最终必须检查真实 Git 变更；无新候选时保留 Host 调用标识并明确停止。`status` 将非权限暂停状态下的旧权限信息显示为 `historical_approval_diagnostics`，避免误当作当前阻断。

受控 npm QA 命令支持 `npm test`、`npm run test`、严格命名空间形式 `npm run test:<suite>`（例如 `test:mysql`、`test:integration:mysql`）以及精确的 `npm run build`。命名空间每段仅允许字母、数字、点、下划线和连字符；`deploy`、`migrate`、`pretest`、`posttest`、`build:*`、路径逃逸和 Shell 运算符继续 Fail-Closed。测试命令配置不受支持时停在 `NEEDS_USER_INPUT / QA`，保留候选提交和已有 Reviewer PASS Evidence，修正配置或升级 Runner 后可从 QA 恢复。

恢复兼容性与边界：已有 Checkpoint 必须使用 `resume`，普通 `start` 不覆盖历史记录。只有明确重启已取消任务时使用 `start --restart-cancelled`（其余参数同首次启动）；旧记录先归档，新运行拒绝旧运行的迟到写入。恢复跳过 Builder 或 Reviewer 前必须找到绑定同一候选与契约的有效前序 Evidence；看板状态本身不能作为跳阶段依据。旧契约快照只有在需求哈希和验收哈希均完全一致时才自动升级，无法证明只是流程日志差异时会停止并报告，不会用“验收标准相同”覆盖需求变化。

权限暂停会保存脱敏、限长的 `approval_diagnostics`（宿主返回的会话、调用、工具事件），单独累计 `approval_attempts`，不消耗本次被拒绝调用的修复/审查/QA 重试预算；不回写修正旧版历史计数。宿主未提供具体授权目标时仍须用户处理，不能据此猜测或添加通配授权。历史 Evidence 不代表当前整改通过。

已有 Runner Checkpoint 的任务，通过 `transition_task.py` 推进开发、审查、测试或验收时，也必须提交该 Checkpoint 中的对应 Evidence 并通过重放校验；应由 Runner 调用，不要手工推进。未使用 Runner 的既有单角色流程保持原契约，这项保护不等于给所有传统工作流新增正式 A 类准出资格。

执行 `accept` 时会再次核对精确候选 HEAD、工作区不可变性、`git diff --check`，并从磁盘重放同一 SHA 且按时序独立的 Builder → Reviewer PASS → QA PASS Evidence；QA 的验收哈希、受控命令退出码、未覆盖风险和缺陷计数任一不满足均拒绝验收。显式 `accept` 会生成并验证 `USER_CONFIRMATION` Evidence，`confirmation_request_id` 本身不等于验收凭据。

---

## 🚀 本地可视化看板启动

无需安装任何第三方库或 Node 依赖，一行命令启动本地实时可视化看板：

```bash
# Linux / macOS
./kanban/start.sh

# Windows PowerShell
.\kanban\start.ps1

# Windows CMD
.\kanban\start.bat

# 或直接运行 Python 核心脚本（全平台通用）
python scripts/start_kanban_server.py
```
启动后在浏览器打开控制台输出的本地 Web URL（默认 `http://127.0.0.1:32886/`）即可。

---

## 🖥️ 看板界面预览

看板内置四套视图，同一份数据自由切换（以下为本地 Web 服务实时界面截图，截至 2026-08-18）：

**数据表格视图** —— 任务明细列表，支持筛选、排序、搜索、批量删除与 JSON 导入导出：

![数据表格视图](https://fastly.jsdelivr.net/gh/YuanYii/multi-agent-flow@main/kanban/screenshots/table-view.png)

**看板-按状态视图** —— 按任务状态分组，卡片跨列拖拽即触发流转审计：

![看板-按状态视图](https://fastly.jsdelivr.net/gh/YuanYii/multi-agent-flow@main/kanban/screenshots/kanban-status.png)

**看板-按负责人视图** —— 按专家角色查看各自任务负载：

![看板-按负责人视图](https://fastly.jsdelivr.net/gh/YuanYii/multi-agent-flow@main/kanban/screenshots/kanban-assignee.png)

**看板-按阶段工作包视图** —— 按阶段工作包（S1–S6）查看任务分布：

![看板-按阶段工作包视图](https://fastly.jsdelivr.net/gh/YuanYii/multi-agent-flow@main/kanban/screenshots/kanban-stage.png)

---

## 📁 目录架构说明

```text
.yy-flow/skill/              # 技能代码（只读资产）
├── SKILL.md                 # 技能主入口（快捷指令与编排协议）
├── README.md                # 产品说明文档
├── rules/                   # 协作红线与防错规约（AGENTS/IDENTITY/SOUL/TOOLS/USER/HEARTBEAT）
├── agents/                  # 8 大专家角色 YAML 定义
├── kanban/                  # 离线与 Web 可视化看板（HTML/JS/CSS）
├── config/                  # 工作流与架构配置模板
├── references/              # 6 大核心规范（路由/流转/防错/Git/文档/交接）
├── templates/               # 标准化报告与文档模板
├── tests/                   # 450+ 项自动化测试套件（覆盖 Runner、Evidence、角色导出等模块）
└── scripts/                 # 初始化/流转/门禁/度量/看板服务/PR解阻 CLI 引擎

# 初始化后在目标项目生成：
.yy-flow/user_data/          # 运行态数据（看板数据 board.json / 审计日志 / 并发锁）
docs/                        # D01-项目管理 ~ D06-文档模板 六分类工程文档骨架
```

---

## 📖 参考规约索引

- [技能主入口 SKILL.md](SKILL.md) — 指令契约、初始化 SOP 与动态流转
- [AI 团队协同索引](references/01-AI-Team-Workflow-Index.md) — 8 大角色职责矩阵与流转总表
- [状态流转与打回规范](references/02-State-Flow-Rules.md) — 8 状态定义、A-G 任务类型与三问判定
- [五层防错门控机制](references/03-Anti-Error-Mechanism.md) — 越权拦截与代行授权协议
- [分支与版本发布规范](references/04-Git-Workflow-Spec.md) — 三层分支模型与 SemVer 标签
- [项目文档管理规范](references/05-Document-Management-Spec.md) — 目录骨架与元数据 Frontmatter 标准

---

## License

MIT
