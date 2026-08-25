# 第二阶段实施报告

## 1. 2A 阶段报告 (已冻结)
- **授权范围**: 仅实施第二阶段 2A（Host 契约、纯只读能力探测、FakeHostAdapter 和契约测试）。
- **执行分支**: phase-2-real-agents
- **基线提交**: 37d0c4419e2b0a7b7672112cab7ad64f9a055f93
- **结论**: 2A 的契约、Fake 实现和防篡改等能力已经过修复并符合预期。2A 范围已被正式冻结，目前开始推进后续流程。

## 2. 2B 阶段返工实施报告
- **结论**: 当前正处于 2B 再次待用户验收状态，不得声称已验收。
- **实施范围**: 仅执行第二阶段 2B（证据存储与状态门禁），包括深层冻结的 Evidence Schema、零竞态原子 Append-only Evidence Store、上下文交叉验证的 EvidenceGate 以及包含对抗/反逃逸案例在内的全量测试。
- **禁止事项**: 严格遵守未修改既有业务流转，禁止执行 2C-2F，严禁用全局真实目录等约束。
- **基线提交**: 0422680e0c3b89ff763babb0560b18a12c60bc96 (2B 原始交接节点)。

### 2B 独立复审缺陷及修复项
根据复审反馈，2B 进行了一次深度重构与漏洞封堵：
1. **Schema 深层冻结**: 原有的 dataclass(frozen=True) 无法防范可变集合。现将 artifacts 转换为 tuple，将 metadata.extra 通过 `MappingProxyType` 与自定义 `freeze_value` 方法强制实现深层递归冻结，并增加了 `project_id`, `created_at` 等必填字段。
2. **原子文件落盘剔除 TOCTOU**: 移除了原来基于 `.tmp` 固化后缀及 `os.replace` 所引入的条件覆盖竞态风险。现通过临时名称 `uuid.uuid4()` 生成无碰撞落盘，并且依靠 `os.rename`/`os.link` 等底层操作系统级语义确保同一 ID 落盘仅有一次成功，从而杜绝任何覆盖行为。
3. **真实绝对路径对抗逃逸拦截**: Evidence Store 对 `evidence_id` 使用了严格白名单正则 `^[\w\-]{1,64}$`。对于路径，使用 `os.path.realpath` 并基于 `os.path.commonpath` 与受控根目录进行严格比对，完美阻断了直接绝对路径注入、`../` 以及各类 Windows Junction/符号链接逃逸。
4. **自定义无损 JSON 序列化器**: 不再直接使用 `dataclasses.asdict` 导致潜在的集合污染。编写了显式的 `_to_dict` 树形递归序列化方案，支持内置枚举和不可变字典转换，并强制拒绝 `NaN`/`Infinity` 入库，保证相同的 Evidence 永远产出唯一的 Canonical JSON。
5. **门禁下放与全方位交叉比对**: EvidenceGate 的校验不再是仅查空值，而是将请求方给定的 `expected_context` (包含上下文的任务编号、流转角色、源宿状态、基线哈希等) 进行深度 1:1 交叉核查。同时明确增加了对包含 `fake/test/mock/simulate` 字样的防御拦截以及 `TASK_COMPLETE` 必须包含 Artifact 等多重约束判定。
6. **全方位机密字段拒绝与覆写屏蔽**: 原有脱敏未阻断受限字段，现对 `invocation_token` 直接抛出安全错误 (Fail-Closed)。新增对 Authorization、Bearer、Cookie、秘钥头及常见密码链接的字段级、内容级全域脱敏。

**Diff Stat (最新 2B 实施部分):**
```text
 scripts/_lib/core/evidence_gate.py   |  57 ++++++++++++++++++++
 scripts/_lib/core/evidence_schema.py |  54 +++++++++++++++++++
 scripts/_lib/core/evidence_store.py  | 138 ++++++++++++++++++++++++++++++++++++++++++++++++
 tests/test_evidence.py               | 122 ++++++++++++++++++++++++++++++++++++++++++
 4 files changed, 371 insertions(+)
```
*(含针对前一次 2B 方案的部分重构行)*

### 测试记录
- **单独测试 `test_evidence.py`**:
  - 执行命令: `python -m pytest tests/test_evidence.py -q -rs`
  - 退出码: 0
  - 核心新增断言: 并发同 ID 不覆盖校验、目录/符号逃逸测例、包含深层不可变抛错的 Schema 断言等完全通过。
- **全量测试**:
  - 执行命令: `python -m pytest tests -q -rs`
  - 退出码: 0
  - 通过数量: 242 passed

全程无回归且运行快速，看板状态验证（T0019 创建与归属修改）前后数据无污染。
