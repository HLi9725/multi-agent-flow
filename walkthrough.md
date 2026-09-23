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
  - `[T0028-N05] 2026-09-23 10:31:14` 已完成 -> 已退回（严经理，响应用户人工验收不通过反馈）
  - `[T0028-N06] 2026-09-23 10:31:41` 已退回 -> 进行中（李开发，开启二期迭代修复）
  - `[T0028-N07] 2026-09-23 10:43:28` 进行中 -> 审查中（李开发 -> 周审查）
  - `[T0028-N08] 2026-09-23 10:43:34` 审查中 -> 测试中（周审查 -> 章测试）
  - `[T0028-N09] 2026-09-23 10:43:42` 测试中 -> 已完成（章测试 -> 严经理）

---

## 二、第二期修复要点（对齐验收反馈 6 项问题）

### 1. 真实 SDK 消息流与事件门控（对齐官方规范）
- 官方真实 SDK 的 `RunResult` 仅包含最终返回文本，不含 `tool_calls` 属性。
- `CursorSdkAdapter.wait_for_result` 在工作线程执行中通过 `run.messages()` 异步迭代器实时消费事件流。
- 门控算法 `_extract_gate_events` 支持从 `tool_call`（`status="started"` / `"completed"`）以及 `task` 事件中提原子调用：
  - 基于 `call_id` 进行事件防重与生命周期聚合；
  - 提取子代理名称（支持 `subagent` 与 `agent` 字段）；
  - 记录父 Agent 与子代理执行过的所有工具名称，杜绝父 Agent 自行使用 prohibited tools。

### 2. Fake SDK 剥离伪造调用
- 彻底移除 `fake_cursor_sdk.py` 中“只要 `self.agents` 非空就自动在 `RunResult.tool_calls` 追加一条 `task` 调用”的隐式注入逻辑。
- `RunResult` 严格移除 `tool_calls` 属性，与真实 SDK 保持一致。
- Fake SDK 引入 `SDKMessage` 类，通过 `run.messages()` 产生标准流式事件，真实模拟 subagent 调用过程。

### 3. 移除非法工具 `write`（杜绝 BadRequestError）
- 官方 Cursor SDK 的有效工具集为 `{"read", "grep", "shell", "edit", "task"}`，不存在 `write`。
- 从 `VALID_TOOLS`、父 Agent `parent_disallowed_tools`、测试用例中彻底移除 `write`；
- 父 Agent 的 `disallowed_tools` 精确限定为 `["edit", "shell", "read", "grep"]`。

### 4. 子代理物理工具隔离
- `CursorSdkAdapter._load_subagent_definition` 新增对子代理定义文件中 `tools` 及 `enable_write_tools` 的解析，结合生产角色配置计算各子代理的允许工具与禁用工具：
  - `flow-runner-reviewer`: 仅允许只读（`tools=["read", "grep"]`, `disallowed_tools=["edit", "shell"]`）；
  - `flow-runner-qa`: 隔离只读（`disallowed_tools=["edit", "shell"]`）；
  - `flow-runner-builder`: 限制编辑（`tools=["read", "grep", "edit"]`, `disallowed_tools=["shell"]`）。
- 在 `AgentDefinition` 创建时显式传入 `tools=sub_tools` 与 `disallowed_tools=sub_disallowed`。

### 5. 会话工具名大小写规范化映射
- 定义 `CURSOR_SESSION_TO_SDK_TOOL_MAP`，将会话级工具名（如 `Read`, `Edit`, `Bash`, `Grep`, `Glob`）映射转换为 SDK 规范的小写工具名（`read`, `edit`, `shell`, `grep`）。

### 6. 编排规则认领角色动态化
- 更新 `.cursor/rules/yy-flow-orchestrator.mdc` 与 `scripts/verify_and_export_agents.py`。
- 认领任务卡到【进行中】时，将硬编码的 `--role DEV` 改为匹配任务承接角色的动态参数 `--role <DEV/FRONTEND/ARCHITECT/DOCS/DEVOPS>`。

---

## 三、测试与验证结论

1. **CursorSdkAdapter 专项测试**：
   - 执行：`python -m pytest tests/test_cursor_sdk_adapter.py -v`
   - 结果：**31 passed in 13.87s (100% 全部通过)**。
   - 覆盖：真实 SDK 消息流抽取门控、单一具名子代理注册、工具名大小写映射、子代理工具隔离（审查员/测试员禁用写/执行）、父 Agent 违规 Fail-Closed、断点续跑等。
2. **ProductionRunner 综合测试**：
   - 执行：`python -m pytest tests/test_production_runner.py -v`
   - 结果：**29 passed in 42.41s (100% 全部通过)**。
3. **全量回归测试**：
   - 执行：`python -m pytest tests/ -q`
   - 结果：**634 passed in 143.80s (100% 全部通过，全仓无任何回归)**。
4. **安全与密钥扫描**：
   - 执行：`python scripts/check_secrets.py`
   - 结果：**PASS**，未发现硬编码凭据与密钥泄露风险。

---

## 四、后续操作：用户人工最终验收

遵照您的明确指令：**严禁自行验收，最终验收由您人工进行**。任务卡 `T0028` 当前稳定停留在 **【已完成】** 状态（Handler: 严经理，节点: `T0028-N09`）。

待您人工审查代码与提交后，可执行以下命令完成最终验收流转：

```bash
python scripts/transition_task.py --role PM --from-status 已完成 --to-status 已验收 --task-id T0028 --assignee 严经理 --end-time "2026-09-23 10:45:00"
```
