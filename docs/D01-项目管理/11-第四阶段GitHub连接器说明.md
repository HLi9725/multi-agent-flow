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

本文只说明本批已经落地的 STDIO 双帧和 GitHub Projects 连接器。第四阶段没有全部通过。D1–D8 没有改为已验证。真实 GitHub 调用未验证。

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

## 3. 真实 GitHub 未验证

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
