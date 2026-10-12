---
title: 第三阶段 Profile 使用、配置与兼容迁移
module: multi-agent-flow
stage: Phase-3
type: guide
status: in-progress
author: 李开发
updated_at: 2026-10-12
tags: [Profile, Lite, Standard, 迁移]
---

# 第三阶段 Profile 使用、配置与兼容迁移

## 1. 使用

工作流 Profile 回答“这次任务要经过哪些独立检查”。它和宿主托管角色配置 `flow-runner-builder`、`flow-runner-reviewer`、`flow-runner-qa` 不是同一件事。

| Profile | 何时使用 | 完成条件 |
| --- | --- | --- |
| standard | 未配置，或常规代码、或内容被判定为高风险 | 开发、独立审查、QA，然后用户验收 |
| lite | 显式选择或 `auto`，且 diff 与文件内容没有高风险信号 | 开发 + 独立 Verifier 的 PASS 证据，然后用户验收 |
| docs-short | 低风险文档，且秘密扫描、frontmatter、路径深度都通过 | 必要检查证据后短链到已完成，然后用户验收 |

选择命令：

```powershell
python scripts/select_workflow_profile.py --data-root <数据目录> --task-id T0001 --profile auto --task-type A --root <仓库> --file app.py
```

Production Runner 使用 `python scripts/run_task.py start --task-id <ID> --profile lite`。省略 `--profile` 时行为与原来的 Standard 全链等价。

原生调度读取同一套 `workflow_policy`。安全规则来自 `rules/safety-core.md`，选中后必须完整读取。

## 2. 配置

策略在任务创建后固化，字段包括 `profile_id`、`profile_version`、`required_roles`、`gates`、`budget`、`requirement_hash`、`acceptance_criteria_hash` 和 `rule_source_version`。

文件位置是数据目录下的 `workflow_policies/<任务编号>.json`。Runner 同时把策略放进 checkpoint 的 `execution_options.workflow_policy`。

预算字段 `budget_consumed` 只增不减。恢复时沿用历史消耗。暂态宿主错误按有界退避重试。权限或认证错误进入等待授权。协议错误不写成业务 PASS 或 FAIL。

## 3. 兼容迁移

没有策略文件的旧入口继续按 Standard 处理，不补写旧 Evidence，也不改旧 checkpoint。

只有快照里已经能证明走过 Builder、Reviewer、QA，且没有 Verifier 语义时，才可以另记一条 Standard 补记。不能证明时只输出差异。两种情况都不覆盖旧快照、旧 Evidence 或历史预算。

Profile 升级要补齐新增检查。降到更轻的 Profile 必须带明确授权，而且不能用来关掉已经存在的缺陷。新的候选提交必须重新取得适用证据，旧证据不能改绑到新 SHA。

导出文件带 `rule_source_version`。`rules/contract-snapshot.json` 与当前状态枚举、角色权限、CLI 默认任务类型不一致时，安装和同步拒绝静默覆盖。

真实模型宿主如果本次没有调用，必须标成未验证。模拟测试不能代替宿主演练。

## 4. 本轮不做

3E Compliance、MCP、远程服务和外部连接器不在本轮。用户验收仍只由真实用户完成。
