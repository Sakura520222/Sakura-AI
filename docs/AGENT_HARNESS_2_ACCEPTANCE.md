# Agent Harness 2.0 验收记录

更新：2026-10-08。范围依据：[Issue #628](https://github.com/Sakura520222/Sakura-AI/issues/628) 正文及用户在本任务中的最新修订。基线为 2026-10-04 获取的 develop `38a021934461bae4932928c3f466df6d47ddc2b8`；沿用 `feature/issue-628-agent-harness` 和原 worktree。10 月 8 日复读 Issue 正文及唯一机器人评论，需求未变；机器人评论不作为当前实现证据。

**当前：Phase 1–6 及正文额外必做项均已接入执行链，并通过下述本地验证和独立审查。** 本记录不代表生产部署、真实模型完成率、MySQL 跨 worker 竞争或 live Docker 隔离验证；这些边界单独列明。没有推送、创建/合并 PR 或关闭 Issue。

范围保留 Issue 的全部必做阶段与 Skills/压缩/恢复额外要求。可选 CLAUDE.md 兼容已实现；隔离 worktree 的可写子 Agent 和具体第三方产品集成仍是 Issue 中的未来设想，不伪称已实现。

## 产品契约与本轮修订

- `/agent` 默认无人值守。已有配置和凭据就绪后，常规读写、Shell、依赖、测试和已配置工具自主执行；不新增审批 Agent、逐工具人工批准或正常运行必须点击继续的路径。
- 保留工作区、宿主凭据、Sandbox 和真实工具访问边界；检查由确定性运行时代码执行。复用 #604 临时依赖出网和 #627 bootstrap，不另建网络授权系统。
- 用户明确删除了九项新增设置及对应运行时限制：模型总轮数、工具总次数、只读并发数、连续重复工具轮次数，以及仓库单文件/总字节数、扫描项数、Skill 数量和元数据字节数。没有隐藏阈值替代；本任务新增的同类隐含长度/数量阈值也已删除。旧数据库行不再被执行链读取或配置页展示/保存，不做破坏性清理。
- 不以累计调用次数或任务步数结束任务、强制总结或切换便宜模型。旧 `max_iterations` 参数只保留为兼容的 no-op；本轮核实 worker/Agent 执行链没有使用它终止任务。
- 用户随后明确保留死循环检测：连续 10 次同工具、相同规范化参数/结果，或相同纯文本，触发一次策略自检提示及可持久化 metadata；继续执行，绝不据该窗口停止或切换模型。新操作/结果/真实指导重置，恢复不会重复发已持久化提示。
- 上述最新指令覆盖 Issue 中“第二次纯文本无进展即 blocked”和原有有界读取数量建议。普通文本仍不构成成功；正常完成仍必须调用 `finish_task`。取消、真实基础设施失败及无法安全恢复的状态保留明确原因，blocked 不等于人工授权。

## 保存点与真实接入

`656c7b65`（`chore(agent): WIP checkpoint for issue #628`）保存了此前 28 个源码、测试、文档文件，+4,343/-197。提交说明明确引用此前 Phase 1 验证，注明 Phase 2 未完成、Phase 5 基础未接入；它本身不是整体验收。该提交未夹带凭据、缓存、日志或生成产物。

Phase 1/2 保存点 `30c4152f` 相对 WIP 修改 28 个文件，+2,658/-933：业务代码 13 个（+1,020/-587）、测试/浏览器 fixture 6 个（+1,423/-185）、文档 9 个（+215/-161）。该保存点相对 develop 合计 38 个文件、+6,204/-333；这是历史提交规模，不包括当前 Phase 3–6 的未提交实现。当前另有 MCP SDK/直接依赖及对应锁文件更新。行数不作为完成度依据。

`fa526d6d` 交付时的变更规模（相对 develop 基线，含两个历史本地提交）：101 个文件，+17,404/-552。业务代码 49 个（+6,734/-504）、测试/fixture 38 个（+10,065/-34）、文档 11 个（+525/-14）、依赖清单 2 个（+10/0）、锁文件 1 个（+70/0）。该次保存前为暂存 0、未暂存 54、未跟踪 32 个任务文件。新增行主要来自行为/失败/恢复/并发矩阵测试，以及 MCP、子会话、能力/Hooks 和持久审计实现；没有凭据、日志、缓存、环境目录或生成文件进入提交。后续 Windows 导入修复见下节。

后端调用链为 worker → iteration_loop → fullstack_expert → HarnessRuntime / ToolExecutor / 调度器；RepositoryContext、Skills、自检、SubagentManager、MCP 发现与执行、能力检查、Hooks、压缩审计均已接线。Skills 状态随已有工具结果事务提交。新 `AgentTeamSubagent` 保存父子关系与不可扩大的只读范围，`AgentTeamUsage` 保存幂等 provider receipt；使用现有 metadata 建表机制，不改已有列含义。`capability_policy.py` 已被执行器、MCP、Hooks、worker 依赖安装及 GitHub 写入链调用，不再是独立基础模块。

| 阶段 | 状态 | 实现与证据 |
|---|---|---|
| Phase 1 | 已验证（按最新用户契约） | finish、调度、取消和 checkpoint/resume；无任务预算；非终止自检；实际同进程及共享目录跨进程互斥/取消/进程退出验证 |
| Phase 2 | 已验证 | 五项交接缺口及两项独立审查 PoC 修复；实际双语配置交互、CSRF、最新回归和复核通过 |
| Phase 3 | 已验证，独立复核通过 | 只读独立会话、实时并发队列、等待/取消/结构化结果、父取消和恢复；SQLite、实际只读进程、描述符安全搜索和取消排空 |
| Phase 4 | 已验证，独立复核通过 | Streamable HTTP 官方 SDK、本地真实 TCP 服务、现代/兼容协议、schema 保真、撤销/秘密/取消；没有 host stdio |
| Phase 5 | 已验证，独立复核通过 | fresh capability/profile 接入真实工具、MCP、Hooks、依赖和 GitHub 控制面；workspace_write 在 runner 强制断网；#604 负责临时出网与回收 |
| Phase 6 | 已验证，独立复核通过 | 11 个 Hooks、完成前 veto、持久化副作用账本、插件配置/API/UI、压缩审计；真实进程/TCP/浏览器及贯通 E2E |

最终组合验证：**5166 passed / 17 skipped**；随后在隔离测试环境补装可选 `aiosqlite==0.22.1`，原跳过项所属文件 **3 passed**，剩余 16 个环境门控跳过项。不能把两组通过数相加。下方 938 项结果仅属于 Phase 1/2 历史保存点。

## Phase 2 缺口与验收证据

| 缺口 | 当前实现/行为 | 对应证据 |
|---|---|---|
| Skills 开关覆盖所有入口 | `repository_context.skills_enabled`、`fullstack_expert._refresh_skills/_project_model_messages`、`tools/registry.py`、`UseSkillTool.execute` 均读取当前开关；禁用时移除元数据/正文投影、清理缓存，直接调用和恢复也不能重新激活。独立仓库规则、原始 human guidance 和普通文件访问保留；已有工作流可自主 `end_skill` 清理 | `test_agent_repository_completion.py` 的 disabled/direct/cache/schema/resume/guidance/ordinary-read 行为用例；真实配置开关保存/重载 |
| 历史限制恢复不扩权 | `skill_scope.SkillRestriction` 用同一声明内的并集、历史与当前声明之间的交集表达约束；`fullstack_expert._execute_tool_calls` 将 runtime-owned scope/ceiling 与结果原子记录；恢复处理缺失、删除、损坏及失败/列表结果 | `test_atomic_skill_scope_resume_cannot_widen`、`test_missing_corrupt_history_cannot_restore_unrestricted_skill`、`test_runtime_scope_and_result_rollback_together`、成功/失败 listing 恢复测试；真实 SQLite 事务适配器 |
| 路径与规则一致性 | `ToolExecutor.repository_requirements` 覆盖现有 schema 的 `file_path/path/directory/file_paths/modified_files` 和 Skill 附件；`RepositoryContext.snapshot` 保留根/祖先/全局规则顺序，读前去重；新作用域替换旧快照，更新/删除/恢复重新读取 | 数组、兄弟目录切换、规则删除/刷新、`.sakura` 局部和 whole 扫描只读一次的测试；不再保留被用户撤销的 total_bytes 上限 |
| DB Skills 和选择器兼容 | DB 文件及附件保留本任务前既有 512 KiB 契约；删除仓库新增 64 KiB 等上限。支持真实工具名、`Read`、精确 `Shell(git status)`、token 前缀 `Bash(git status:*)` / `Bash(git *)`；未知/损坏语法明确拒绝，不降级为无限 Shell。语法校验不替代 Sandbox | DB 大文件、边界、附件、同长度/同时间内容更新、nofollow；合法选择器及串联/替换/重定向拒绝矩阵 |
| 发现、列表、正文分离 | 发现只读取 frontmatter；无头标记时不读完整正文行；目录列表不加载正文，`use_skill` 才读请求文件。每次安全打开新内容，缓存只保留指纹，观察相同 mtime/长度下的变化 | `test_listing_never_reads_main_body`、`test_metadata_listing_does_not_read_admin_body_and_exact_size_limit`、`test_no_frontmatter_marker_does_not_read_plaintext_body_line`、fresh-cache 用例 |
| 配置和文档 | 两语言 Skills 说明、九项控件撤销、使用指南和 README 对齐；历史外部实现文档明确不代表 Sakura 的审批或 fork/hooks 行为 | `test_agent_repository_config_ui.py` 两语言通过，实际浏览器关闭/开启、保存/重载、旧键忽略与 CSRF 拒绝；见 `AGENT_REPOSITORY_CONTEXT.md` |

独立审查复现并修复了两项问题：成功 `list_files` 的早退遗漏当前持久化 ceiling，导致恢复时重新放宽；`.sakura/AGENTS.md` 在全局和祖先遍历中被重复读取。对应回归先失败再通过。复核覆盖 112 项相关测试和 5 组自检状态转换，没有新的阻断问题。

一次自动权限审查曾把删除隐含长度/数量阈值判为超出五项点名设置。确认这些阈值全部由本任务新增，并提交用户最新指令和基线 diff 后，同一自动审查批准了小补丁；没有更换执行器绕过拒绝。当前没有遗留的权限审批阻塞。

## 要求映射

| ID | 要求 | 实现/验证 | 状态 |
|---|---|---|---|
| C1–C3 | 纯文本不成功；正常成功必须经过运行时 finish 协议 | `fullstack_expert.py`、`tools/base.py`、`finish_task_tool.py`；伪造/失败 finish、12 次文本后 finish、恢复终态测试 | 已验证 |
| C4 | 取消/阻塞/不可恢复错误独立记录 | checkpoint、worker outcome 与恢复测试；blocked 沿用 failed 任务状态并保留具体 phase/reason | 已验证 |
| C5 | 最新用户要求的非终止无进展自检 | `strategy_self_check.py`；10 次完全相同操作提示，超过窗口继续运行，变化证据/指导/恢复测试 | 已验证；不声称检测全部语义循环 |
| P1 | 安全只读并行（用户撤销数量上限） | `tool_scheduler.py`；7 个只读同批重叠执行 | 已验证 |
| P2–P3 | 写入、Shell/Git、finish 独占 | 工作区屏障、内核目录锁、真实跨进程读写冲突、线程/进程取消清理；`test_agent_workspace_process_lock.py` | 已验证同进程及共享文件系统目录上的协作进程；不是分布式任务所有权 |
| P4–P5 | 并行结果账本顺序/原子性、取消传播 | checkpoint SQLite 事务与任务/事件取消测试 | 已验证 |
| R1–R3 | 根/目录 AGENTS、Sakura rules；可选 CLAUDE | RepositoryContext/批次作用域投递及真实临时文件测试 | 已验证 |
| R4–R5 | 仓库数据不成为系统权限、不能越界/读宿主秘密 | user 层投影及路径穿越、内外符号链接、硬链接、特殊文件、秘密目录和恢复扩权测试 | 已验证运行时边界；不声称模型不受任何自然语言误导 |
| X1–X3 | 两种仓库 Skills、元数据先行/正文按需、allowed_tools 只收窄 | 上表对应实现/正常和异常测试 | 已验证 |
| X6–X7 | 恢复终态一致性、pending 调用策略 | 读取可重试，不确定写入不盲目重放，旧会话迁移原子化 | 已验证 |
| G1–G5 | Sandbox/checkpoint/guidance/compression/Skills 兼容 | 最终组合回归；当前模型投影刷新规则且保留真实指导；后端前置条件见运行指南 | 已验证本地链；真实部署见限制 |
| S1–S6 | 子 Agent 创建、独立 context、并发调度、只读、结果/取消、父取消 | `subagents.py`、`tools/subagent_tools.py`、`test_agent_subagents.py`；即时 slots 无累计上限；执行时只读范围不可由恢复/模型更改 | 已验证并独立复核 |
| M1–M5 | MCP 配置/发现、统一边界、秘密/网络策略、失败隔离 | `mcp_runtime.py`、`tools/mcp_tool.py`；`test_agent_mcp_http.py` 实际 HTTP/SSE、取消/清理、SDK 诊断秘密回显拒绝；`test_agent_mcp_schemas.py` 验证 provider schema 保真 | 已验证并独立复核；未调用付费模型端点 |
| K1–K5 | 能力 schema/profiles、抽象请求、执行级回收和审计 | `capability_policy.py`、`harness_runtime.py`、worker；`test_agent_policy_hooks_integration.py` 覆盖实时撤销、配置伪造、拒绝后无外部调用、实际网络收窄和关闭顺序 | 已验证并独立复核 |
| H1–H4、X8–X11 | 系统生命周期 Hooks、finish veto、仓库不能提权、统一插件与 WebUI | `lifecycle_hooks.py`、`plugin_config.py`、`routes/agent_plugins.py`；实际本地 Hook 进程、取消 drain、双语管理保存/重载；通用 API 已屏蔽凭据并拒绝绕过专用校验 | 已验证并独立复核 |
| X4–X5 | 每次 compaction 审计、前后 token 和保留任务/工具/错误证据 | `context_compressor.py`、`compaction_evidence.py`；记录 token 估计而非伪称 provider 实测；SQLite 证明审计先于下一次模型请求 | 已验证并独立复核 |
| G6–G7 | #604/#627 接入完整架构；全阶段集成/E2E | `test_agent_harness_e2e.py` 贯通真实 SQLite/文件/子会话/TCP MCP/命令 Hook、失败修正、usage 和终态恢复；模型为测试替身 | 本地 E2E 与相关回归通过 |

## 最终修复与独立审查

三份独立审查覆盖 runtime/policy/hooks、durability/subagents/compaction/read-only、MCP/config/UI；均给出 spec 和 quality verdict。修复后按差异复核，全部 actionable finding 已关闭：

| 问题 | 修复与实际证据 |
|---|---|
| Sandbox `{workspace}` 使用宿主路径 | runner-owned `execution_workspace` 映射；本地真实进程、Sandbox 客户端真实序列化、未知映射拒绝；先复现后修复 |
| workspace_write 继承更宽出网 | `execution_network_policy` 收窄 actual runner；Sandbox 原先返回 egress、Local 原先继续执行的两项失败均修复；保持默认 autonomous 原网络语义 |
| MCP SDK `client` 日志绕过过滤、远端 schema 被内建转换改变 | 对实际 SDK logger 施加上下文过滤，MCP schema 不套用内建工具的默认/必填改写；真实 TCP RED 23 failed/4 passed → GREEN 27 passed；相关 150 passed；独立复核通过 |
| 搜索绕过单链接读取边界 | `grep_tool.py` 固定、与关键词无关的 NUL 候选枚举；只发布重新验证/读取后的匹配；后台投影取消标记与排空；硬链接、替换、截断、Unicode/换行、取消回归先失败再通过；相关 317 passed；独立复核通过 |
| 旧 Git 的保护选项不支持 | 公开要求 `check_changes` 后端 Git 支持 `--no-lazy-fetch`（上游 2.45+）；前置检查返回非终止专用错误，不执行后续 Git 或移除选项重试；29 项 Git 测试通过并独立复核 |

早期两项审查因服务额度/工具拒绝中断，没有被算作通过。最终完整分片审查与上述修复差异复核均已完成。持久报告在忽略的任务目录中；本文保留可提交的需求、实现、命令和边界证据。

## 最终实际验证

在原 issue worktree 执行；隔离环境通过 `UV_PROJECT_ENVIRONMENT=.superpowers/sdd/2026-10-04-issue-628-agent-harness/venv uv sync --frozen` 建立，不修改主 checkout 的 `.venv`。

```bash
.superpowers/sdd/2026-10-04-issue-628-agent-harness/venv/bin/python -m pytest \
  tests updater/tests sandboxer/tests -q -rs -p no:cacheprovider --tb=short
```

结果：**5166 passed, 17 skipped，93.93 秒**。完整结果文件：`/tmp/sakura-628-final-all-suites.log`。

```bash
# 仅补装到该隔离测试环境，没有修改运行时依赖清单。
uv pip install --python .superpowers/sdd/2026-10-04-issue-628-agent-harness/venv/bin/python aiosqlite
.superpowers/sdd/2026-10-04-issue-628-agent-harness/venv/bin/python -m pytest \
  tests/test_legacy_placeholder_atomicity.py -q -p no:cacheprovider --tb=short
RUFF_NO_CACHE=true .superpowers/sdd/2026-10-04-issue-628-agent-harness/venv/bin/python run_ruff.py --check
git diff --check
git diff --check 38a021934461bae4932928c3f466df6d47ddc2b8
```

结果：安装 `aiosqlite==0.22.1` 后 **3 passed，0.38 秒**；Ruff **All checks passed**；两次 diff 检查通过。Ruff wrapper 按仓库约定读取现有 `.venv` 的 Ruff；只读检查没有同步或改写它。前一轮完整测试的 3 个失败均已处理：两项测试服务端 DEBUG 捕获混入客户端日志断言，以及本次路由代码移动导致的两处时间 API allowlist 行号漂移。

真实浏览器运行 `tests/harness_plugins_ui_app.py`：英文/中文管理页面，遮罩配置保存、禁用 MCP/添加 Hook、重载、不合法版本拒绝。`tests/harness_phase2_ui_app.py` 另验证新增子 Agent 并发字段从默认 4 修改为 3、Save All 后中文重载仍为 3；零控制台错误，保留既有 Tailwind CDN 警告。Skills 开关保存/恢复及 CSRF 证据见历史段落和当前 HTTP 回归。Fixture 身份/存储为测试替身，真实 FastAPI/Jinja/CSRF/保存与浏览器代码执行。两个测试服务均已关闭。

## 2026-10-08 Windows CI 导入回归修复

用户推送 `fa526d6d` 后，[Windows uv 冒烟任务](https://github.com/Sakura520222/Sakura-AI/actions/runs/37729363960/job/113154750933) 的锁定依赖同步成功，但 `import backend.main` 失败。调用链是 WebUI 插件路由 → plugin_config → lifecycle_hooks → tool_scheduler；新增的顶层 `import fcntl` 在 Windows 抛出 `ModuleNotFoundError`。同一 run 的 Linux Python 和 updater 检查成功。CI 未因锁文件漂移失败。

`tool_scheduler.py` 将 POSIX 模块改为可选导入；真正获取工作区锁时，缺少 `fcntl`、`O_DIRECTORY` 或 `O_NOFOLLOW` 则明确拒绝并保留 `workspace_lock_unavailable` 原因，不降级为无进程间互斥的执行。普通后端导入不再依赖这些执行期能力，Windows 原生 Harness 工作区操作仍未实现。

新增 `tests/test_agent_platform_import.py`：独立解释器屏蔽 POSIX 模块/目录标志后导入真实后端，并验证缺少锁能力不会执行操作或遗留读写等待状态。先观察到 **4 failed**，修复后 **4 passed**；与原有真实进程锁测试合跑 **11 passed**。以下相关回归 **180 passed，8.16 秒**（测试范围有重叠，不相加）：

```bash
UV_PROJECT_ENVIRONMENT=.superpowers/sdd/2026-10-04-issue-628-agent-harness/venv \
UV_CACHE_DIR=/tmp/sakura-628-uv-cache uv run --no-sync python -m pytest \
  tests/test_agent_platform_import.py tests/test_agent_workspace_process_lock.py \
  tests/test_agent_harness_runtime.py tests/test_agent_policy_hooks_integration.py \
  tests/test_agent_subagents.py tests/test_agent_harness_e2e.py \
  tests/test_agent_plugins_ui.py tests/test_uv_pyproject.py \
  -q -p no:cacheprovider --tb=short
```

新测试最后强化为同时缺少模块和目录标志后再次执行：**4 passed，1.84 秒**。仓库 `run_ruff.py --check`、两文件格式检查、`git diff --check` 均通过。以上是 Linux 上的能力缺失模拟和真实 Linux 回归；修复未推送，真实 Windows runner 尚未复验，不能将原失败 run 描述为已转绿。

## 2026-10-09 PR #661 审查修复

在主工作区 `feature/issue-628-agent-harness` 上复现并修复 [PR #661](https://github.com/Sakura520222/Sakura-AI/pull/661) 对 `471d29b1` 的三项意见：

- **取消传播**：事件取消保持结构化 `cancelled` 结果；外部 `Task.cancel()` 保留原 `CancelledError`，在子 Agent、压缩器和 Harness 资源排空后重新抛出。重复取消或清理存储故障不能覆盖原取消；清理故障记录安全 ERROR 日志。实际 IterationLoop/父子 SQLite 状态与资源关闭测试，初轮 **3 failed / 4 passed**（包含下面 seed 缺陷），独立审查补出清理边界 **2 failed / 8 passed**，修复后 **10 passed**。
- **恢复 seed 幂等性**：`_ensure_system_checkpoint` 跳过恢复历史；仅 system seed 已提交就崩溃的会话，连续两次恢复仍只有一条持久化/模型 system 消息。新会话仍写一次 seed。没有修改或删除已有历史记录。
- **审计与展示**：新 Harness、压缩、worker 控制审计使用 `role=audit`，保留全部 metadata；流端点在可见分页前过滤新审计角色及历史 user-role 审计。真实用户、指导与工具结果继续展示。Live View 完整/增量加载排空 `has_more`，加载期间的 SSE 刷新合并保留并在排空后补读，避免已完成任务遗漏最终消息。

审计分页与实际 JavaScript 先复现 **4 failed / 1 passed**，独立审查补出 SSE 竞态 **2 failed**，修复后针对测试 **30 passed**、Agent Team 回归 **226 passed**。取消/恢复/用量及相关调用链回归 **204 passed**。测试范围重叠，不相加。时间 API allowlist 仅同步本次新增路由 helper 导致的两处行号，保留显式 UTC 语义。

真实中英文浏览器使用生产路由、SQLite、Jinja/JS 和实际 300 ms SSE 防抖：450 条历史审计 + 205 条新审计不形成空白气泡，205 条真实消息全部加载；在持有 HTTP 快照响应时追加完成消息并发送 SSE，释放后自动补读，中文/英文分别显示 206/207 条消息，完成可见、零空白审计气泡、零控制台错误。仅 fixture 身份是替身，没有真实 MySQL/provider/GitHub 调用；服务和浏览器均关闭。

独立审查和两项补修的差异复核均为 spec/quality PASS。临时证据：`/tmp/sakura-661-runtime-review-tests-report.md`、`/tmp/sakura-661-audit-stream-fix-report.md`、`/tmp/sakura-661-independent-review.md`；截图在 `/tmp/sakura-661-audit-stream-browser/`。最终完整回归命令 `UV_CACHE_DIR=/tmp/sakura-661-uv-cache uv run --no-sync python -m pytest tests updater/tests sandboxer/tests -q -rs -p no:cacheprovider --tb=short`：**5189 passed / 17 skipped，134.04 秒**。跳过原因是 1 项缺少可选 aiosqlite、1 项 root/sticky、3 项 root/systemd 和 12 项显式 Docker gate；不计作已通过。仓库 Ruff 和 `git diff --check` 通过，没有运行远端 CI 或推送。

## 2026-10-11 PR #661 执行效果与恢复审查修复

沿用主工作区 `/home/firefly/Projects/Sakura-AI` 和 `feature/issue-628-agent-harness`，以 `cc6a585524` 为本轮基线；开始时工作区干净。本轮仅处理用户提供的四项审查反馈和直接相关的恢复、取消边界，不代表重新验收或扩展整个 #628。

| 审查要求 | 已验证的实现与行为 | 对应行为测试 |
| --- | --- | --- |
| Hook 写入参与完成统计 | `iteration_loop.py` 在所有 Hook/子任务清理后、保存会话结果与“未修改文件”判断前，复用 `git_workspace_service.py` 的真实 Git 变更统计，合并到现有结果。包括仅 Hook 写入、模型与 Hook 混合、before/after_finish、暂存修改、删除、重命名及未跟踪目录中的文件。Git 使用 NUL 分隔的 numstat/status，保留中文、制表符、换行文件名；保留旧文本 parser 的默认接口。 | `test_completion_includes_actual_hook_changes_before_durable_result`（含完成后恢复、不重复执行 Hook）；`test_completion_accounting_failure_and_cancellation_are_durable` |
| 项目检测取消排空 | `tools/project_detect_tool.py` shield/drain 自有线程操作，以线程安全信号和任务取消事件在下一次探测/依赖读取前停止；当前文件读取未退出前继续持有共享工作区锁。重复取消保留原取消异常。 | `test_project_detection_drains_reader_before_writer_and_stops_next_read`（直接 Task 取消、事件取消与后续排他写入） |
| 只读子 Agent 不得声称修改文件 | `tools/base.py` 在不可变只读执行边界拒绝非空 finish_task.modified_files，返回非终止的 `SUBAGENT_INVALID_RESULT`，子 Agent 可自主修正并继续。`fullstack_expert.py` 清空旧完成 ledger 恢复的子结果；`subagents.py` 在新结果保存和旧结果读取/父级恢复投影处清空修改声明。主 Agent 的完成参数接口保留。 | `test_readonly_finish_cannot_publish_claimed_modifications`（三个入口）；`test_child_claims_do_not_survive_checkpoint_wait_or_parent_resume`；`test_legacy_child_finish_and_saved_result_never_report_claimed_files` |
| 递归调查与恢复收到当前目录规则 | `tools/base.py` 将 search_in_files/glob 按全仓库调查预加载有明确 scope 的目录规则，与已有 Shell 规则预加载共用纯参数推导。finish_task 保留此前调查作用域和实际工具登记的写入。`fullstack_expert.py` 在 ledger 校验后，从最近成功的工作区工具批次重建恢复作用域，并在第一条恢复模型请求前重新读取当前规则；不复用旧规则正文。 | `test_recursive_investigation_receives_scoped_rules_before_results`；`test_completed_investigation_resume_refreshes_its_scope_before_model`（主/子 Agent × search/glob，规则更新与删除） |

独立审查额外复现终止核对阶段的真实 TrustedGit 元数据线程取消竞态。`iteration_loop.py::_completion_changes` 在共享锁内 shield/drain 整个自有 Git 核对操作；线程和命令真正结束后才释放锁、重新抛出原 Task.cancel()。补充 `test_completion_cancellation_drains_real_git_metadata_before_next_writer`，先复现 **1 failed**，修复后通过；独立复现脚本也确认重复取消不覆盖原消息，后续写入仅在线程退出后进入。调查恢复缺口另先复现 **4 failed**，修复后通过。

四项原始问题先复现 **14 failed**。补查特殊文件名、重命名、核对故障和核对期间事件取消又复现 **6 failed / 12 passed**。这些是不同快照的证据，测试数量不相加。最终新增的 23 项行为测试均包含在下列 398 项回归与完整测试中。

本轮初次回归发现本地环境缺少锁定的 MCP SDK；运行 `uv sync --locked` 恢复仓库锁定依赖（包括 mcp 2.3.0），未修改依赖清单或锁文件。既有两项取消单元测试和依赖引导单元测试明确使用 Git 替身；既有 Harness E2E 的工作区补为真实 Git 仓库，符合生产 worker 工作区契约。

```bash
UV_CACHE_DIR=/tmp/sakura-661b-uv-cache uv sync --locked

UV_CACHE_DIR=/tmp/sakura-661b-uv-cache uv run --no-sync python -m pytest \
  tests/test_agent_harness_review_effects.py tests/test_agent_harness_e2e.py \
  tests/test_agent_harness_review_runtime.py tests/test_agent_harness_runtime.py \
  tests/test_agent_harness_checkpoint.py tests/test_agent_team_cancel_and_prompts.py \
  tests/test_agent_dependency_bootstrap.py tests/test_agent_subagents.py \
  tests/test_agent_repository_context.py tests/test_agent_repository_completion.py \
  tests/test_agent_project_detect_capability.py tests/test_agent_policy_hooks_integration.py \
  tests/test_agent_search_boundaries.py tests/test_agent_team_workspace.py \
  -q -p no:cacheprovider --tb=short
# 398 passed，10.05 秒。

UV_CACHE_DIR=/tmp/sakura-661b-uv-cache uv run --no-sync python -m pytest \
  tests updater/tests sandboxer/tests -q -rs -p no:cacheprovider --tb=short
# 5212 passed / 17 skipped，103.92 秒。

UV_CACHE_DIR=/tmp/sakura-661b-uv-cache RUFF_NO_CACHE=true \
  uv run --no-sync python run_ruff.py --check
git diff --check
# All checks passed；diff 检查无错误。
```

限定范围的独立审查最终无待修正问题：23 项集成行为测试与独立恢复复现合跑 **25 passed**；独立 checkpoint/context 回归 **64 passed**；真实 TrustedGit 取消复现 exit 0。临时审查记录为 `/tmp/sakura-661b-independent-review.md`，可复现的集成测试已纳入仓库；临时报告可能被环境清理。

证据边界：真实执行临时 Git 仓库、命令 Hook、文件读取、线程/工作区锁、asyncio 取消及 SQLite checkpoint；模型和事件发布为替身，核对故障测试使用明确的 Git 替身，元数据/检测线程测试延迟真实读取来暴露竞态。没有真实 provider、GitHub 发布、MySQL 竞争、Docker 部署或远端 Windows CI 验证；本轮不涉及 WebUI 页面改动。17 项跳过为 1 项 root/sticky、1 项可选 aiosqlite、3 项 systemd 和 12 项 Docker 门控；不能计作通过。

兼容性：无新增数据库迁移、配置项、调用次数/轮数预算、审批 Agent、人工继续步骤或网络授权系统；保留既有 Sandbox/TrustedGit 边界。核对失败保留可观察的 `agent_workspace_changes_unavailable` 和非成功会话结果，不冒充完成；恢复可继续使用已提交的完成 ledger 重新核对工作区。本轮通过验证的文件允许保存为本地提交，不推送、不修改 PR 或 Issue 状态。

## Phase 1/2 保存点的历史验证

以下均在 `/home/firefly/.codex/worktrees/issue-628-agent-harness/Sakura-AI` 执行。此前 900/929 等数字属于历史快照，不替代下列最终结果；重叠测试不能相加。

```bash
UV_CACHE_DIR=/tmp/sakura-628-uv-cache uv run --no-sync python -m pytest -q \
  -o cache_dir=/tmp/sakura-628-pytest-cache \
  tests/test_agent_repository_completion.py tests/test_agent_repository_unlimited.py \
  tests/test_agent_repository_context.py tests/test_agent_skills.py \
  tests/test_agent_harness_runtime.py tests/test_agent_harness_checkpoint.py \
  tests/test_agent_team_resume.py tests/test_agent_team_cancel_and_prompts.py \
  tests/test_agent_team_tool_calling.py tests/test_agent_tool_framework.py --tb=short
```

结果：**264 passed，2.63 秒**。前置行为测试实际复现：初轮 19 项 Phase 2 缺口失败；后续 7 项 quota/审查 PoC 失败；4 项无限文本流程失败；重复自检和指导重放也先出现预期失败，再修复至通过。

```bash
UV_CACHE_DIR=/tmp/sakura-628-uv-cache uv run --no-sync python -m pytest -q \
  -o cache_dir=/tmp/sakura-628-pytest-cache \
  tests/test_agent*.py tests/test_sakura_agent_conversation.py \
  tests/test_config_basic_settings.py tests/test_default_configs_source.py \
  tests/test_webui_unified_config.py --tb=short
```

结果：**938 passed, 1 skipped，17.73 秒**。既有 `test_sticky_dir_foreign_owner_regression` 需要 root 与 `fs.protected_regular>=2`，本环境不满足，没有改变宿主权限/系统设置。

```bash
UV_CACHE_DIR=/tmp/sakura-628-uv-cache uv run --no-sync python -m pytest -q \
  -o cache_dir=/tmp/sakura-628-parent-pytest-cache \
  tests/test_agent_repository_config_ui.py tests/test_agent_team_cancel_and_prompts.py \
  tests/test_sakura_agent_conversation.py

UV_CACHE_DIR=/tmp/sakura-628-uv-cache RUFF_NO_CACHE=true \
  uv run --no-sync python run_ruff.py --check

git diff --check
```

结果：父代理相关测试 **37 passed，1.89 秒**；仓库规定 Ruff 检查 **All checks passed**；本单元 12 个后端/测试文件 Ruff format 检查通过；diff 检查通过。

浏览器命令：`UV_CACHE_DIR=/tmp/sakura-628-uv-cache uv run --no-sync python tests/harness_phase2_ui_app.py`。通过真实 `/config` 页面点击 Skills 开关关闭、保存/重载，再开启并保存；中英文显示正确；九个旧配置均无控件。向真实保存路由额外提交旧键也不写入；无效 CSRF 返回 **403** 且没有新增提交/审计。正常操作无控制台错误；主动 403 检查产生预期失败日志，既有 Tailwind CDN 警告保留。fixture 已正常关闭。

## 证据边界与下一步

- **真实执行**：临时文件/目录、nofollow 读取、asyncio 并发与取消、写线程清理、SQLite 提交/回滚、实际浏览器及 FastAPI/Jinja/配置保存/CSRF 路由。
- **测试替身**：模型、GitHub/外部服务；浏览器身份、数据库、网络状态和与 Phase 2 无关的配置节持久化。SQLite 通过已有同步 Session 的异步测试适配器运行，不代表 MySQL 跨进程测试。
- **未验证**：真实模型完成率、真实 Docker/Sandbox 部署、生产 GitHub 发布、MySQL 竞争、macOS/arm64 实机。Phase 1 的原始 938 项证据仅覆盖同进程/事件循环；新增 `test_agent_workspace_process_lock.py` 已实际覆盖共享目录跨进程互斥、并行读取、取消及进程退出。它不是跨主机或分布式任务所有权租约。策略检测仅识别可观察的相同内容；语义空转未必触发。
- **环境门控项**：1 项 sticky 目录跨属主测试需要 root 和 `fs.protected_regular>=2`；3 项 updater systemd 测试需要 root/PID1 systemd 及显式启用；12 项 live Docker 测试需要 Docker gate 的显式授权和环境。本任务没有扩大这些宿主权限。部署验证需提供这些条件后另行执行，不把它们算作已通过。
- **支持边界**：本地只读进程需要 Linux/Landlock/libseccomp；Git 检查需要支持 `--no-lazy-fetch` 的版本。MCP SDK discovery 有独立握手探测期限，工具调用没有新增总时长预算；任意远端 JSON Schema 的真实 GLM/ZAI/OpenAI 接受情况未实测。插件专用设置尚未纳入通用备份，迁移时需独立保存。以上不是隐藏实现或虚假成功路径，详见运行/插件指南。

历史 Phase 2 浏览器证据使用 `/tmp/sakura-628-phase2-browser/` 和 `/tmp/sakura-628-phase2-ui-evidence.json`（临时文件可能随环境清理）；最新插件页面证据在 `/tmp/sakura-628-plugin-browser/`。持久子任务报告位于忽略目录 `.superpowers/sdd/2026-10-04-issue-628-agent-harness/`。日志/截图不混入提交；原 develop 工作区现在有其他任务的 staged billing 改动，本任务不触碰。
