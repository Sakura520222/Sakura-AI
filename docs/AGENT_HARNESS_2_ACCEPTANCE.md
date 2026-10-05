# Agent Harness 2.0 验收记录

更新：2026-10-05。范围依据：[Issue #628](https://github.com/Sakura520222/Sakura-AI/issues/628) 正文及用户在本任务中的最新修订。基线为 2026-10-04 获取的 develop `38a021934461bae4932928c3f466df6d47ddc2b8`；沿用 `feature/issue-628-agent-harness` 和原 worktree。

**当前：Phase 1/2 本地验证通过；完整 #628 目标已重新设为执行目标，Phase 3–6 尚未完成。** 先保存本阶段本地提交，再按依赖继续，不能把本记录描述成整个 Issue 已解决。没有推送、创建/合并 PR 或关闭 Issue。

## 产品契约与本轮修订

- `/agent` 默认无人值守。已有配置和凭据就绪后，常规读写、Shell、依赖、测试和已配置工具自主执行；不新增审批 Agent、逐工具人工批准或正常运行必须点击继续的路径。
- 保留工作区、宿主凭据、Sandbox 和真实工具访问边界；检查由确定性运行时代码执行。复用 #604 临时依赖出网和 #627 bootstrap，不另建网络授权系统。
- 用户明确删除了九项新增设置及对应运行时限制：模型总轮数、工具总次数、只读并发数、连续重复工具轮次数，以及仓库单文件/总字节数、扫描项数、Skill 数量和元数据字节数。没有隐藏阈值替代；本任务新增的同类隐含长度/数量阈值也已删除。旧数据库行不再被执行链读取或配置页展示/保存，不做破坏性清理。
- 不以累计调用次数或任务步数结束任务、强制总结或切换便宜模型。旧 `max_iterations` 参数只保留为兼容的 no-op；本轮核实 worker/Agent 执行链没有使用它终止任务。
- 用户随后明确保留死循环检测：连续 10 次同工具、相同规范化参数/结果，或相同纯文本，触发一次策略自检提示及可持久化 metadata；继续执行，绝不据该窗口停止或切换模型。新操作/结果/真实指导重置，恢复不会重复发已持久化提示。
- 上述最新指令覆盖 Issue 中“第二次纯文本无进展即 blocked”和原有有界读取数量建议。普通文本仍不构成成功；正常完成仍必须调用 `finish_task`。取消、真实基础设施失败及无法安全恢复的状态保留明确原因，blocked 不等于人工授权。

## 保存点与真实接入

`656c7b65`（`chore(agent): WIP checkpoint for issue #628`）保存了此前 28 个源码、测试、文档文件，+4,343/-197。提交说明明确引用此前 Phase 1 验证，注明 Phase 2 未完成、Phase 5 基础未接入；它本身不是整体验收。该提交未夹带凭据、缓存、日志或生成产物。

本阶段相对 WIP 修改 28 个文件，+2,658/-933：业务代码 13 个（+1,020/-587）、测试/浏览器 fixture 6 个（+1,423/-185）、文档 9 个（+215/-161）。相对 develop 基线合计 38 个文件、+6,204/-333。新增行主要是恢复/失效/兼容和循环检测行为测试以及真实接线；无锁文件、依赖或生成文件变更。行数不作为完成度依据。

本轮后端调用链为 worker → iteration_loop → fullstack_expert → ToolExecutor/调度器；RepositoryContext、Skill 开关、历史限制和自检均在真实链上。Skills 的状态随已有工具结果事务提交，不新增表或迁移。`capability_policy.py` 仍没有生产调用方，保持为 Phase 5 基础。

| 阶段 | 状态 | 实现与证据 |
|---|---|---|
| Phase 1 | 已验证（按最新用户契约） | finish、调度、取消和 checkpoint/resume 保留；任务预算已删除，重复检测改成非终止自检；最新相关回归 938 通过/1 前置条件跳过 |
| Phase 2 | 已验证 | 五项交接缺口及两项独立审查 PoC 修复；实际双语配置交互、CSRF、最新回归和复核通过 |
| Phase 3 | 未开始 | 下一项：只读 Subagents、独立 durable session、等待/取消/汇总及父取消传播；不引入累计调用预算 |
| Phase 4 | 未开始 | MCP 配置、协议、发现、schema、边界和审计尚待实现 |
| Phase 5 | 部分实现 | capability_policy 的基础类型/判断及 30 项基础测试已存在，尚未接入执行器或发布链 |
| Phase 6 | 未开始 | 通用 Hooks、统一插件及管理界面、逐次压缩审计尚待实现 |

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
| P2–P3 | 写入、Shell/Git、finish 独占 | 工作区屏障与后台线程取消清理测试 | 已验证，仅同进程、同事件循环 |
| P4–P5 | 并行结果账本顺序/原子性、取消传播 | checkpoint SQLite 事务与任务/事件取消测试 | 已验证 |
| R1–R3 | 根/目录 AGENTS、Sakura rules；可选 CLAUDE | RepositoryContext/批次作用域投递及真实临时文件测试 | 已验证 |
| R4–R5 | 仓库数据不成为系统权限、不能越界/读宿主秘密 | user 层投影及路径穿越、内外符号链接、硬链接、特殊文件、秘密目录和恢复扩权测试 | 已验证运行时边界；不声称模型不受任何自然语言误导 |
| X1–X3 | 两种仓库 Skills、元数据先行/正文按需、allowed_tools 只收窄 | 上表对应实现/正常和异常测试 | 已验证 |
| X6–X7 | 恢复终态一致性、pending 调用策略 | 读取可重试，不确定写入不盲目重放，旧会话迁移原子化 | 已验证 |
| G1–G5 | Sandbox/checkpoint/guidance/compression/Skills 兼容 | 938 项相关回归；当前模型投影刷新规则且保留真实指导 | 已验证本地链；真实部署见限制 |
| S1–S6 | 子 Agent 创建、独立 context、并发调度、只读、结果/取消、父取消 | Phase 3 尚待实现；不得使用累计调用次数终止 | 未开始 |
| M1–M5 | MCP 配置/发现、统一边界、秘密/网络策略、失败隔离 | Phase 4 尚待实现 | 未开始 |
| K1–K5 | 能力 schema/profiles、抽象请求、执行级回收和审计 | 仅独立基础模块；#604/#627 已有执行链保持，尚无统一接线 | 部分实现 |
| H1–H4、X8–X11 | 系统生命周期 Hooks、finish veto、仓库不能提权、统一插件与 WebUI | Phase 6 尚待实现；可写子 Agent/具体第三方集成示例仍是未来设想 | 未开始 |
| X4–X5 | 每次 compaction 审计、前后 token 和保留任务/工具/错误证据 | 现有压缩可用，额外审计未实现 | 未开始 |
| G6–G7 | #604/#627 接入完整架构；全阶段集成/E2E | 当前回归通过；后续阶段接线与完整 E2E 待完成 | 部分实现 |

## 实际验证

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
- **未验证**：真实模型完成率、真实 Docker/Sandbox 部署、生产 GitHub 发布、MySQL 竞争。**Phase 1 互斥证据只覆盖同一进程、同一事件循环；跨 worker 保证仍待核实。** 策略检测仅识别可观察的相同内容；不同随机输出/时间戳或语义空转可能不会触发。
- **仍需完成**：Phase 3–6 必做功能及集成验证、压缩审计；下一项为只读 Subagent 的独立 durable session、执行边界和取消/恢复，不添加累计调用预算。

本轮浏览器证据保存在 `/tmp/sakura-628-phase2-browser/` 和 `/tmp/sakura-628-phase2-ui-evidence.json`；子任务验证记录位于忽略目录 `.superpowers/sdd/2026-10-04-issue-628-agent-harness/`。这些日志/截图未混入提交；原 develop 工作区保持干净。
