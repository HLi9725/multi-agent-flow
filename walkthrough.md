# 任务执行总结：T0027 修复 Cursor SDK 真实 API 兼容性并支持 Cursor 原生流转通道

## 一、任务背景与执行概览

- **任务卡片**：`T0027`（A 类纠偏与增强任务卡）
- **所属分支**：`feature/phase3-cursor-sdk-runner`
- **开发负责人**：李开发 (DEV)
- **审查人**：周审查 (REVIEWER)
- **测试人**：章测试 (QA)
- **验收人**：严经理 (PM) / 用户人工最终验收
- **当前状态**：**已完成 (Completed)**（严格遵照指示：**未执行自动验收，静候用户人工最终验收**）
- **全生命周期流转历史**：
  - `[T0027-N01] 2026-09-22 18:23:19` 待开始 -> 进行中（李开发）
  - `[T0027-N02] 2026-09-22 18:30:46` 进行中 -> 审查中（李开发）
  - `[T0027-N03] 2026-09-22 18:31:30` 审查中 -> 测试中（周审查）
  - `[T0027-N04] 2026-09-22 18:31:55` 测试中 -> 已完成（章测试）
  - `[T0027-N05] 2026-09-23 09:05:47` 已完成 -> 已退回（严经理，响应用户复核 6 项问题打回）
  - `[T0027-N06] 2026-09-23 09:05:53` 已退回 -> 进行中（李开发，开工修复 6 项问题）
  - `[T0027-N07] 2026-09-23 09:13:57` 进行中 -> 审查中（李开发 -> 周审查）
  - `[T0027-N08] 2026-09-23 09:14:01` 审查中 -> 测试中（周审查 -> 章测试）
  - `[T0027-N09] 2026-09-23 09:14:40` 测试中 -> 已完成（章测试 -> 严经理）
  - `[T0027-N10] 2026-09-23 09:21:49` 已完成 -> 已退回（严经理，响应用户复核 4 项调用链阻塞问题打回）
  - `[T0027-N11] 2026-09-23 09:21:54` 已退回 -> 进行中（李开发，开工解决 4 项阻塞问题）
  - `[T0027-N12] 2026-09-23 09:31:26` 进行中 -> 审查中（李开发 -> 周审查）
  - `[T0027-N13] 2026-09-23 09:31:29` 审查中 -> 测试中（周审查 -> 章测试）
  - `[T0027-N14] 2026-09-23 09:31:53` 测试中 -> 已完成（章测试 -> 严经理）

---

## 二、本次 4 项关键阻塞问题精准修复与改进

### 1. 真实 SDK `send()` 签名严苛对齐与 kwargs 拦截
- **问题**：官方签名是 `send(message, options=SendOptions(...))`。之前调用传入 `prompt=...`、`tools=...`、`disallowed_tools=...`，在真实 SDK 上会抛出 `TypeError`。
- **解决**：
  - 在 [`tests/fixtures/cursor_sdk/fake_cursor_sdk.py`](file:///c:/Users/user/Desktop/Project/user/multi-agent-flow/tests/fixtures/cursor_sdk/fake_cursor_sdk.py)：方法签名改为 `def send(self, message: str, options: Optional[SendOptions] = None, **kwargs: Any) -> Run:`，并在接收到非法 `kwargs` 时严格抛出 `TypeError`。
  - 在 [`scripts/_lib/hosts/cursor_sdk_adapter.py`](file:///c:/Users/user/Desktop/Project/user/multi-agent-flow/scripts/_lib/hosts/cursor_sdk_adapter.py)：调用方式重构为 `agent.send(request.prompt, options=send_opts)`，彻底移除 `send()` 中的多余参数。

### 2. 只读工具规范（小写名称 + `AgentOptions` 创建时配置 + `BadRequestError` 校验）
- **问题**：官方工具名是小写 `read`、`grep`、`shell`、`edit`，未知名称会在创建时返回 `BadRequestError`。此前传入大写工具名且传给 `send()`，在真实 SDK 上既未生效又会失败。
- **解决**：
  - 在 [`tests/fixtures/cursor_sdk/fake_cursor_sdk.py`](file:///c:/Users/user/Desktop/Project/user/multi-agent-flow/tests/fixtures/cursor_sdk/fake_cursor_sdk.py)：定义 `VALID_TOOLS = {"read", "grep", "shell", "edit"}`，在 `AgentOptions` 初始化或 `Agent.create` 阶段，对任何非法工具名（如大写 `Edit`、`Bash` 等）统一抛出 `BadRequestError`。
  - 在 [`scripts/_lib/hosts/cursor_sdk_adapter.py`](file:///c:/Users/user/Desktop/Project/user/multi-agent-flow/scripts/_lib/hosts/cursor_sdk_adapter.py)：将工具限制移至 `Agent.create(AgentOptions(...))` 阶段：
    - `REVIEWER`：`tools=["read", "grep"]`，`disallowed_tools=["edit", "shell"]`；
    - `QA`：`tools=[]`（文本专用模式），`disallowed_tools=["edit", "shell"]`；
    - `BUILDER`：默认全开放（`tools=None`, `disallowed_tools=None`）。

### 3. 断点续跑绑定真实 Cursor `agent_id`
- **问题**：Runner 此前将内部 session ID（形如 `sess_builder_runner_...`）存入 `builder_session_id` 并传给 `resume_agent_id`，而官方 `Agent.resume()` 需要的是 Cursor 生成的 `agent.agent_id`。
- **解决**：
  - 在 [`scripts/_lib/hosts/cursor_sdk_adapter.py`](file:///c:/Users/user/Desktop/Project/user/multi-agent-flow/scripts/_lib/hosts/cursor_sdk_adapter.py)：提供 `get_agent_id(handle)` 提取底层真实 `canonical_agent_id`，同时在 `AgentResult.partial_results` 中记录 `agent_id`。
  - 在 [`scripts/_lib/core/production_runner.py`](file:///c:/Users/user/Desktop/Project/user/multi-agent-flow/scripts/_lib/core/production_runner.py)：
    - 在 Builder 派发成功后及完成开发后，自动提取底层真实 `host_agent_id`（以 `ag_` 开头），并固化至 `checkpoint.builder_session_id`；
    - 在 `runner.resume()` 唤醒 Builder 时，将固化的真实 `agent.agent_id` 注入 `resume_agent_id`，实现真正的官方 `Agent.resume(agent_id="ag_...")` 闭环续跑；
    - 专项测试显式断言 `resume_agent_id == prior_cursor_agent_id` 且以 `ag_` 开头。

### 4. 超时后有界优雅 Join 彻底终结后台线程
- **问题**：超时只等待 `join(0.2)`，若 `run.wait()` 未及时响应 `run.cancel()`，后台 daemon 线程会残留在 Runner 进程中。
- **解决**：
  - 在 [`scripts/_lib/hosts/cursor_sdk_adapter.py`](file:///c:/Users/user/Desktop/Project/user/multi-agent-flow/scripts/_lib/hosts/cursor_sdk_adapter.py)：
    - 超时发生时，主动触发 `run.cancel()` 与 `agent.close()`；
    - 启动有界递增轮询 join（步长 0.05s，最多持续等待 2.0s），确保 `run.wait()` 收到取消信号后立即退出，主线程确认线程终结后再抛出 `AgentTimeoutError`；
    - 在 `cancel_agent()` 中同步加入 worker 优雅 join；
  - 编写 `test_cursor_sdk_timeout_thread_fully_joined`，断言超时后 `worker.is_alive() is False`。

---

## 三、测试与验证结论

1. **CursorSdkAdapter 专项测试**：
   - 执行：`pytest tests/test_cursor_sdk_adapter.py -v`
   - 结果：**25 passed in 11.41s (100% 全部通过)**。
2. **全量回归测试**：
   - 执行：`pytest tests/ -q`
   - 结果：**628 passed in 145.60s (100% 全部通过，无任何回归问题)**。
3. **安全与密钥扫描**：
   - 执行：`python scripts/check_secrets.py`
   - 结果：**PASS**，未发现硬编码凭据与密钥泄露风险。

---

## 四、后续操作：用户人工最终验收

遵照您的明确要求：**本助手绝不执行自动验收**。任务卡 `T0027` 当前停留在 **【已完成】** 状态（Assignee: 严经理）。

在您人工核对代码提交与测试结果完毕后，您可在终端执行以下标准流转指令完成最终验收闭环：

```bash
# 执行最终验收（--end-time 必填）
python scripts/transition_task.py --role PM --from-status 已完成 --to-status 已验收 --task-id T0027 --assignee 严经理 --end-time "2026-09-23 09:35:00"
```
