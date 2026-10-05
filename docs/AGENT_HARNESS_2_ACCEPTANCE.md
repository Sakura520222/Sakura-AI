# Agent Harness 2.0 验收记录

范围依据：[Issue #628](https://github.com/Sakura520222/Sakura-AI/issues/628) 正文、实施阶段与验收要求。基线为 2026-10-04 获取的 `origin/develop`，提交 `38a021934461bae4932928c3f466df6d47ddc2b8`。

本轮已按用户修订停止后续阶段扩展，仅做实际盘点、需求纠偏和 Phase 1 收尾。**Phase 1 本轮收尾已验证，整个 Issue 尚未完成**。保留已有 Phase 2 接入与 Phase 5 基础代码，不把保留等同于验证完成。

产品约束：配置和凭据就绪后，正常代码读写、Shell、依赖、测试及已配置工具自主执行。边界由确定性代码和现有 Sandbox 强制执行；不增加审批 Agent、逐工具授权、人工批准页面或必须点击继续的正常路径。复用 #604 临时出网与清理，不另建网络授权系统。部署配置不等于运行时审批；故障 blocked 不等于等待授权。本约束不改变 Codex 开发环境权限。

状态采用：`未开始`、`部分实现`、`已实现未验证`、`已验证`。某项局部测试通过不代表其所在阶段整体完成。`CLAUDE.md` 兼容为可选；可写子 Agent 和具体第三方集成示例属于未来范围。

## 最终实际变更规模

相对上述基线：0 个本地提交、0 个暂存文件；16 个已跟踪文件未暂存修改，12 个未跟踪新增文件，总计 28 个文件，新增 4,343 行、删除 197 行。未跟踪文件已计入，而不是只统计 `git diff --stat`。

| 分类 | 文件数 | 新增/删除行 | 主要来源 |
|---|---:|---:|---|
| 业务代码、配置及界面 | 16 | +1,923 / -192 | 完成协议、调度、恢复事务、worker 终态、部分仓库上下文及未接入能力定义 |
| 测试与手工浏览器 fixture | 8 | +2,146 / -5 | 并发/取消、SQLite 恢复、worker 三入口、仓库路径和边界矩阵；外部模型/服务使用替身 |
| 文档 | 4 | +274 / -0 | 两个 README 各一项、实施计划、验收表 |
| 锁文件 | 0 | 0 | 没有依赖/锁文件变更 |
| 生成产物 | 0 | 0 | 交付 diff 不含截图、日志、缓存、凭据；`.ruff_cache`、`.superpowers`、`.venv` 为忽略的本地工具产物 |

停止后续扩展时的快照是 +3,803/-197；收尾增量主要来自无进展检测纠偏及其复现测试、最终盘点文档。未为了降低行数删除实现或测试。

原 `develop` 工作区保持干净。工作均位于已有 `feature/issue-628-agent-harness` worktree。浏览器证据与阶段报告留在 `/tmp/sakura-628-*`，未混入业务文件。

## 阶段与真实接入情况

| 阶段 | 当前状态 | 执行接入与证据 |
|---|---|---|
| Phase 1 | 已验证 | worker → iteration_loop → fullstack_expert → tool_scheduler/ToolExecutor 已接入；checkpoint 结果/终态与旧会话迁移原子提交。最终相关回归 900 通过/1 跳过；import 修正后 146 项重跑通过；Ruff、独立复审与真实浏览器恢复/CSRF 操作通过 |
| Phase 2 | 部分实现 | repository_context、UseSkillTool、ToolContext 与 FullStack 批次预检已经接入真实执行链，不是闲置辅助代码。17 项新增测试通过；此前扩大回归 193 通过、1 个指导 checkpoint 用例失败；该兼容性回归本轮已修复并重跑。其余缺口未补，见后续清单 |
| Phase 3 | 未开始 | 尚无 Sakura 产品的 spawn/wait/cancel Agent 工具或子会话编排；Codex 开发子 Agent 不属于产品实现 |
| Phase 4 | 未开始 | 尚无 MCP 客户端运行时、配置、发现、schema 规范化或 MCP E2E；未添加相关依赖 |
| Phase 5 | 部分实现 | capability_policy 的 schema/profile/直接边界判断有 30 项基础单测；backend 中没有其他模块引用它，尚未接入工具、执行器或发布链。没有产品级 grant token/审批状态机 |
| Phase 6 | 未开始 | 尚无通用 Hooks、统一插件抽象或插件管理界面。Phase 1 的 checkpoint 回调和配置字段不能算作 Hooks/插件实现 |

## 已确认偏差与收尾范围

- 新增的 worker 映射曾将所有 blocked 置为 `waiting_human`，统一摘要还要求“人工处理”；本轮改用兼容的任务 `failed` 状态，并保留独立的 `current_phase=blocked`、会话 outcome 和具体原因。沿用原有可选故障恢复入口，不增加人工授权。
- 新增默认 64 轮/256 次工具总量上限会截停仍有进展的会话，且与现有提示约定冲突。本轮改为默认无限总量、正数表示显式预算，使用确定性的连续无变化保护。
- Phase 2 无内容的仓库快照仍被追加进 checkpoint，干扰指导故障路径；本轮只修正这个直接影响 Phase 1 的接入问题，不继续完成 Phase 2。
- 没有代码证据表明新增了审批 Agent、授权弹窗或人工批准 API。两处仓库规则预检分别位于整批调度前和工具执行器内，存在重复读取，但承担批次上下文投递与执行边界检查；目前不将它们定性为无用的重复授权系统。
- 未发现本任务改动触及无关业务模块；测试包含可复现的故障、真实文件/并发和数据库事务场景，未发现为了数量而重复同一断言的确证，不因行数删除测试。
- 工作区屏障的实现范围是同一进程事件循环；未核实跨进程工作区租约，不声称跨 worker 互斥已获得保证，本轮不新增分布式调度。

## 保留但未完成的后续工作

以下均来自当前代码调用链或已运行结果；除已记录的指导回归外，本轮不扩展其修复：

1. `FullStackExpertAgent._execute` 无条件发现仓库 Skills，尚未接入 `agent_team_skills_enabled` 开关；旧路径 `submission_context.load_skills_context` 会读取该开关。
2. `_restore_skill_workflows` 使用当前元数据重建限制，尚未持久化历史限制上限。`ctx.repository_instructions.update` 跨作用域累计内容，但 `total_bytes` 只约束单次收集。
3. 预检只提取 `file_path/path/directory`，尚未覆盖路径数组；规则更新、删除、跨作用域刷新和恢复的一致性仍不完整。
4. DB Skill 读取改用 64 KiB 默认上限，选择器改为严格工具名；对既有较大 Skill 或其他选择器格式的兼容性尚未建立证据。目录列表仍会读取主 Skill 正文，列表/缓存边界尚未完成验证。
5. Phase 2 五个配置项尚缺双语目录、完整文档和独立审查；本轮不把配置出现在页面等同于阶段完成。
6. 未实现 context compaction 的逐次审计、前后 token 记录和保留任务/工具/错误的审计证明；未实现 Subagents、MCP、Hooks 或统一插件管理。

下一项明确工作是另轮恢复 Phase 2 的存量接入收尾，先补 Skills 开关及恢复限制上限的行为测试；本轮报告交付后停止扩展。

## 逐项要求

| ID | 验收要求 | 实现位置 | 验证证据 | 状态 |
|---|---|---|---|---|
| C1 | 普通文本不能结束为成功 | `fullstack_expert.py` | `test_agent_harness_runtime.py` | 已验证 |
| C2 | 成功由运行时终态协议确认 | `tools/base.py`、`tools/finish_task_tool.py` | 伪造终态与失败 finish 回归 | 已验证 |
| C3 | 正常成功统一经过 finish_task | 同 C2、`iteration_loop.py` | 正常 finish 与恢复 finish 回归 | 已验证 |
| C4 | 取消、阻塞、不可恢复错误分别持久化 | `fullstack_expert.py`、`conversation_checkpoint.py`、worker | `test_agent_harness_worker_outcomes.py` | 已验证 |
| C5 | 连续无进展保护与可选显式预算 | `runtime_limits.py`、`fullstack_expert.py` | 无工具回复、恢复预算、调用上限回归 | 已验证 |
| P1 | 安全读取能够有界并行 | `tool_scheduler.py`、`tools/base.py` | 同批读取重叠与并发上限测试 | 已验证 |
| P2 | 同一工作区写入互斥 | 同 P1 | 跨执行器屏障、取消中的线程写入测试 | 已验证 |
| P3 | Shell/Git 写入与 finish 独占执行 | 同 P1 | 串行屏障测试 | 已验证 |
| P4 | 并行调用的 checkpoint 状态一致 | `conversation_checkpoint.py` | SQLite 事务、顺序与原子结果测试 | 已验证 |
| P5 | 取消传播并等待并行任务清理 | `tool_scheduler.py` | 任务取消/事件取消与写线程清理测试 | 已验证 |
| R1 | 根目录 AGENTS.md | `repository_context.instructions_for`、`FullStackExpertAgent._execute` | 17 项仓库测试中的真实文件读取 | 已验证 |
| R2 | 目标目录规则继承与覆盖顺序 | `repository_context.py`、`ToolExecutor.repository_requirements` | 祖先/兄弟目录与批次投递通过；路径数组、刷新与累计范围尚未补齐 | 部分实现 |
| R3 | .sakura/AGENTS.md 与 rules | `RepositoryContext.instructions_for` | Sakura 规则与排序单测通过 | 已验证 |
| R4 | 仓库规则保持低于系统的信任级别 | `FullStackExpertAgent._repository_message` | 模拟模型/压缩边界验证 user 角色，不是 system | 已验证 |
| R5 | 仓库提示注入无法扩大权限或越界 | `repository_context.py`、`tools/base.py` | 穿越/符号链接/FIFO/硬链接拒绝通过；完整恢复上限与攻击链证据未齐 | 部分实现 |
| S1 | 主 Agent 按需创建只读子 Agent | — | — | 未开始 |
| S2 | 子 Agent 独立上下文与会话 | — | — | 未开始 |
| S3 | 子 Agent 数量/资源有上限 | — | — | 未开始 |
| S4 | 子 Agent 无法修改工作区 | — | — | 未开始 |
| S5 | 等待、取消与结构化结果汇总 | — | — | 未开始 |
| S6 | 父任务取消递归清理子 Agent | — | — | 未开始 |
| M1 | 可配置并连接 MCP 服务 | — | — | 未开始 |
| M2 | 动态发现并规范化 MCP 工具 | — | — | 未开始 |
| M3 | MCP 工具经过统一访问边界检查 | — | — | 未开始 |
| M4 | MCP 保持网络/秘密/工作区/系统访问策略边界 | — | — | 未开始 |
| M5 | MCP 不可用时拒绝调用且不损坏核心流程 | — | — | 未开始 |
| K1 | 权限覆盖网络以外的能力 | `capability_policy.py`，无生产调用方 | 30 项基础单测；未改变执行链权限判定 | 部分实现 |
| K2 | 明确 capability schema 与 profiles | `Capability`、`PermissionProfile`、`PolicySnapshot` | 基础矩阵验证通过；配置/运行时集成未做 | 部分实现 |
| K3 | 模型只请求抽象能力 | 既有 `network_policy.py` / Shell 参数 | #604 边界保留并通过相关回归；统一接口尚未接入 | 部分实现 |
| K4 | 临时能力在执行结束后回收 | 复用 #604 的 `sandbox_client.py` / one-shot runner | 本轮没有另建 grant/token 系统；统一任务范围集成未做 | 部分实现 |
| K5 | 能力决策和生命周期审计 | 既有 #604 日志与未接入的 `CapabilitySession.check` | 直接判断审计单测通过；统一持久化集成未做 | 部分实现 |
| H1 | before_tool / after_tool | — | — | 未开始 |
| H2 | before_finish 能阻止不合格的成功 | — | — | 未开始 |
| H3 | hook 由系统策略控制 | — | — | 未开始 |
| H4 | 仓库内容不能授权高权限 hook | — | — | 未开始 |
| G1 | Sandbox 安全边界回归 | 既有 runner/sandboxd，底层未修改 | Agent/Sandbox 相关本地回归通过；未跑真实 Docker 部署 | 已验证 |
| G2 | checkpoint / resume 保持可用 | checkpoint、Agent、worker、恢复路由 | 旧会话原子迁移、状态恢复、异常/取消与浏览器故障恢复通过 | 已验证 |
| G3 | human guidance 队列兼容 | 既有 guidance 接入、空上下文修正 | 原失败用例与完整相关回归重跑通过 | 已验证 |
| G4 | context compression 兼容 | 既有压缩桥、Phase 2 user 上下文增强 | 共享路径回归通过；累计上下文边界及压缩审计未补 | 部分实现 |
| G5 | Skills 按需加载保持可用 | `UseSkillTool`、仓库发现 | 已有直接测试通过；开关、恢复上限与既有格式兼容性未齐 | 部分实现 |
| G6 | 接入 #604 与 #627，保持职责边界 | 既有网络能力与依赖 bootstrap | 旧实现回归通过；新能力模型尚未集成 | 部分实现 |
| G7 | 单元、集成、端到端验证与实际 UI 操作 | `tests/test_agent_harness_*`、`tests/harness_ui_app.py` | Phase 1 已验证；完整 #628 跨阶段 E2E 未实现 | 部分实现 |

## 正文与 Phase 6 的额外明确要求

| ID | 要求 | 实现/验证记录 | 状态 |
|---|---|---|---|
| X1 | .agents/skills 与 .sakura/skills 自动发现 | `RepositoryContext.discover_skills` 已接入；发现与优先级单测通过，开关缺口保留 | 部分实现 |
| X2 | Skill 元数据先展示，正文按需读取 | 元数据发现和 use_skill 正文路径已测；list_files 路径仍会读取正文 | 部分实现 |
| X3 | Skill allowed_tools 只收窄权限 | 当前运行中的收窄/end_skill 已测；恢复的历史上限未持久化 | 部分实现 |
| X4 | 每次压缩审计与前后 token 估计 | — | 未开始 |
| X5 | 审计保留的任务、近期工具、未解决错误 | — | 未开始 |
| X6 | 恢复检查终态/工具状态一致性 | Phase 1 原子迁移、状态恢复及复审通过 | 已验证 |
| X7 | 明确崩溃后 pending 调用恢复策略 | 读取允许重试；不确定写入阻塞，不自动重放 | 已验证 |
| X8 | 全部命名生命周期事件 | session_start、before/after_model、before/after_tool、before/after_write、before/after_finish、task_failed、task_cancelled | 未开始 |
| X9 | 受控 formatter / lint / test / 仓库校验 hook | — | 未开始 |
| X10 | Skills、MCP、Hooks 统一插件抽象 | — | 未开始 |
| X11 | WebUI 插件管理、访问控制、双语及真实交互验证 | — | 未开始 |

## 已执行的阶段验证

- 基线：208 项相关测试通过。
- Phase 1：扩展回归 457 项通过；独立审查发现的三项问题均已修复，新增 8 项复现用例通过，修复后相关回归 245 项通过，Ruff、格式与 diff 检查通过，复审批准。本阶段 C1–C5、P1–P5、X6–X7 通过；跨阶段回归在最终集成后复验。
- Phase 1 浏览器：实际模板中点击阻塞任务的恢复按钮，经真实路由/CSRF 校验转为 queued/resuming，审计与恢复提交各一次；无效 CSRF 返回 403。数据库、认证和后台提交使用本地测试替身，没有生产或 GitHub 副作用。修正 fixture 辅助接口后，正常操作无控制台错误；现有 Tailwind CDN 警告仍存在。
- 能力基础模块：30 项单测通过；未接入实际执行链，不能据此宣称 Phase 5 完成。

以上历史结果不替代本轮最终验证；本轮结果与范围列于下文。

## 本轮最终验证

以下命令均在 `/home/firefly/.codex/worktrees/issue-628-agent-harness/Sakura-AI` 执行。没有执行真实模型调用、GitHub 发布或生产部署。

```bash
UV_CACHE_DIR=/tmp/sakura-628-uv-cache uv run --no-sync python -m pytest -q \
  -o cache_dir=/tmp/sakura-628-pytest-final \
  tests/test_agent*.py tests/test_sakura_agent_conversation.py \
  tests/test_config_basic_settings.py tests/test_default_configs_source.py \
  tests/test_webui_unified_config.py
```

结果：**900 passed, 1 skipped，18.37 秒**。跳过的既有 `test_sticky_dir_foreign_owner_regression` 需要 root 与 `fs.protected_regular>=2`；单独使用 `pytest -q -rs` 复核了该跳过原因，没有更改宿主权限或系统设置。

```bash
UV_CACHE_DIR=/tmp/sakura-628-uv-cache RUFF_NO_CACHE=true \
  uv run --no-sync python run_ruff.py --check

UV_CACHE_DIR=/tmp/sakura-628-uv-cache uv run --no-sync python -m pytest -q \
  -o cache_dir=/tmp/sakura-628-pytest-final \
  tests/test_agent_tool_framework.py tests/test_agent_skills.py \
  tests/test_agent_repository_context.py tests/test_agent_harness_runtime.py

git diff --check
```

- Ruff：**All checks passed**。首次全变更检查发现两处本次新增 import 排版错误，已仅修正 import；没有借格式整理扩展 Phase 2。一次包装命令使用 `RUFF_NO_CACHE=1` 被 Ruff 参数解析拒绝，改为布尔值 `true` 后通过。
- import 修改后受影响的测试：**146 passed，1.49 秒**。这与上面的 900 项有重叠，不能相加当作独立测试数量。
- 独立限定复审：Phase 1 收尾批准，没有确认的直接阻断问题；不包含对 Phase 2 的完整验收。
- 最终浏览器：通过 `tests/harness_ui_app.py` 启动 localhost fixture，实际点击既有失败任务恢复按钮。任务由 `failed/current_phase=blocked` 变为 `queued/resuming`，仅一次审计和一次恢复提交；无效 CSRF 返回 403 且没有新增提交。正常操作无控制台错误，仅有既有 Tailwind CDN 警告；测试服务已通过退出接口关闭。

验证性质明确区分如下：

- **真实本地执行**：临时文件读写、asyncio 并发/取消、后台写线程清理、SQLite 提交/回滚、浏览器与实际 FastAPI/Jinja/CSRF 路径。
- **模拟边界**：模型响应、外部 GitHub/供应商/服务使用替身；浏览器 fixture 的身份、数据库与后台调度使用替身。SQLite checkpoint 用同步 Session 的异步测试适配器，不是线上 MySQL 并发测试。
- **未验证**：真实模型任务完成率、真实 Docker/Sandbox 部署、生产 GitHub 发布、MySQL 跨进程竞争；Phase 2 列明的缺口及 Phase 3–6 完整功能。

无进展检测比较规范化的工具/参数/结果和新上下文证据，排除调用 ID；它不是语义裁判。时间戳、随机输出或其他不同内容可能看起来是新证据，因此不声称能够证明所有语义空转均被终止。默认总量预算为 0，显式正数预算仍可设置；连续无新证据阈值默认 8。

最终截图位于 `/tmp/sakura-628-final-ui/failed-state.png`，测试输出在 `/tmp/sakura-628-wrap-final-tests.log`、`/tmp/sakura-628-wrap-import-regression.log`、`/tmp/sakura-628-wrap-ruff-final.log`。这些本地证据没有作为源代码或凭据提交。

本轮保留未提交工作：Phase 1 与尚未完成的 Phase 2 共用部分文件，不把部分实现混成已完成阶段提交。没有推送、创建/合并 PR 或关闭 Issue。**这是阶段性交付，不能声明整个 #628 已解决。**
