# 任务执行总结：T0028 Cursor Runner 按角色名启动独立 Agent 与父 Agent 门控

## 一、任务背景与执行概览

- **任务卡片**：`T0028`（A 类架构设计与能力演进任务卡）
- **任务名称**：`[A] Cursor Runner 按角色名启动独立 Agent 与父 Agent 门控`
- **所属分支**：`feature/phase3-cursor-sdk-runner`
- **开发负责人**：李开发 (DEV)
- **审查人**：周审查 (REVIEWER)
- **测试人**：章测试 (QA)
- **验收人**：严经理 (PM) / 用户人工最终验收
- **当前状态**：**已完成 (Completed)**（严格遵照用户指令：**未执行自动验收，停在已完成状态等待用户人工最终验收**）
- **全生命周期流转历史**：
  - `[T0028-N01] 2026-09-23 10:11:56` 待开始 -> 进行中（李开发）
  - `[T0028-N02] 2026-09-23 10:25:41` 进行中 -> 审查中（李开发 -> 周审查）
  - `[T0028-N03] 2026-09-23 10:25:48` 审查中 -> 测试中（周审查 -> 章测试）
  - `[T0028-N04] 2026-09-23 10:25:54` 测试中 -> 已完成（章测试 -> 严经理）
  - `[T0028-N05] 2026-09-23 10:31:14` 已完成 -> 已退回（严经理，响应第一轮验收反馈）
  - `[T0028-N06] 2026-09-23 10:31:41` 已退回 -> 进行中（李开发，第二轮迭代）
  - `[T0028-N07] 2026-09-23 10:43:28` 进行中 -> 审查中（李开发 -> 周审查）
  - `[T0028-N08] 2026-09-23 10:43:34` 审查中 -> 测试中（周审查 -> 章测试）
  - `[T0028-N09] 2026-09-23 10:43:42` 测试中 -> 已完成（章测试 -> 严经理）
  - `[T0028-N10] 2026-09-23 10:51:33` 已完成 -> 已退回（严经理，响应第二轮验收反馈）
  - `[T0028-N11] 2026-09-23 10:51:50` 已退回 -> 进行中（李开发，第三轮迭代）
  - `[T0028-N12] 2026-09-23 11:09:54` 进行中 -> 审查中（李开发 -> 周审查）
  - `[T0028-N13] 2026-09-23 11:10:04` 审查中 -> 测试中（周审查 -> 章测试）
  - `[T0028-N14] 2026-09-23 11:10:25` 测试中 -> 已完成（章测试 -> 严经理）
  - `[T0028-N15] 2026-09-23 11:17:24` 已完成 -> 已退回（严经理，响应第三轮工具真实拒绝与Glob解耦反馈）
  - `[T0028-N16] 2026-09-23 11:17:34` 已退回 -> 进行中（李开发，第四轮深度对齐修复）
  - `[T0028-N17] 2026-09-23 11:28:46` 进行中 -> 审查中（李开发 -> 周审查）
  - `[T0028-N18] 2026-09-23 11:29:11` 审查中 -> 测试中（周审查 -> 章测试）
  - `[T0028-N19] 2026-09-23 11:29:25` 测试中 -> 已完成（章测试 -> 严经理）
  - `[T0028-N20] 2026-09-23 11:38:09` 已完成 -> 已退回（严经理，响应第四轮验收反馈）
  - `[T0028-N21] 2026-09-23 11:39:05` 已退回 -> 进行中（李开发，第五轮深度对齐修复）
  - `[T0028-N22] 2026-09-23 11:51:30` 进行中 -> 审查中（李开发 -> 周审查）
  - `[T0028-N23] 2026-09-23 11:52:12` 审查中 -> 测试中（周审查 -> 章测试）
  - `[T0028-N24] 2026-09-23 11:52:21` 测试中 -> 已完成（章测试 -> 严经理）

---

## 二、第五轮关键修复要点（内联覆盖杜绝、违规强失败门控与适配器端到端验证）

### 1. 彻底避免内联 `AgentDefinition` 覆盖文件配置
- **根因分析**：官方 SDK 的 `AgentDefinition` 仅有 `description`, `prompt`, `model`, `mcp_servers` 等参数，无 `tools` 与 `disallowed_tools`。之前尝试在 Python 对象上 `setattr` 挂载属性无效；且向 `AgentOptions(agents={...})` 传入同名内联对象会导致 SDK 覆盖 `.cursor/agents/*.md`，使子代理降级回平台默认的全量工具集。
- **修复方案**：
  - 在 `CursorSdkAdapter.dispatch_agent()` 中，**不再构造内联 `AgentDefinition` 传入 `AgentOptions`**，将 `parent_agents` 保持为 `None`；
  - 依赖官方 SDK 从工作区 `.cursor/agents/*.md` 的原生文件发现机制，完整保留文件中的 Frontmatter 工具约束（如 `flow-runner-reviewer.md` 的 `tools: [Read]`）；
  - `_load_subagent_definition` 若在工作区未检测到对应角色文件（如单元测试空目录场景），以原子安全方式自包含补充，同时对生产环境 Git worktree 不修改任何已跟踪代码。

### 2. Fake SDK 违规调用强失败并与适配器门控形成闭环
- **根因分析**：上一轮 Fake SDK 在 `send()` 遇到未授权工具时仅将单条消息置为 `status="error"`，`Run` 整体仍按 `finished` 返回成功；真实运行时该违规会导致平台请求失败，且适配器门控之前未将消息流中的子代理工具违规列入 FAILED 断言。
- **修复方案**：
  - `fake_cursor_sdk.py` 的 `send()` 在发生违规工具调用时，记录错误消息并将 `Run` 整体状态置为 `status="error"`，且记录 `BadRequestError`；
  - `Run.wait()` 严格透传 `RunResult(status="error", error=...)`；
  - `CursorSdkAdapter._extract_gate_events` 明确区分父 Agent 工具调用与子代理工具调用；
  - `wait_for_result` 在提取到 `subagent_violations` 或 Run 出错时，一票否决判定为 `AgentStatus.FAILED`，物理形成拦截闭环。

### 3. 全局唯一工具策略事实源 (`resolve_subagent_tool_policy`)
- **根因分析**：之前适配器和 Fake SDK 分别维护了一套工具策略解析逻辑，容易产生隐式分歧。
- **修复方案**：
  - 在 `cursor_sdk_adapter.py` 中导出唯一的 `resolve_subagent_tool_policy(agent_id_or_name, raw_tools, enable_write_tools)` 函数；
  - Fake SDK 的 `discover_subagents_from_dir` 直接导入并调用该函数，彻底实现策略事实源统一。

### 4. 彻底通过适配器调度（Adapter-Mediated）进行全流程测试
- **根因分析**：之前的单测直接实例化 Fake SDK 的 Agent 检查权限，没有走真实的 `adapter.dispatch_agent` 和 `adapter.wait_for_result`，无法检验适配器门控是否真的能拦截子代理违规。
- **修复方案**：
  - 重构 `test_cursor_sdk_subagent_tool_rejection_and_inline_override`：
    1. 审查员（Reviewer）违规调用 `edit` -> 适配器门控判定 `AgentStatus.FAILED`；
    2. 测试员（QA）违规调用 `read` -> 适配器门控判定 `AgentStatus.FAILED`；
    3. 审查员合法调用 `read` -> 适配器正常返回 `AgentStatus.SUCCESS`；
    4. 构建员（Builder）合法调用 `edit`/`glob` -> 适配器正常返回 `AgentStatus.SUCCESS`；
    5. 校验创建的 Agent 严格采用文件发现（`from_file=True`，无内联覆盖）；
    6. 对照实验：验证内联传入无工具定义确实会导致隔离丢失，证明适配器 `agents=None` 设计的必要性。

---

## 三、测试与验证结论

1. **CursorSdkAdapter 专项测试**：
   - 执行：`python -m pytest tests/test_cursor_sdk_adapter.py -v`
   - 结果：**33 passed in 13.81s (100% 全部通过)**。
2. **ProductionRunner 综合测试**：
   - 执行：`python -m pytest tests/test_production_runner.py -v`
   - 结果：**29 passed in 43.08s (100% 全部通过)**。
3. **全量回归测试**：
   - 执行：`python -m pytest tests/ -q`
   - 结果：**636 passed in 153.25s (100% 全部通过，全仓零回归)**。
4. **安全与密钥扫描**：
   - 执行：`python scripts/check_secrets.py`
   - 结果：**PASS**，未发现硬编码凭据与密钥泄露风险。

---

## 四、后续操作：用户人工最终验收

遵照您的明确指令：**严禁自行验收，最终验收由您人工进行**。任务卡 `T0028` 当前稳定停留在 **【已完成】** 状态（Handler: 严经理，节点: `T0028-N24`）。

待您人工审查代码与提交后，可执行以下命令完成最终验收流转：

```bash
python scripts/transition_task.py --role PM --from-status 已完成 --to-status 已验收 --task-id T0028 --assignee 严经理 --end-time "2026-09-23 12:00:00"
```
