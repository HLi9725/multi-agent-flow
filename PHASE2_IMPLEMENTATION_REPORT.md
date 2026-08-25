# 第二阶段实施报告

## 1. 2A 阶段报告 (已冻结)
- **授权范围**: 仅实施第二阶段 2A（Host 契约、纯只读能力探测、FakeHostAdapter 和契约测试）。
- **执行分支**: phase-2-real-agents
- **基线提交**: 37d0c4419e2b0a7b7672112cab7ad64f9a055f93
- **结论**: 2A 的契约、Fake 实现和防篡改等能力已经过修复并符合预期。2A 范围已被正式冻结。

## 2. 2B 阶段返工实施报告（已验收并冻结）
- **结论**: 2B 已于 2026-08-25 完成 AutoLaw 独立复审、Codex QA 准出和用户授权终态验收。所有独立对抗复现审查指出的缺陷（DEF-T0020-1 至 5）已修正，未越权实施 2C-2F。
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
- `T0020` 已依法完成【测试中 -> 已完成 -> 已验收】，2B 范围正式冻结。
- Codex 最终验收命令：`python -m pytest tests/test_evidence.py tests/test_host_adapter.py -q -rs`，退出码 0，`25 passed in 0.96s`。
- Codex 全量命令：`python -m pytest tests -q -rs`，退出码 0，`251 passed in 41.08s`，0 failed，0 skipped。
- `git diff --check` 退出码 0；候选代码提交为 `f6b79e903fb39d8724bfe303e817d7a0748cd7b6`。
- 全量测试产生 `T0021 / Delete Me` 污染卡；未使用快照覆盖或直接编辑看板，已通过合法 PM/USER 代行流转软取消归档。
- 2C 尚未实施。实施范围、测试矩阵和双端交接合同见 `docs/D04-研发过程/D01-任务/Phase2-2C-Worktree隔离实施任务书.md`。

## 3. 2C 阶段实施报告（Worktree 隔离）
- **实施范围**: 第二阶段 2C（Worktree 请求、状态校验与 Git 操作封装）
- **基线提交**: 4c61976e492d163b369a75bbb80afaf2bbb69e33

### 核心实现方案
1. **Worktree Schema 抽象**: 引入 `WorktreeRequest`、`WorktreeDescriptor` 和 `WorktreeStatus` 三层结构，使用严格的 dataclass 和 `freeze_value` 提供深度不可变性。
2. **WorktreeManager**:
   - 依赖注入: `controlled_root` 和 `target_repo_path`，强制绝对路径约束。
   - 仓库身份绑定: 显式记录并双向核验 `target_repo_root`、`git_common_dir` 和由其唯一计算的 `repository_identity`，杜绝多仓库跨登记冒充。
   - 隔离 ID 无歧义: 采用 Canonical JSON 字典规范化与 SHA-256 摘要哈希计算隔离标识，根除字段连字符拼接时的边界碰撞。
   - 并发创建与原子发布保护: 先创建唯一 Git branch 作为跨进程原子锁，写入独立临时文件完成 write-all 和 fsync 后，通过 create-if-absent 原子发布到最终 Registry 路径，确保写入期间并发 inspect/list 绝观察不到半成品文件。
   - 真实校验: 严格验证 `git rev-parse --absolute-git-dir` 和 `--git-common-dir`。
   - 目录与分支名安全: 正则验证和 `git check-ref-format` 防御注入，隔离逃逸目录限制（阻止 `../` 和 Symlink/Junction 等攻击）。
   - 只读与清理计划: `verify` 和 `inspect` 只读执行 Git 解析，`get_cleanup_plan` 返回不带有破坏性清理动作的结构化计划。
   - 完全抽离注册表: `.registry` 存储在 Agent worktree 外部，确保 `git status` 原生干净，实施严格交叉核验。
3. **测试覆盖**:
   - `test_worktree_manager.py` 通过真实 `pytest tmp_path` 生成 Git 临时空仓库并挂载文件流。
   - 测试涵盖全场景: 绝对路径逃逸防御、并发覆盖防护、非法命令注入拦截、清理不落盘、linked worktree 场景验证、Registry 碰撞保全、短写防护、严格 Schema 校验、原子发布并发隔离、双仓库身份绑定校验和无歧义隔离标识边界测试。

### 测试记录（最新真实数据）
- **定向工作树测试**: `python -m pytest tests/test_worktree_manager.py -q -rs` -> `25 passed in 16.80s` (0 failed, 0 skipped)
- **定向环境测试**: `python -m pytest tests/test_host_adapter.py tests/test_evidence.py -q -rs` -> `25 passed in 1.46s` (0 failed, 0 skipped)
- **全量测试**: `python -m pytest tests -q -rs` -> `276 passed in 59.67s` (0 failed, 0 skipped)
- **代码规范**: `git diff --check` -> 退出码 0，零尾随空白错误

### 2C 历次返工修复记录

#### DEF-T0023-1 ~ 9 修复
1. **[DEF-T0023-1, 5, 6] 注册表独立与严格核验**: 将元数据抽离到 `.registry` 独立目录，恢复原生 clean 状态，并实施字典交叉核验（重建 ID、对比路径和分支），对损坏或篡改强 Fail-Closed。
2. **[DEF-T0023-2] 完善 verify() 的 Fail-Closed**: 增加了对 OSError 和所有异常的捕获。
3. **[DEF-T0023-3] 代码格式**: 彻底清除了所有尾随空白。
4. **[DEF-T0023-4] 失败保留分支**: 移除 `branch -D`，在 worktree 创建失败时报错提醒 `recovery_required`。
5. **[DEF-T0023-7] 40位 SHA 校验**: 在创建前统一验证，在元数据与请求中拦截短 SHA 与无效引用。
6. **[DEF-T0023-8] 清理计划结构化**: `get_cleanup_plan` 不再生成 raw git string，改用安全的纯数据格式。
7. **[DEF-T0023-9] 只读 list_worktrees**: 实现了无副作用的 `list_worktrees`，并对损坏记录保持 Fail-Closed。

#### DEF-T0023-10 ~ 11 修复（含历史缺陷标注）
1. **[DEF-T0023-10] 完善 Registry 根类型校验**: 在 `inspect()` 中增加 `isinstance(data, dict)` 断言，封堵由于非对象 JSON 触发 `AttributeError` 的漏洞。
2. **[DEF-T0023-11] [历史阶段性尝试/已在 DEF-T0023-14 彻底废弃]**: 该阶段曾尝试在失败路径执行 `os.unlink` 清理元数据；后因引入路径竞态 (TOCTOU)，已在 DEF-T0023-14 及后续版本中彻底废除一切自动 `unlink`，全面转为保留现场的 Fail-Closed 恢复架构。

#### DEF-T0023-12 ~ 13 修复（含历史缺陷标注）
1. **[DEF-T0023-12] [历史阶段性尝试/已在 DEF-T0023-14 彻底废弃]**: 该阶段曾尝试通过 `lstat/fstat` 校验文件身份后执行回滚；后经独立复现证实路径仍存在读取后删除前的 TOCTOU 窗口，已在 DEF-T0023-14 中彻底弃用回滚删除逻辑。
2. **[DEF-T0023-13] 高强度的 Schema 与提交真实性防御**: 在 `inspect()` 加入字典边界与类型校验。拒绝包含不合理 Float 的假造时间，对 `baseline_commit` 进行底层 `git rev-parse` 严格双重校验。

#### DEF-T0023-14 ~ 17 修复
1. **[DEF-T0023-14] 彻底消除回滚 TOCTOU 竞态**: 摒弃一切基于路径解析的删除逻辑，确立零 unlink 架构：1) 校验通过并生成唯一 Git branch（跨进程原子锁）；2) 以 `O_EXCL` 创建 Registry；3) 若写入或 worktree add 失败，原样保留已创建的 branch 和 Registry 现场以供审计与人工干预，在 `WorktreeError` 中附带结构化字段 `recovery_required=True, branch_retained=True, registry_retained=True`。
2. **[DEF-T0023-15] Registry os.write 短写保护与持久化**: 使用 `memoryview` 与循环 `write-all` 模式对 `os.write` 进行字节确认，写完后强制 `os.fsync`，最后才启动 `git worktree add`。
3. **[DEF-T0023-16] 严苛的小写 Canonical SHA-1 同态断言**: 入参强制使用 `^[0-9a-f]{40}$` 正则严格校验，禁止隐式 `lower()` 宽容转换，`git rev-parse` 解析结果必须与输入 SHA 完全相同。
4. **[DEF-T0023-17] 卫生清理与测试隔离**: 清理临时脚本与未跟踪残留，测试数据全面使用 `copy.deepcopy` 防污染。

#### DEF-T0023-18 ~ 22 修复
1. **[DEF-T0023-18] Registry 顶层与 Request 精确键集合及控制字符/空白防御**:
   - `inspect()` 中强制校验顶层 Registry 键集合与 `request` 键集合必须完全匹配，未知或缺失键均抛出 `WorktreeSecurityError` 并使 `verify()` Fail-Closed。
   - 所有字符串字段强制检查：非字符串类型拒绝、纯空白 (`not val.strip()`) 拒绝、ASCII 控制字符 (`[\x00-\x1f\x7f]`) 拒绝。
2. **[DEF-T0023-19] 修正 Registry 创建碰撞与准确的现场保留证据**:
   - 在 `create_worktree()` 中精确区分 `existing_foreign_registry`、`no_registry`、`partial_owned_registry`，真实上报现场保留证据。
3. **[DEF-T0023-20] 完整恢复被删除/弱化的测试断言**:
   - 恢复 `desc.request == req`、`branch_name` 命名规范、`cleanup_plan` 不含可执行命令、并发碰撞必须为 `WorktreeSecurityError(already exists)` 的严格断言。
4. **[DEF-T0023-21] 规范化提交证据链**:
   - 基于基线正常追加新提交，绝不使用 amend/rebase 改写历史。
5. **[DEF-T0023-22] 实施报告格式与历史结论全面修正**:
   - 彻底清除报告中的所有控制字符和转义反斜杠，将历史回滚尝试明确标注为历史废弃机制。

#### DEF-T0023-23 ~ 26 修复（当前最新交付）
1. **[DEF-T0023-23] Registry 原子发布与并发隔离**:
   - 禁止在最终 Registry 路径中逐步写入文件。先在独立临时文件完成 UTF-8 完整写入、write-all 循环保障与 `os.fsync`。
   - 采用 `create-if-absent` 原语（Windows 下基于 `os.rename` 目标存在失败，POSIX 下基于 `os.link` + `unlink`）将临时文件原子发布到最终 Registry 路径，不覆盖已有 Registry。
   - 写入与发布期间并发调用的 `inspect()` 和 `list_worktrees()` 绝不会观察到残缺、半成品或损坏 JSON。
2. **[DEF-T0023-24] 仓库身份 1:1 强绑定与跨仓库隔离**:
   - `WorktreeDescriptor` 和 Registry 显式记录规范化的 `target_repo_root`、`git_common_dir` 和由两者计算出的唯一摘要 `repository_identity`。
   - 即使两个仓库拥有完全相同的提交历史和 commit SHA，因其 `git_common_dir` 物理路径不同，其 `repository_identity` 也完全不同。
   - `inspect()`、`verify()` 与 `list_worktrees()` 必须进行 1:1 仓库身份核对，不匹配时强制 Fail-Closed。
3. **[DEF-T0023-25] 隔离 ID 无歧义与边界碰撞防御**:
   - 废除普通的连字符拼接公式，采用 Canonical JSON（严格排序键与无空白紧凑编码）+ SHA-256 摘要哈希计算 `worktree_id`。
   - 彻底消除了因不同字段组合（如 `project_id="a-b", task_id="c"` 与 `project_id="a", task_id="b-c"`）在原连字符拼接下产生的歧义碰撞漏洞。
4. **[DEF-T0023-26] 流程真实性与看板闭环核验**:
   - 状态流转后显式重新读取权威 `board.json` 校验 status 与 assignee，禁止仅依赖 CLI 成功输出。
