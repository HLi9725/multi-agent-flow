---
title: 托管 QA 交接契约与恢复边界
updated: 2026-09-17
status: implemented-pending-live-validation
---

# 托管 QA 交接契约与恢复边界

## 两层契约

托管 QA 只输出六个语义字段：`decision`、`acceptance_coverage`、
`negative_scenarios`、`uncovered_risks`、`defects`、`summary`。
Runner 从本次调用上下文绑定任务、baseline/candidate SHA、session/request ID、
验收契约哈希，并从真实受控进程结果绑定测试命令、退出码和摘要。
内部 Evidence 的完整 Schema、签名与准出校验不变。

标准字段和有真实宿主样本的兼容字段走同一校验路径。已知顶层兼容字段为
`verdict/status`、`acceptance_criteria/acceptance_matrix/criteria`、`commands`；
负向场景兼容 `scenario/verdict/observed_behavior`。这不是任意字段猜测。
提供了错误身份、矛盾别名、冲突命令结果或输出哈希时必须拒绝，不能用 Runner
记录覆盖冲突来制造 PASS。缺少必要验收覆盖、负向证据或存在风险也不能 PASS。

## 协议重试

协议失败与业务测试失败分开处理，即使测试失败，也不能从无效 QA 报告生成
业务流转 Evidence。协议重试携带上一份脱敏报告及错误，要求只修格式；
不允许借修格式改变判断或证据。数组顺序变化不视为语义变化。
重试有界，并保留总调用预算；重复 JSON 键和多个不同终态报告拒绝接纳。

### 字段错误与截断必须分开诊断

真实宿主曾输出完整的六字段 JSON，但验收状态为 `COVERED`，负向场景只含
`scenario/observed_behavior` 而没有状态。不能将其归因为 JSON 截断，更不能
把 COVERED 自动转换为 PASS。现在记录字段路径、错误类别、响应字符数和摘要
哈希；不把完整原文或敏感数据写进状态诊断。语法错误的位置只是诊断，不能
单凭它断言模型触及 token 上限。

可解析报告先按对象逐字段脱敏，再序列化给修复调用；不得把整个 JSON 行当普通
日志脱敏，否则一个包含 access_token 的测试名就可能抹掉整个报告。

修复调用必须保留已有明确 PASS/FAIL、证据、缺陷和风险，允许模型依据已有
证据显式补齐不合法或缺失的状态。无效协议事件不再打印为业务 QA FAIL。
输出 Schema 和提示词要求 evidence 不超过 600 字符、summary 不超过 400 字符，
减少重复叙述；这些生成约束不用于丢弃完整的历史合法报告。262144 字符以上
输出显式拒绝处理；无效报告超过 64000 字符则明确暂停，不截取尾部拼凑重试。

CLI 所有终态 JSON 和进度 JSONL 使用无损 ASCII 转义，兼容 GBK/ASCII/UTF-8
终端和管道。JSON 消费方解码后仍获得原中文和 Emoji，无需改变全局控制台设置。
这解决“Checkpoint 已保存但打印结果再次崩溃”，不掩盖真实任务失败退出码。

## 运行与权限

QA 的无工具隔离保持不变，独立角色配置不受影响。字段结构问题不需要
全局 Shell、工作区外读取或跳过权限参数。Runner 负责受控测试与落库。
权限拒绝、真实测试失败、证据不足、网络超时仍可能合法暂停，不承诺永不中断。

恢复时保留任务、候选、历史 Evidence、调用和耗时计数；先核对活动进程、
工作区、当前版本与预算，通过公开 resume 入口执行一次。不得手改 Checkpoint。
只有 `PENDING_USER_ACCEPTANCE` 表示流水线准出，仍需用户验收。

## 回归范围

覆盖真实宿主两种结构的脱敏结构样本、六字段输出、完整字段输出、冲突别名、
身份错误、命令/哈希伪造、布尔退出码、重复键、多报告歧义、格式修复与语义
变更检测，以及规范化报告进入 EvidenceGate 后的完整流水线测试。
真实日志回放只验证协议兼容，不重新认证历史业务结论。
