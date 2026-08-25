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
- `T0020` 已依法完成【测试中 → 已完成 → 已验收】，2B 范围正式冻结。
- Codex 最终验收命令：`python -m pytest tests/test_evidence.py tests/test_host_adapter.py -q -rs`，退出码 0，`25 passed in 0.96s`。
- Codex 全量命令：`python -m pytest tests -q -rs`，退出码 0，`251 passed in 41.08s`，0 failed，0 skipped。
- `git diff --check` 退出码 0；候选代码提交为 `f6b79e903fb39d8724bfe303e817d7a0748cd7b6`。
- 全量测试产生 `T0021 / Delete Me` 污染卡；未使用快照覆盖或直接编辑看板，已通过合法 PM/USER 代行流转软取消归档。
- 2C 尚未实施。实施范围、测试矩阵和双端交接合同见 `docs/D04-研发过程/D01-任务/Phase2-2C-Worktree隔离实施任务书.md`。

## 3. 2C 阶段实施报告（Worktree 隔离）
- **实施范围**: 第二阶段 2C（Worktree 请求、状态校验与 Git 操作封装）
- **基线提交**: 4c61976e492d163b369a75bbb80afaf2bbb69e33

### 核心实现方案
1. **Worktree Schema 抽象**: 引入 \WorktreeRequest\、\WorktreeDescriptor\ 和 \WorktreeStatus\ 三层结构，使用严格的 dataclass 和 \reeze_value\ 提供深度不可变性。
2. **WorktreeManager**:
   - 依赖注入: \controlled_root\ 和 \	arget_repo_path\，强制绝对路径约束。
   - 并发创建保护: \worktree_id\ 唯一且具有原子级别锁定（使用 \os.O_CREAT | os.O_EXCL\ 生成锁文件）。
   - 真实校验: 严格验证 \git rev-parse --absolute-git-dir\ 和 \--git-common-dir\。
   - 目录与分支名安全: 正则验证和 \git check-ref-format\ 防御注入，隔离逃逸目录限制（阻止 \../\ 和 Symlink/Junction 等攻击）。
   - 只读与清理计划: \erify\ 和 \inspect\ 只读执行 Git 解析，\get_cleanup_plan\ 返回不带有破坏性清理动作的结构化计划。
   - 完全抽离注册表: .registry 存储在 Agent worktree 外部，确保 \git status\ 原生干净，实施严格交叉核验。
3. **测试覆盖**:
   - \	est_worktree_manager.py\ 通过真实 \pytest tmp_path\ 生成 Git 临时空仓库并挂载文件流。
   - 测试涵盖全场景: 绝对路径逃逸防御、并发覆盖防护、非法命令注入拦截、清理不落盘、linked worktree 场景验证。

### 测试记录
- **定向工作树测试**: \python -m pytest tests/test_worktree_manager.py -q -rs\ -> 13 passed (0 failed, 0 skipped)
- **定向环境测试**: \python -m pytest tests/test_host_adapter.py tests/test_evidence.py -q -rs\ -> 25 passed
- **全量测试**: \python -m pytest tests -q -rs\ -> 264 passed (0 failed, 0 skipped)
- **代码规范**: \git diff --check\ -> 通过无报错

### 2C 返工修复记录（DEF-T0023-1 ~ 9）
1. **[DEF-T0023-1, 5, 6] 注册表独立与严格核验**: 将元数据抽离到 .registry 独立目录，不仅恢复了原生 clean 状态，并且实施了最严格的字典交叉核验（重建 ID、对比所有路径和分支）。对损坏或篡改的登记文件强 fail-closed。
2. **[DEF-T0023-2] 完善 verify() 的 Fail-Closed**: 增加了对 OSError 的捕获。
3. **[DEF-T0023-3] 代码格式**: 彻底清除了所有尾随空白。
4. **[DEF-T0023-4] 失败保留分支**: 移除 \ranch -D\，在 worktree 创建失败时报错提醒 recovery_required。
5. **[DEF-T0023-7] 40位 SHA 校验**: 在创建前统一转化为 canonical 小写，并在元数据与请求中拦截短 SHA 与无效引用。
6. **[DEF-T0023-8] 清理计划结构化**: \get_cleanup_plan\ 不再生成 raw git string，改用安全的纯数据格式。
7. **[DEF-T0023-9] 只读 list_worktrees**: 实现了无副作用的 \list_worktrees\，并对损坏记录保持 fail-closed。

### 2C 返工修复记录（DEF-T0023-10 ~ 11）
1. **[DEF-T0023-10] 完善 Registry 根类型校验**: 在 \inspect()\ 中增加 \isinstance(data, dict)\ 的断言，彻底封堵了由于 \json.load\ 解析出列表、整数、字符串或 null 时导致后续字典读取触发 \AttributeError\ 的漏洞。如今面对任意合法的非对象 JSON 亦能稳定返回安全错误（Fail-Closed）。
2. **[DEF-T0023-11] 修复 Create 失败路径 Registry 残留阻塞重试**: 在 \create_worktree()\ 中，若其后的 Git 分支创建（如已存在）或 Worktree 创建抛出异常失败，会主动将刚建立的 registry 元数据撤销/清理（\os.unlink\），从而防止失败导致的悬挂元数据永久性阻塞后续的重试尝试。

### 2C 返工修复记录（DEF-T0023-12 ~ 13）
1. **[DEF-T0023-12] 严密的 Registry 身份与所有权回滚**: 彻底修正了 \create_worktree()\ 中的文件回滚漏洞。不再使用简单且具有竞态风险的 \os.path.exists()\ 判断。利用 \os.fstat(fd)\ 锁定 O_EXCL 创建时的 inode (\st_ino\) 与 device ID (\st_dev\)，当且仅当 \os.lstat()\ 证实元数据文件身份一致、且严格比对写入二进制内容无篡改后，才准许 \os.unlink()\ 进行安全回滚，对外部恶意替换/Symlink/Junction 免疫。
2. **[DEF-T0023-13] 高强度的 Schema 与提交真实性防御**: 在 \inspect()\ 加入了极致的字典边界与类型校验。拒绝包含不合理 Float 的假造时间，强制 \isinstance(val, str)\ 检测。对 baseline_commit 进行底层 \git rev-parse\ 严格双重校验，保证内外 commit 与请求绝对一致。对于任何畸变类型强制抛出 \WorktreeSecurityError\，使门禁校验 (\erify()\) 永远返回无效而非抛出异常裸奔（Fail-Closed）。
