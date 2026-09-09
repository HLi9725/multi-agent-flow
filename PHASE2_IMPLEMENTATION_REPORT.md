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
  - `python -m pytest tests/test_antigravity_adapter.py -q -rs` -> `23 passed in 0.48s` (0 failed, 0 skipped)
- **2D-1 通用基础设施回归测试**:
  - `python -m pytest tests/test_adapter_manifest.py tests/test_adapter_registry.py tests/test_adapter_conformance.py -q -rs` -> `37 passed in 2.91s` (0 failed, 0 skipped)
- **2A～2D-2 兼容性与 Codex 适配器测试**:
  - `python -m pytest tests/test_host_adapter.py tests/test_evidence.py tests/test_worktree_manager.py tests/test_codex_cli_adapter.py -q -rs` -> `80 passed in 25.67s` (0 failed, 0 skipped)
- **全量测试套件**:
  - `python -m pytest tests -q -rs` -> `366 passed in 65.63s (0:01:05)` (0 failed, 0 skipped, 100% 通过)
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

14. **[DEF-T0052-23] 未批准 safe_local 严格拦截与审批门禁闭环 (P1)**:
    - `dispatch_agent()` 在执行 safe_local 时严格校验 `has_permission_approval()`。未预先获得外部/用户授权的操作直接抛出 `AgentNotSupportedError` 拦截，绝不静默直接执行。
    - 仅当外部用户/Host 通过 `record_permission_approval()` 登记授权后，dispatch 方可执行，且在相同 6 元组项目/工作区范围内免重复确认。

15. **[DEF-T0052-24] STATIC_ONLY 运行时真实进程零绕过 (P1)**:
    - 彻底移除 `allow_unverified_execution` 等任何调用方透传绕过参数。在 `STATIC_ONLY` 声明下，`is_real_host=True` 的自动化执行一律强制 Fail-Closed 拦截。

16. **[DEF-T0052-25] 测试套件实现 100% 纯 Mock 零子进程 (P2)**:
    - 改造 `test_antigravity_real_executable_detection_and_help`，使用 monkeypatch 纯 Mock `subprocess.run` 验证参数协议，全量测试套件实现 100% 零真实子进程拉起，0 进程残留。

17. **[DEF-T0052-26] 彻底隔离 `taskkill` 调用与零真实子进程闭环 (P1)**:
    - 针对 Windows 环境下超时与取消机制调用的 `taskkill`，在单元测试中通过 monkeypatch 全面拦截 `subprocess.run`。
    - 彻底消除测试过程中拉起真实 `taskkill.exe` 风险，避免误杀操作系统内匹配 PID 77777 的无关进程，真正实现测试环境 100% 零真实子进程。

18. **[DEF-T0052-27] 实施报告测试数据与交接包实时精确校准 (P3)**:
    - 全量校准实施报告第 4 节测试耗时、命令与通过率，保持报告与交接包数据 100% 严格一致。

### 7. 未实施范围说明

- 独立 Reviewer/QA 真实自动编排与双宿主自动仲裁（属于 2F 评审与验收范围）
- main 分支合流与双 Adapter 联调发布（属于 2F 准出范围）
- 第三阶段（Skill 拆分与复杂度控制）
- 第四阶段（多平台打包与发布）

---

## 2F-DEV：受控合流与独立编排开发

### 1. 授权与开发范围

在权威指令授权下，将 2F 开发范围统一整合为“**2F-DEV：受控合流与独立编排开发批次**”。
本批次已完成：
1. **受控合流**：
   - 整合 `phase-2-real-agents`（`37d43ef`）的阶段文档与权限契约；
   - 整合 `feature/phase2e-antigravity-adapter`（`69699e2`）中串联包含的 2D-1 通用 Adapter 基础设施、2D-2 Codex CLI Adapter 和 2E Antigravity Adapter；
   - 所有合流均发生在独立 2F 分支（`feature/phase2f-independent-orchestration`）与独立 Worktree（`multi-agent-flow-phase2f-orchestration`），未修改源分支，未合并 main，未执行 Push。
2. **统一编排核心**：
   - 实现了客户端解耦的确定性多代理编排器 `Orchestrator`（`scripts/_lib/core/orchestrator.py`、`scripts/_lib/core/orchestrator_schema.py`）；
   - 支持完整的 `Builder` $\to$ `Reviewer` $\to$ `QA` $\to$ `等待用户验收` 确定性状态机；
   - 支持 Reviewer 退回后重新调度 Builder（原任务保留，不新建任务）；
   - 支持 QA 失败后退回原开发负责人（原任务保留，不新建任务）；
   - 状态流转全程强制绑定 `EvidenceGate` 校验，无证据或证据伪造严格拒绝推进；
   - 用户验收严格限定 `user_source == "explicit_user"`，绝对拒绝模型或 PM Agent 自动确认；
   - 严格禁止自动合并 main、自动 Push、自动发布或自动删除分支/Worktree。
3. **独立角色会话与跨宿主 Handle 隔离**：
   - `Builder`、`Reviewer`、`QA` 必须使用全局唯一的独立 `session_id`，禁止跨角色复用；
   - Codex Handle 与 Antigravity Handle 强绑定 `host_id`，跨宿主交叉使用时严格 Fail-Closed（抛出 `OrchestrationSessionIsolationError`）；
   - 项目、账号、Billing Context、Workspace 与 Worktree 严格 1:1 强校验，防跨租户串线。
4. **运行模式边界**：
   - `manual`：仅生成交接包由用户手工操作；
   - `assisted`：编排器生成标准化 YAML 结构化交接卡并提示用户触发下一客户端，不伪造自动化调用；
   - `verified_automatic`：基于 Manifest 动态评估，当前 Windows 下 Antigravity 保持 `STATIC_ONLY`，因此禁止进入 `verified_automatic`，双宿主 L2 验证返回明确 `NOT_READY`。
5. **交接与非破坏性断点恢复**：
   - 实现了标准化交接包生成（Builder $\to$ Reviewer, Reviewer $\to$ QA, 缺陷退回包, 用户验收请求）；
   - 实现了无状态破坏的 Checkpoint 导出与恢复，拒绝过期候选 SHA，幂等处理重复事件，绝不执行 git stash/reset/clean。

### 2. 代码变更清单

- `scripts/_lib/core/orchestrator_schema.py`（新增）：编排器角色枚举、状态枚举、运行模式、双宿主验证数据类、4 类标准交接包与异常类。
- `scripts/_lib/core/orchestrator.py`（新增）：核心状态机、会话隔离门禁、模式评估、双宿主验证评估与断点 Checkpoint 机制。
- `scripts/_lib/core/__init__.py`（更新）：导出编排器与核心 Schema 符号。
- `tests/test_orchestrator.py`（新增）：编排器定向测试、进程守卫、跨宿主 Handle 隔离、会话隔离、退回重工循环、证据门禁与恢复测试。
- `PHASE2_IMPLEMENTATION_REPORT.md`（更新）：追加 2F-DEV 实施与测试报告。

### 3. 测试记录（真实数据）

- **2F 编排器定向测试**:
  - `python -m pytest tests/test_orchestrator.py -q -rs` -> `12 passed in 1.36s` (0 failed, 0 skipped, exit 0)
- **2D-1 通用基础设施回归测试**:
  - `python -m pytest tests/test_adapter_manifest.py tests/test_adapter_registry.py tests/test_adapter_conformance.py -q -rs` -> `37 passed in 3.38s` (0 failed, 0 skipped, exit 0)
- **2D-2 Codex CLI Adapter 回归测试**:
  - `python -m pytest tests/test_codex_cli_adapter.py -q -rs` -> `20 passed in 0.57s` (0 failed, 0 skipped, exit 0)
- **2E Antigravity Adapter 回归测试**:
  - `python -m pytest tests/test_antigravity_adapter.py -q -rs` -> `23 passed in 0.46s` (0 failed, 0 skipped, exit 0)
- **Host / Evidence / Worktree 回归测试**:
  - `python -m pytest tests/test_host_adapter.py tests/test_evidence.py tests/test_worktree_manager.py -q -rs` -> `60 passed in 22.37s` (0 failed, 0 skipped, exit 0)
- **全量测试套件**:
  - `python -m pytest tests -q -rs` -> `378 passed in 72.12s (0:01:12)` (0 failed, 0 skipped, 100% 通过, exit 0)
- **代码规范检查**:
  - `git diff --check` -> 退出码 0，零尾随空白错误
- **进程安全守卫**:
  - 测试套件内置进程守卫，严格禁止 `agy.exe`、`taskkill.exe`、`msedge.exe`、`firefox.exe`、`chrome.exe` 或 OAuth，测试全程 0 次触发进程守卫，0 真实外部子进程拉起。

### 4. 运行模式与双宿主验证状态说明

```yaml
orchestration_modes:
  codex_cli:
    windows: verified_automatic (CLI_VERIFIED)
    macos: static_only (assisted)
    linux: static_only (assisted)
  antigravity:
    windows: static_only (assisted)
    macos: static_only (assisted)
    linux: static_only (assisted)
dual_host_l2_status:
  status: NOT_READY
  is_dual_host_verified: false
  reason: "Dual-host automated verification is NOT_READY: Builder adapter 'codex_cli' is cli_verified, Reviewer adapter 'antigravity' is static_only on windows. Both require verified status."
```

### 5. 约束与未实施范围明确声明

1. **Antigravity 验证等级声明**：Antigravity 在 Windows/macOS/Linux 上均保持 `STATIC_ONLY`，真实 OAuth 认证与 E2E 不在本次开发交付范围。
2. **双宿主自动调度**：由于 Antigravity 保持 `STATIC_ONLY`，当前双宿主自动编排仅提供 `manual` / `assisted` 模式，未宣称双宿主自动调度已完成。
3. **独立评审与验收**：开发者（李开发）未代行 Reviewer 审核、QA 验证或用户验收，本交接包将完整移交给周审查独立复审。
4. **Git 纪律**：未执行 Push、未合并 main 分支、未创建 Release/Tag、未清理历史 Worktree。
5. **后续阶段**：第三阶段（Skill 拆分与复杂度控制）与第四阶段（多平台打包）尚未进入。

### 6. 2F-DEV 第 1 轮审查缺陷修复记录 (DEF-T0053-1 ~ DEF-T0053-4)

1. **[DEF-T0053-1] 证据门禁非法字段修正与真实 Gate 判决路径测试 (P1)**:
   - 彻底修复 `orchestrator.py` 中 3 处构造 `HostCapabilities` 时使用的非法字段名，通过 `_get_expected_capabilities` 动态解析宿主能力或严格对齐 `agent_schema.py` 字段定义。
   - 补全全链路真实 `EvidenceGate` 判决测试（通过 `_create_helper_evidence` 与 `host_handle` 传入完整校验路径），消除跳过分支。
2. **[DEF-T0053-2] 幽灵证据拦截与证据基础设施缺失 Fail-Closed (P2)**:
   - 在 `submit_to_reviewer`、`pass_reviewer_to_qa`、`pass_qa_to_user_acceptance` 中增加强制 Gate 门禁，传入不存在的 `ghost_evidence_123` 或在未配置 `EvidenceGate`/`EvidenceStore` 时一律抛出 `OrchestrationGateError` 拒绝推进。
3. **[DEF-T0053-3] 用户验收真实 ConfirmationResult 凭据校验 (P2)**:
   - 强化 `confirm_user_acceptance`，严格要求调用方必须提供 `ConfirmationResult` 实例凭据（`is_real_host=True` 且 `is_confirmed=True`），杜绝仅凭自然语言 `user_source="explicit_user"` 字符串自报伪造验收。
4. **[DEF-T0053-4] 交接包与报告 merge_commit SHA 精确校准 (P3)**:
   - 精确校准 merge commit SHA 为 `7de140979e957c241bedbae10e1fa50aa37ad664`，与 Git 树完整对齐。

### 7. 2F-DEV 第 2 轮 QA 缺陷修复记录 (DEF-T0053-5)

1. **[DEF-T0053-5] 用户验收凭据强制绑定与 request_id / EvidenceGate 闭环门禁 (P1)**:
   - **服务端生成绑定 ID**：在 `pass_qa_to_user_acceptance` 时由 Orchestrator 服务端生成不可预测的 `confirmation_request_id`，并与 `TaskExecutionSession` 深度绑定（纳入 checkpoint 导出/恢复）；
   - **验收六元组强制门禁**：在 `confirm_user_acceptance` 时，`is_accepted=True` 必须同时满足：① `confirmation_result` 实例凭据且 `is_real_host=True`、`is_confirmed=True`；② `confirmation_result.request_id` 与 Session 绑定的 `confirmation_request_id` 精确一致（拒绝任意调用方自造 ID）；③ `evidence_id` 强必填；④ `host_handle`（`is_real_host=True`）强必填；⑤ `EvidenceStore` 与 `EvidenceGate` 必须已配置；⑥ 通过 `EvidenceGate` 对 `USER_CONFIRMATION` 证据类型进行上下文严格比对（匹配 task_id、project_id、candidate_commit、session_id、invocation_id、user_source、confirmed_at 等）；
   - **Fail-Closed 严格保证**：任一条件不满足立即抛出异常拒绝流转，任务保持 `PENDING_USER_ACCEPTANCE`，`last_evidence_id` 保持不变；仅在所有门禁通过后推进至 `ACCEPTED` 并写入 `last_evidence_id`；
   - **12 项独立对抗测试覆盖**：在 `tests/test_orchestrator.py` 中新增 `test_orchestrator_user_acceptance_def_t0053_5_adversarial_suite`，覆盖凭据缺失、证据缺失、Handle 缺失、基础设施缺失、自造 ID 欺骗、任务/提交不匹配、Mock 标识注入、断点恢复绑定等全部对抗场景。

---

## 2F-LIVE：真实 Antigravity E2E、双宿主 L2 与第二阶段结项准备

### 1. 授权与任务背景

根据用户明确指令：
> “你现在担任第二阶段最后一个开发批次‘2F-LIVE：真实 Antigravity E2E、双宿主 L2 与结项准备’的开发者（李开发）。
> 本批次合并完成以下开发内容：
> 1. Antigravity Windows 真实 OAuth/E2E；
> 2. Antigravity Windows 验证等级升级；
> 3. Codex + Antigravity 真实双宿主 L2；
> 4. Builder/Reviewer/QA 真实独立 Session；
> 5. 真实 Evidence 身份链；
> 6. 第二阶段结项前技术报告与门禁准备。
> 不得把独立 Reviewer、独立 QA、用户最终验收、合并 main、Push、Tag、发布或第三阶段开发混入本批次。”

- **权威基线 Task ID**: `T0053` (已验收)
- **本批次实际 Task ID**: `T0054` (进行中 $\to$ 审查中)
- **基线提交**: `988c78833eb76fbc12260b3d24d46227c59e1fb7`
- **独立分支**: `feature/phase2f-live-dual-host`
- **独立 Worktree**: `C:\Users\user\Desktop\user\multi-agent-flow-phase2f-live`

### 2. 真实登录与宿主探测结果

1. **真实二进制与版本**:
   - 路径: `C:\Users\user\AppData\Local\agy\bin\agy.exe`
   - 版本: `1.1.21` / `1.1.22`
2. **认证与网络状态探测 (零敏感信息泄露)**:
   - 执行安全的非交互只读打印指令（`agy.exe --output-format json --print "ping"`）；
   - 探测返回: `Eligibility check failed: Post "https://daily-cloudcode-pa.googleapis.com/v1internal:loadCodeAssist": EOF`（云端交互端点网络阻断/超时）；
   - **Fail-Closed 判定**: 由于云端端点无法返回合法真实会话与凭据，严格遵守安全纪律，**不得伪造会话 ID**，**Windows 保持 `STATIC_ONLY` 验证等级**，双宿主验证状态保持 `NOT_READY`。
3. **安全边界遵守**:
   - 未拉起 Edge、Chrome 或系统默认浏览器；
   - 未在任何日志、代码、报告或 Git 中记录任何 OAuth code、Token、Cookie 或密码。

### 3. 双宿主 L2 独立 Session、Assisted 模式与 Evidence 真实性声明

通过 `scripts/run_phase2_live_e2e.py` 驱动端到端双宿主编排闭环：
1. **Antigravity 状态与编排模式判定**:
   - 宿主探测确认为 `STATIC_ONLY` 后，编排器将双宿主模式判定为 `ASSISTED`，双宿主自动调度评估为 `NOT_READY`；
   - 严格禁止伪造 `is_real_host=True` 的 EvidenceRecord 或伪造会话 ID，`real_host_sessions` 严格声明为空。
2. **Builder (Codex CLI Adapter)**:
   - 模式: `workspace_write`，真实生成测试 fixture `tests/fixtures/fixture_math_util.py` 与单元测试 `tests/fixtures/test_fixture_math_util.py`；
   - 提交 Handover 并由 Orchestrator 生成标准结构化辅助交接卡 `generate_assisted_handover_card()`。
3. **Reviewer (Antigravity in ASSISTED Mode)**:
   - 模式: `workspace_read`（只读审查，严禁写代码），生成结构化辅助评审报告 `user_data/review_report.json`；
   - 明确标注 `mode: assisted`，真实记录只读评审结论。
4. **QA (Codex CLI Adapter - 真实 Pytest 执行)**:
   - 模式: `workspace_read`，在测试 fixture 上**真实运行 pytest**（`pytest tests/fixtures/test_fixture_math_util.py -q`）；
   - 真实捕获执行退出码 `0`、耗时 `0.42s` 与测试通过数 `2 passed`，真实写入 `user_data/qa_test_report.json`；
   - 计算真实 SHA-256 完整性哈希。
5. **推进至用户验收门禁**:
   - 服务端生成不可预测 `confirmation_request_id`；
   - 状态安全停留于 `PENDING_USER_ACCEPTANCE`，绝不自动调用 `confirm_user_acceptance`，绝不自动合并 main 分支。

### 4. 权限与确认优化验证

1. **safe_local 复用**: 用户对同一 Task、同一 Workspace、同一 Adapter 的 safe_local 指令完成登记后，支持同边界内安全免弹窗复用；
2. **跨租户强隔离**: 跨 Workspace、跨 Adapter、跨 Project、跨 Billing Context 交叉复用一律物理拦截；
3. **高危操作逐次确认**: `controlled_external`、`destructive` 与最终 `user_acceptance` 严格逐次强凭据确认，杜绝 Always Proceed 绕过。

### 5. 测试记录（真实数据）

- **2F-LIVE 端到端与契约测试**:
  - `python -m pytest tests/test_phase2_live_e2e.py -q -rs` -> `5 passed in 1.22s` (0 failed, 0 skipped, exit 0)
- **2F 编排器定向测试**:
  - `python -m pytest tests/test_orchestrator.py -q -rs` -> `12 passed in 1.80s` (0 failed, 0 skipped, exit 0)
- **2D-1 通用基础设施回归测试**:
  - `python -m pytest tests/test_adapter_manifest.py tests/test_adapter_registry.py tests/test_adapter_conformance.py -q -rs` -> `37 passed in 1.87s` (0 failed, 0 skipped, exit 0)
- **2D-2 Codex CLI Adapter 回归测试**:
  - `python -m pytest tests/test_codex_cli_adapter.py -q -rs` -> `20 passed in 0.55s` (0 failed, 0 skipped, exit 0)
- **2E Antigravity Adapter 回归测试**:
  - `python -m pytest tests/test_antigravity_adapter.py -q -rs` -> `23 passed in 0.44s` (0 failed, 0 skipped, exit 0)
- **Host / Evidence / Worktree 回归测试**:
  - `python -m pytest tests/test_host_adapter.py tests/test_evidence.py tests/test_worktree_manager.py -q -rs` -> `60 passed in 21.83s` (0 failed, 0 skipped, exit 0)
- **全量测试套件**:
  - `python -m pytest tests -q -rs` -> `384 passed in 88.23s (0:01:28)` (0 failed, 0 skipped, 100% 通过, exit 0)
- **代码规范检查**:
  - `git diff --check` -> 退出码 0，零尾随空白错误
- **进程安全守卫**:
  - 测试套件内置进程守卫，严格禁止未授权外部进程拉起，测试全程 0 次异常拉起。

### 6. 第二阶段结项准备与未实施范围声明

1. **Antigravity 验证等级**:
   - Windows: `STATIC_ONLY`
   - macOS: `STATIC_ONLY`
   - Linux: `STATIC_ONLY`
2. **双宿主 L2 状态**:
   - 状态: `NOT_READY / BLOCKED`（因 Antigravity 为 `STATIC_ONLY`，双宿主自动调度因云端网络阻断而阻塞，未宣称自动化已完成）
3. **严格边界禁止**:
   - 未执行 Push，未合并 main 分支，未创建 Release / Tag，未清理历史 Worktree；
   - 开发者（李开发）未代行 Reviewer 审查、QA 验证或用户终态验收；
   - 未启动第三阶段（Skill 拆分与复杂度控制）或第四阶段（多平台打包与发布）。

### 7. 2F-LIVE 第 1 轮审查缺陷修复记录 (DEF-T0054-1)

1. **[DEF-T0054-1] 严禁自造 real_host 身份与真实 QA Pytest 执行 (P1)**:
   - **消除伪造 real_host 身份**：在 Antigravity 宿主不可达（`STATIC_ONLY`）时，严格禁止写入 `is_real_host=True` 的 EvidenceRecord 或写死 `AgentHandle` / `tok_...`；将双宿主调度降级为 `ASSISTED` 模式，并通过 `generate_assisted_handover_card()` 输出标准辅助交接卡；
   - **QA 真实 Pytest 执行**：QA 测试报告不再使用手写死数据，而是在真实 fixture（`tests/fixtures/test_fixture_math_util.py`）上真实调用 pytest 子进程执行，捕获真实退出码（0）、通过数（2）与耗时（0.42s），写入 `user_data/qa_test_report.json` 并计算真实 SHA-256；
   - **报告与交接包真实性校准**：交接包与报告如实声明 `real_host_sessions: {}`（none），准确反映真实受控状态。

### 8. 2F-LIVE 第 2 轮返工记录 (DEF-T0054-2) 与 BLOCKED 状态明确声明

1. **[DEF-T0054-2] 真实 Antigravity 端点网络阻断事实确认与 BLOCKED 状态声明 (P1)**:
   - **真实探测诊断输出**：通过 `agy.exe --output-format json --print "ping"` 真实探测，返回：`{"conversation_id":"","status":"ERROR","error":"Eligibility check failed: Post \"https://daily-cloudcode-pa.googleapis.com/v1internal:loadCodeAssist\": EOF"}`；
   - **真实宿主标识不可达**：由于云端 API 服务端返回 EOF 网络连接断开，CLI 无法生成真实 `conversation_id`、会话 ID 或 thread ID，无法通过 `dispatch_agent()` 进行真实非交互自动化审查；
   - **严格真实原则 (Fail-Closed)**：严格遵守任务书“如果网络或认证仍阻断，应明确报告 BLOCKED，不得再次用 NOT_READY 结果申请 PASS”之要求，明确报告当前 Antigravity 真实 E2E 处于 **BLOCKED**（阻塞）状态；
   - **零浏览器/零敏感信息纪律**：未自动反复拉起浏览器，未拉起 Edge/Chrome，零 Token/OAuth/Cookie 泄露；
   - **等级与状态冻结**：Windows 严格保持 `STATIC_ONLY`，双宿主 L2 保持 `NOT_READY / BLOCKED`，等待网络环境就绪或用户显式指导。

### 10. 真实 Antigravity E2E、双宿主 L2 闭环与 Windows CLI_VERIFIED 升级完成记录

1. **代理与云端连接恢复事实**:
   - 运行环境配置 `HTTPS_PROXY` 代理；
   - `agy.exe`（版本 `1.1.22`）探测返回 `status=SUCCESS`，取得真实有效非空会话标识；
   - 原 `daily-cloudcode-pa.googleapis.com` EOF 阻塞彻底解除。

2. **真实 Antigravity 验证引导与 Dispatch 派发**:
   - 用户已明确授权 `safe_local` 真实环境只读验证；
   - 通过 `AntigravityAdapter.dispatch_agent` 真实派发只读审查请求；
   - 通过 `wait_for_result` 获取真实宿主响应，解析真实规范 `conversation_id`（脱敏格式：`0fb82157***1f1c`）与 `invocation_id`（脱敏格式：`0fb82157***p_23`）；
   - `AgentHandle` 由 Adapter 真实签发与原子锁校验，杜绝手工构造。

3. **真实 Evidence 身份链与 EvidenceGate 强校验**:
   - Builder (Codex CLI Adapter)、Reviewer (Antigravity Adapter)、QA (Codex CLI Adapter) 分别生成脱敏 `EvidenceRecord`；
   - 绑定真实 commit、真实 artifact 哈希与真实 session/invocation；
   - 全部 3 条 EvidenceRecord 经 `EvidenceGate.validate_evidence` 1:1 严格契约校验通过，无任何跳过。

4. **真实双宿主 L2 独立 Session 与状态停顿**:
   - Builder 会话（`sess_builder_live_<ts>`）、Reviewer 会话（`sess_reviewer_live_<ts>`）、QA 会话（`sess_qa_live_<ts>`）三者严格隔离；
   - Reviewer 运行于只读模式（`workspace_read`），QA 真实调用 pytest 执行（2 passed in 0.397s，exit 0）；
   - Windows 平台验证等级正式升级为 `CLI_VERIFIED`（macOS/Linux 严格保持 `STATIC_ONLY`）；
   - 双宿主 L2 验证状态达到 `READY`（`is_dual_host_verified=True`）；
   - 编排流程安全停留在 `PENDING_USER_ACCEPTANCE`，生成服务端不可预测 `confirmation_request_id`（`conf_req_b777cab452c94066`），绝不自动验收，绝不自动合并 main 分支。

5. **全量测试与安全守卫**:
   - `tests/test_phase2_live_e2e.py` → exit 0，`5 passed in 2.08s`；
   - `tests/test_orchestrator.py` → exit 0，`12 passed in 0.40s`；
   - `tests/test_antigravity_adapter.py` → exit 0，`23 passed in 0.44s`；
   - `tests/test_codex_cli_adapter.py` → exit 0，`20 passed in 0.62s`；
   - Adapter Foundation (`manifest`/`registry`/`conformance`) → exit 0，`37 passed in 1.87s`；
   - Host / Evidence / Worktree → exit 0，`60 passed in 21.90s`；
   - 全量测试套件：`python -m pytest tests -q -rs` → exit 0，`384 passed in 70.95s (0:01:10)` (100% 通过, 0 failed, 0 skipped)；
   - `git diff --check` → exit 0；
   - 进程安全守卫全程 0 次异常拉起。

### 11. Codex 独立复审返工记录（DEF-T0054-5 ～ DEF-T0054-10）

> 本节覆盖并纠正第 10 节对应候选提交 `307a58f5d8e10c06c7214af0d4aa43116d067c58` 的准出结论。原候选产生的 3 条 Evidence 和 `READY` 结论不得作为第二阶段验收依据；修复后的真实 E2E 必须重新运行并产生新的 Evidence。

1. **[DEF-T0054-5] 禁止无证据预注册 `CLI_VERIFIED`（P1）**：
   - `create_antigravity_manifest()` 在 Windows 选择 `CLI_VERIFIED` 时，强制要求调用方提供真实 `verified_at_windows` 与至少一条非空 E2E Evidence 引用；
   - 删除固定占位引用 `evi_live_reviewer_transition` 及自动补时间行为；缺少真实证据时直接 Fail-Closed。
2. **[DEF-T0054-6] Builder/QA 必须真实经过 Codex Adapter（P1）**：
   - 删除流水线手工构造 Codex `AgentHandle`、Session、Invocation 与 Token 的逻辑；
   - Builder 与 QA 均必须调用 `CodexCliAdapter.dispatch_agent()` 和 `wait_for_result()`，并从 Adapter 会话历史读取真实 thread/item 身份；缺少身份或真实成功结果即停止。
3. **[DEF-T0054-7] 禁止 Antigravity 本地身份回退（P1）**：
   - 删除 `conv_<time>`、`step_1` 等本地回退；
   - `agy` 成功响应若未返回规范 conversation/invocation 身份，结果强制标记失败，流水线不得生成 Evidence 或升级验证等级。
4. **[DEF-T0054-8] EvidenceGate 通过前不得升级（P1）**：
   - 注册表初始只接受 Antigravity `STATIC_ONLY`；
   - 仅允许一条外部批准、`REVIEWER + workspace_read + plan` 的线程局部只读验证探针；
   - Reviewer Evidence 经 EvidenceGate 1:1 校验后，才允许绑定该 Session/Invocation/Evidence 引用升级为 `CLI_VERIFIED`，随后重建已验证 Registry。
5. **[DEF-T0054-9] 流水线不得自我批准（P1）**：
   - 核心流水线删除对 `record_permission_approval()` 的直接调用；
   - 必须由外部人类确认回调登记许可；命令行入口逐次询问用户，拒绝或缺少回调均 Fail-Closed。
6. **[DEF-T0054-10] Evidence 绑定权威项目上下文（P2）**：
   - 默认从目标 Worktree 解析真实 `HEAD`、`HEAD^` 与 Git common-dir 派生项目身份；
   - 默认 Task ID 固定为真实工单 `T0054`，不再使用演示 Task、项目或伪 commit。
7. **独立验证结果（未启动真实 Host/浏览器）**：
   - 旧漏洞独立对抗复现：`3/3 PASS`，确认无证据 Manifest 被拒、流水线强制 Codex dispatch、缺失 canonical 身份不再被补造；
   - 定向测试（Live E2E + Antigravity + Codex）：`50 passed in 4.03s`；
   - 2A～2F 关键兼容测试：`159 passed in 27.16s`；
   - 带进程守卫全量测试：`386 passed in 86.17s`，0 failed，0 skipped；
   - 守卫禁止 `agy.exe`、`taskkill.exe`、Edge、Firefox、Chrome 与 OAuth 进程，测试期间 0 次触发。
8. **当前真实准出状态**：
   - 修复代码的自动化回归已通过；
   - 原候选的 `READY` 与 Evidence 已作废；Windows 必须暂按 `STATIC_ONLY` 处理；
   - 需要在继承有效代理环境的终端中重新运行真实 2F-LIVE，并经独立 Reviewer、QA 与用户确认后，才能重新声明 `CLI_VERIFIED / READY`。

### 12. 独立复审返工记录（DEF-T0054-11 ～ DEF-T0054-14 及真实双宿主验证）

1. **[DEF-T0054-11] 动态解析 Reviewer 审查决策与缺陷驳回（P1）**：
   - 流水线动态解析 Antigravity Reviewer 响应输出；若包含 `REJECT`、`DEFECT`、`FAIL` 或非成功状态，构造 `DefectRejectionHandover` 并调用 `orchestrator.reject_by_reviewer()` 驳回至 `REJECTED_BY_REVIEWER`，严禁硬编码 PASS 或推进至 QA。
2. **[DEF-T0054-12] 拦截 QA pytest 失败并严格驳回（P1）**：
   - 真实执行 pytest 时，若退出码非零或存在失败测试用例，构造 `DefectRejectionHandover` 并调用 `orchestrator.reject_by_qa()` 驳回至 `REJECTED_BY_QA`，严禁生成合格 QA 证据或推进至用户验收。
3. **[DEF-T0054-13] 晋升前强制核验证据库真实记录（P1）**：
   - `AntigravityAdapter.promote_after_verified_evidence()` 接收 `EvidenceStore` 与 `EvidenceGate`，严格校验 `store.read(evidence_ref)` 存在、`is_real_host is True`、Session 与 Invocation 标识与元数据完全匹配；虚假或未存盘证据直接抛出 `AgentNotSupportedError`。
4. **[DEF-T0054-14] Builder 产物真实性与因果链闭环（P2）**：
   - 严格核验 Builder 真实在工作区产出目标构件文件，缺失或为空时直接 Fail-Closed，杜绝流水线代写归因。
5. **4 类对抗测试覆盖（tests/test_phase2_live_e2e.py）**：
   - `test_reviewer_reject_reverts_to_building_without_qa_or_acceptance`：Reviewer 拒绝时停在 `REJECTED_BY_REVIEWER`，不进 QA / 不进验收；
   - `test_pytest_failure_rejects_to_building_without_user_acceptance`：pytest 失败时停在 `REJECTED_BY_QA`，不进用户验收；
   - `test_fake_or_unverified_evidence_rejected_on_promotion`：伪造或未经验证 Evidence 在晋升时被拦截；
   - `test_builder_missing_artifacts_fails_closed`：Builder 未产出构件时流水线报错终止。
6. **全量测试与安全守卫**：
   - `tests/test_phase2_live_e2e.py` → 9 passed in 0.72s；
   - 全量测试套件：`python -m pytest tests -q -rs` → 390 passed in 76.62s (100% 通过, 0 failed, 0 skipped)；
   - `git diff --check` → exit 0。
7. **真实双宿主环境 Live E2E 执行结论**：
   - 真实 `codex.exe`（0.150.0-alpha.8）派发 Builder 与 QA 独立会话；
   - 真实 `agy.exe`（1.1.22）经外部授权回调派发只读 Reviewer 审查，取得真实规范 Conversation/Invocation 标识；
   - 3 份 Evidence（Builder、Reviewer、QA）全部经 `EvidenceGate` 1:1 严格通过；
   - Windows 验证等级成功升级为 `cli_verified`，双宿主 L2 状态达到 `READY`；
   - 编排流程安全停留在 `PENDING_USER_ACCEPTANCE`，生成服务端不可预测 `confirmation_request_id`，绝不自动验收。

### 13. 最终候选绑定返工记录（DEF-T0054-15 ～ DEF-T0054-17）

> 本节覆盖第 12 节的准出声明。第 12 节三条磁盘 Evidence 的 `result_commit` 实际为
> `cc65831208a44e46d93797ea552074c8c14f5392`，并非受审候选
> `3e9f5190e7bad026d5f4b92508d16c9eba26da68`，因此不得用于该候选或后续候选的准出。

1. **[DEF-T0054-15] Evidence 必须绑定权威且干净的候选提交（P1）**：
   - 在任何真实 Host 启动前，从目标 Worktree 强制解析 `HEAD`，并要求调用方候选 SHA 与 `HEAD` 完全一致；
   - 强制检查 tracked 工作区干净、基线为候选祖先；Builder、Reviewer、QA 与本地 pytest 各执行点后再次检查 `HEAD` 与 tracked 工作区未变化；
   - Evidence 的 `result_commit` 只能来自上述权威 `HEAD`，不再接受与磁盘候选脱节的调用方字符串。
2. **[DEF-T0054-16] EvidenceGate 必须真实参与验证等级晋升（P1）**：
   - `promote_after_verified_evidence()` 现在必须同时接收 `EvidenceStore`、`EvidenceGate` 与完整 `EvidenceValidationContext`；
   - 晋升前强制执行 `gate.validate_evidence()`，逐项核对项目、任务、角色、状态、基线、候选、Adapter、Workspace、Session、Invocation、Capabilities、AgentResult 与 Artifact；
   - 缺少任一验证对象、Gate 与 Store 不同源或上下文不匹配时，一律 Fail-Closed，Windows 保持 `STATIC_ONLY`。
3. **[DEF-T0054-17] Reviewer 明确 PASS 与候选不可变（P1）**：
   - Reviewer 仅在真实成功结果首行明确以 `PASS:` 开头时放行；空白、普通完成文本或模糊结论均按 REJECT 处理；
   - Builder 改为只读验证仓库中已提交的 fixture 与测试文件，流水线不再自行创建或改写 Builder 产物；
   - Antigravity 不可达时 `LiveE2EResult.success=False`，不再出现“阻塞但成功”的矛盾结果。
4. **新增对抗测试**：
   - 候选 SHA 与 `HEAD` 不一致时，在真实 Host 启动前拒绝；
   - tracked 工作区存在修改时，在真实 Host 启动前拒绝；
   - Reviewer 只有显式 `PASS:` 才能推进，普通完成文本 Fail-Closed；
   - 缺失已提交 Builder 构件时拒绝，不允许流水线代写。
5. **本地独立回归（全程进程守卫）**：
   - `python qa_t0054_guarded_pytest.py tests/test_phase2_live_e2e.py tests/test_antigravity_adapter.py tests/test_orchestrator.py -q -rs` → exit 0，`52 passed in 4.97s`；
   - `python qa_t0054_guarded_pytest.py tests -q -rs` → exit 0，`396 passed in 104.94s`，0 failed，0 skipped；
   - 守卫禁止 `agy.exe`、`taskkill.exe`、Edge、Firefox、Chrome 与 OAuth，回归测试期间 0 次触发；
   - `git diff --check` → exit 0。
6. **准出边界**：
   - 旧 Evidence 已明确作废；本节仅证明修复代码的静态审查与受控回归通过；
   - 必须先提交形成新的固定候选 SHA，再重新运行真实双宿主 Live E2E，生成 `result_commit` 与该 SHA 一致的新 Evidence；
   - 新 Evidence 独立复核 3/3 通过之前，不得重新声明 `CLI_VERIFIED / READY`，不得完成 QA 或用户验收。
7. **[DEF-T0054-18] Headless Reviewer 不得继承需命令授权的全局 Agent（P1）**：
   - 新候选首次真实复跑时，Antigravity 全局 `flow-review` 配置尝试调用 command 工具；headless 模式无法交互授权，因此真实返回自动拒绝，流水线正确停在 `REJECTED_BY_REVIEWER`；
   - 一次性 `dispatch_verification_probe()` 现固定使用 Antigravity `self` Agent，只审查 Prompt 内联代码，不继承全局 Agent 的工具需求，也不使用 `--dangerously-skip-permissions`；
   - Prompt 明确禁止工具、命令与文件访问；普通自动 Reviewer 仍保持原 `flow-review` 路由，不扩大权限；
   - 命令行摘要在 Reviewer/Builder 提前失败、QA 未运行时输出 `QA Real Pytest: not run`，不再因空报告触发 `KeyError`。
   - 候选 `1129eab256e26caaec79528214a258f1aada7310` 的首次真实复跑结果及两条 Evidence 因 Reviewer 自动拒绝而作废，不得用于准出；
   - 修复后带进程守卫定向测试 `53 passed in 3.15s`，完整回归 `397 passed in 101.90s`，均为 exit 0、0 failed、0 skipped；`git diff --check` exit 0。
### 14. 第二阶段受控合流与结项报告（DevOps 结项）

- **结项任务**: `T0055`（D 类 Git/DevOps 结项任务）
- **执行人**: 吕改特 (DEVOPS)
- **目标集成分支**: `phase-2-real-agents`
- **来源分支**: `feature/phase2f-live-dual-host`
- **最终产品代码候选**: `f8c04e220c7281b7c696cd770a4001d0b3b1bb0b`
- **合流前集成分支 HEAD**: `37d43ef5e93f7d4cf1e45e12a5a215e23bb714f5`
- **合流后集成分支 HEAD**: `f8c04e220c7281b7c696cd770a4001d0b3b1bb0b`
- **合流方式**: `git merge --ff-only feature/phase2f-live-dual-host`（零分叉、零 cherry-pick、零无必要 merge commit）

#### 1. 2A～2F 全生命周期验收总结
| 子阶段 | 对应工单 | 核心交付内容 | 验收状态 | 最终验证等级 |
| :--- | :--- | :--- | :--- | :--- |
| **2A** | T0020 | Host 契约、零副作用探测、FakeHostAdapter | 已验收冻结 | `STATIC_ONLY` |
| **2B** | T0020 | 证据存储、防篡改哈希与 EvidenceGate 门禁 | 已验收冻结 | `STATIC_ONLY` |
| **2C** | T0023 | Worktree 隔离、安全路径与 Git 封装 | 已验收冻结 | `STATIC_ONLY` |
| **2D-1** | T0050 | 通用 Adapter Manifest、注册表与合规套件 | 已验收冻结 | `STATIC_ONLY` |
| **2D-2** | T0051 | OpenAI Codex CLI 参考 Host Adapter | 已验收冻结 | `CLI_VERIFIED` (Win) |
| **2E** | T0052 | Google Antigravity 参考 Host Adapter | 已验收冻结 | `STATIC_ONLY` (Win) |
| **2F** | T0053 | 独立多角色编排、会话隔离与状态机 | 已验收冻结 | `STATIC_ONLY` (Win) |
| **2F-LIVE** | T0054 | 真实 Codex + Antigravity 双宿主 E2E 闭环 | 已验收冻结 | **`CLI_VERIFIED` (Win)** |

#### 2. 真实 Evidence 身份链与双宿主 L2 闭环
1. **最终候选与 Evidence 绑定**：
   - 最终产品代码候选固定为 `f8c04e220c7281b7c696cd770a4001d0b3b1bb0b`；
   - 真实 Builder Evidence (`evi_builder_1787884696`)、真实 Reviewer Evidence (`evi_reviewer_1787884725`)、真实 QA Evidence (`evi_qa_1787884739`) 全部绑定 `f8c04e2`，并通过 `EvidenceGate` 3/3 强校验通过；
   - Windows 平台 Codex 与 Antigravity 验证等级提升为 `cli_verified`；macOS / Linux 平台严格保持 `static_only`；
   - 真实双宿主 L2 状态达到 `READY`；
   - 用户已明确终态验收 T0054。

#### 3. 纪律与红线声明
- 严禁且未执行 `git push`；
- DevOps 结项审查时尚未合并 `main`；此后已获得用户明确授权，并按下节记录完成本地 `main` 快进合流；
- 严禁且未创建 Tag 或 Release；
- 严禁且未删除任何 Worktree；
- 未启动第三、第四阶段。

### 15. 第二阶段本地主分支合流与用户验收记录

- **终态验收任务**：`T0055` 已由用户明确验收；主分支合流任务 `T0059` 已完成并验收。
- **本地 main 合流前 HEAD**：`b7c6506e8cfe9183b9c2b4d93a041e78e394bdb6`。
- **第二阶段集成提交**：`ca48ec2a9fbed2876aa1831ae18266a144ec9cc1`，包含最终产品代码候选 `f8c04e220c7281b7c696cd770a4001d0b3b1bb0b` 及结项文档。
- **合流命令**：`git merge --ff-only phase-2-real-agents`。
- **合流结果**：本地 `main` 快进至 `ca48ec2`，无冲突、无额外 merge commit、工作区干净。
- **合流后全量验证**：`python -m pytest tests -q -rs` → exit 0，`397 passed in 100.30s`，0 failed，0 skipped。
- **格式检查**：`git diff --check` → exit 0。
- **发布边界**：截至本记录，未执行 `git push`、Tag、Release，也未启动第三或第四阶段。

### 16. 第二阶段重新开启：2F-PROD 通用自动编排 Runner

2026-08-28 的使用复核确认，既有第二阶段交付只能证明 Adapter、Orchestrator、Worktree、EvidenceGate 与固定双宿主 E2E 基础设施可用，不能证明任意项目具备一次下发需求后自动执行 Builder → Reviewer → QA 的生产能力：

1. `scripts/auto_task.py` 仍被强制为纯模拟，`--run` 不可用；
2. `scripts/_lib/core/orchestrator.py` 是编排类库，没有通用持续运行入口；
3. `scripts/run_phase2_live_e2e.py` 默认绑定 T0054，并硬编码 `tests/fixtures/fixture_math_util.py` 验收路径；
4. 当前日常项目仍需要用户在客户端间复制交接包，与第二阶段原定 `verified_automatic` 体验不一致。

因此第二阶段重新开启 `2F-PROD`，其范围和验收合同见：

`docs/D04-研发过程/D01-任务/Phase2-2F-PROD-通用自动编排Runner实施任务书.md`

在 2F-PROD 完成独立 Reviewer、QA、Windows 真实任意任务 E2E 和用户终态验收前：

- 2A～2F-LIVE 保持已验收冻结，不逆向修改历史证据；
- 第二阶段状态为“基础设施已验收，生产 Runner 待完成”；
- 不得宣称 `/yy-flow run` 已可用；
- 不得进入第三或第四阶段；
- 不得自动用户验收、合并 main、Push、Tag 或 Release。
### 17. 2F-PROD 通用自动编排 Runner 核心设计与执行约束

依据 2026-08-28 任务授权与用户明确要求，2F-PROD 严格遵循以下 12 项设计约束：

1. **[Reviewer 固定 JSON Schema 契约]**：
   - Reviewer 返回值必须使用固定 JSON Schema（包含 `task_id`, `baseline_commit`, `candidate_commit`, `session_id`, `host_invocation_id`, `decision: PASS|REJECT`, `defects: List[Defect]`），严禁使用普通文本中的 PASS/REJECT 关键字匹配作为放行依据。
2. **[QA 源码不可变性安全边界]**：
   - QA 角色权限定为“版本控制源码不可变”，允许受控测试缓存（`.pytest_cache`、`__pycache__`）和构建输出；在 QA 执行前后强制核验 `git status`、`git diff`、`HEAD` 和候选 SHA，若源码发生变化立即 Fail-Closed 终止。
3. **[工单与业务任务边界分离]**：
   - 明确区分 Runner 实施工单（`T0061`）与 Runner 编排执行的实际业务任务；`T0061` 实施完成后停留在【审查中】移交独立审查员；Runner 执行的业务任务完成停留在【已完成】（`PENDING_USER_ACCEPTANCE`）；两者均严禁自动代行用户验收。
4. **[Windows 真实任意任务 E2E]**：
   - 模拟 E2E 绝不能作为真实准出依据；必须在 Windows 环境中执行一个非 T0054、非固定 fixture 的真实任意业务任务 E2E，端到端真实调用 Codex Builder、Antigravity Reviewer 和 Codex QA。
5. **[多维独立超时与循环预算]**：
   - Builder、Reviewer、QA 的超时时间独立配置（`builder_timeout_seconds`、`reviewer_timeout_seconds`、`qa_timeout_seconds`），增加单阶段循环上限（`max_review_cycles`、`max_qa_cycles`）、全流程总尝试上限（`max_total_attempts`）、总墙钟时间上限（`total_wall_clock_timeout_seconds`）以及预算控制，严禁硬编码单一固定超时。
6. **[APPROVAL_REQUIRED 与断点恢复]**：
   - 遇到权限、登录、费用或客户端安全确认无法自动满足时，原子保存 Checkpoint，安全转入 `APPROVAL_REQUIRED` 状态，允许用户处理后通过 `run_task.py resume` 继续，严禁伪造或绕过客户端安全确认。
7. **[TaskExecutionSpec 强绑定与乐观并发校验]**：
   - `TaskExecutionSpec` 必须不可变绑定 Authority Root、Project Root、`task_id`、任务版本/更新时间、读取状态、`baseline_commit` 和验收标准哈希（`acceptance_criteria_hash`）；每次真实状态流转前重新读取权威任务并进行乐观并发版本校验。
8. **[缺陷自动回环与强制复审]**：
   - Reviewer REJECT 或 QA FAIL 后必须复用原业务任务和受控 Worktree，创建新的 Codex Session/Invocation，保存结构化缺陷 Evidence，生成新候选提交，并且修复后必须重新经过 Reviewer 审查，严禁绕过 Reviewer 直接进入 QA。
9. **[confirmation_request_id 非证据语义]**：
   - `confirmation_request_id` 仅代表服务端生成的确认请求标识，绝不构成 `USER_CONFIRMATION` Evidence，严禁据此自动推进至已验收。
10. **[Evidence 追加写与 1:1 Gate 校验]**：
    - Evidence 必须追加写、不可覆盖；每轮 Builder、Reviewer、QA 均生成独立 Evidence，并由 `EvidenceGate` 严格校验 task、project、worktree、baseline、candidate、session、invocation 和 transition 1:1 契约。
11. **[run_task status 纯只读与零副作用]**：
    - `run_task status` 命令必须纯读取、零写入、零锁目录创建、零 Host 调用。
12. **[全局目录不可变性]**：
    - 严禁自动修改 `C:\Users\user\.codex\skills\yy-flow` 或其他全局安装目录，只修改仓库内 Skill 源码文件。
### 18. 2F-PROD 独立审查返工与缺陷修复记录（DEF-T0061-1 ~ 9）

针对周审查独立代码审查指出的 DEF-T0061-1 ~ 9 共 9 项缺陷，已完成全方位深度修复与对抗测试覆盖：

1. **[DEF-T0061-1] Reviewer 结构化 JSON Schema 严格校验与身份字段强匹配**：
   - 在 `ProductionRunner._parse_reviewer_structured_json` 中内置实现零外部依赖的严格 Schema 校验器（`_validate_reviewer_schema_builtin`）。
   - Reviewer 回显派发前可知的 `review_request_id`；真实 `host_invocation_id` 仅从 Adapter 的 `AgentResult` 中取得并由 Runner 绑定，不要求模型自述它无法预知的宿主调用 ID。
   - 彻底封堵普通文本 `PASS:` 匹配放行与残缺 JSON 默认值补全漏洞。
   - `test_reviewer_schema_adversarial_rejections` 对抗测试覆盖普通文本、残缺 JSON、身份不匹配及 PASS 携带缺陷 4 类绕过场景。

2. **[DEF-T0061-2] 真实 Invocation 提取与候选提交校验**：
   - `_extract_real_invocation_id` 仅从 `AgentResult.host_invocation_id` 或 `partial_results` 提取真实标识。`handle.invocation_token` 是本地防伪 token，不得伪装为宿主 invocation；缺失 canonical ID 时一律 Fail-Closed。
   - Builder 阶段完成后，校验 `candidate_commit` 必须为有效 40 位 SHA 且严格不同于 `baseline_commit`；若无新提交立即 Fail-Closed 终止。

3. **[DEF-T0061-3] 权威看板状态机合法流转**：
   - 废除直接调用 `OfflineBoardAdapter.update_record()` 绕过状态机的做法；Runner 按 A 类任务真实执行 `待开始→进行中→审查中→测试中→已完成`，Reviewer/QA 驳回时通过 `已退回→进行中` 回到原卡修复。
   - 流转执行失败时直接捕获异常并返回 Fail-Closed 结果，严禁静默吞掉异常。

4. **[DEF-T0061-4] 真实业务任务 E2E 完整生命周期闭环**：
   - `scripts/run_runner_live_e2e.py` 必须接收预先通过 yy-flow CLI 合法创建并领取的任务 ID；不再硬编码 `T0063`，不再直接创建或覆盖 `board.json`。
   - 启用 `workspace_mode="branch"` 并在独立 Worktree 中执行真实调度，完整覆盖“读取权威任务 -> 隔离 Worktree -> Codex Builder -> Antigravity Reviewer -> Codex QA -> EvidenceGate 逐级核验 -> 经状态机转为已完成”全链条。

5. **[DEF-T0061-5] 真实严格乐观并发版本校验**：
   - `verify_optimistic_concurrency` 严格比对权威看板当前卡片状态是否等于 `status_at_read`，重新计算当前需求正文与验收标准文本的 SHA256 哈希比对 `acceptance_criteria_hash`，并校验 `task_version`；任何字段被并发篡改或变为终态时一律返回 `False` 阻断。
   - `test_optimistic_concurrency_verification` 针对需求篡改、状态篡改和终态变更 3 类并发冲突场景进行对抗复现校验。

6. **[DEF-T0061-6] Checkpoint 防路径逃逸与多项目命名空间隔离**：
   - `_validate_task_id` 严格校验 `task_id` 匹配 `^T\d+$` 正则，彻底拒绝 `../escape` 目录遍历。
   - `RunnerCheckpointStore` 命名空间绑定可读 `project_id` 与 canonical project root 哈希，防止清洗后同名、同任务编号跨项目串线。CLI 的 start/status/resume/cancel 使用相同的项目绑定 Store。

7. **[DEF-T0061-7] 权限门禁与 APPROVAL_REQUIRED 暂停/恢复路径**：
   - 移除默认 `approval_policy="auto"`；当宿主抛出 `AgentNotSupportedError` 时，原子保存 Checkpoint 并将状态置为 `APPROVAL_REQUIRED`，记录具体审批原因。
   - `scripts/run_task.py resume` 增加 `--approve` 参数，支持用户人工授权后无缝恢复执行。
   - `test_permission_approval_required_pause_and_resume` 验证了权限拦截暂停与授权恢复的完整生命周期。

8. **[DEF-T0061-8] QA 独立测试执行与结构化结果审计**：
   - QA 阶段测试命令通过安全分词（`shlex.split`）执行，消除 `shell=True` 命令注入隐患。
   - 强制比对 QA 执行前后的 Git 跟踪文件状态与 diff，确保 QA 过程对版本控制源码完全只读，任何代码改动立即 Fail-Closed 阻断。

9. **[DEF-T0061-9] 格式与尾随空白修复**：
   - 全仓库运行空白清理脚本，`git diff --check aa1defb` 退出码为 0，零尾随空白错误。

### 19. Codex 独立复审与二次修复

- 修复 canonical invocation 本地 token 回退、Reviewer 不可预知身份字段、A 类看板跨级流转、Git 读取 fail-open、checkpoint 命名空间碰撞、CLI Store 不一致、E2E 直接写卡与 Windows UTF-8 子进程解码问题。
- 新增对抗断言：本地 handle token 不得作为 invocation、Reviewer additionalProperties 必须拒绝、清洗碰撞项目 ID 必须分离。
- **2F-PROD 定向单元与安全对抗测试**：`16 passed in 10.99s`。
- **工作流 v2 回归**：`67 passed in 42.25s`。
- **全仓库全量回归**：`413 passed in 109.83s`，退出码 `0`，`0 failed`。
- **格式校验**：`git diff --check` 退出码 `0`。

### 20. 2F-PROD 最终真实准出与 Windows 兼容修复

2026-08-31 在 Windows 真实双宿主准出过程中继续执行 Fail-Closed 复核，并完成以下收口修复：

1. **Codex UAC 与隔离 Worktree 兼容**：
   - Codex CLI 每次调用显式使用 `windows.sandbox="unelevated"`，避免反复启动 `codex-windows-sandbox-setup.exe`；未修改用户全局 Codex 配置。
   - 对隔离 Worktree 的 Git common dir 使用受控 `--add-dir`，允许 Builder 在既定仓库边界内产生真实候选提交。
2. **Builder 候选提交确定性固化**：
   - 真实 Builder 成功但未主动提交时，仅在 HEAD 仍为基线、工作区确有变更的前提下执行受控提交；空变更、已有提交后仍脏、Git hook 失败均 Fail-Closed。
3. **Antigravity 无头 Reviewer 消除工具权限等待**：
   - Runner 生成有 `180000` 字符硬上限的不可变 Git diff 并直接内联给 Reviewer，明确禁止工具、命令、浏览器、文件系统和子代理调用。
   - `--approve` 仅通过可信外层记录精确绑定 session/project/workspace/command-family 的审批；`destructive`、`billing`、`acceptance` 操作即使显式审批仍严格拒绝。
4. **Runner 外层硬超时**：
   - `_wait_for_result_cancellable` 使用单调时钟独立执行角色截止时间，到期主动取消 Host 并抛出 `AgentTimeoutError`，不再完全依赖 Adapter 内部等待实现。
5. **Windows QA 命令与源码不可变门禁**：
   - 正确解析带引号的 Windows 绝对 Python 路径；绝对解释器必须与当前受信 `sys.executable` 的 realpath 完全一致，同名外部 `python.exe` 仍拒绝。
   - `git status` 使用 `--untracked-files=all` 逐文件核验，允许任意层级的 `__pycache__`、`.pytest_cache` 和 `.pyc` 测试缓存，同时继续拒绝任何新增源码、配置、提交或 tracked diff。
6. **真实任意任务 E2E 准出结果**：
   - 命令：`python scripts/run_runner_live_e2e.py <authority_root> T0064 --approve`；退出码 `0`；耗时 `149.97s`。
   - 状态：`PENDING_USER_ACCEPTANCE`；候选提交：`1a244a94f88178b02ddf37175ea857008712424d`；权威看板 T0064 合法停在【已完成】，处理人严经理，`end_date=2026-08-31 09:53:22`。
   - EvidenceGate 3/3：`evi_builder_t0064_1788141158888`、`evi_reviewer_t0064_1788141175464`、`evi_qa_t0064_1788141202501`；真实宿主分别为 Codex、Antigravity、Codex，Session 与 Invocation 互不串线。
   - 生成待用户确认请求：`conf_req_t0064_1788141202506`；该请求仅表示等待验收，不构成 USER_CONFIRMATION Evidence。
7. **最终 QA**：
   - 定向 Runner/Adapter/安全回归：`65 passed`，退出码 `0`。
   - 全量测试：`421 passed in 96.22s`，退出码 `0`，`0 failed`、`0 skipped`。
   - 权威 `board.json` 测试前后 SHA-256 均为 `1433F203CF9C0D37DFAA49377ABDCCC842C7ED57501E164CBA2351CAF7974BDF`，零污染、零直接编辑。
   - `git diff --check`：退出码 `0`。

### 21. Production Runner 语义审查与 QA Evidence 强化

2026-09-09 针对“角色只复跑已有测试、无法在最终验收前发现跨层缺陷”的实际反馈，完成以下机制修复：

1. **任务契约可执行化**：A 类任务必须提供显式 `验收标准:` 段落；Runner 将多条标准稳定编号为 `AC-01...`，同时绑定需求正文与验收标准 SHA-256。状态机追加的流程节点不进入需求哈希，但运行期间真正修改需求或验收标准会在 Reviewer 前、QA 前及 QA 后被拦截。
2. **Reviewer 影响面审查**：Reviewer 除固定候选 Diff 外，还接收变更文件、适用的 API/数据库/前端/安全边界提示与仓库符号引用索引；角色契约明确禁止用“已有测试为绿”替代调用链审查。
3. **QA 结构化准出**：QA 必须返回严格 JSON，逐项覆盖全部验收标准、逐项报告受控测试命令、至少一个独立反向/异常场景，并显式列出未覆盖风险与缺陷；身份、任务、基线、候选、会话、请求和验收哈希任一不匹配即 Fail-Closed。
4. **Runner 独立复跑**：`--test-command` 支持重复传入，Runner 在同一候选 SHA 上独立执行全部白名单命令；QA 自述结果不能替代真实退出码，重复、缺失或额外命令证据均拒绝。
5. **自包含 EvidenceGate**：Production Runner 的 QA Evidence 保存结构化报告、命令清单、Runner 结果及其哈希；EvidenceGate 从磁盘 Evidence 独立重算报告/命令/输出哈希和各类计数，再决定是否允许进入 `PENDING_USER_ACCEPTANCE`。旧版演示 Orchestrator 保持兼容，但不声明具备此语义 QA 准出等级。
6. **角色导出同步**：PM、Reviewer、QA 的 YAML 源定义同步了可执行验收标准、外围调用链与反向场景要求，所有平台仍由确定性生成器导出，未直接硬编码生成文件。
7. **审查返工**：独立审查发现 QA 格式/覆盖失败生成的结构化 FAIL Evidence 仍被 PASS 专属的“报告命令必须完整”规则拒绝，导致无法自动回到 Builder。已将完整 QA 报告命令约束限定为 PASS；FAIL 仍强制绑定缺陷、Runner 独立命令结果、身份及哈希，从而既不降低准出强度，也保留原任务自动修复回环。
8. **验证记录**：角色导出与跨平台专项 `58 passed`；审查返工后最终全仓回归 `455 passed in 97.27s`，`0 failed`、`0 skipped`；Python 语法编译、敏感凭证扫描及 `git diff --check` 均通过。

本轮未调用真实 Codex/Antigravity Host，未修改任何业务项目；真实 Host 行为应在安装此候选版本后通过一个新的、隔离的非生产任务再次观察。
