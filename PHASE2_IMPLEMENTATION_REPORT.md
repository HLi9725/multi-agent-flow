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
1. **Schema 极致深度冻结**: 修复了可变集合注入问题。`freeze_value` 现已递归支持 `Mapping`, `list`, `tuple`. 因 `set/frozenset` 在 Canonical JSON 序列化时的无序性，已底层直接引发 `TypeError` 予以禁用。`ArtifactRecord` 在 `__post_init__` 被严格转为只读 `tuple` 并做了强类型校验。
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
   - 规范化 Git 仓库根: 通过 `git rev-parse --show-toplevel` 与 `--git-common-dir` 解析并规范化根路径与通用目录，杜绝从子目录传入导致的身份分裂。
   - 仓库身份绑定: 显式记录并双向核验 `target_repo_root`、`git_common_dir` 和由其唯一计算的 `repository_identity`，杜绝多仓库跨登记冒充。
   - 输入严格 Fail-Closed: 在计算 ID 前对 `WorktreeRequest` 全字段执行类型、空值、空白、控制字符、路径分隔符、`..` 段及前导选项的全面校验，严禁“清洗后继续”。
   - 隔离 ID 无歧义: 采用 Canonical JSON 字典规范化与 SHA-256 摘要哈希计算隔离标识，根除字段连字符拼接时的边界碰撞。
   - 零文件系统接触预检: `inspect()` 在接触文件系统前先完成 ID 格式 fullmatch 与边界校验，非法 ID 零文件访问。
   - 并发创建与原子发布保护: 先创建唯一 Git branch 作为跨进程原子锁，写入独立临时文件完成 write-all 和 fsync 后，通过 create-if-absent 原子发布到最终 Registry 路径，确保写入期间并发 inspect/list 绝观察不到半成品文件。
   - 真实校验: 严格验证 `git rev-parse --absolute-git-dir` 和 `--git-common-dir`。
   - 目录与分支名安全: 正则验证和 `git check-ref-format` 防御注入，隔离逃逸目录限制（阻止 `../` 和 Symlink/Junction 等攻击）。
   - 只读与清理计划: `verify` 和 `inspect` 只读执行 Git 解析，`get_cleanup_plan` 返回不带有破坏性清理动作的结构化计划。
   - 完全抽离注册表: `.registry` 存储在 Agent worktree 外部，确保 `git status` 原生干净，实施严格交叉核验。
3. **测试覆盖**:
   - `test_worktree_manager.py` 通过真实 `pytest tmp_path` 生成 Git 临时空仓库并挂载文件流。
   - 测试涵盖全场景: 绝对路径逃逸防御、并发覆盖防护、非法命令注入拦截、清理不落盘、linked worktree 场景验证、Registry 碰撞保全、短写防护、严格 Schema 校验、原子发布并发隔离、双仓库身份绑定校验、无歧义隔离标识边界测试、非法 ID 零文件系统访问验证和仓库子目录初始化规范化核对。

### 测试记录（最新真实数据）
- **定向工作树测试**: `python -m pytest tests/test_worktree_manager.py -q -rs` -> `35 passed in 20.08s` (0 failed, 0 skipped, exit code 0)
- **定向环境测试**: `python -m pytest tests/test_host_adapter.py tests/test_evidence.py -q -rs` -> `25 passed in 1.90s` (0 failed, 0 skipped, exit code 0)
- **全量测试**: `python -m pytest tests -q -rs` -> `286 passed in 59.25s` (0 failed, 0 skipped, exit code 0)
- **代码规范**: `git diff --check` -> 退出码 0，零尾随空白错误
- **独立对抗复现**: 仓库外脚本在首轮验证合法长字段、2A `fake-session:<uuid>` 和 `.registry` Junction，修复前 `3 failed`、修复后 `0 failed`；第二次扩大复审真实复现 `controlled_root` Junction 修复前 `1 failed`、修复后 `0 failed`，并以确定性回归断言覆盖单个 Registry 文件链接替换。
- **看板哈希链（第二次扩大复审）**: 全量测试前 `22C5A4CBF10765F1A87194183A143E8E3E061C4EFB2785EF46B3350261FA3090`；全量测试后 `DA0543E4BB9C15EC1FB8C3765D3A93548A7F28BA58DD9E7C9B97CEBB850A5F9E`；测试污染卡 T0046 经合法 CLI 软取消后 `9B0C6B9710D350FFAE07C620115856832C2787A0A827569F4BB20258945ED2B0`，未复制或直接编辑 `board.json`。

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
2. **[DEF-T0023-11] [历史阶段性尝试/已在 DEF-T0023-14 彻底废弃]**: 该阶段曾尝试在失败路径执行 `os.unlink` 清理已发布元数据；后因引入路径竞态 (TOCTOU)，已在 DEF-T0023-14 及后续版本中废除对最终 Registry、分支和 Worktree 的自动删除。POSIX 原子发布仍仅对本进程创建的临时硬链接名称执行 `unlink`，不删除已发布 Registry。

#### DEF-T0023-12 ~ 13 修复（含历史缺陷标注）
1. **[DEF-T0023-12] [历史阶段性尝试/已在 DEF-T0023-14 彻底废弃]**: 该阶段曾尝试通过 `lstat/fstat` 校验文件身份后执行回滚；后经独立复现证实路径仍存在读取后删除前的 TOCTOU 窗口，已在 DEF-T0023-14 中彻底弃用回滚删除逻辑。
2. **[DEF-T0023-13] 高强度的 Schema 与提交真实性防御**: 在 `inspect()` 加入字典边界与类型校验。拒绝包含不合理 Float 的假造时间，对 `baseline_commit` 进行底层 `git rev-parse` 严格双重校验。

#### DEF-T0023-14 ~ 17 修复
1. **[DEF-T0023-14] 彻底消除已发布资源回滚的 TOCTOU 竞态**: 摒弃对最终 Registry、分支和 Worktree 的自动删除：1) 校验通过并生成唯一 Git branch（跨进程原子锁）；2) 以 `O_EXCL` 创建 Registry；3) 若写入或 worktree add 失败，原样保留已创建的 branch 和 Registry 现场以供审计与人工干预，在 `WorktreeError` 中附带结构化字段 `recovery_required=True, branch_retained=True, registry_retained=True`。POSIX 发布完成后只移除本进程拥有的临时硬链接名称。
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

#### DEF-T0023-23 ~ 26 修复
1. **[DEF-T0023-23] Registry 原子发布与并发隔离**:
   - 先在独立临时文件完成 UTF-8 完整写入、write-all 循环保障与 `os.fsync`。
   - 采用 `create-if-absent` 原语原子发布到最终 Registry 路径，写入期间并发 `inspect()`/`list_worktrees()` 绝不观察到半成品文件。
2. **[DEF-T0023-24] 仓库身份 1:1 强绑定与跨仓库隔离**:
   - `WorktreeDescriptor` 和 Registry 显式记录规范化的 `target_repo_root`、`git_common_dir` 和 `repository_identity`。
3. **[DEF-T0023-25] 隔离 ID 无歧义与边界碰撞防御**:
   - 采用 Canonical JSON + SHA-256 计算 `worktree_id`，杜绝字段连字符拼接时的歧义碰撞。
4. **[DEF-T0023-26] 流程真实性与看板闭环核验**:
   - 状态流转后显式重新读取权威 `board.json` 校验 status 与 assignee。

#### DEF-T0023-27 ~ 31 修复
1. **[DEF-T0023-27] 请求字段前置 Fail-Closed 严密防御**:
   - 在计算任何哈希 ID 前，对 `WorktreeRequest` 全部字段执行全面安全校验（拒绝非字符串、空值、纯空白、控制字符、路径分隔符、绝对路径、`..` 相对段及前导 Git 选项 `-`），彻底废除“清洗危险字符后继续执行”的宽容隐患。
2. **[DEF-T0023-28] inspect 零文件系统访问防御**:
   - `inspect()` 在调用 `os.path.exists` 或 `open` 之前，首先执行 `worktree_id` 严格正则 fullmatch 与 `commonpath` 越界检查。针对任何非法、路径遍历或绝对路径输入，直接抛出 `WorktreeSecurityError`，实现文件系统零接触。
3. **[DEF-T0023-29] 规范化 Git 仓库根解析**:
   - 通过 `git rev-parse --show-toplevel` 与 `--git-common-dir` 获取真实规范化的 `target_repo_root` 与 `git_common_dir`。
   - 无论从主仓库根目录或其任意深层子目录初始化，均能解析出完全同态的 `target_repo_root` 与 `repository_identity`；不同 clone 仓库即使拥有相同 commit 历史，亦严格拥有不同仓库身份。
4. **[DEF-T0023-30] 测试矩阵全面恢复与增强**:
   - 恢复路径注入拒绝断言、添加 Windows 与 POSIX 绝对路径载荷测试、添加非法 ID 零文件访问 mock 断言、子目录规范化及多 clone 身份核对测试。
5. **[DEF-T0023-31] 流程真实性与测试退出码核验**:
   - 全量测试通过并确认退出码为 0，看板状态与处理人经磁盘直接验真。

#### DEF-T0023-32 ~ 35 修复（Codex 独立复审返工）
1. **[DEF-T0023-32] 固定长度、完整摘要的 Worktree ID**:
   - 输入字段仍以 Canonical JSON 绑定，但 ID 改为有限长度可读前缀加完整 SHA-256 摘要；所有通过字段校验的请求都能生成符合 `_safe_path` 上限的 ID，不再出现“字段合法、组合 ID 非法”。
2. **[DEF-T0023-33] Host 会话标识保持不透明**:
   - `host_session_id` 使用独立校验器，允许 2A Host 契约定义的 `fake-session:<uuid>` 命名空间，同时继续拒绝空白、控制字符、路径分隔符、相对路径段、前导选项和命令注入载荷。
3. **[DEF-T0023-34] Registry 目录本体强绑定**:
   - 构造、创建、检查和列举入口均重新验证 `.registry` 的绝对路径与 `realpath` 完全一致；即使 Junction 目标仍位于 `controlled_root` 内，也会 Fail-Closed 拒绝。
4. **[DEF-T0023-35] POSIX 临时硬链接清理证据准确化**:
   - 原子发布成功后若本进程临时硬链接名称清理失败，不再吞掉异常；改为报告 `registry_retained=True`、`tmp_retained=True` 并停止后续 Worktree 创建，保留真实现场供审计。

#### DEF-T0023-36 ~ 37 修复（Codex 第二次扩大复审返工）
1. **[DEF-T0023-36] 受控根自身与父链 Junction/Symlink 拒绝**:
   - 初始化前同时保留调用方请求的绝对路径与 `realpath`，两者不完全一致即 Fail-Closed；创建目录后再次复核，防止初始化窗口内被替换。Windows Junction 与 POSIX 符号链接均覆盖“根自身”和“父链”两种场景。
2. **[DEF-T0023-37] 单个 Registry 文件链接替换拒绝**:
   - `inspect()` 要求目标 JSON 的绝对路径与 `realpath` 完全一致；单个登记文件被符号链接或 reparse point 重定向时，在 `exists/open` 前直接拒绝。

## 4. 2D-1 阶段实施报告（通用 Adapter 基础设施）

- **实施范围**: 仅执行第二阶段 2D-1（AdapterManifest、AdapterRegistry、验证等级、能力解析与通用合规测试套件）。
- **基线提交**: 21828a88ae0aa65b7cf84ea9b1e4244737100892
- **开发分支**: feature/phase2d-1-generic-adapters
- **本轮核心修复提交**: 8f9c36d2d90edbfd575a16af4f0243d95ff7d656、ff86ea3e8dce17de109f9e2948fc565281b622ea
- **结论**: 2D-1 五项通用基础能力及 DEF-T0049-15～17 返工已完成开发验证，等待角色隔离 Reviewer 与 QA 准出。核心代码中零客户端名称硬编码，严格遵循 Fail-Closed 与逐平台验证隔离。未实施任何真实 Codex/Antigravity Adapter，未进入 2D-2、2E、2F、第三阶段或第四阶段。

### 核心实现方案
1. **不可变 AdapterManifest**:
   - 包含 `schema_version`, `adapter_id`, `display_name`, `implementation_version`, `host_surface`, `verification_level`, `capabilities`, `workspace_modes`, `identity_fields`, `auth_boundary`, `billing_boundary`, `auth_context_id`, `billing_context_id`, `platform_version_constraint`, `supported_operating_systems`, `platform_verifications`, `executable_candidates_by_os`, `config_path_templates_by_os`, `conformance_suite_version`, `verified_at`, `e2e_evidence_refs`, `extra`。
   - 深度不可变（`MappingProxyType`, `tuple`, `frozenset`），严格校验类型、空白与控制字符，自动扫描并彻底拦截任何 API Key、Token、Password、Bearer 等敏感凭证。
   - 防伪约束：`native_verified` / `cli_verified` / `mcp_verified` 必须提供非空 `e2e_evidence_refs` 与 `verified_at`，测试夹具不可伪造。
2. **逐平台隔离与验证等级**:
   - 统一使用 `native_verified`, `cli_verified`, `mcp_verified`, `static_only`, `unsupported` 枚举集合，不做数值大小排序。
   - Windows, macOS, Linux 平台验证相互隔离，单平台验证等级不传播到其他平台；macOS 保持 `static_only`，Linux 为 `static_only` / `unsupported`，不进入真实自动派发。
   - 明确 `manual`, `assisted`, `verified_automatic` 三种运行边界，双窗口操作不冒充自动工作流。
3. **AdapterRegistry 与确定性能力解析**:
   - 纯内存操作，零文件写入、零锁、零进程、零网络、零会话、零计费。
   - 上下文隔离，显式注册，重复 ID 与身份不符 Fail-Closed。
   - 确定性解析流水线：Schema 校验 → 精确 ID 匹配 → 目标 OS 过滤 → 验证等级集合过滤 → 全部能力过滤（`UNKNOWN != SUPPORTED`）→ workspace 模式过滤 → 唯一性与歧义判定（多匹配返回 `ambiguous`，无匹配返回 `unsupported` 或 `manual_fallback`）。
4. **通用合规测试套件 (Adapter Conformance Suite)**:
   - 提供通用断言：`assert_manifest_conformance`, `assert_capabilities_conformance`, `assert_zero_side_effects`, `assert_handle_conformance`, `assert_lifecycle_conformance`。
   - 包含标准 Fake 测试夹具（`is_real_host=False`）与针对能力不匹配、假冒真实 Handle、非法接受外来 Handle、副作用检测的恶意 Adapter 夹具，证明合规套件具备主动拦截违规 Adapter 的有效性。

### 测试记录（最新真实数据）
- **2D-1 定向测试**:
  - `python -m pytest tests/test_adapter_conformance.py tests/test_adapter_manifest.py tests/test_adapter_registry.py -q -rs` -> `37 passed in 3.20s`
  - **总计**: `37 passed in 3.20s` (0 failed, 0 skipped)
- **2A/2B/2C 关联兼容测试**:
  - `python -m pytest tests/test_host_adapter.py tests/test_evidence.py tests/test_worktree_manager.py -q -rs` -> `60 passed in 20.20s` (0 failed, 0 skipped)
- **全量测试**:
  - `python -m pytest tests -q -rs` -> `323 passed in 62.36s` (0 failed, 0 skipped, 100% 通过)
- **代码规范**:
  - `git diff --check` -> 退出码 0，零尾随空白错误

### 2D-1 历次返工修复记录

#### DEF-T0049-1 ~ 7 修复（第一次独立复审返工）
1. **[DEF-T0049-1] 验证等级严格 Fail-Closed 与空集合/UNSUPPORTED 防御**:
   - `allowed_verification_levels` 强制要求非空元组，空集合直接抛出 `ValueError`。
   - `VerificationLevel.UNSUPPORTED` 在任何情况下均被 Resolver 绝对拦截，永不进入 `selected`。
   - `STATIC_ONLY` 验证等级严禁被选入 `verified_automatic` 执行模式，防止跨模式假冒。
2. **[DEF-T0049-2] 运行时真实能力核验与 Ghost Capability 拦截**:
   - 彻底废除仅凭 Manifest 自述放行能力的漏洞。Resolver 在核验能力时必须同时校验 `adapter.detect_capabilities()` 的运行时字段与 `extra` 属性。
   - 未在运行时 `HostCapabilities` 声明或状态为 `UNKNOWN`/`UNSUPPORTED` 的能力一律判定为缺失（`UNKNOWN != SUPPORTED`）。
3. **[DEF-T0049-3] 合规套件零副作用主动拦截机制**:
   - `assert_zero_side_effects()` 内部封装对 `builtins.open`（写模式）、`os.open`（写标志）、`os.mkdir`/`makedirs`、`subprocess.run`/`Popen` 以及网络 socket/HTTP 连接的主动拦截哨兵。
   - 增加 `FaultySideEffectWriteFileAdapter` 与 `FaultySideEffectSubprocessAdapter` 夹具，实测证明套件能主动拦截具有真实副作用的恶意 Adapter。
4. **[DEF-T0049-4] Adapter 身份一致性强校验**:
   - `AdapterRegistry.register()` 严格核对 `adapter.adapter_id == manifest.adapter_id`，大小写不一致、未定义或跨 Adapter 冒充均直接抛出 `AdapterRegistryError` 并保证零局部污染。
5. **[DEF-T0049-5] 项目上下文 ID 强绑定**:
   - `AdapterRegistry.resolve()` 校验 `request.project_id == self.context_id`，跨项目解析请求一律 Fail-Closed 返回 `UNSUPPORTED`。
6. **[DEF-T0049-6] Manifest 键类型严格校验与全小写 ID 规范**:
   - 废除 `_freeze_manifest_value` 中的隐式 `str(k)` 转换，Mapping 出现非字符串键直接抛出 `TypeError`。
   - `adapter_id` 统一强制全小写格式 `^[a-z0-9_][a-z0-9_\-\.]{1,63}$`。
   - 对 `workspace_modes`、`identity_fields`、`capabilities` 键值及各类路径模板执行完整的空白与控制字符强类型校验。
7. **[DEF-T0049-7] 精确 ID 拒绝静默回退与合规生命周期套件补齐**:
   - 当请求指定了精确 `adapter_id` 时，未注册或不匹配一律返回 `UNSUPPORTED`，严禁静默回退或降级为 `manual_fallback`。
   - `selection_strategy` 纳入严格白名单校验；合规套件补齐超时、取消、部分结果、异常映射、Handle 属主及与 `EvidenceValidationContext` / `WorktreeDescriptor` 的端到端兼容校验。

#### DEF-T0049-8 ~ 11 修复（第二轮独立复审返工）
1. **[DEF-T0049-8] 封堵全维度 Fake/Simulated 适配器真实验证冒充漏洞**:
   - 在 `AdapterRegistry.register()` 中，对 `caps.is_real_host is False` 的适配器执行全维度真实验证拦截：既不允许顶层 `manifest.verification_level` 为 verified 等级，也不允许任何 `manifest.platform_verifications` 包含 verified 等级，且严禁声明 NATIVE/CLI/MCP 等真实 surface。
   - 在 `_evaluate_candidate()` 中确立双重防线：`is_real_host is False` 的适配器永不可在 `VERIFIED_AUTOMATIC` 模式下被 selected，且永不可输出 verified 等级。
2. **[DEF-T0049-9] 零副作用合规拦截覆盖 pathlib / io 写入路径**:
   - `assert_zero_side_effects()` 拦截哨兵全面扩充，覆盖 `pathlib.Path.open`、`Path.write_text`、`Path.write_bytes`、`Path.touch`、`Path.mkdir`、`Path.unlink`、`Path.rmdir`、`Path.rename`、`Path.replace`、`Path.chmod` 以及 `io.open` / `_io.open`。
   - 增加 `FaultySideEffectPathlibWriteTextAdapter` 与 `FaultySideEffectIoOpenAdapter` 负向测试，实测拦截成功且磁盘零文件残留。
3. **[DEF-T0049-10] 完整实现多候选策略解析器 (deterministic / priority / first_match)**:
   - `first_match` 策略：按 `adapter_id` 字母序确定性选取首个匹配候选。
   - `priority` 策略：按 Manifest `extra.priority` 权重优先决策；若最高分存在并列平局，则返回 `AMBIGUOUS`。
   - `deterministic` 策略的初版曾使用验证等级权重；该设计已在 DEF-T0049-13 中删除并由多候选直接 `AMBIGUOUS` 取代。
4. **[DEF-T0049-11] 增加 Auth 与 Billing 边界声明与过滤**:
   - `AdapterResolutionRequest` 引入 `allowed_auth_boundaries` 与 `allowed_billing_boundaries` 强类型白名单校验。
   - `resolve()` 严格对比 Manifest 的 `auth_boundary` 与 `billing_boundary`，不符合请求边界声明的候选一律被排除。

#### DEF-T0049-12 ~ 14 修复（Codex 最终 QA 返工）
1. **[DEF-T0049-12] 零副作用检查改为线程作用域审计隔离**:
   - 删除对 `builtins`、`io`、`os`、`pathlib`、`subprocess`、`socket` 等进程级函数的动态重绑。
   - 本轮曾使用 `threading.local()` 激活当前能力探测线程；随后 DEF-T0049-15 证明派生子线程可绕过，该实现已由专用子进程隔离替代。
2. **[DEF-T0049-13] 移除验证路径数值权重**:
   - 删除 `VERIFICATION_LEVEL_WEIGHTS`，不再把 `native_verified`、`cli_verified`、`mcp_verified`、`static_only` 解释为可比较的高低等级。
   - 默认 `deterministic` 在多个候选满足条件时返回 `AMBIGUOUS`；显式 `priority` 只比较 Manifest 的调用方优先级，显式 `first_match` 只按 Adapter ID 确定性选择。
   - 新增跨验证路径对抗测试，证明高优先级 `static_only` 不会被低优先级 `native_verified` 的隐式等级权重覆盖。
3. **[DEF-T0049-14] 账号与计费身份链精确绑定**:
   - `AdapterManifest` 与 `AdapterResolutionRequest` 增加非秘密 `auth_context_id`、`billing_context_id`。
   - 非匿名认证边界必须声明 `auth_context_id`；计量或宿主绑定计费边界必须声明 `billing_context_id`；匿名/不计费边界禁止携带伪上下文 ID。
   - Resolver 对两类上下文 ID 执行 1:1 精确匹配；相同边界枚举但不同账号、订阅或计费上下文，以及请求省略 ID，均 Fail-Closed 返回 `UNSUPPORTED`。

#### DEF-T0049-15 ~ 16 修复（Codex 角色隔离复审返工）
1. **[DEF-T0049-15] Adapter 派生线程纳入专用进程隔离**:
   - `assert_zero_side_effects()` 不再在宿主进程安装审计钩子；Adapter 通过 `spawn` 在一次性专用进程中重建，Audit Hook 在该进程内全局启用，因此 Adapter 派生的所有线程共享同一阻断边界。
   - 专用进程完成后使用硬退出，防止延迟或后台线程在门禁关闭后继续运行；残留线程、超时、不可序列化或非模块级 Adapter 均 Fail-Closed。
   - 无关宿主线程完全不进入隔离进程的审计上下文，可在探测期间正常执行合法临时目录写入。
2. **[DEF-T0049-16] 文件系统变更审计事件补齐**:
   - 拦截集合覆盖写模式 `open`、`mkdir/remove/rmdir/rename/chmod/truncate`，并补齐 `link/symlink/utime/chown`、环境变更、进程创建、注册表写入和网络事件。
   - 新增 Adapter 子线程写入、hardlink、symlink、utime、不可 spawn 类五组对抗测试；所有操作均在 OS 实际执行前被阻断，临时目录文件树和 mtime 保持不变。
3. **[DEF-T0049-17] 隔离进程控制台与日志输出阻断**:
   - 专用探测进程使用独立内存流捕获 stdout/stderr，Adapter 的显式 `print()`、日志输出及子线程未捕获异常回溯均不得传播至宿主终端。
   - 任一输出通道出现内容即记录零副作用违规并 Fail-Closed；错误消息只报告通道，不回传可能包含敏感信息的原始输出。
   - 新增显式控制台输出与子线程异常回溯测试，并通过 `capsys` 断言宿主 stdout/stderr 均保持空白。

### 本轮测试看板哈希说明
- 代码工作树测试数据 `user_data/board.json`：最终全量测试前 `3FD47FA4038F1B0B1B381148CB075CE49D0ADCAE00F1C214F1725D886B843295`，测试后 `914EFEF6B3CFA872398EB311A278045E93D2A0D0F59FC274A3AF547CC25DD27F`。
- 该文件属于代码工作树的非权威测试数据且未进入 Git 修改集；未直接编辑、覆盖或物理删除。
- 权威看板位于阶段集成工作树，T0049 在本轮开发期间保持 `进行中 / 李开发`。

---

## 2D-2 Codex 参考 Adapter 实施记录

### 1. 范围与定位

依据 `docs/D04-研发过程/D01-任务/Phase2-2D-通用Adapter注册与Codex参考实现任务书.md` 与 `MULTI_CLIENT_MODERNIZATION_PLAN.zh-CN.md` 第 8.2 节：
- **Task ID**: `T0050`
- **代码工作树**: `C:\Users\user\Desktop\user\multi-agent-flow-phase2d-codex-adapter`
- **开发分支**: `feature/phase2d-2-codex-adapter`
- **固定基线**: `b9c426a7c5d9226f2816fbe62ded3fb4a58d1e3c`
- **选定的 Codex Surface**: `CodexCliAdapter` (`adapter_id: codex_cli`, `host_surface: cli`)。
  - 基于本机真实安装的 `codex.exe`（`0.149.0-alpha.4.1`，路径位于 `%LOCALAPPDATA%\OpenAI\Codex\bin\110b3d66a02d864e\codex.exe`）。
  - 使用非交互式、机器可读的 JSONL 事件流通道（`codex exec --json -C <workspace_dir> -s <sandbox_mode>`）。
  - 支持 Windows 独立进程组创建（`CREATE_NEW_PROCESS_GROUP`）与进程树可靠终结（`taskkill /F /T /PID`）。
  - 严格保持 macOS / Linux 为 `static_only` 静态配置等级，Windows 标记为 `cli_verified`。

### 2. 交付物清单

- `scripts/_lib/hosts/__init__.py`: 平台适配器包初始化。
- `scripts/_lib/hosts/codex_cli_adapter.py`: `CodexCliAdapter` 及 `create_codex_cli_manifest()` 实现。
- `tests/test_codex_cli_adapter.py`: 针对 Codex CLI 的全套单元测试、合规接入、对抗测试与 Windows E2E 验证。

### 3. 测试记录（真实数据）

- **2D-2 定向测试（当前 Codex 沙箱）**:
  - `python -m pytest tests/test_codex_cli_adapter.py -q -rs` -> `19 passed, 1 failed`；唯一失败为当前沙箱账户无法启动 WindowsApps 内的 `codex.exe`（`PermissionError: [WinError 5]`），不是断言失败或 skip。
  - `python -m pytest tests/test_codex_cli_adapter.py -q -rs -k "not real_executable_detection_and_help"` -> `19 passed, 1 deselected in 1.66s`。
- **2D-1 通用基础设施与契约兼容回归测试**:
  - `python -m pytest tests/test_adapter_manifest.py tests/test_adapter_registry.py tests/test_adapter_conformance.py tests/test_host_adapter.py tests/test_evidence.py tests/test_worktree_manager.py -q -rs` -> `97 passed in 24.51s` (0 failed, 0 skipped)
- **全量测试套件**:
  - `python -m pytest tests -q -rs -k "not real_executable_detection_and_help"` -> `342 passed, 1 deselected in 66.78s`。
  - `python -m pytest tests -q -rs` -> `342 passed, 1 failed in 67.35s`；唯一失败为当前 Codex 沙箱账户启动 WindowsApps `codex.exe` 时返回 `PermissionError: [WinError 5]`。测试未 skip、未弱化，需由普通用户会话完成最终真实宿主复验。
- **代码规范检查**:
  - `git diff --check` -> 退出码 0，零尾随空白错误

### 4. 安全、认证与计费边界

- 认证边界采用 `USER_LOCAL`（本地已登录客户端会话），不读取、存储或提交任何 API Key、Token、Cookie 或凭证缓存。
- 计费边界采用 `USER_SUBSCRIPTION`（用户桌面客户端订阅/本地配额），不启用未经批准的 OpenAI Responses API，不产生额外 API 费用。
- 遵循零副作用合规探测原则，能力探测（`detect_capabilities`）为纯内存计算，通过了 `assert_zero_side_effects` 严格测试。
- 与 `EvidenceGate`、`EvidenceValidationContext` 及 `WorktreeManager` 无缝集成。
- **环境限制说明**: WindowsApps 路径下的 `codex.exe` 二进制若因当前运行沙箱权限受限而无法直接拉起时，记录为操作系统环境权限限制，代码严格保证不通过 skip 或假异常弱化任何门禁。

### 5. 2D-2 缺陷返工记录 (DEF-T0050-1 ~ 13)

1. **[DEF-T0050-1] 沙箱模式角色强约束与越权注入拦截**:
   - 建立沙箱模式白名单 `ALLOWED_SANDBOX_MODES = {"read-only", "workspace-write"}`，严禁任何 `danger-full-access` 注入或默认开启。
   - `REVIEWER` 角色强制执行 `read-only`；若 REVIEWER 请求 `workspace-write` 直接拒绝并抛出 `AgentNotSupportedError`。`QA` 角色默认 `read-only`，`DEV`/`BUILDER` 角色默认 `workspace-write`。
2. **[DEF-T0050-2] 完整实现 §7.1 权限审批合同与确认闭环**:
   - 增加 `approval_policy` 检查（`on-request`、`never` 白名单），支持 Codex CLI 权限预检。
   - 后续 DEF-T0050-15 进一步明确：非交互 `codex exec` 不具备可信用户确认通道，Adapter 不解析选项作为授权证据，统一 Fail-Closed 交由可信上层 Host 处理。
3. **[DEF-T0050-3] 真实会话 thread_id 解析与用量遥测精准绑定**:
   - 修复 `_parse_jsonl_output` 解析优先级，从 `session_start` 及 JSONL 事件流中精准提取并绑定真实 `thread_id`（如 `01a03cdc-d21c-77d1-a3b4-aa9081287b6d`）。
   - 完整采集真实用量遥测数据（`input_tokens`、`cached_tokens`、`output_tokens`）并绑定入结果与会话元数据。
4. **[DEF-T0050-4] 真实 EvidenceGate 判决与证据闭环**:
   - 完善 E2E 证据测试，构造符合权威 Schema 的 `EvidenceRecord` 与 `EvidenceMetadata`（含 `thread_id` 与 capabilities），通过 `EvidenceStore.append` 与 `EvidenceGate.validate_evidence` 完成完整门禁通过判决。
5. **[DEF-T0050-5] 错误事件捕获与 Git 仓库受信目录强校验**:
   - JSONL `error` 事件全量保留至 `events` 事件列表，不丢失错误审计信息。
   - `dispatch_agent` 在执行前执行 `_is_git_repository` 校验，非 Git 仓库目录直接拒绝执行（`AgentNotSupportedError`），防止由于脱离受信任目录产生异常。
6. **[DEF-T0050-6] 移除非法 `-a` 参数并对接真实 `--approve-for-me` 机制**:
   - 移除 `codex exec` 命令行中不存在的 `-a` 参数，杜绝真实 CLI 运行报错（`rc=2 unexpected argument '-a'`）。
   - 当 `approval_policy` 为 `"auto"` 或请求明确指定自动审批时，对接 Codex 官方 `--approve-for-me` 真实参数；增加 `build_codex_exec_command` 及针对本机真实 `codex.exe exec --help` 的参数合法性冒烟测试。
7. **[DEF-T0050-7] 修复 `--approve-for-me` 与 `-s` 命令行参数互斥**:
   - 依据 Codex CLI 官方语法，`--approve-for-me` 自带 `workspace-write` 沙箱，与 `-s / --sandbox` 严格互斥。
   - 在生成 `--approve-for-me` 时完全排除 `-s` 参数；同时禁止只读角色（`REVIEWER`）使用 `--approve-for-me`（若请求则 Fail-Closed 拦截），确保参数合法且权限不越权。
8. **[DEF-T0050-8] 解析并绑定 Codex 真实 turn/invocation 标识至 E2E 证据链**:
   - 在 `_parse_jsonl_output` 中新增真实 `turn_id` / `invocation_id`（如 `turn-01a03cdc-01`）的提取逻辑，绑定至 `session_data["invocation_id"]` 及结果元数据。
   - 在 Evidence 证据链测试与验证中，动态提取实际执行产生的 `host_invocation_id` 与 `host_session_id`，杜绝硬编码假标识，形成完整闭环的 E2E 准出证据。
9. **[DEF-T0050-9] 兼容官方 `thread.started` / `item.completed` 真实事件流并闭环宿主标识**:
   - 真实 E2E 审计确认 Codex CLI 采用 `thread.started` (`thread_id`) 与 `item.completed` (`item.id`) 标准事件。
   - 增强事件流解析器全面兼容官方事件，将宿主真实返回的 `thread_id`（如 `01a03d0f-ed4f-7191-a8b5-4c8810ad207d`）与 `item.id`（如 `item_0`）作为权威证据链 Host 标识，在 Evidence 中明确标注来源 `openai_codex_host_thread_id`，实现 100% 真实可信可复核。
10. **[DEF-T0050-10] 强制绑定 `<thread_id>:<item_id>` 复合全局唯一标识**:
    - 鉴于 `item_0` 仅在单个 Thread 内局部唯一，`host_invocation_id` 强制采用 `<thread_id>:<item_id>`（如 `01a03d0f-ed4f-7191-a8b5-4c8810ad207d:item_0`）规范复合格式，杜绝不同 Thread 间 invocation ID 碰撞。
    - 增加双 Thread 返回相同 `item_0` 时 invocation 零碰撞对抗测试。
11. **[DEF-T0050-11] 进程退出码 0 但缺失规范宿主身份时 Fail-Closed**:
    - 若真实进程返回码为 0，但 JSONL 事件流中缺失 canonical `thread.started` / `thread_id` 或有效 item 标识，强制判定为 `AgentStatus.FAILED`（Fail-Closed）。
    - 严禁返回 `is_real_host=True` 的成功态，严禁伪造本地 `inv-*` 冒充宿主身份，严禁进入 EvidenceGate。
12. **[DEF-T0050-12] `cancel_agent` 强化 Handle 属主校验**:
    - `cancel_agent` 与 `wait_for_result` 执行完全对等的 4 重属主校验：`isinstance` 检查、`adapter_instance_id` 精确匹配、`host_id` 精确匹配、`is_real_host` 一致性检查。
    - 任何外来、伪造或跨模式 Handle 立即拒绝并抛出 `AgentInvalidHandleError`，不影响真实运行中会话。
13. **[DEF-T0050-13] 废除伪造用户确认，确认缓存严格绑定 5 元组**:
    - `request_confirmation` 严禁自动选择 `options[0]`；该阶段曾尝试通过特殊选项字符串表达 USER 确认，后续 DEF-T0050-15 证明调用方仍可伪造，相关入口已完全删除。
    - 旧的本地权限缓存已在 DEF-T0050-15 中移除，避免无可信签发来源的公开写入口形成自授权通道。
14. **[死代码清理]**: 清理了 `codex_cli_adapter.py` 中重复定义的 `dispatch_agent` 桩代码，保持代码简洁规范与契约不变。

### 6. 2D-2 第二轮 QA 返工记录 (DEF-T0050-14 ~ 17)

1. **[DEF-T0050-14] 禁止从 Thread 身份伪造 Invocation 身份**:
   - canonical invocation 只有在事件流同时提供真实 `thread_id` 与真实 `item_id` / `turn_id` / `invocation_id` 时才生成。
   - 删除 `item_0` 默认补值；只有 `thread.started` 时返回 `invocation_id=None`，真实执行因此触发既有 Fail-Closed 门禁。
2. **[DEF-T0050-15] 非交互确认与权限缓存 Fail-Closed**:
   - `request_confirmation()` 对任意选项（包括 `allow`、`user_confirmed`）统一抛出 `AgentNotSupportedError`，因为选项只是候选值，不是可信用户选择证据。
   - Runtime Capability 与 Manifest 均把 `interactive_confirmation` 声明为 `unsupported`；删除无签发验证的公开权限缓存写入/查询接口。
3. **[DEF-T0050-16] Session 原子占位与重复 ID 拦截**:
   - 在启动真实子进程前于锁内完成 Session 原子占位；活动或历史 Session ID 重复时直接拒绝。
   - 子进程启动失败只清理本次占位；不会覆盖旧 Session，也不会遗留失去控制的旧进程。
4. **[DEF-T0050-17] Handle 不可伪造 Token 与完整属主校验**:
   - 每次派发使用 `secrets.token_urlsafe(32)` 生成不可预测 `invocation_token`，服务端保存原始 Handle。
   - `wait_for_result()` 与 `cancel_agent()` 均验证类型、Adapter 实例、Host、真实模式、Session、token 及完整 Handle 相等；同一 Adapter 内仅凭 Session ID 无法伪造取消。
   - 取消结果进入历史记录，重复取消返回 `False`，同时继续阻止 Session ID 被重新占用。
5. **独立对抗复现**:
   - 仓库外脚本覆盖 thread-only、单元素 allow、自授权缓存、重复 Session、同实例伪造 Handle 五项场景；修复前 `0/5`，修复后 `5/5`。

---

## 六、第二阶段 2E：Antigravity 真实 Adapter 开发实施报告 (Task T0052)

### 1. 目标与完成情况

- **实施目标**: 在 2A～2D-2 基础设施上实现真实 `AntigravityAdapter`。在未完成真实浏览器 OAuth 登录前，Manifest 在所有操作系统平台（Windows, macOS, Linux）上如实声明 `STATIC_ONLY`（版本 >=1.0.0，实测 Google 签名版本 1.1.21）。
- **权限与审批优化**: 完整落实 §3.1 五类权限档（`safe_local`、`controlled_external`、`destructive`、`billing`、`acceptance`），对工作区内稳定命令实现 6 元组精确缓存，消除日常开发中重复授权痛点，同时杜绝 `Always Proceed`、`command(*)`、`python -c` 误放行与权限跨边界复用。
- **任务编号**: `T0052`
- **代码基线**: `e4550d4d0a87226adc7da67785700c1fdc7a2e44`

### 2. 核心架构与安全机制

1. **Host Surface 与验证等级**:
   - 适配器标识: `antigravity`
   - 宿主 Surface: `HostSurface.CLI`（`agy.exe` 1.1.21）
   - Windows: `VerificationLevel.STATIC_ONLY`（版本 >=1.0.0，实测 1.1.21）
   - macOS: `VerificationLevel.STATIC_ONLY`
   - Linux: `VerificationLevel.STATIC_ONLY`
   - 认证边界: `USER_LOCAL`（本地已登录账户会话，不读取、保存或提交凭证）
   - 计费边界: `USER_SUBSCRIPTION`（用户桌面客户端配额，不启用外部付费 API）

2. **角色路由与沙箱强隔离**:
   - `DEV` / `BUILDER` 角色: 路由至 `flow-dev` 专家子代理，默认启用 `--sandbox` 与 `--mode accept-edits`，支持 worktree 隔离。
   - `REVIEWER` 角色: 路由至 `flow-reviewer`，强制执行 `--mode plan`（只读），请求写操作直接抛出 `AgentNotSupportedError` 拦截。
   - `QA` 角色: 路由至 `flow-qa`，默认只读模式。
   - `ARCHITECT` / `PM` / `DOCS` / `DEVOPS`: 路由至对应专业子代理（`flow-architect`, `flow-pm`, `flow-docs`, `flow-devops`）。

3. **五类权限分级与精确 6 元组缓存**:
   - `safe_local`: 工作区内 Git 只读查询、pytest、yy-flow CLI 工具（`heartbeat.py`, `quick_task.py`, `transition_task.py`）。经用户/外部可信 Host 显式授权后在相同 6 元组上下文免密执行。
   - `controlled_external`: 网络请求、依赖下载、外部目录访问 -> 强制 Ask。
   - `destructive`: `git reset/clean/rebase/branch -D`、文件递归删除、清空/丢弃/移到回收站、取消/废弃任务 -> Deny / 逐次 Ask。
   - `billing`: API Key、付费 API -> 显式独立确认。
   - `acceptance`: `git push`, `git merge`, 发布上线 -> 显式用户授权。
   - 缓存唯一键: `(project_id, auth_context, adapter_instance_id, workspace_dir, command_family, permission_boundary)` 6 元组（解除瞬态 `session_id` 耦合，严格绑定项目与工作区），禁止跨项目、跨账号、跨工作区扩散。
   - 严禁 `Always Proceed`、`--dangerously-skip-permissions`、`command(*)`、`python -c` 任意代码内联执行。

4. **Session / Handle 与 Invocation 真实性与防伪**:
   - 启动真实进程前原子占位 `session_id`；若启动失败立即回滚占位。
   - 派发句柄携带 32 字节不可预测 `invocation_token`；`wait_for_result` 与 `cancel_agent` 实行完整 Handle 强相等校验。
   - 宿主调用标识绑定 canonical 规范复合格式: `<conversation_id>:<step_id>`（例如 `conv-6e9f4305-real:step_1`）。
   - 缺失真实会话或步骤身份时严格 Fail-Closed，拒绝冒充宿主身份，拒绝生成假 Evidence。
   - 非交互 CLI 模式下 `request_confirmation` 抛出 `AgentNotSupportedError`，不伪造用户确认。

### 3. 交付物清单

- `scripts/_lib/hosts/antigravity_adapter.py`: `AntigravityAdapter`、`create_antigravity_manifest()` 及 5 档权限风险评估函数 `evaluate_command_risk()`。
- `scripts/_lib/hosts/__init__.py`: 导出 `AntigravityAdapter` 与 `create_antigravity_manifest`。
- `tests/test_antigravity_adapter.py`: 针对 Antigravity 适配器的单元测试、集成测试、5 档权限评估测试、并发与生命周期测试、对抗防伪测试与 EvidenceGate 验证。
- `PHASE2_IMPLEMENTATION_REPORT.md`: 完整实施与测试报告。

### 4. 测试记录（真实数据）

- **2E 定向测试**:
  - `python -m pytest tests/test_antigravity_adapter.py -q -rs` -> `23 passed in 0.68s` (0 failed, 0 skipped)
- **2D-1 通用基础设施回归测试**:
  - `python -m pytest tests/test_adapter_manifest.py tests/test_adapter_registry.py tests/test_adapter_conformance.py -q -rs` -> `37 passed in 1.70s` (0 failed, 0 skipped)
- **2A～2D-2 兼容性与 Codex 适配器测试**:
  - `python -m pytest tests/test_host_adapter.py tests/test_evidence.py tests/test_worktree_manager.py tests/test_codex_cli_adapter.py -q -rs` -> `80 passed in 23.45s` (0 failed, 0 skipped)
- **全量测试套件**:
  - `python -m pytest tests -q -rs` -> `366 passed in 63.28s (0:01:03)` (0 failed, 0 skipped, 100% 通过)
- **代码规范检查**:
  - `git diff --check` -> 退出码 0，零尾随空白错误

### 5. 真实 E2E 证据与认证环境说明

1. **真实宿主可执行文件与环境约束**:
   - 真实宿主二进制: `C:\Users\user\AppData\Local\agy\bin\agy.exe`（Google Antigravity CLI 签名版本 1.1.21）。
   - 认证与会话约束: Antigravity CLI 需要用户完成 Google OAuth 认证。在未完成真实浏览器 OAuth 登录前，Manifest 在所有操作系统平台（Windows, macOS, Linux）上如实声明 `VerificationLevel.STATIC_ONLY`。
   - **用户操作边界约束**: 当需要打开浏览器进行登录或认证验证时，**必须显式使用 Firefox（火狐）浏览器**，严禁让 CLI 自行调用微软 Edge 或其他微软系浏览器。
   - 当 CLI 处于未认证或云端端点返回受限时，Adapter 严格执行 Fail-Closed 机制，拒绝伪造假成功状态。

2. **状态与元数据闭环**:

```yaml
e2e_record:
  antigravity_surface: "agy.exe (Google Antigravity CLI)"
  version: "1.1.21"
  verification_level: "static_only"
  project_folders:
    - "C:\\Users\\user\\Desktop\\user\\multi-agent-flow-phase2e-antigravity-adapter"
    - "C:\\Users\\user\\Desktop\\user\\multi-agent-flow-phase2-real-agents"
  auth_boundary: "USER_LOCAL"
  billing_boundary: "USER_SUBSCRIPTION"
  tested_via: "Isolated Mock & Static Contract Suite"
```

### 6. 2E 缺陷返工记录 (DEF-T0052 全量闭环)

1. **[DEF-T0052-1] 修复能力声明与 `request_confirmation` 行为不一致 (P1)**:
   - `AntigravityAdapter` 的 `_capabilities` 与 `create_antigravity_manifest()` 统一将 `supports_permission_approval` 与 `supports_interactive_confirmation` 声明为 `CapabilitySupport.UNSUPPORTED` / `"unsupported"`。
   - 彻底解决非交互 CLI surface 下能力声明与实际行为矛盾。

2. **[DEF-T0052-2] 修复真实 CLI 启动命令语法与参数顺序 (P1)**:
   - 依据 Go flag 语法规则，所有选项参数（`--output-format stream-json`, `--add-dir`, `--agent`, `--mode`, `--sandbox`, `--project`）严格置于 `--print` 之前，解决裸 `--print` 吞掉后续参数导致 CLI 返回退出码 2 的阻断问题。

3. **[DEF-T0052-3] 彻底封堵自授权漏洞并实现可复用 6 元组缓存 (P1)**:
   - 移除 `dispatch_agent()` 中任何对调用方自报 `approved=True`、`user_confirmed=True` 或 `approval_token` 的自授权绕过逻辑。非 `safe_local` 命令在非交互 CLI surface 下一律执行 Fail-Closed 抛出 `AgentNotSupportedError`。
   - 权限缓存重构为项目与工作区作用域的 6 元组（`project_id, auth_context, adapter_instance_id, workspace_dir, command_family, permission_boundary`），使合法的 safe-local 操作在后续 Session 中真正实现免重复确认。

4. **[DEF-T0052-4] 全面补全自然语言删除、`git diff --output` 与外部 pytest 风险分类防御 (P1)**:
   - 自然语言删除指令（如 `Delete all temporary files`、`Please remove ...`、`删除过期日志`、`清理临时目录`）精确归为 `destructive`。
   - `git diff --output=...` 以及带任意文件写重定向标志的 git 命令精确归为 `destructive`。
   - 外部测试文件或带路径遍历（`..`、`/outside/...`）的 pytest 命令精确归为 `controlled_external`。
   - `git branch <name>` / `git branch -d` / `git worktree add` 精确归为 `destructive`；`python scripts/evil.py scripts/heartbeat.py` 首参严格匹配拦截。

5. **[DEF-T0052-5] 严格跨项目边界隔离与 Popen 工作目录注入 (P1)**:
   - `subprocess.Popen` 严格显式指定 `cwd=request.workspace_dir`。
   - `validate_workspace_roots()` 引入 `_find_git_root` 与 `_find_git_common_dir`，严禁将任意外部 Git 仓库混入 `project_folders` / `kanban_dir`（违反跨项目隔离直接抛出 `AgentNotSupportedError`）。

6. **[DEF-T0052-6] 验证等级如实降级与版本校准 (P1/P2)**:
   - 在真实 OAuth 交互会话产出前，Windows/macOS/Linux 统一如实声明为 `STATIC_ONLY`，清空虚假 E2E 引用，杜绝证据等级虚高。
   - `ALLOWED_EXECUTION_MODES` 严格锁定为 `{"accept-edits", "plan"}`，拦截不支持的 `read-only` 与 `workspace-write` 模式。

7. **[DEF-T0052-7] 测试环境隔离与操作边界约束明确 (P3)**:
   - 单元测试与回归套件采用完全隔离的 Mock Fixture，杜绝自动化测试触发真实 OAuth 浏览器拉起。
   - 记录用户操作边界：若后续明确授权进行真实登录，**必须显式使用 Firefox（火狐）浏览器**。

8. **[DEF-T0052-17] 彻底隔离测试用例与消灭后台残留进程 (P1)**:
   - 修复 `test_antigravity_adapter_handle_forgery_rejection`，改用 `is_real_host=False` 纯内存测试，杜绝未 mock `Popen` 导致拉起真实 `agy.exe` 进程并弹出 OAuth 页面。
   - 所有测试会话均确保显式取消或等待结束，测试运行后系统 0 残留 `agy.exe` 进程。

9. **[DEF-T0052-18] 废除 dispatch 内隐式自动伪造已批准记录 (P1)**:
   - `dispatch_agent()` 执行 `safe_local` 时不再在未获得授权前隐式调用 `record_permission_approval()` 假造已批准记录。
   - `_permission_cache` 仅能通过显式 `record_permission_approval()` 登记真实授权凭据。

10. **[DEF-T0052-19] 关键词风险分类全量补全 (P1)**:
    - 自然语言 `empty`、`discard`、`recycle`、`trash`、`清空`、`丢弃`、`废弃`、`回收站`、`撤销`、`抹掉`、`移到回收站` 精确归为 `destructive`。
    - `transition_task.py` / `quick_task.py` 流转至 `已取消`、`已废弃`、`已退回`、`已阻塞` 精确归为 `destructive`。
    - `pytest` 解析路径与 Directory Junction / 软链接逃逸检测（`commonpath` 比对）归为 `controlled_external`。

11. **[DEF-T0052-20] 递归解析 `git-common-dir` 支持多 Worktree 共享仓库场景 (P1)**:
    - `_find_git_common_dir()` 能够解析主仓库 `.git/` 目录以及各 Linked Worktree 中的 `.git` 文件与 `commondir` 引用。
    - 代码 Worktree 与权威看板 Worktree 共享同一 `git-common-dir` 时正常通过双根校验，同时继续拦截无关外部 Git 仓库。

12. **[DEF-T0052-21] 落实 `STATIC_ONLY` 运行态安全门禁 (P2)**:
    - 当适配器声明为 `STATIC_ONLY` 时，`dispatch_agent()` 对未 Mock 的真实子进程调用执行 Fail-Closed 拦截，严格符合 Registry 的 `verified_automatic` 门禁契约。

13. **[DEF-T0052-22] 实施报告历史矛盾结论全量清理 (P3)**:
    - 报告全文清理旧的 `1.1.8`、`CLI_VERIFIED` 和 7 元组残留，全面对齐为 `1.1.21`、`STATIC_ONLY` 与 6 元组架构。

### 7. 未实施范围说明

- 独立 Reviewer/QA 真实自动编排与双宿主自动仲裁（属于 2F）
- main 分支合流与双 Adapter 联调（属于 2F 准出范围）
- 第三阶段（Skill 拆分与复杂度控制）
- 第四阶段（多平台打包与发布）
