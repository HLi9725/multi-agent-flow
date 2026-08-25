---
title: 第二阶段2C Worktree隔离实施任务书
module: multi-agent-runtime
stage: Phase-2
type: task
status: active
author: 李文通
updated_at: 2026-08-25
tags: [Worktree, 隔离, 安全, 交接]
---

# 第二阶段 2C Worktree 隔离实施任务书

## 1. 批次定位

2A Host 契约与 2B Evidence Store/Gate 已验收并冻结。2C 只建立可供后续 Host Adapter 使用的 Git worktree 隔离基础，不调用真实 Codex、Antigravity 或其他 AI 客户端，不实现独立 Reviewer/QA 编排。

本批次完成后必须停止在“2C 待独立复审”，不得自行进入 2D～2F、第三阶段或第四阶段。

## 2. 权威工作区与基线

- 工作目录：`C:\Users\user\Desktop\user\multi-agent-flow-phase2-real-agents`
- 开发分支：`phase-2-real-agents`
- 2C 代码工单：`T0023`，当前必须保持【待开始】，由 Antigravity 开工前合法领取
- 2B 已验收候选提交：`f6b79e903fb39d8724bfe303e817d7a0748cd7b6`
- 实际 2C 起始基线：包含本任务书的最新本地 `HEAD`；执行提示词必须给出该 SHA，且 `f6b79e9` 必须是其祖先。
- 权威数据根：上述工作目录内的 `user_data/`；其他 worktree 的看板只读。

发现分支不符、工作区不干净、远端存在本地未包含提交、历史分叉、`f6b79e9` 不是祖先或权威看板不一致时，立即停止，不得自行 merge、rebase、reset、stash 或覆盖。

## 3. 允许范围

仅允许实现以下能力：

1. 不可变的 Worktree 请求、描述符、状态与统一异常；
2. `WorktreeManager`，以参数数组方式调用本机 Git，不得使用 `shell=True`；
3. 校验 Git 仓库根、Git common dir、基线提交、目标分支、受控 worktree 根和仓库身份；
4. 为 `project_id + task_id + role + session/invocation` 建立唯一隔离标识；
5. 在受控根内创建新分支和 worktree，并返回真实路径、分支、基线提交和仓库身份；
6. 对同名分支、同路径、并发创建、已有目录、符号链接/Junction、`..`、绝对路径注入和非法 Git ref 执行 Fail-Closed；
7. 提供只读的 `inspect/list/verify` 能力；
8. 提供“可清理性检查/清理计划”，但不得在 2C 物理删除 worktree、分支或用户文件；
9. 最小项目内登记与锁机制；登记必须原子写入、项目隔离、不可把凭证或原始提示词落盘；
10. 单元测试、真实临时 Git 仓库集成测试和实施报告追加。

建议文件，不强制大规模迁移：

```text
scripts/_lib/core/worktree_schema.py
scripts/_lib/core/worktree_manager.py
tests/test_worktree_manager.py
PHASE2_IMPLEMENTATION_REPORT.md
```

如需修改已冻结的 `agent_schema.py`、`host_adapter.py`、Evidence 实现、第一阶段代码或现有 CLI 默认状态链，必须停止并请求确认。

## 4. 明确禁止事项

- 禁止实现或调用 Codex、Antigravity、ChatGPT、AutoLaw 等真实 Adapter；
- 禁止自动合并、自动 rebase、自动提交、自动 push、PR、发布、Tag；
- 禁止执行 `git reset`、`git stash`、`git clean`、强制 checkout、`git worktree remove`、`git worktree prune` 或删除分支；
- 禁止在失败回滚中递归删除非本次创建且已验证归属的目录；
- 禁止清理用户未提交文件，禁止覆盖已有目标目录或分支；
- 禁止写用户级 Codex/Antigravity 全局目录；
- 禁止新增或升级依赖；
- 禁止删除测试、弱化断言或增加无理由 skip；
- 禁止用 mock Git 成功路径替代真实临时仓库集成测试；
- 禁止进入 2D～2F、第三阶段或第四阶段。

## 5. 安全不变量

1. **受控路径**：目标路径经过绝对化、`realpath` 和 `commonpath` 校验；受控根自身及父链不得是越界 Junction/符号链接。
2. **仓库绑定**：记录并复核仓库 root、common dir 和仓库身份；不得把项目 A 的 worktree 登记到项目 B。
3. **显式基线**：创建必须指定完整 commit SHA；不存在、歧义或不属于目标仓库时拒绝。
4. **分支唯一**：分支名必须通过 `git check-ref-format --branch`，禁止调用方注入 Git 选项。
5. **无覆盖创建**：目标目录、分支或登记键已存在时拒绝；并发请求最多一方成功。
6. **命令安全**：所有 Git 调用使用固定可执行文件和参数列表，捕获退出码、stdout/stderr；报告前脱敏。
7. **失败保守**：只清理本次进程已创建、仍能证明归属且为空/安全的临时资源；无法证明时保留并报告，禁止猜测删除。
8. **状态真实性**：`inspect/verify` 必须以真实 Git 输出核验，不相信登记文件自述。
9. **无隐式清理**：2C 只能生成清理计划。实际删除 worktree/分支必须以后由用户对精确目标另行授权。
10. **不推进业务状态**：Worktree 创建成功不是开发完成证据，不得直接推进任务看板。

## 6. 最低测试矩阵

必须使用 `pytest tmp_path` 创建真实临时 Git 仓库，并至少覆盖：

1. 正常创建后，路径、分支、HEAD、基线和 common dir 一致；
2. 两个角色 worktree 物理隔离，文件修改互不出现；
3. 已有目录、已有分支、非法 ref、绝对路径、`..` 被拒绝；
4. Windows Junction/符号链接逃逸被拒绝；
5. 仓库路径和 worktree 路径包含空格时可用；
6. 当前调用目录本身是 linked worktree 时正确识别 common dir；
7. 并发创建相同隔离键只有一方成功且无覆盖；
8. Git 命令失败时不删除用户预存文件；
9. 脏工作区、未跟踪文件和已有 worktree 不被 reset、stash、clean 或覆盖；
10. `inspect/list/verify` 只读，文件树和 Git 状态不变化；
11. 清理接口只返回计划，确认 2C 不执行 remove/prune/branch delete；
12. 命令注入载荷不能改变参数边界；
13. 登记数据不含 Token、API Key、Cookie、原始 Prompt；
14. 既有 2A/2B 定向测试和全量测试无回归。

测试命令至少包括：

```powershell
python -m pytest tests/test_worktree_manager.py -q -rs
python -m pytest tests/test_host_adapter.py tests/test_evidence.py -q -rs
python -m pytest tests -q -rs
git diff --check
```

测试前后必须记录 `user_data/board.json` 的 SHA-256。测试必须隔离数据根；若既有全量测试仍产生 `Delete Me` 等污染卡，只能通过合法状态 CLI 软取消并记录，禁止复制快照覆盖或直接编辑 `board.json`。

## 7. Antigravity 开发交付合同

Antigravity 是 2C Builder，只能在上述工作目录和分支开发。开始前原样报告：

```powershell
git fetch origin
git branch --show-current
git rev-parse HEAD
git log -5 --oneline
git rev-list --left-right --count origin/phase-2-real-agents...HEAD
git merge-base --is-ancestor f6b79e9 HEAD
git status --short
git worktree list --porcelain
```

开始前还必须输出预计修改文件、Schema/类接口草案、受控路径与仓库身份方案、并发策略、失败回滚边界、测试计划、风险和未提交文件。将既有 `T0023` 从【待开始】合法领取为【进行中】后才能修改文件，不得另建重复代码任务。

完成后：

1. 本地提交代码和报告，Commit Message 不含内部工单号或虚拟角色名；
2. 工作区必须干净，不 push；
3. 给出基线/结果 SHA、逐文件 diff、所有测试命令/退出码/数量、看板前后哈希、已知限制；
4. 将原 2C 工单合法推进到【审查中】，处理人设为 Reviewer；
5. 停止，等待 AutoLaw 独立复审，不进入 2D。

## 8. AutoLaw 独立复审合同

AutoLaw 只做 Reviewer，不参与开发、不修改候选源码、不追加“顺手修复”。必须从开发交接包取得明确候选 SHA，并核验该 SHA 与当前 HEAD 一致。

复审至少包括：

1. 核对分支、工作区、基线祖先关系、候选提交范围和看板状态；
2. 逐行审查从本任务书基线到候选 SHA 的全部 diff；
3. 搜索并阻断 `shell=True`、字符串拼接 Git 命令、`reset/stash/clean/remove/prune`、递归删除、路径 `startswith` 等危险实现；
4. 独立复跑最低测试矩阵，并补做路径逃逸、并发碰撞、命令注入、linked-worktree、用户文件保留等对抗复现；
5. 对报告中的文件数、增删行、命令、退出码、测试数量和 Git 状态逐项验真；
6. 测试前后校验权威看板，污染卡只能合法软取消；
7. REJECT 时在原工单追加 `DEF-<task-id>-N`，流转到【已退回】并交回原负责人；
8. PASS 时将原工单从【审查中】推进到【测试中】，处理人设为 QA，然后停止；不得宣布用户验收或开始 2D。

## 9. 停止条件

遇到以下任一情况立即停止并请求用户确认：

- 需要删除、移动或覆盖已有目录、worktree、分支或用户文件；
- 需要修改冻结的 2A/2B/第一阶段契约；
- 需要新增依赖、写全局目录、调用外部 Host/API 或产生费用；
- 无法证明路径、仓库、分支、基线或登记归属；
- 真实 Git 行为在当前 Windows 权限下无法验证；
- 测试失败且无法在 2C 范围内解释；
- 候选分支分叉或工作区存在不明修改。

## 10. 2C 准出定义

只有同时满足以下条件，Codex 才可建议用户验收 2C：

- Worktree 创建与只读核验通过真实 Git 集成测试；
- 路径逃逸、碰撞、命令注入和误删测试全部通过；
- 2A/2B 与全量测试绿色，无 skip；
- AutoLaw 独立复审 PASS，QA 独立测试完成；
- 候选提交、测试证据和看板状态可核对；
- 工作区干净，未 push、未合并、未删除 worktree；
- 明确停止在“2C 待用户验收”，未进入 2D。
