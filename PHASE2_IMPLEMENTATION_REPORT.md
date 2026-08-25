# 第二阶段实施报告

## 1. 当前结论

- **已完成批次**：2A（Host 契约、只读能力探测、`FakeHostAdapter`、契约测试）。
- **技术准出结论**：Codex 于 2026-08-25 完成最终修复与独立对抗验证，2A 技术门禁通过。
- **当前停止点**：2A 已完成，2B 尚未实施；必须由用户使用独立批准语句授权 2B。
- **冻结范围**：第一阶段 7.1～7.6 及其验收证据未修改。

## 2. 基线、分支与提交

- **2A 基线提交**：`37d0c4419e2b0a7b7672112cab7ad64f9a055f93`
- **执行分支**：`phase-2-real-agents`
- **最终代码与测试提交**：`9c59b14`（`fix(core): 收紧 Host 契约安全边界`）
- **远端操作**：未 push、未创建 PR、未发布、未打 Tag。
- **报告提交说明**：本报告由代码提交之后的独立文档提交承载；最终交付 HEAD 以 `git rev-parse HEAD` 的现场输出为准，避免在文档中伪造自引用提交号。

## 3. 2A 实际产物

- `scripts/_lib/core/agent_schema.py`
  - 定义 Host、请求、计划、Handle、结果、确认和统一异常契约；
  - 所有契约对象冻结；字典、列表、集合递归复制为只读映射、元组和不可变集合；
  - 请求超时只接受有限、非负数值。
- `scripts/_lib/core/host_adapter.py`
  - 定义 `BaseHostAdapter` ABC 和 Fail-Closed 的 `FakeHostAdapter`；
  - Fake session 使用 `fake-session:<uuid>`，Adapter 实例使用独立命名空间；
  - Handle 使用不可猜测 invocation token 的 bearer-capability 模型，并校验 Host、Adapter 实例、session 与 token；
  - 使用 `time.monotonic()`、请求默认超时、显式覆盖值和剩余执行时间计算超时；
  - 不支持的交互确认明确抛出 `AgentNotSupportedError`。
- `tests/test_host_adapter.py`
  - 覆盖深层不可变、Fake 能力降级、命名空间、跨 Adapter/tampered Handle、取消、部分结果缓存和超时边界；
  - 能力探测测试封锁常见文件、目录、临时文件、子进程、socket 和 HTTP I/O 入口。

相对 2A 基线的代码与测试统计：

```text
 scripts/_lib/core/agent_schema.py | 152 +++++++++++++++++++++++++
 scripts/_lib/core/host_adapter.py | 212 +++++++++++++++++++++++++++++++++++
 tests/test_host_adapter.py        | 226 ++++++++++++++++++++++++++++++++++++++
 3 files changed, 590 insertions(+)
```

## 4. 最终验证证据

### 4.1 定向契约测试

```text
命令：python -m pytest tests/test_host_adapter.py -q -rs
退出码：0
结果：11 passed in 0.41s
失败：0
跳过：0
```

### 4.2 全量回归测试

```text
命令：python -m pytest tests -q -rs
退出码：0
结果：237 passed in 43.35s
失败：0
跳过：0
```

### 4.3 Git 与权威看板保护

```text
命令：git diff --check
退出码：0
结果：无空白错误；仅有 Git 的 Windows LF/CRLF 转换提示

测试前 board.json SHA-256：
C194313AD1A9BB0DECBE4C005A752021FA366B384B4BFEFE07191CF3194D52EB

测试后检测到测试数据写入，已恢复测试前快照；最终 SHA-256：
C194313AD1A9BB0DECBE4C005A752021FA366B384B4BFEFE07191CF3194D52EB
```

## 5. 安全模型与后续约束

- `AgentHandle` 是 bearer capability：持有完整 session、Adapter 实例 ID 和 invocation token 的精确副本视为合法；任何字段被篡改或跨 Adapter 使用均 Fail-Closed。
- invocation token 仅用于进程内句柄校验，2B Evidence Store **不得持久化或输出该 token**；证据只保存非敏感 invocation ID/引用。
- 递归冻结使用只读映射，2B 必须通过明确的 canonical serializer 转换为 JSON，禁止直接依赖 `dataclasses.asdict()` 或默认 JSON 编码器处理只读映射。
- 2A 的 Fake 结果始终为 `is_real_host=false`，不得被 2B 门禁视为真实状态流转证据。
- 2A 没有实现 Evidence Store、状态门禁、worktree、Codex/Antigravity 真实 Adapter、Reviewer/QA 独立运行、MCP 或远程生态。

## 6. 下一步

下一步仅允许实施 **2B：证据存储与状态门禁**。完整执行合同和可复制提示词见 `MULTI_CLIENT_MODERNIZATION_PLAN.zh-CN.md` 的“2B 推荐完整批准语句”。
