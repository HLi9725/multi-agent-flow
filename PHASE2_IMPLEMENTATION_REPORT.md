# 第二阶段实施报告

## 1. 2A 阶段报告 (已冻结)
- **授权范围**: 仅实施第二阶段 2A（Host 契约、纯只读能力探测、FakeHostAdapter 和契约测试）。
- **执行分支**: phase-2-real-agents
- **基线提交**: 37d0c4419e2b0a7b7672112cab7ad64f9a055f93
- **结论**: 2A 的契约、Fake 实现和防篡改等能力已经过修复并符合预期。2A 范围已被正式冻结。

## 2. 2B 阶段返工实施报告
- **结论**: 当前正处于 **2B 第三次待用户验收** 状态。所有独立对抗复现审查指出的缺陷（DEF-T0020-1 至 5）已彻底修正，未越权进入 2C-2F。
- **实施范围**: 仅执行第二阶段 2B（证据存储与状态门禁）。
- **基线提交**: 004b37dbcd9a2b1a9c2bb02d4c464751c516baf3

### 2B 历次返工核心修复项
1. **Schema 极致深度冻结**: 修复了可变集合注入问题。`freeze_value` 现已递归支持 `Mapping`, `list`, `tuple`。因 `set/frozenset` 在 Canonical JSON 序列化时的无序性，已底层直接引发 `TypeError` 予以禁用。`ArtifactRecord` 在 `__post_init__` 被严格转为只读 `tuple` 并做了强类型校验。
2. **零竞态的 Create-if-absent 文件落盘**: 去除 `os.replace`。依靠 `os.open(tmp_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)` 原子生成临时文件，且确保证据不可覆盖、多线程下同写有且只有一方成功并安全清理残留文件。
3. **真实对抗逃逸拦截**: 通过 `os.path.realpath` 与 `commonpath` 检查，彻底粉碎包括真实 Windows `mklink /J` Junction 等任何突破 `project_root` 的企图。
4. **显式用户确认过滤**: `USER_CONFIRMATION` 严格要求下发 `ConfirmationResult`，真实请求源必须为 `explicit_user`，并拦截任何试图由 `model`, `system`, `fake` 混入的确认数据。
5. **全覆盖无死角凭证脱敏**: `invocation_token` 一律安全抛错决不存盘。`access_token`, `ci_job_token`, `AWS_ACCESS_KEY_ID`, 数据库连接串等均全量执行 `***MASKED***` 深度替换掩蔽。

### 2B 第三次独立复审重点修复（DEF-T0020-1 ~ 5）
针对此前复审对抗复现指出的三项 P2 与两项 P3 缺陷，已完成彻底修复：
1. **[DEF-T0020-1/2/3] 重构与填补 1:1 上下文精确校验链**：
   - 为避免直接修改已冻结的 2A 契约实体 (`AgentHandle`) 导致范围越界，现显式要求调用门禁的校验上下文 `EvidenceValidationContext` 直接携带来自业务最顶层、无可篡改的期望数据：`expected_invocation_id`、`expected_workspace_mode` 和 `expected_adapter`。
   - 这完全废弃了之前 `hasattr` 探测死代码的隐患，并在核心门禁层直接与存盘自报数据作双向不可为空 (`None!=None`) 的严格比较。由于不再仅靠不包含敏感词来放行，彻底断绝了伪造上下文蒙混过关的漏洞。
2. **[DEF-T0020-4] 证据类型重放拦截**：
   - 为上下文显式引入了 `expected_evidence_type`。当前每一次对 Evidence 校验不仅核对业务流转和主机属性，更强制核对其本身的类别必须属于所期生命周期事件，杜绝跨证据类型的挂羊头卖狗肉（重放拦截）。
3. **[DEF-T0020-5] 实施报告统计修正**：
   - 已根据最新执行结果如实登记 `test_host_adapter.py` 为 11 passed。

### 测试记录 (无假测例，拒绝 Skip)
本轮保留了所有单元测试基础，包含真实基于 `ThreadPoolExecutor` 的并发不覆盖测试、真实的 `mklink /J` 结界逃逸测试及各类严密的凭证脱敏核查。
- **单独定向测试**:
  - `python -m pytest tests/test_evidence.py -q -rs` -> `14 passed in ~0.73s` (0 failed, 0 skipped)
  - `python -m pytest tests/test_host_adapter.py -q -rs` -> `11 passed` (0 failed, 0 skipped)
- **全量回归测试**:
  - 命令: `python -m pytest tests -q -rs`
  - 结果: `251 passed in ~41.03s` (0 failed, 0 skipped)

### 看板与 Git 状态
- 根据流转纪律，开发方 (李开发) 已接管之前遭拦截的 `T0020` 工单重新置于 `进行中`。测试前已充分完成看板哈希快照备份。所有针对门禁上下文扩容与精确匹配补丁已准备妥当。
- `git status --short` 处于干净清空状态，`git diff --check` 未遗留残余行尾空格。
