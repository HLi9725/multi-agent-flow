---
title: 第四阶段 GitHub 连接器说明
module: multi-agent-flow
stage: Phase-4
type: guide
status: in-progress
author: 李开发
updated_at: 2026-10-15
tags: [MCP, GitHub Projects, 4E, STDIO]
---

# 第四阶段 GitHub 连接器说明

本文说明已落地的 STDIO 双帧和 GitHub Projects 连接器。第 9–10 节保留初次实测与失败记录；第 11 节记录修复后的复验。第四阶段整体结项仍须通过独立审查、全量测试和阶段门禁，不能根据历史局部失败或局部成功推断当前整体结果。

## 1. 已实现

- STDIO 读取先看第一个字节。`C` / `c` 仍走 Content-Length。其他首字节按一行 UTF-8 JSON，响应使用与该次请求相同的帧。
- 超限、截断和非对象 JSON 仍是 `FrameError`。解析失败不写响应帧，异常不携带正文。
- 本地命令：`python scripts/mcp_server.py sync-github --project-id <已登记 ID> --mode read-only`。不接受路径参数。`--mode` 缺省 `read-only`。
- `--mode write` 只有登记里的 `write_enabled` 为 true 才继续。否则拒绝，网络写次数为 0。
- 同步账本和差异报告在项目数据根下的 `mcp_github_sync/`。不改看板卡必填字段，也不经 MCP 工具暴露。
- 只读对齐只拉 ProjectV2 `items(first: 50, after)`。不写 YY-Flow，也不调用 mutation。
- 写出模式的可见性只使用 `archiveProjectV2Item` / `unarchiveProjectV2Item`。不调用 `deleteProjectV2Item`。
- 外部状态不能把 YY-Flow 推进到已验收或已完成。未映射登录只记 `unmapped_actor`。

登记可选字段：`github_project_id`、`write_enabled`（缺省 false）、`identity_map`、`github_credential_env`（凭证所在环境变量的名字，缺省 `GITHUB_TOKEN`）。登记里若另有凭证值，只留在进程内，不进入日志、差异报告或命令输出。

## 2. 假传输已测

`tests/test_mcp_github_sync.py` 使用 `FakeGithubTransport`，不访问网络。覆盖超过一页、页预算、字段缺失、无权限、速率限制、重复事件、乱序事件、双向冲突、部分写失败后续跑、外部验收被忽略、未映射处理人、无凭证、`write_enabled` 为 false 时拒绝写出，以及软删除只走归档和取消归档。

速率限制测试注入时钟，不真实等待 60 秒。

## 3. 初期真实 GitHub 未验证记录

没有可用的沙箱项目记录。真实 HTTP 用例默认跳过，名称中保留“未验证”，并且不会发出请求。假传输通过不能写成真实 GitHub 已验证。

## 4. 无凭证阻断

环境变量 `GITHUB_TOKEN` 与登记中的等价凭证都为空时，连接前失败，原因是 `CREDENTIALS_ABSENT`。此时不访问 `api.github.com`，也不用空对象表示一个空项目。

本文不记录令牌、口令或连接串。

## 5. DEF-T0041 修复

审查退回后的修复仍只在假传输上验证，真实 GitHub 调用保持未验证。

- 创建草稿只声明 `addProjectV2DraftIssue` 实际接收的 `projectId`、`title`、`body`。任务编号和交付 ID 写进正文的 `yy-flow-task-id` 与 `yy-flow-delivery-id` 行，读回时去掉这两行再比较正文。创建响应丢失后，下一轮用该约定认领已有项并记 `applied`，不再创建一次。
- 状态更新先读取 Project 的 Status 单选，用 YY-Flow 当前状态名匹配同名 option。找到才把 `fieldId` 和 `optionId` 写入 `updateProjectV2ItemFieldValue`。没有同名 option 时该条保持 `failed`，本轮不是成功。不调用 `validate()`，也不改 YY-Flow 的状态。
- 归档和取消归档继续使用已有 item id，不把状态名当作 ID，也不调用 `deleteProjectV2Item`。

## 6. 2026-10-14 沙箱条件

本机没有 `GITHUB_TOKEN`、`GH_TOKEN`，也没有 `gh` 命令。`~\.config\gh\hosts.yml`、`%APPDATA%\GitHub CLI\hosts.yml` 和 `~\.github\token` 都不存在。因此没有发起 `api.github.com` 请求。假传输结果仍然不是真实 GitHub 通过。

继续只读验证需要沙箱 Project 的 GraphQL 节点编号，以及连接器实际会读取的凭据。凭据不要粘贴到对话里。只读凭据不能做写入测试。写入前还要单独授权目标条目和清理方式。禁止操作正式业务看板。GitHub CLI 的登录状态不会被当前连接器读取。

## 7. 2026-10-15 条件复检

沿用 T0041 做了只读条件检查。记录和文档里仍然没有沙箱 Project 所有者和项目编号。本机进程、用户和系统范围都没有 `GITHUB_TOKEN`、`GH_TOKEN`、`GITHUB_PAT`，没有 `gh` 命令，也没有 `~\.config\gh\hosts.yml`、`%APPDATA%\GitHub CLI\hosts.yml` 或 `~\.github\token`。因此没有连接 `api.github.com`。假传输和协议自测仍然不是真实 GitHub 验证。当时把 CLI 登录状态写成可用凭据来源，与实现不符，以第 8 节为准。

## 8. 2026-10-15 凭据来源核对

`python scripts/mcp_server.py sync-github --help` 退出码 0。正式入口只有 `--project-id` 和 `--mode`。`--mode` 缺省 `read-only`。命令没有 `--registry`。登记文件只从环境变量 `YY_FLOW_MCP_REGISTRY` 读取。`--project-id` 是登记里的 YY-Flow 项目编号，不是 GitHub Project 编号。GitHub 目标写在登记字段 `github_project_id`，查询使用 `node(id: $projectId)`，所以它必须是 Project 的 GraphQL 节点编号。

连接器实际读取凭据的顺序是：登记字段 `github_credential`，然后 `github_credential_env` 所指的环境变量。该字段缺省，或在登记里写成空，解析时就定为 `GITHUB_TOKEN`。只有这个名字不是 `GITHUB_TOKEN` 且对应变量没有值时，才再读 `GITHUB_TOKEN`。它不读取 `GH_TOKEN`，除非登记把 `github_credential_env` 设成这个名字。它也不读取 GitHub CLI 的 `hosts.yml` 或 `gh auth token`。登记里的 `github_credential` 不得写入仓库。

同日再次检查：进程、用户和系统范围的 `GITHUB_TOKEN`、`GH_TOKEN`、`GITHUB_PAT` 都为空。文档和代码注释里没有沙箱所有者和 `github_project_id`。没有执行 `sync-github`，因为没有可映射的沙箱，也没有连接器认可的凭据。真实只读、真实多页和真实写入都未验证。`write_enabled` 保持缺省 false。T0041 在记录这次核对后回到已阻塞。

下一次开始前需要同时提供：

1. 沙箱 Project 的所有者和 GraphQL 节点编号。不要改用其他项目代替。
2. 只含该沙箱 Projects 读取权限的凭据，放在当前进程的 `GITHUB_TOKEN`，或放在登记 `github_credential_env` 指定的环境变量里。不要把令牌贴进对话、登记文件或提交。
3. 只读通过后，写入仍要单独授权目标条目、最小权限和清理方式。

## 9. 2026-10-15 真实空沙箱只读验证

用户创建并指定了私有沙箱 `https://github.com/users/HLi9725/projects/2`，名称为 `yy-flow-s4-sandbox`。在源码 HEAD `29c1667f4664079a96b1976746b3252610249965` 上，真实 GitHub GraphQL 查询确认当前账号为 `HLi9725`，目标节点为 `PVT_kwHOEv0NRs4BnI4K`，`public=false`、`closed=false`。查询不包含 mutation。

本机用户环境变量已配置凭据。执行时仅在子进程环境中读取并注入 `GITHUB_TOKEN`，没有打印或保存凭据。此前第 6–8 节描述的是当时缺少条件的历史记录，不代表本轮仍缺少读取条件。

隔离登记保存在 `C:/Users/user/AppData/Local/Temp/yyflow-github-s4-20261015/registry.json`，项目数据根为同目录的 `data/`，登记项目 ID 为 `yyflow-github-s4-sandbox`，`write_enabled=false`。通过进程环境变量 `YY_FLOW_MCP_REGISTRY` 绑定此登记，没有修改正式项目看板后端。没有启动 Cursor MCP 服务，也没有改 Cursor 配置。

真实命令：`python scripts/mcp_server.py sync-github --project-id yyflow-github-s4-sandbox --mode read-only`。

| 检查 | 退出码 | 实际结果 |
| --- | --- | --- |
| 首次正式 CLI 只读同步 | 0 | `ok=true`、`attempted_network=true`、`network_reads=1`、`network_writes=0`、`partial=false` |
| 新 Python 进程重复只读同步 | 0 | 同样读取 1 次、写入 0 次；空账本中无任务、交付和写出动作 |
| 子进程不注入凭据 | 2 | `CREDENTIALS_ABSENT`，未访问网络，读写次数均为 0 |
| 隔离登记禁写时调用 write 模式 | 2 | `WRITE_DISABLED`，未访问网络，读写次数均为 0；没有放宽登记或令牌权限 |

同步生成的 `ledger.json` 和 `diff_report.json` 位于隔离数据根的 `mcp_github_sync/`。本轮缺凭据和禁写属于真实 CLI 前置门禁验证，不是真实 GitHub 服务端拒绝写入的证据。

本轮只验证了私有空沙箱的真实连接、单页读取、新进程重复读取以及上述本地门禁；不证明非空字段映射、真实多页、建卡幂等、状态投影、归档/取消归档或写入中断恢复。远程沙箱保持空，无测试卡创建。源码未修改，因此未重复运行全量 pytest。没有代验收、推送或合并。

T0041 仍不能完成：后续须有明确的沙箱写入授权和可用的写入凭据，才能创建仅含测试数据的条目并覆盖上述剩余项。classic `read:project` 令牌不是项目级隔离授权；程序的唯一沙箱目标限制不能被描述为 GitHub 令牌只具有该沙箱的访问范围。

## 10. 2026-10-15 用户授权后的真实写入与循环反例

用户明确授权只操作 `yy-flow-s4-sandbox`。真实 `/user` 响应确认账号 `HLi9725`，令牌 scope 为 `project`；不打印令牌。所有项目写入严格限定第 9 节节点。此 scope 本身不是单 Project 限制，唯一沙箱约束由测试脚本执行。

测试脚本和脱敏观测保存在第 9 节临时目录的 `drill.py`、`observations.jsonl`。正式写入登记为 `write-registry.json`；先前只读登记没有被覆盖。原失败账本在 `data/mcp_github_sync/` 保留；独立归档循环使用 `visibility-data/mcp_github_sync/`，不清洗原失败账本。

### 已实际验证

| 项目 | 正式入口/边界 | 结果 |
| --- | --- | --- |
| 首次建卡 | 临时本地卡由 `quick_task.py create` 创建，随后正式 `sync-github --mode write` | exit 0，远程写 1 次，生成唯一测试草稿 `PVTI_lAHOEv0NRs4BnI4KzhA5-Uk` |
| 默认状态不匹配 | GitHub 默认 `Todo`，YY-Flow 为「待开始」 | 第二轮 exit 1、`FAILED`，没有错误地放行缺失状态选项 |
| 补齐沙箱字段后续跑 | 仅沙箱增加 9 个中文状态，保留原选项及其 ID；字段设置是测试准备，不是连接器能力证据 | 正式 CLI 重试 exit 0，写 1 次，查回「待开始」 |
| 同一数据重复同步 | 新 CLI 进程，保留账本 | exit 0，远程写 0 次，只有一张连接器测试卡 |
| 真实多页 | 沙箱额外创建 50 张 `YYFLOW-S4-PAGE-SEED-*` 草稿作为夹具；这些创建不是连接器建卡测试 | API 确认 51 条后，完整正式只读同步读取 2 页，exit 0，写入 0 次 |
| 页预算与进程边界续跑 | 对真实 API 调用 `run_sync(page_budget=1)`，之后退出该进程，由新进程正式 CLI 续跑 | 首轮 `PARTIAL`、非空游标、1 次读取；续跑 exit 0、再读 1 页；下一次全量扫描读取 2 页 |
| 首次归档与恢复 | 本地使用正式 MCP `handle_tool` 的 `soft_delete` / `restore`，随后正式同步；这是服务入口测试，不冒充一次 Cursor 客户端调用 | 两轮均 exit 0、远程写 1 次；直接 API 查回 `isArchived=true` / `false` |

GitHub 的项目列表/计数有短暂可见性延迟，创建返回后的即时 `totalCount` 曾滞后。分页结论只依据后来明确查回 51 条、正式命令 `network_reads=2` 以及保存的真实游标，不依据即时计数。

### 未通过的真实反例（阻断验收）

1. **DEF-T0041-LIVE-01，P1：成功投影后不推进基线，正常后续更新误报冲突。** 第一次将 `Todo` 写成「待开始」后，账本的 `baseline.gh.status` 仍是 `Todo`。随后通过正式 CLI 将同一临时任务从「待开始」推进到「进行中」，再次同步返回 exit 1、`CONFLICT(fields=[status])`、网络写 0 次。远程查回仍为「待开始」。原基线没有随着本连接器已成功的写入推进，因此把自己的先前写入也算成对端修改。定位：`_diff_tasks` 基线建立逻辑及 `_execute_action` 成功分支。
2. **DEF-T0041-LIVE-02，P1：动作键只按任务/操作去重，循环意图被旧 applied 吞掉并误报成功。** 在另一份干净隔离账本中，首次归档和恢复均实际成功；再次通过正式 MCP 服务软删除同一任务后，同步却返回 exit 0、`ok=true`、`network_writes=0`。直接 API 查回 `isArchived=false`，预期应为 true。`archive:T0001` 已是 applied，`_ensure_action` 没有为新的业务意图建动作，执行层直接跳过。状态动作也使用固定 `project:T0001` 键，修复时必须覆盖连续不同状态及多轮归档/恢复，而非只修首次归档。

本次没有改连接器源码或重写账本来让反例通过。既有模拟回归 `python -m pytest tests/test_mcp_github_sync.py -q --tb=line` 在正常权限下为 **20 passed、1 skipped**，exit 0，仍未覆盖上述真实循环缺陷。首次在受限进程中运行因临时目录 `os.replace` 权限错误出现 15 failed；经授权在正常权限环境重跑后通过，不能把权限失败归为本轮业务缺陷。未重复全量 pytest。

页预算后新进程续跑不是操作系统强杀测试，也不证明远程 mutation 已应用、账本落盘前进程崩溃的恢复正确。本轮不能把这些尚未执行的场景或第四阶段整体改为已通过。

50 张分页夹具只做可恢复归档，不永久删除；连接器测试卡和两份本地失败证据保留以便修复复验。没有修改正式仓库 Issue、PR、主分支或其他 GitHub Project，没有推送或合并。

## 11. 原任务修复与真实中断恢复复验

用户明确授权直接修复并重新测试验收。沿用 T0041，不修改其他 Project，不重置原失败账本。

### 修复边界

- 成功状态投影只推进对应状态字段的双端基线，不把无关标题、正文或契约变更一并确认。旧版 applied 记录只有在远程状态与原已记录意图吻合时才能补齐对应旧基线。
- 状态、归档和恢复使用独立动作代际；相同意图重试复用，新的循环生成新动作，历史动作保留。旧 pending/failed 不能绕过新冲突，也不能执行已被新意图替代的操作。
- 网络变更前先保存动作意图，每项结果逐项保存；同一项目使用跨进程排他锁。损坏账本、损坏本地数据或锁占用明确失败，不按空数据重新建卡。
- 建卡发送前保存 attempted 标记。响应丢失后按远程任务/交付标记认领；远程暂未可见时报告 `REMOTE_RECONCILIATION_REQUIRED`，不冒险重复建卡。
- 页预算暂停保留前页内容，续跑后对完整数据集做差异比较。旧游标没有前页内容时安全重扫，失败时仍保留旧检查点。普通权限拒绝不能仅因带有 rate-limit reset 响应头就进入限流长等待。

### 已执行的真实复验

在第 10 节原 `data/mcp_github_sync/ledger.json` 上直接运行修复后代码，没有删除、清洗或改写历史账本来规避失败：

1. 原误报冲突解除，正式 CLI 连续投影「进行中 → 审查中 → 测试中」，每步直接 API 查回正确状态。
2. 正式 MCP 服务更新临时本地可见性，再由正式同步执行三轮「归档 → 恢复」。每次直接 API 查回 `isArchived` 与预期一致。最后重复同步 exit 0、`network_writes=0`。
3. 新建独立的中断样本 T0002「SANDBOX-中断恢复独立样本」。首次样本名称相似度校验拒绝，未发远程请求；改用清晰不同的测试名称后正常建卡。
4. 在真实 GitHub `addProjectV2DraftIssue` 成功返回后，测试子进程立即执行 `os._exit(77)`，故意不落盘 applied 结果。77 是本次故障注入的预期退出码，不是成功退出码。
5. 新 Python 进程通过正式 CLI 恢复，认领原远程草稿 `PVTI_lAHOEv0NRs4BnI4KzhA6UFk`。直接 API 查回 `yy-flow-task-id:T0002` 只有 1 项，与账本一致。恢复轮有一次状态初始化写入，因此不将该轮描述为“零网络写入”；确认没有第二次建卡。
6. 新的显式 opt-in 真实只读 pytest 用例通过：`test_real_github_projects_sandbox_read_only`，1 passed、exit 0。默认全量不启用网络；必须显式选择唯一沙箱 URL，并通过远程身份校验才能执行。

原始记录仍追加于临时目录 `observations.jsonl`。这些是真实 GitHub API 与本地进程演练，不冒充新增 Cursor 客户端演练。常规安全与全量回归结果在最终审查后补记，不提前宣称第四阶段整体通过。

### 最终审查与回归

独立限定复审发现一个补充 P1：未知建卡结果后，本地内容变化可能刷新 create 身份。修复为未对账 create 始终复用原身份和 attempted，并保留原请求标题/正文；非 create 的新投影意图才换代。明确的 401/403/GraphQL 校验拒绝可在修正授权后重试，500/丢失响应不据此重复建卡。独立复审确认该 P1 关闭并给出限定 PASS。

- 最终定向：`python -m pytest tests/test_mcp_github_cycles.py tests/test_mcp_github_sync.py -q --tb=short`，**42 passed、1 skipped**，exit 0。
- 最终全量：`python -m pytest -q --tb=line --durations=10`，**882 passed、1 skipped**，exit 0，**447.65 秒**。本轮实际由主协调者执行，独立 QA 核验最终输出和冻结哈希后给出限定通过，不能写成 QA 亲自执行。
- 默认跳过项为需要显式唯一沙箱 URL 的真实网络测试；显式只读单项已实际 **1 passed**。跳过不再意味着该沙箱从未验证。
- 三个运行源码/测试文件在最终全量开始和结束后哈希一致。此前 881 passed 的全量是中途候选，因后来补修 P1 不作为最终准出凭据。测试权限审批等待没有启动记录的调用也不记为执行。
- 秘密扫描和差异检查通过，未写入令牌或完整凭据。最终通过正式服务和连接器可恢复归档两张功能样本，直接查回均 archived，随后同步 exit 0、远程写 0 次；连同 50 张分页夹具共 52 张测试草稿保留归档，没有永久删除。

用户已明确本批按验证过的能力交付，真实客户端 SSE 留作后续兼容验证。最终阶段结项仍以 `check_stage_gate.py --action close --stage Phase-4` 的实际结果及逐任务验收为准；不通过修改旧失败记录或豁免门禁来结项。
