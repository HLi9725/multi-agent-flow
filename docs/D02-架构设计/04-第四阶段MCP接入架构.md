---
title: 第四阶段 MCP 接入架构
module: multi-agent-flow
stage: Phase-4
type: design-spec
status: draft
author: 钱架构
updated_at: 2026-10-14
tags: [MCP, 协议适配, 权限, GitHub Projects]
---

# 第四阶段 MCP 接入架构

本文是 4A 到 4E 的实施契约。后续开发按本文实现协议适配层，领域状态机、Evidence 格式和 Checkpoint 语义保持现状。本文不包含服务器实现代码。协议版本与官方 Python SDK 的安装名、导入路径在实施时必须核对 PyPI/spec，下文给出的是设计锁定意图，不是已查证的安装结果。

规划依据是 `docs/D01-项目管理/06-第三四阶段开发实施计划.md` 第 6 节，以及第三阶段已落地的 Profile 兼容说明。4A 到 4E 按依赖顺序交付：只读协议、受控写入、远程认证、客户端验证、单个外部连接器。本批不部署生产，不实现 Jira 与飞书的全量写入。

## 1. 设计结论

MCP 层只做六件事：协议适配、项目登记、权限、脱敏、分页、幂等。任务是否允许从某状态走到下一状态，仍由现有 `validate()`、`transition_task_pipeline()`、`EvidenceGate` 与 `validate_managed_transition()` 决定。

服务端事先登记 `project_id`。工具参数里的路径、`user_id`、`tenant_id` 一律不作为授权依据。模型可以填写这些字段，服务在进入领域调用前丢弃。

上传证据与准出是两步。`submit_evidence` 只持久化记录。是否允许推进状态，必须再经过现有 EvidenceGate。`decision=PASS` 出现在上传体里，不构成准出。

用户验收是独立工具 `request_acceptance`，权限为 `acceptance`。通用 `transition_task` 若目标状态为 `已验收`，直接返回 `ACCEPTANCE_REQUIRED`，不调用流转管道。

外部看板是投影。GitHub Projects 不能把任务写成 `已验收`，也不能覆盖 YY-Flow 的 Evidence 与 Checkpoint。

## 2. 总体结构

```
客户端 (Cursor / 其他 MCP 宿主 / 测试夹具)
        |  JSON-RPC
        v
protocol.py  ---- STDIO 帧 或 Streamable HTTP
        |
        v
auth.py + registry.py     主体与 project_id 在此确定
        |
        v
service.py                 参数校验、分页、幂等、错误映射
        |
        +--> 现有领域服务（只调用，不复制）
        |      transition_task_pipeline / validate
        |      EvidenceStore / EvidenceGate
        |      validate_managed_transition
        |      OfflineBoardAdapter 读与软删除
        |      workflow_policy（由 validate 间接执行）
        |
        +--> audit_store.py / 幂等账本（MCP 侧新存储，不改 Evidence 格式）

connectors/github_projects_sync.py
        只在 4E 使用；通过传输接口调用 GitHub，不进入 MCP 工具的任意路径参数
```

本地只读试点只启用 STDIO 与四个读工具。写入工具、HTTP 监听和连接器分别由后续批次的入口开关打开。未打开的工具不得出现在 `tools/list` 中。

Cursor 原生编排、Codex/Antigravity Production Runner、单角色 CLI 继续走现有入口。它们不导入 MCP 包，也不因本设计而改变默认 Profile。

## 3. 模块清单

路径均相对于仓库根目录。允许实施时把纯函数在下列文件之间挪动，但职责边界保持下表，且不得把状态判断写进协议文件。

| 路径 | 批次 | 职责 |
| --- | --- | --- |
| `scripts/_lib/mcp/protocol.py` | 4A，4C 扩展 | JSON-RPC 2.0、STDIO 帧、Streamable HTTP 最小会话。官方 SDK 与兼容层都调用同一组 handler |
| `scripts/_lib/mcp/schema.py` | 4A/4B | 工具名、入参 JSON Schema、出参字段白名单。Schema 版本号常量 `SCHEMA_VERSION` |
| `scripts/_lib/mcp/errors.py` | 4A | 稳定错误码、retryable、对外 message。禁止把异常文本原样返回 |
| `scripts/_lib/mcp/redaction.py` | 4A | 调用 `EvidenceStore` 已有脱敏函数，并裁剪响应白名单 |
| `scripts/_lib/mcp/registry.py` | 4A | `project_id` 到数据根、看板配置、Evidence 根的服务端映射。拒绝未登记 ID 与任何路径参数 |
| `scripts/_lib/mcp/service.py` | 4A/4B | 工具实现。唯一允许调用领域函数的模块 |
| `scripts/_lib/mcp/stdio_server.py` | 4A | 本地 STDIO 循环。无网络监听 |
| `scripts/_lib/mcp/http_server.py` | 4C | 单组织 Streamable HTTP。默认不在 4A 启动 |
| `scripts/_lib/mcp/auth.py` | 4C，4A 预留本地主体 | 从传输上下文取主体、校验受众/有效期/撤销，映射权限 |
| `scripts/_lib/mcp/audit_store.py` | 4B 起写入，4A 记拒绝 | 追加型审计。与看板、Evidence 分文件 |
| `scripts/mcp_server.py` | 4A | 进程入口。子命令 `stdio` 与 `http`。不内嵌业务判断 |
| `scripts/_lib/connectors/github_projects_sync.py` | 4E | 同步引擎。可注入传输层以便无凭据单测 |
| `tests/test_mcp_stdio.py` | 4A | 帧、Schema、未知项目、分页、脱敏 |
| `tests/test_mcp_write.py` | 4B | 越权、重放、版本冲突、门禁、验收隔离 |
| `tests/test_mcp_http_auth.py` | 4C | 主体来源、撤销、限流、跨项目 |
| `tests/test_mcp_github_sync.py` | 4E | 分页、乱序、冲突、部分失败。真实网络调用默认跳过并标记未验证 |

`service.py` 禁止 `subprocess`、禁止 `os.system`、禁止把工具参数拼进路径。`registry.py` 中的路径只来自登记文件，并用 `os.path.realpath` 固定到登记时的绝对路径。

## 4. 项目登记

登记文件由管理员在服务器本机写入，例如数据目录下的 `mcp_registry.json`。模型与 MCP 工具都不能创建或修改该文件。`admin` 权限的管理动作若后续需要变更登记，也必须走单独的本地管理命令，不暴露为模型可调用工具。

登记记录字段：

| 字段 | 约束 |
| --- | --- |
| `project_id` | `^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$` |
| `display_name` | 可返回给客户端，最长 128 字符 |
| `org_id` | 4C 单组织阶段只有一个组织。请求中的 org 声明必须与此相等，否则 `FORBIDDEN` |
| `data_root` | 仅服务端保存。响应中永不出现 |
| `board_config` | 传给现有 `get_board_adapter` / `transition_task_pipeline` 的配置路径 |
| `evidence_root` | 现有 `EvidenceStore` 根目录 |
| `board_kind` | 本批读路径为 `offline`。`github` 只表示 4E 投影目标已配置，不表示 MCP 直接读写 GitHub |

解析规则：

1. 工具缺少 `project_id`，或格式不合法：`VALIDATION`。
2. 登记表中不存在：`UNKNOWN_PROJECT`。
3. 参数出现 `path`、`data_root`、`board_file`、`workspace`、绝对路径或 `..`：`PATH_REJECTED`。即使该字符串碰巧等于登记路径，也拒绝，避免模型靠试探确认磁盘位置。
4. 登记路径在启动时做一次 realpath。运行中若 realpath 逸出登记根目录：`PATH_REJECTED`，并记审计。
5. 只读工具也按主体的项目授权过滤。知道别的 `project_id` 但没有 `read`：`FORBIDDEN`。不存在与无权要使用不同错误码，避免用 `NOT_FOUND` 掩盖越权；未登记仍是 `UNKNOWN_PROJECT`。

`get_project` 的返回白名单：`project_id`、`display_name`、`org_id`、`board_kind`、`schema_version`、`server_revision`。不返回文件系统路径、令牌、环境变量和环境用户名。

## 5. 稳定错误、分页、长度与脱敏

### 5.1 错误码

领域失败在 `tools/call` 的 result 里返回，`isError` 按表中的列设置。JSON-RPC 传输层的解析失败、未初始化仍使用 JSON-RPC 错误，`data.code` 与下表相同。`message` 使用固定中文短句，不拼接异常、SQL、路径或令牌。

| 错误码 | JSON-RPC code | isError | retryable | 何时使用 |
| --- | --- | --- | --- | --- |
| `UNKNOWN_PROJECT` | -32001 | true | false | `project_id` 未登记 |
| `FORBIDDEN` | -32002 | true | false | 主体无该项目权限，或角色不被允许执行该流转 |
| `PATH_REJECTED` | -32003 | true | false | 参数含路径，或登记路径逸出 |
| `VALIDATION` | -32004 | true | false | Schema、缺字段、长度、状态枚举不合法 |
| `CONFLICT` | -32005 | true | false | 预期版本不符、幂等键被不同请求占用、同步冲突未自动合并 |
| `IDEMPOTENCY_REPLAY` | -32006 | false | false | 同一幂等键与同一请求摘要已经成功。返回原结果，并带 `replayed: true`。这是成功重放，不是又写了一次 |
| `NOT_FOUND` | -32007 | true | false | 项目已授权，但任务或证据不存在 |
| `RATE_LIMITED` | -32008 | true | true | 超过主体或下游速率预算 |
| `UNAUTHENTICATED` | -32009 | true | false | HTTP 缺少或无法校验凭证。STDIO 在进程主体无法映射时也用此码 |
| `REVOKED` | -32010 | true | false | 凭证 `jti` 已进入撤销表，或主体被管理员停用 |
| `GATE_REJECTED` | -32011 | true | false | EvidenceGate、Verifier 门禁或 Profile 门禁拒绝。看板状态保持原样 |
| `ACCEPTANCE_REQUIRED` | -32012 | true | false | 通用流转试图进入 `已验收`，或验收工具缺少独立授权 |

同一幂等键、不同请求摘要：`CONFLICT`，不返回上一次的结果正文。幂等记录处于 `pending` 且原请求仍在锁内：`CONFLICT`，`retryable: true`，`reason: IN_PROGRESS`。客户端等待后用同一键重试，不得更换键再提交。

错误体形状：

```json
{
  "code": "GATE_REJECTED",
  "message": "证据未通过服务端门禁",
  "retryable": false,
  "replayed": false,
  "details": {}
}
```

`details` 只允许放枚举型原因：缺哪个逻辑字段名、期望状态名、当前状态名。禁止放入文件内容、环境变量、Authorization、cookie、连接串和堆栈。

### 5.2 分页

`list_tasks` 使用稳定游标，不用“页码乘页大小”跨两次读取拼接。

- 排序键：任务 `id` 升序。`id` 缺失的记录不进入 MCP 列表，并在审计中记一条 `VALIDATION` 诊断，避免静默丢项。
- 默认 `limit` 为 20，最大 50。超过最大值为 `VALIDATION`。
- 请求可带 `cursor`。游标是服务端签名的不透明字符串，内容为上一页最后的 `id`、过滤条件摘要、以及读取时的看板版本。签名密钥只在服务端。
- 下一页沿用同一过滤摘要。摘要不一致：`VALIDATION`。
- 读取使用 `OfflineBoardAdapter.list_records_readonly`。因为现有实现是 offset/limit，MCP 层在内存中按 `id` 过滤 `id > cursor_id` 后再截取 `limit`，并循环直到凑满一页或数据结束。不得把 adapter 的 offset 直接暴露给客户端，否则并发插入会漏项或重复。
- 响应包含 `items`、`next_cursor`、`has_more`、`board_version`。`has_more=false` 时 `next_cursor` 为空。
- 翻页期间看板版本变化：仍返回本页已读到的项，并设置 `stale: true`。客户端应从新的 `get_project` 版本重新拉全量。服务不得在版本变化时跳过中间 ID。
- 软删除项默认不列出。`include_deleted` 仅 `admin` 可用。

4E 的 GitHub 分页单独定义，见第 11 节。现有 `GitHubProjectsAdapter.list_records` 只发送 `first`，忽略 `offset`，不能当作全量读取。

### 5.3 长度上限

| 对象 | 上限 | 超出时 |
| --- | --- | --- |
| 工具字符串参数 | 8 KiB | `VALIDATION` |
| 任务名称 | 200 字符 | `VALIDATION` |
| 备注 | 4 KiB | `VALIDATION` |
| 单页任务数 | 50 | `VALIDATION` |
| 证据 `extra` 序列化后 | 256 KiB | `VALIDATION`，不落盘 |
| 响应中的单个文本字段 | 4 KiB | 截断，并加 `truncated: true` 与 `original_length` |
| 响应总 JSON | 256 KiB | 改为摘要与 `truncated: true`，不返回半截 JSON |
| 审计单行 | 4 KiB | 先脱敏再截断 |

截断诊断不能用来补造 `PASS`、验收结论或证据摘要。缺内容就保持缺内容。

### 5.4 脱敏

响应与审计在序列化前调用 `EvidenceStore.prepare_metadata` / `_mask_secrets` / `_mask_text` 的同一套规则，包括私钥块、令牌键名、连接串和 `invocation_token` 拒绝。MCP 层不维护第二份更弱的正则。

额外白名单：任务响应只含 `id`、`name`、`status`、`assignee`、`owner`、`type`、`task_version`、`is_deleted`、`start_time`、`end_time`、`remarks`（已脱敏且可截断）。`process` 默认不返回；若调用方有 `read` 且显式 `include_process=true`，最多返回最后 20 条已脱敏节点。

`invocation_token` 出现在入参：整请求 `VALIDATION`，不改写成掩码后继续处理。错误响应、日志、审计使用同一出口函数，避免异常处理器绕过脱敏。

## 6. 工具 Schema 摘要

所有工具的 JSON Schema 为 draft 2020-12 对象。`additionalProperties` 为 false。公共字段：

- `project_id`：string，必填，登记格式。
- 写工具另必填 `idempotency_key`：`^[A-Za-z0-9][A-Za-z0-9._:-]{7,127}$`。
- 写工具与 `get_task` 之后的变更必填 `expected_version`：64 位小写十六进制，算法见第 8 节。

`SCHEMA_VERSION` 初值为 `4.0.0`。增删字段必须升版本。旧客户端看到未知必填字段时应失败而不是忽略。

### 6.1 4A 只读

`get_project`

- 入参：`project_id`
- 出参：第 4 节白名单

`get_task`

- 入参：`project_id`，`task_id`（`^T[0-9]{4,}$`），可选 `include_process`
- 出参：第 5.4 节白名单加 `board_version`
- 已软删除：默认 `NOT_FOUND`。`admin` 且 `include_deleted=true` 时返回并带 `is_deleted: true`

`list_tasks`

- 入参：`project_id`，可选 `status`、`assignee`、`cursor`、`limit`、`include_deleted`
- 出参：`items`、`next_cursor`、`has_more`、`board_version`、`stale`
- `status` 只能取现有看板状态枚举。未知字符串为 `VALIDATION`

`get_status`

- 入参：`project_id`，`task_id`
- 出参：`task_id`、`status`、`assignee`、`task_version`、`board_version`、`profile_id`（若 `workflow_policies/<task_id>.json` 存在则读取，否则为 `standard`，与第三阶段“无策略文件按 Standard”一致）
- 本工具不返回证据正文

### 6.2 4B 写入

`create_task`

- 入参：`project_id`，`idempotency_key`，`name`，`type`（A–G），`assignee`，可选 `stage`、`wp`、`wbs`、`owner`，可选 `force_duplicate`
- 行为：等价于 `quick_task.py create`，即 `transition_task_pipeline(..., create_only=True)`。新建状态只能是 `待开始`。MCP 不提供把新卡直接建成 `进行中` 的参数
- 角色：服务端按主体映射到建卡角色。映射结果不是 PM，且 assignee 不是该角色本人时，沿用管道现有规则失败，对外 `FORBIDDEN`
- 重名：管道返回重复候选时，对外 `CONFLICT`，`details.reason=DUPLICATE_TASK`。只有 `force_duplicate=true` 且主体有 `admin` 时才把 `force=True` 传给管道
- 出参：`task_id`、`status=待开始`、`task_version`

`submit_evidence`

- 入参：`project_id`，`task_id`，`idempotency_key`，`expected_version`，`evidence_type`（只允许现有四值 `task_start`、`task_transition`、`task_complete`、`user_confirmation`），`baseline_commit`，`result_commit`，`metadata` 对象，`artifacts` 数组（相对路径加 sha256）
- 行为：组装现有 `EvidenceRecord`，经 `EvidenceStore.append` 落盘。`evidence_id` 由 `project_id + task_id + idempotency_key` 的稳定摘要生成，长度符合现有 `^[\w\-]{1,64}$`
- 明确不做：不调用 `validate()`，不改看板状态，不把 metadata 里的 `decision` 解释为门禁通过
- 出参：`evidence_id`、`content_hash`、`gate_status=NOT_EVALUATED`
- 相同 id 已存在且内容摘要一致：`IDEMPOTENCY_REPLAY`。内容不一致：`CONFLICT`

`transition_task`

- 入参：`project_id`，`task_id`，`idempotency_key`，`expected_version`，`from_status`，`to_status`，`assignee`，`task_type`，可选 `end_time`、`remarks`、`evidence_id`
- `to_status=已验收`：立即 `ACCEPTANCE_REQUIRED`，不读后续参数的业务含义，不写看板
- 行为：在领域锁内核对 `expected_version` 后调用 `validate()` 与 `transition_task_pipeline()`。Runner 托管任务同时调用 `validate_managed_transition()`。任一拒绝则 `GATE_REJECTED` 或 `FORBIDDEN`，状态不变
- 角色取主体映射，不取模型单独传入的 `role`。若将来 Schema 出现 `role` 字段，与映射不一致则 `FORBIDDEN`
- 出参：`task_id`、`status`、`assignee`、`task_version`、`evidence_id`（若本步引用了证据）

`request_acceptance`

- 入参：`project_id`，`task_id`，`idempotency_key`，`expected_version`，`evidence_id`
- 权限：`acceptance`。只有 `read` 或 `task.transition` 时返回 `FORBIDDEN`
- 当前状态必须已是 `已完成`。其他状态：`ACCEPTANCE_REQUIRED`
- 行为见第 8.4 节。成功出参：`status=已验收`、`task_version`、`evidence_id`

`soft_delete` 与 `restore`

- 入参：`project_id`，`task_id`，`idempotency_key`，`expected_version`
- 权限：`admin`
- 只改 `is_deleted` 及现有恢复字段 `restored_at`。`status` 保持不变。终态任务可以隐藏，不能借恢复改回中间状态
- 重复软删除同一已删除卡：`IDEMPOTENCY_REPLAY` 若键相同；键不同且已经处于目标可见性：返回成功当前快照，`replayed: false`，不报错，避免把可见性开关做成第二套状态机

## 7. 4A：STDIO 只读

入口：

```text
python scripts/mcp_server.py stdio --registry <登记文件>
```

进程标准输入输出使用 JSON-RPC。官方 STDIO 帧仍是 `Content-Length: <字节数>\r\n\r\n` 加 UTF-8 正文。2026-10-13 已核对规范页，官方帧没有改成换行 JSON，因此不得删除现有 Content-Length 读写。客户端若首字节不是该头，按第 10.2 节兼容；只改 `protocol.py` 的帧读写，不改 `service.py`、工具 Schema、权限和状态机。

最小方法集：`initialize`、`notifications/initialized`、`tools/list`、`tools/call`、`ping`。`initialize` 声明的协议版本字符串锁定为 `2025-11-25`。客户端协商到其他版本：会话失败，`VALIDATION`，进程不降级到未写明的旧行为。

4A 的 `tools/list` 只有 `get_project`、`get_task`、`list_tasks`、`get_status`。

STDIO 没有网络凭证。主体取启动进程的操作系统用户，经登记文件里的 `local_principals` 映射为 `principal_id` 与权限集合。映射缺失：`UNAUTHENTICATED`，四个工具都不可用。模型参数中的 `user_id` 不参与映射。

4A 验收对应的测试必须覆盖：Schema 往返、未知项目、跨项目 `FORBIDDEN`、路径参数 `PATH_REJECTED`、超过一页不漏 ID、插入并发时 `stale`、敏感键与私钥文本不出现在 stdout。

## 8. 4B：受控写入与一致性

### 8.1 主体、权限与版本

写工具在 4A 的 STDIO 上就可以启用，供本地试点。启用前登记文件为该 OS 用户授予细权限。未单独授予的权限不隐含包含。

`task_version` 算法：对任务卡现有字段中的 `id`、`status`、`assignee`、`owner`、`name`、`end_time` 或 `end_date`、`is_deleted`、`remarks` 做规范化 JSON（UTF-8、键排序、紧凑分隔符），再取 SHA-256。不向卡上新增版本字段，因此旧卡无需迁移。`get_task` 与 `get_status` 返回该摘要，键名为 `task_version`。

`expected_version` 在状态变更前比对。比对点必须位于 `transition_task_pipeline` 已经取得任务锁、并且已经重新读取卡片之后、写入之前。实施时给管道增加可选参数 `expected_card_version`，默认空字符串。空字符串保持现有 CLI 行为。MCP 始终传入。不匹配则管道返回失败且不写卡，MCP 映射为 `CONFLICT`，并附带新的 `task_version` 供客户端重读。该参数是乐观并发条件，不是新状态。

禁止在 MCP 进程里再套一层与 `acquire_concurrency_lock` 相同的锁然后调用管道，以免不可重入锁死锁。版本检查放进管道已有锁内。

### 8.2 幂等

账本文件位于登记的 `data_root` 下 `mcp_idempotency/<project_id>.jsonl`，与 Evidence 目录分开。记录字段：`principal_id`、`tool`、`idempotency_key`、`request_sha256`、`state`（`pending` 或 `completed`）、`result`、`created_at`。

顺序：

1. 对 `(principal, tool, key)` 加账本锁。
2. 已完成且摘要相同：直接返回原结果，码为 `IDEMPOTENCY_REPLAY`，不再调用领域函数。
3. 已完成且摘要不同：`CONFLICT`。
4. `pending` 且未超过 60 秒：`CONFLICT` / `IN_PROGRESS`。
5. `pending` 且已超时：进入对账。对账只读看板或 Evidence。若目标事实已经达成（同一 `evidence_id` 已存在且摘要一致，或状态已是本次 `to_status` 且 assignee 一致），把账本标成 `completed` 并按重放返回。若事实未达成，沿用原键再调用一次领域函数。
6. 新请求：先追加 `pending` 并 fsync，再调用领域函数，成功后把同一键标成 `completed` 并写入结果。领域函数失败：记录失败码，允许同一键与同一摘要重试；换摘要则 `CONFLICT`。

超时后的客户端必须先用原键查询重放，禁止新键重复建卡。服务端靠上述账本保证这一点，不依赖客户端自觉。

### 8.3 证据上传与门禁

`submit_evidence` 的 metadata 在写入前经过 `EvidenceStore.prepare_metadata`。随后 `EvidenceStore.append` 负责原子写与 `content_hash`。MCP 不计算第二套哈希算法。

准出只发生在 `transition_task` 或 `request_acceptance` 内部，调用顺序固定：

1. 读登记与权限。
2. 管道锁内版本检查。
3. `validate(role, from_status, to_status, ...)`，Profile 规则仍由该函数执行。Lite 完成仍要求 Verifier 证据与低风险 diff；docs-short 仍要求文档检查；Standard 仍拒绝 Verifier 冒充审查。
4. 若数据根上存在该任务的 Runner Checkpoint，调用 `validate_managed_transition()`。无 Checkpoint 的单角色旧任务继续走该函数今天的放行分支，MCP 不额外加门。
5. 需要 EvidenceGate 的步骤调用 `EvidenceGate.validate_evidence` 或 `validate_lite_verifier`。异常 `EvidenceGateError` 映射为 `GATE_REJECTED`。
6. 全部通过后才由 `transition_task_pipeline` 写看板与审计。

因此客户端可以上传一份自称 `PASS` 的证据并得到 `gate_status=NOT_EVALUATED`。随后的流转若门禁失败，看板不动。MCP 不提供“只信客户端 decision”的开关。

远程或本地执行器若要上传证据，只能提交符合 `EvidenceRecord` 的结构化字段。MCP 不从客户端路径读文件。artifact 的相对路径检查仍由 `EvidenceGate.validate_evidence` 在准出时执行；上传时若路径是绝对路径或包含 `..`，提前 `PATH_REJECTED`，避免坏记录落盘。

### 8.4 用户验收

验收与通用流转拆开。`request_acceptance` 只申请，不把任务写成「已验收」。

- `transition_task` 看到 `已验收`：`ACCEPTANCE_REQUIRED`。
- 进程环境变量 `YY_FLOW_MCP_ACCEPTANCE_CONFIRMED` 和模型参数 `acceptance_confirmed` 都不构成逐次用户确认。STDIO 与 HTTP 把上下文里的该标记固定为假。
- 申请成功时，服务端生成不可预测编号，写入项目数据根 `mcp_auth/acceptance/<编号>.json`。返回项目、任务、候选 SHA、契约哈希、质量证据摘要、动作 `accept`、编号和过期时间。任务保持「已完成」。同一幂等键返回同一编号。
- 用户在本机交互式控制台执行 `python scripts/mcp_server.py acceptance --registry <登记> --id <编号> --decision approve|reject`，并在控制台设备上重新输入确认编号。管道标准输入不能批准。身份只取操作系统进程令牌上的账户名（Windows 打开当前进程令牌后解析账户，POSIX 使用 `geteuid`），不读取 `USERNAME`、`USER`、`LOGNAME`，也不使用线程模拟令牌。该账户必须匹配登记主体，并且该主体对项目有 `acceptance`。命令行里的用户编号不参与判断。同一操作系统账户不能单独证明是人在批准；非交互调用会被拒绝。模型若能自己打开伪终端，这条边界仍然不够，不能写成已经具备独立人类审批控件。
- 申请必须引用检查点里的最新候选，以及同一候选上的独立质量链。Standard 需要真实宿主上的审查 PASS 与测试 PASS，二者会话不同。审查通过可以记在 `transition_to=TESTING`，测试通过记在 `qa_decision`。Lite 需要可区分的 Verifier PASS（`verifier_decision`）。这些记录可以没有产物。`USER_CONFIRMATION` 不是质量证据。契约哈希包含检查点快照里的验收标准哈希；快照若带有验收条目或验收正文，一并纳入。没有检查点时，使用任务卡上的候选、验收条目和备注。改变条目或候选后，旧确认编号失效。
- 批准在质量链通过之后才写入用户确认证据，并仍要独立通过 EvidenceGate。门禁失败不消费为成功验收。拒绝把原任务从「已完成」退回「已退回」，不新建任务。成功后记录一次性消费；已消费且终态审计已落盘时，再次调用只返回原结果。若看板已经改完但终态审计写入失败，返回「业务已应用、审计待补记」；重试只补记审计，不再次流转。
- Runner 的 `accept` / `reject` 仍走检查点上的 `confirmation_request_id`。MCP 确认编号是另一套编号。申请时由服务端把检查点编号写入申请记录，批准时用该编号调用 `Runner.accept`，不把两个编号当成同一个，也不改写检查点里的编号。用户确认证据同时记下 `confirmation_id`（Runner）和 `mcp_confirmation_id`（MCP）。锁顺序是先占验收记录锁，再占 Runner 锁。

没有检查点、或检查点不能唯一确定最新候选时，不能申请。批准前任务必须仍是「已完成」。

### 8.5 状态、审计、并发与故障

一次成功的写在对外返回前满足：

- 看板卡与管道原有审计已提交（管道现有 `record_audit_event`）。
- MCP 幂等账本已标 `completed` 并 fsync。
- MCP `audit_store` 追加了一行：时间、principal、project、tool、task、结果码、请求摘要。不含证据正文。

返回 JSON 失败时不得回滚已成功的看板写入；客户端用原幂等键重放读取结果。这与现有“最终 JSON 输出失败不能破坏已持久化状态”一致。

服务在 `pending` 之后崩溃：重启不自动重放写操作。下次同一键的请求按第 8.2 节对账。重启不清空账本，不重置限流计数的当日累计（若实现了日配额）。

软删除与恢复不经过状态机。在 `OfflineBoardAdapter` 增加锁内方法，语义对齐现有看板 HTTP 的 `is_deleted` / `restore`：列表默认过滤已删除项，恢复只清除删除标记。MCP 与连接器禁止直接打开 `board.json`。

负向测试至少包括：越权项目、错误角色、重放相同键、相同键不同体、并发双写版本冲突、响应在 fsync 前断开后的重放、进程重启后的对账、重复 `evidence_id`、伪造 `PASS`、用 `transition_task` 申请验收、错误响应不含令牌样例。

## 9. 4C：远程部署与认证

本批交付单组织、按项目隔离的远程进程设计，并给出可本机试验的配置。不部署生产，不开放多租户自助开通。

### 9.1 协议锁定与官方 SDK 接入点

协议版本锁定 MCP `2025-11-25`。Python 侧官方 PyPI 包名仍是 `mcp`。2026-10-13 对照 PyPI 与规范页 `https://modelcontextprotocol.io/specification/2025-11-25/basic/transports` 的结果：

- 发行版名称仍为 `mcp`，当时的当前版本是 `2.3.0`，要求 Python >=3.10，说明支持 Streamable HTTP，并声明兼容更早的协议修订。
- 端点是单一 `POST /mcp` 与 `GET /mcp`。POST 必须带同时包含 `application/json` 和 `text/event-stream` 的 `Accept`。会话头规范写法是 `MCP-Session-Id`，后续请求带 `MCP-Protocol-Version: 2025-11-25`。
- 官方服务器入口在该版文档中位于 `mcp.server` / `mcp.server.mcpserver.MCPServer`。本仓库默认 `--transport-backend compat`。当前实施环境执行 `import mcp` 失败，SDK 后端未启用，也未安装到用户环境。不要把未导入成功写成已验证。

代码里的协议版本字符串保持 `2025-11-25`。兼容层不依赖官方包，旧环境不安装 SDK 仍可使用 STDIO 与 compat HTTP。

官方 SDK 接入点设计如下，供核对后填写真实导入：

1. `service.py` 暴露普通函数 `handle_tool(name, arguments, context) -> dict`。
2. 官方 SDK 的服务器对象只在 `http_server.py` 与 `stdio_server.py` 的工厂函数里创建。
3. 每个 MCP 工具注册为薄包装：从 SDK 的请求上下文取出 `AuthContext`，调用 `handle_tool`，再把 `McpError` 转成 SDK 的工具错误。
4. 业务模块不 import SDK。这样 SDK 缺失时，兼容层仍能调用 `handle_tool`。

若当前环境无法安装官方包，使用不依赖网络的兼容层，放在 `protocol.py`：

- JSON-RPC 2.0 请求、成功响应、错误响应、通知。
- STDIO：官方帧保持第 7 节的 Content-Length。另有一种只在读取端按首字节分流的换行 JSON，契约在第 10.2 节。两种帧都要有测试。不得为了换行 JSON 删掉 Content-Length。
- Streamable HTTP 最小集：单一路径 `POST /mcp` 与 `GET /mcp`。POST 的 `Accept` 必须同时包含 `application/json` 与 `text/event-stream`；本批工具调用可以回 `application/json` 单对象，缺 Accept 则拒绝。`GET /mcp` 在会话建立后返回短 `text/event-stream`（心跳或空事件）并结束，不长期挂起。会话头以规范为准，名为 `MCP-Session-Id`；协议版本头为 `MCP-Protocol-Version`。这两个常量只放在 `protocol.py`。HTTP 头匹配不区分大小写。
- `initialize` 之后才能 `tools/call`。
- 兼容层不实现完整采样、根目录、资源订阅。本阶段工具只有第 6 节列出的任务工具，不提供 resources 读磁盘。

兼容层与官方 SDK 二选一由入口参数 `--transport-backend compat|sdk` 决定。默认 `compat`，直到实施记录写明 PyPI 核对通过并在测试环境安装成功。两个后端共用 `handle_tool`。

### 9.2 认证模型

可信主体只来自请求上下文 `AuthContext`：

| 字段 | 来源 |
| --- | --- |
| `principal_id` | 凭证主题 `sub`，或 STDIO 的 OS 用户映射 |
| `org_id` | 凭证 `org` 声明，必须等于登记组织 |
| `project_grants` | 服务端授权表，不从令牌里的项目列表单独信 |
| `scopes` | 授权表与令牌 scope 的交集 |
| `jti`、`exp`、`aud` | 令牌校验结果 |
| `acceptance_confirmed` | 仅当传输层见证了人类确认控件 |

HTTP 凭证是 Bearer 令牌。校验失败的统一对外码是 `UNAUTHENTICATED`。过期、受众不匹配、签名失败都用这一码，细节只进服务端审计。`aud` 必须等于该部署配置的受众。`exp` 必须在时钟偏移 60 秒以内。令牌里的 `project_id` 或 `user_id` 若与上下文冲突，以服务端授权表为准；若令牌主题无法在授权表找到：`UNAUTHENTICATED`。

权限：

| Scope | 允许的工具 |
| --- | --- |
| `read` | `get_project`、`get_task`、`list_tasks`、`get_status` |
| `evidence.submit` | `submit_evidence` |
| `task.transition` | `create_task`、`transition_task` |
| `acceptance` | `request_acceptance` |
| `admin` | `soft_delete`、`restore`，以及 `include_deleted`、强制重复建卡 |

Scope 不自动包含低级 scope。验收主体如果也要读任务，授权表必须同时写下 `read` 与 `acceptance`。

撤销：授权表与 `jti` 撤销列表落在数据目录 `mcp_auth/`。每次工具调用先查撤销列表。命中为 `REVOKED`。撤销动作是本地管理命令，不是模型工具。列表只增记录，用 `revoked_at` 表示失效，不物理删行。

限流：每个 `(principal_id, project_id)` 令牌桶。读工具默认每分钟 60 次，写工具每分钟 20 次。超限 `RATE_LIMITED`。GitHub 下游限流不占用这个桶的“成功”次数，但同步引擎有自己的预算，见第 11 节。

超时：单个工具处理 15 秒。超时返回 JSON-RPC 超时错误并映射 `retryable: true`，幂等账本保持 `pending`，由重试对账。不得在超时后再启动第二次无账本的写入。

远程进程不能读取用户本机仓库。Evidence 中的 artifact 必须已经在登记的 `project_root` 内，由执行器在授权边界内事先放入。MCP HTTP 进程不接受客户端上传的原始文件字节。

禁止：工具参数触发 shell、任意路径、跨组织 ID、把一个项目的 `evidence_id` 用到另一个项目。`service.py` 在调用 `EvidenceStore.read` 前确认该 id 属于当前 `project_id` 的前缀或账本映射。

### 9.3 存储、备份与恢复

本批远程试验仍可使用现有文件型看板与 Evidence。不把 MCP 引入作为更换数据库的条件。若实施时选择把幂等账本与审计放到数据库，必须单独写迁移说明，并保持 Evidence 文件格式不变。

需要备份的新增目录：`mcp_registry.json`、`mcp_auth/`、`mcp_idempotency/`、`mcp_audit/`。看板与 Evidence 沿用现有备份。恢复顺序：先恢复登记与撤销列表，再恢复幂等账本，再恢复看板与 Evidence。恢复后抽查一条已完成幂等键，确认重放结果与看板一致。演练在副本上做，不在生产数据上做。

审计行追加写。发现缺行或摘要断裂：服务对写工具 Fail-Closed，返回 `GATE_REJECTED`，`details.reason=AUDIT_GAP`，直到管理员修复副本。读工具可以继续。

部署手册在 4C 实施时说明：数据目录位置、保留天数、谁可以持有 Bearer 签发权、本批不进入生产。操作说明见 `docs/D01-项目管理/09-第四阶段远程部署与认证说明.md`。该说明只记录已经实现和已经实测的部分，不把第四阶段整段标成通过。

### 9.4 实施核对（2026-10-13）

- 规范页已打开。会话头常量是 `MCP-Session-Id`，协议头常量是 `MCP-Protocol-Version`，只放在 `protocol.py`。架构草稿里的 `Mcp-Session-Id` 与规范大小写不同；HTTP 解析不区分大小写。
- PyPI `mcp` 2.3.0 已核对。当前解释器 `import mcp` 为 `ModuleNotFoundError`。`--transport-backend sdk` 退出码 2，文档标记 SDK 后端未启用。未执行 pip 安装。
- 默认后端是 compat。4C 交付时 STDIO 仍只实现 Content-Length。4D 实测后的双帧契约见第 10.2 节；在按该节改 `protocol.py` 之前，运行中的服务端仍只读 Content-Length。
- 本批不部署生产，不在进程内终止 TLS。监听只允许回环地址。

## 10. 4D：客户端接入验证清单

平台行为以真实客户端为准。本文不根据产品名称推断某个客户端已经允许 MCP 写入。下列各项在本文写作时均为 **未验证**。2026-10-13 的客户端实测没有把任何一项改成已验证。做不到或未做完的项改为阻断，或继续保持未验证。模拟单测不能把标记改成已验证。

| 编号 | 项 | 通过标准 | 状态 |
| --- | --- | --- | --- |
| D1 | 目标客户端能否配置 STDIO MCP | 能完成 initialize 与 `tools/list`，且只看到当前批次工具 | 阻断 |
| D2 | 只读闭环 | `get_project` 到 `list_tasks` 到 `get_task`，未知项目被拒绝 | 未验证 |
| D3 | 写入确认 | 客户端在真正调用写工具前有人类确认；确认结果进入传输上下文而不是模型参数 | 阻断 |
| D4 | 写后查回 | `create_task` 或 `transition_task` 后 `get_task` 版本变化与账本一致 | 未验证 |
| D5 | 撤销 | 撤销 `jti` 后下一次调用为 `REVOKED`，撤销前的成功结果仍可按幂等键重放给原主体以外的管理员读审计 | 未验证 |
| D6 | Streamable HTTP | 核对 spec 后的会话头、POST、SSE 在目标客户端可用 | 未验证 |
| D7 | 验收控件 | `request_acceptance` 没有人类确认时失败，有确认时才进入门禁 | 阻断 |
| D8 | Cursor 原生工作流 | 不配置 MCP 时，现有子代理与 CLI 流转仍可进行 | 未验证 |

Cursor 原生工作流不强制安装 MCP，也不强制安装官方 SDK。MCP 是可选入口。可选卡片 UI 若以后要做，只消费本章工具的结构化 JSON；没有 UI 时，客户端展示 `get_status` 的文本字段即可。UI 不与 4A–4C 的服务发布绑在一起，缺 UI 不阻断工具验收。

ChatGPT 或其他宿主的交互在 D1–D7 有该宿主的记录之前，不写入支持列表。

### 10.1 实施核对（2026-10-13）

命令、退出码和阻断原因见 `docs/D01-项目管理/10-第四阶段客户端接入记录.md`。第四阶段没有全部通过。4E 连接器未做。可选卡片 UI 未做，缺 UI 不阻断已有服务。仓库内没有新增会被 Cursor 自动加载的 `.cursor/mcp.json`。

### 10.2 STDIO 双帧兼容

本节锁定 `protocol.py` 的 STDIO 读写。代码已按本节实现，真实客户端仍未复测。本节不把 D1–D8 改成已验证。依据是 2026-10-13 的客户端记录：Claude Code 2.1.90 的 STDIO 首包是一行 UTF-8 JSON，以 `\n` 结尾，没有 `Content-Length`；服务端因此没有完成健康检查。官方规范里的 STDIO 仍是 Content-Length。两种帧并存，只作用于 STDIO。Streamable HTTP 的 POST、GET 和 SSE 不改。

读取下一条消息时先看第一个字节：

1. 尚无字节即 EOF：与现有行为一样，返回空，不报 `FrameError`。
2. 第一个字节是 `C` 或 `c`：把该字节放回缓冲，整帧按现有 Content-Length 规则读取。头名必须是 `Content-Length`（大小写不敏感），形式仍是 `Content-Length: <字节数>\r\n\r\n` 加正好该字节数的正文。头块上限仍是 256 字节。正文上限仍是 1 MiB（1048576 字节）。其他头名、重复长度、长度越界、截断、非 UTF-8、非 JSON 对象，都是 `FrameError`。
3. 第一个字节不是 `C` 或 `c`：本帧按单行 UTF-8 JSON。从该字节读到第一个 `\n`，一行一条。去掉结尾的 `\n`；若剩余末字节是 `\r`，再去掉这一个 `\r`。剩余字节必须不超过 1 MiB，并且 `json.loads` 的结果必须是一个对象。未遇到 `\n`、空行、超限、编码失败、JSON 失败或结果不是对象，都是 `FrameError`。不得把多行拼成一条，也不得在这一行里面再找 `Content-Length`。

响应使用与本次已成功解析的请求相同的帧：

- 本次请求是 Content-Length：响应仍走现有 `encode_message`，即 Content-Length 头加正文。
- 本次请求是换行 JSON：响应是同一条紧凑 UTF-8 JSON，以 `\n` 结尾，前面不加 `Content-Length`。不含结尾 `\n` 的正文不得超过 1 MiB。
- 通知没有响应体，就不写帧。
- 解析失败不写响应帧。`FrameError` 不携带正文，异常文本为空。已读字节不得进入异常、日志、审计或 JSON-RPC 的 `message` / `data`。

同一连接上可以按条切换帧：每一条响应只跟自己的请求走，不因为上一条是 Content-Length 就拒绝下一条换行 JSON。已经通过的 Content-Length 测试必须继续通过。后续测试至少覆盖：Content-Length 往返、一行 `initialize` 得到一行响应、超长行与坏 JSON 为 `FrameError` 且输出中没有原正文。这些测试通过之前，D1 仍是阻断。

本兼容不改工具 Schema、`SCHEMA_VERSION`、权限 scope、`validate()`、Evidence 类型和看板状态枚举。代码已按本节实现；D1 是否已验证仍以客户端接入记录为准。

## 11. 4E：GitHub Projects 连接器

本批只做 GitHub Projects。Jira 与飞书不实现全量写入，只保留现有 Adapter 与第 11.6 节的后续排期。

### 11.1 权威与方向

YY-Flow 看板、Evidence、Checkpoint 是执行状态的权威。GitHub Project 是投影。

同步模式分两段，必须按顺序打开：

1. **只读对齐**：拉取 GitHub 项，生成差异报告，不写任何一边。
2. **受控写出**：仅当配置 `write_enabled=true` 时，把 YY-Flow 的非验收字段投影到 GitHub。默认 `false`。

GitHub 上的状态变化不能把 YY-Flow 推进到 `已验收`、`已完成` 或任何其他状态。若外部状态看起来像验收，记冲突 `EXTERNAL_ACCEPTANCE_IGNORED`，等待 `request_acceptance`。

外部标题或正文按第 11.7 节处理：记契约变更并停止覆盖该任务的投影，直到 YY-Flow 侧有明确的契约更新。引擎不改任务的名称、验收标准或 Profile。

### 11.2 同步引擎

模块 `scripts/_lib/connectors/github_projects_sync.py` 依赖注入：

```text
class GithubTransport(Protocol):
    def query(self, query: str, variables: dict) -> GithubResult
```

`GithubResult` 区分：成功数据、GraphQL 错误、HTTP 状态、速率限制重置时间、分页 `pageInfo`。禁止用空字典表示失败。现有 `GitHubProjectsAdapter._graphql_query` 在失败时返回 `{}` 并打印日志，同步引擎不调用该吞掉错误的路径作为成功依据。遗留 Adapter 保留给旧 CLI。新传输可以复用其查询字符串，但必须向上返回错误类型。

无 `GITHUB_TOKEN` 且登记文件中的等价凭证也为空时，按第 11.7 节返回 `CREDENTIALS_ABSENT`。单元测试使用 `FakeGithubTransport`，不访问网络。文档与测试报告把真实调用标为 **未验证**，直到有沙箱项目的记录。不得用生产看板做演练。

身份映射表在连接器配置中：`github_login -> yy_flow_assignee`。未映射的登录名写入差异报告的 `unmapped_actor`，不猜测角色，不把 GitHub 登录写成 YY-Flow 处理人。令牌权限取项目读，写出阶段才增加项目字段写。令牌不进入仓库、日志和 MCP 响应。

### 11.3 分页、速率、事件与冲突

只读拉取使用 ProjectV2 `items(first, after)` 与 `pageInfo { hasNextPage endCursor }`。`first` 固定 50。循环直到 `hasNextPage=false` 或达到页预算（默认 40 页）。预算耗尽：同步结果为部分失败，保留 cursor，下次续拉。不得把第一页当成全量。

速率限制：读取响应中的成本或重置时间。遇到限制时停止本轮、持久化 cursor、返回 `RATE_LIMITED`，退避上限 3 次，间隔按重置时间截断到 60 秒。不清空已写入的同步检查点。

事件：

- 以交付 ID 做幂等。重复交付直接确认，不第二次写投影。
- 每条外部项保存 `external_updated_at`。更早的事件记 `stale` 并跳过。
- 乱序到达时，较新事件先应用；较旧事件随后到达则保持跳过。

冲突、软删除映射和部分失败续跑按第 11.7 节执行。软删除不是新的看板状态，归档也不是 `已取消` 或 `已验收`。

现有 Adapter 的缺口（实施时要在新引擎补上，而不是在旧方法里返回假成功）：

- `list_records` 无 cursor，`offset` 未使用。
- `get_record` 只扫描有限的第一页。
- `update_record` 依赖项目字段 ID；缺参时返回 False。同步引擎把 False 当成失败进入重试队列，不当成已对齐。

### 11.4 与 MCP 的边界

连接器不是 MCP 工具。模型不能通过 MCP 传入 GitHub 查询、URL 或令牌。若 4C 的管理员需要触发一次同步，使用本地命令：

```text
python scripts/mcp_server.py sync-github --project-id <已登记 ID> --mode read-only
```

该命令读取登记，不接受路径参数。`--mode write` 仅当配置已打开 `write_enabled`。

### 11.5 4E 测试

使用假传输覆盖：超过一页、字段缺失、无权限、速率限制、重复事件、乱序事件、双向冲突、部分写失败后续跑、外部验收被忽略。真实 HTTP 用例默认跳过，名称中保留“未验证”。

### 11.6 Jira 与飞书后续排期

不在本批实现。后续各自独立批次，沿用同一同步引擎接口时再写适配。约束先写明：

- Jira 状态机与 YY-Flow 状态机不是同一个。禁止用 Jira transition 反向驱动 `已验收`。
- 飞书现有 Adapter 通过 `npx` 子进程调 CLI。MCP 进程与同步引擎都不得把这种 shell 调用暴露给模型，也不得接受来自工具参数的命令字符串。后续飞书批次若继续，应换成显式 HTTP API，并单独做令牌与分页设计。
- 两者都要先只读对齐，再决定是否受控写出。

### 11.7 实施锁定

第 11.1 到 11.6 节加上本节即可开工 4E。本节只写死原先可以两边解释的五点。不新增看板状态名，不新增 Evidence 类型。`CREDENTIALS_ABSENT`、`EXTERNAL_ACCEPTANCE_IGNORED`、`NO_EXTERNAL_ITEM`、`unmapped_actor` 只是同步结果原因，不是 `validate()` 的状态，也不是现有四类 Evidence。真实 GitHub 调用保持未验证。当前没有可用的 GitHub 凭据，实施时不得把缺凭据的单测写成真实调用已通过。

**默认只读。** `write_enabled` 缺省为 false。本地命令默认 `--mode read-only`。只读轮次只发送 ProjectV2 查询并生成差异报告，不调用 mutation。`--mode write` 在 `write_enabled` 不是 true 时拒绝启动，网络写的次数为零。

**无凭据。** 环境变量 `GITHUB_TOKEN` 与登记文件中的等价凭证都为空时，真实传输在打开连接之前失败，原因固定为 `CREDENTIALS_ABSENT`。不访问 `api.github.com`，不用空对象表示空项目，不把凭证写入仓库、日志、差异报告或 MCP 响应。`FakeGithubTransport` 不需要令牌；使用假传输的测试不能把真实调用标成已验证。

**软删除映射。** 只在写出模式、并且同步账本已经有该任务的 GitHub item id 时投影可见性：

- `is_deleted=true`：调用 `archiveProjectV2Item`。
- `is_deleted=false` 且该项已归档：调用 `unarchiveProjectV2Item`，沿用同一个 item id，不新建项。

禁止 `deleteProjectV2Item`。没有 item id 时，不为了归档去创建一项；该任务的差异原因是 `NO_EXTERNAL_ITEM`，本条不算已对齐。归档和取消归档都不修改 YY-Flow 的 `status`，也不表示 `已取消` 或 `已验收`。

**冲突。** 投影字段只有标题、正文、GitHub Status 单选、处理人。Evidence、Checkpoint、幂等账本和验收标记不是投影字段。

- 外部改了标题或正文：审计记契约变更，并停止继续写出该任务。不改 YY-Flow 的名称、状态、Profile 或验收标准。其他任务继续。YY-Flow 侧没有新的契约更新记录之前，不得恢复覆盖该任务。
- 外部 Status 与 YY-Flow 当前状态不一致：不调用 `validate()`，不推进 YY-Flow。写出模式用 YY-Flow 当前状态名重写同名 option；没有同名 option 时，该条进入部分失败队列。外部 Status 文本是 `已验收`，或用其他文本表示验收：YY-Flow 保持原状态，审计 `EXTERNAL_ACCEPTANCE_IGNORED`。写出模式仍可把 GitHub Status 改回 YY-Flow 当前状态的同名 option，不得把 YY-Flow 写成 `已验收`。
- 上次成功同步之后，两边都改过同一个投影字段：该任务结果为 `CONFLICT`，停止该任务的写出，不自动合并。其他任务继续。
- 未出现在 `github_login -> yy_flow_assignee` 中的登录名只记 `unmapped_actor`。同步不修改 YY-Flow 的 assignee，也不按登录名猜测角色。

**部分失败续跑。** 每个写出动作只有 `pending`、`applied`、`failed`。进程中断后按原顺序找到第一条 `pending` 或 `failed` 继续。`applied` 不再次创建。创建前先用账本中的 item id 或交付 ID 读取；GitHub 上已经有该项就改记 `applied` 再继续，没有才重试同一次创建。还有任一条 `failed` 时，本轮总结果不是成功。续跑不清 cursor，不清已 `applied` 的记录，也不把第一页当成全量。

## 12. 兼容

| 入口 | 本设计之后的行为 |
| --- | --- |
| Cursor 原生编排与 `flow-*` 子代理 | 继续按 Profile 调度。不安装 MCP 也可工作 |
| `scripts/run_task.py` Production Runner | 继续写 Checkpoint 与 Evidence。MCP 验收必须复用其确认 ID 与 EvidenceGate |
| 单角色 `transition_task.py` / `quick_task.py` | 参数与默认行为保持。新可选参数 `expected_card_version` 默认关闭 |
| Profile `standard` / `lite` / `docs-short` | 仍由 `workflow_policy` 与 `validate()` 执行。MCP 不另写 Profile 判断 |
| 旧任务、旧 Checkpoint、旧 Evidence | 不改字段含义，不补写历史 Evidence，不迁移旧 Checkpoint 结构 |

`expected_card_version` 是唯一计划中的领域函数签名增量，且默认空值等价于今天的 CLI。除此之外，不改 `EvidenceRecord`、`RunnerCheckpoint` 和看板卡的必填字段。

## 13. 与现有函数的复用点

| 能力 | 复用 |
| --- | --- |
| 建卡 | `quick_task` 所调用的 `transition_task_pipeline(..., create_only=True)`，含重复名检查与“必须先到待开始” |
| 流转门控 | `validate()` 的角色矩阵、终态锁、Profile、Lite Verifier、docs-short、结束时间、并发上限 |
| 落卡 | `transition_task_pipeline` 的锁、适配器写入、`record_audit_event` |
| Runner 准出 | `validate_managed_transition`、`replay_record`、`EvidenceGate.validate_evidence`、`validate_lite_verifier` |
| 证据持久化 | `EvidenceStore.append` / `read`、`prepare_metadata`、`canonical_json` |
| 用户验收证据形状 | 与 `production_runner` 中 `USER_CONFIRMATION` 记录一致：`explicit_user`、`confirmation_id`、`confirmed_at`、`EvidenceType.USER_CONFIRMATION` |
| 编排期验收校验 | `orchestrator` 中对 `ConfirmationResult` 与 `confirmation_request_id` 的匹配规则。MCP 远程路径不得放宽 |
| 只读看板 | `OfflineBoardAdapter.get_record_readonly`、`list_records_readonly` |
| 软删除语义 | 对齐 `start_kanban_server` 已有的 `is_deleted` 与 restore 字段，但实现放在适配器锁内方法，不复制一份直接写文件的逻辑，也不经 MCP 改 `board.json` |
| 脱敏 | `EvidenceStore._mask_text` 与 `_mask_secrets` |
| 策略缺省 | 无 `workflow_policies` 文件时按 Standard，见第三阶段迁移说明 |
| GitHub 低层 API 形状 | 可参考 `GitHubProjectsAdapter` 的 ProjectV2 查询与 `updateProjectV2ItemFieldValue`。错误处理与分页以新传输为准 |
| 版本冲突体验 | 可参考看板 HTTP `atomic_mutate_board_data` 的 expected version 比对方式。MCP 的任务级摘要按第 8.1 节计算，不强制改用整板哈希 |

不复用、且禁止在 MCP 中调用的入口：飞书 Adapter 的 `subprocess`/`npx`，以及任何把工具参数当作 shell 或路径的代码。

## 14. 实施顺序

1. 4A：`protocol.py`、`schema.py`、`errors.py`、`redaction.py`、`registry.py`、`service.py` 的四个读工具、`stdio_server.py`、`mcp_server.py stdio`、`tests/test_mcp_stdio.py`。
2. 4B：幂等账本、`expected_card_version` 可选参数、写工具、适配器软删除方法、`tests/test_mcp_write.py`。
3. 4C：`auth.py`、`http_server.py`、撤销与限流、兼容层或核对后的官方 SDK、`tests/test_mcp_http_auth.py`。本步结束仍不部署生产。
4. 4D：按第 10 节清单做真实客户端记录。未做的项保持未验证。STDIO 双帧只按第 10.2 节改 `protocol.py`；改完并补测试之前，D1 仍是阻断。
5. 4E：按第 11 节与第 11.7 节实现同步引擎和假传输测试。只读模式先于 `write_enabled`。不新增状态名或 Evidence 类型。真实 GitHub 调用单独标记为未验证。

任一步发现需要新的状态名或新的 Evidence 类型，停止并回到架构评审。用新增状态绕过 `已验收` 或 EvidenceGate 不符合本设计。

## 15. 开发方案问题分析与任务风险等级评估

### 15.1 破坏性变更

- 领域签名只计划增加默认空的 `expected_card_version`。旧 CLI 不传该参数时行为应与现在一致。这是低风险兼容点，但实施时要用现有流转测试回归，确认默认路径没有开始要求版本号。
- 看板卡、Evidence、Checkpoint 的格式语义不变。软删除沿用已有 `is_deleted`，不新增状态。
- 官方 SDK 与兼容层如果同时改工具注册方式，有可能让 `tools/list` 与本文 Schema 漂移。规避方法是 Schema 只定义在 `schema.py`，两个后端都从该处生成。

### 15.2 性能与容量

- `list_records_readonly` 当前会读全表再切片。项目任务量上升到数千后，MCP 按 ID 过滤仍是全表扫描。4A 接受此成本，页大小上限 50。若单板超过 5000 张卡，列表延迟会成为瓶颈，应在后续为适配器加按 ID 的范围读，而不是在 MCP 里复制一份索引状态机。
- 证据上限 256 KiB 防止把日志整文件塞进 metadata。大日志继续走现有 artifact 哈希，不进 MCP 响应。
- GitHub 分页预算与速率退避防止一次同步打满 API。预算耗尽必须显式部分失败。

### 15.3 风险等级

| 项 | 等级 | 理由 | 回滚 |
| --- | --- | --- | --- |
| 4A 只读 STDIO | 低 | 不写看板、不写 Evidence | 停用 `mcp_server.py` 进程。领域 CLI 不受影响 |
| 4B 幂等与版本参数 | 中 | 触碰流转管道的锁内顺序；默认参数若被误设为必填会破坏 CLI | 保留空字符串默认；MCP 入口用配置关闭写工具后，管道额外参数不生效 |
| 4B 验收拆分 | 高 | 若误把 `已验收` 留在通用工具里，模型可以绕过人类授权 | 写工具默认关闭。`to_status=已验收` 在进入管道前硬拒绝。回滚时关闭 `request_acceptance`，验收仍只走现有 Runner / 人工 CLI |
| 4B 伪造 PASS | 高 | 上传与准出若写在同一个工具里，门禁会被客户端字段替代 | `submit_evidence` 不接收“同时流转”的参数。回滚即拒绝所有写 scope |
| 4C 远程认证 | 高 | 主体来源一旦改读模型参数，即跨项目泄露 | HTTP 入口默认不启动。凭证校验失败全部 `UNAUTHENTICATED`。回滚为只保留 STDIO |
| 4C 兼容层与官方 SDK 不一致 | 中 | 未核对 PyPI/spec 就宣称兼容会让客户端连不上 | 默认 `compat`，协议常量集中。核对失败则不切换 `sdk` |
| 4D 客户端 | 中 | 未验证项被写成已支持会误导接入 | 清单默认未验证。没有记录不得改标记 |
| STDIO 双帧 | 中 | 删掉 Content-Length 会使已测 4A 夹具失败；换行帧若回显正文会泄露 | 只改 `protocol.py` 的帧读写。回滚时恢复为只读 Content-Length。不改 Schema、权限、状态机 |
| 4E 写出 | 高 | 外部状态反向覆盖会绕过验收；旧 Adapter 空字典会被当成空项目 | 默认只读。`write_enabled` 默认 false。真实令牌缺失时不发网络。回滚配置即可停止写出，YY-Flow 数据不从 GitHub 回灌 |
| 4E 飞书 shell | 高 | 现有实现带命令执行。若从 MCP 调用即任意命令风险 | 本批不接飞书。同步引擎禁止 subprocess |

高风险项在对应批次的写开关默认关闭时才允许合并代码。打开写开关、HTTP 监听或 GitHub 写出，都需要该批次自己的负向测试证据。本文不把这些开关视为已经打开。

### 15.4 回滚总策略

1. 停止 `mcp_server.py` 与 `sync-github` 进程。
2. 保留幂等账本与审计以便对账，不删除 Evidence，不改历史看板状态。
3. 领域 CLI 与 Runner 继续使用原路径。
4. 若 `expected_card_version` 的默认行为被证明影响旧 CLI，撤回该参数的强制校验，只在 MCP 调用路径传入；不得为了回滚 MCP 而放宽 EvidenceGate 或终态锁。

本设计的实施风险集中在“协议层是否偷做了状态判断”和“外部投影是否反向写了验收”。这两处在代码审查时对照第 1 节与第 8.4、11.1 节即可判断是否偏离架构。

## 13. 2026-10-15 经用户确认的本批交付边界

用户在本轮结项中明确选择“按已验证能力交付，SSE 明确列为后续兼容验证”。因此第 10 节 D1–D8 表保留为原始客户端验证目标及当时状态，不整体改绿；本批按以下实际支持面交付：

| 能力 | 本批依据 | 不宣称的能力 |
| --- | --- | --- |
| Cursor STDIO | 真实 initialize/工具发现、读、授权建卡、查回、同键回放、重启与主体停用；客户端记录第 7 节 | 不代表其他客户端版本通过 |
| Cursor HTTP JSON | 回环真实客户端读写、查回、重启与 jti 撤销；远程认证说明第 10 节 | 未观察到客户端 REVOKED 正文，不能伪称界面已显示；真实客户端 SSE 单列后续 |
| 用户验收 | 真实用户本人通过本机可信命令批准与拒绝，原任务和 Runner/证据/审计闭环 | 不采用模型提供的 acceptance_confirmed；不宣称新增人类专用 UI 或可防同账号伪终端 |
| 原生工作流 | 既有 Cursor 子代理与领域 CLI 保留，自动化兼容回归 | 不将原生自述回执升级为真实宿主证据，不强制 Cursor SDK |
| GitHub | 唯一私有沙箱的真实只读、建卡、多页、循环投影与跨进程恢复；连接器说明第 9–11 节 | 不扩展到 Jira/飞书、其他组织或生产看板 |

旧 D3 中依赖传输 confirmed 标志的描述不再是验收授权实现；以第 8.4 节的独立可信决定为准。普通写工具由登记主体与最小 scope 控制，不能把模型参数当作人类审批。D7 的申请本身不推进验收，批准仍经独立入口与 EvidenceGate。

延期或未支持项必须在操作指南明确列出：真实客户端 SSE、官方 SDK 后端、多客户端通用 UI、Jira/飞书、公网生产/TLS、多组织自助接入。结项不代表这些项通过；阶段门禁、代码测试和安全门禁不因本次能力范围确认而豁免。
