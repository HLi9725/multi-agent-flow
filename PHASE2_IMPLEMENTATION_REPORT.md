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
- **结论**: 2D-1 五项通用基础能力已作为统一批次实现并通过全部合规测试。核心代码中零客户端名称硬编码，严格遵循 Fail-Closed 与逐平台验证隔离。未实施任何真实 Codex/Antigravity Adapter，未进入 2D-2、2E、2F、第三阶段或第四阶段。

### 核心实现方案
1. **不可变 AdapterManifest**:
   - 包含 `schema_version`, `adapter_id`, `display_name`, `implementation_version`, `host_surface`, `verification_level`, `capabilities`, `workspace_modes`, `identity_fields`, `auth_boundary`, `billing_boundary`, `platform_version_constraint`, `supported_operating_systems`, `platform_verifications`, `executable_candidates_by_os`, `config_path_templates_by_os`, `conformance_suite_version`, `verified_at`, `e2e_evidence_refs`, `extra`。
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
  - `python -m pytest tests/test_adapter_manifest.py -q -rs` -> `7 passed in 0.05s`
  - `python -m pytest tests/test_adapter_registry.py -q -rs` -> `12 passed in 0.09s`
  - `python -m pytest tests/test_adapter_conformance.py -q -rs` -> `10 passed in 0.07s`
  - **总计**: `29 passed in 0.21s` (0 failed, 0 skipped)
- **2A/2B/2C 关联兼容测试**:
  - `python -m pytest tests/test_host_adapter.py tests/test_evidence.py tests/test_worktree_manager.py -q -rs` -> `60 passed in 23.18s` (0 failed, 0 skipped)
- **全量测试**:
  - `python -m pytest tests -q -rs` -> `315 passed in 62.49s` (0 failed, 0 skipped, 100% 通过)
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
   - `deterministic` 策略：多阶段严格排序（验证等级权重 -> 优先级），若顶层仍平局则 Fail-Closed 返回 `AMBIGUOUS`。
4. **[DEF-T0049-11] 增加 Auth 与 Billing 边界声明与过滤**:
   - `AdapterResolutionRequest` 引入 `allowed_auth_boundaries` 与 `allowed_billing_boundaries` 强类型白名单校验。
   - `resolve()` 严格对比 Manifest 的 `auth_boundary` 与 `billing_boundary`，不符合请求边界声明的候选一律被排除。
