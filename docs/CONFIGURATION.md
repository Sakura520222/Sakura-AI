# 配置参考

> Sakura AI 全部配置项的位置、键名与说明。普通动态配置可即时生效；应用时区等重启键保存后必须重启。

← [文档索引](README.md) · [README](../README.md)

---

## 配置优先级

- **全局配置**：数据库 `app_config`（WebUI 管理） > Settings 默认值
- **用户偏好**：UserConfig > `app_config` > Settings 默认值
- **策略/标签节键**：审查策略与标签定义以节键（`strategy.*` / `label.*`）存于 `app_config`，由 WebUI「统一配置」页（`/config`）的节表单保存；读取时与内置默认深度合并——用户改动的叶子生效，升级新增的默认叶子自动出现（「删除」单个叶子不受支持，需整节重置回默认）
- **本地文件**：`config/` 目录仅剩 `connection.json`（Setup Wizard 数据库连接引导用）；旧版 `strategies.yaml` / `labels.yaml` 已废弃，首次启动检测到旧文件时会按节对比、仅导入与默认有差异的内容

> **动态配置**：通过 WebUI「统一配置」页（`/config`）修改的普通动态配置项即时生效。AI 运行时的账号、端点、凭据与模型仅由「AI 配置」页（独立页面，不并入统一页）的账号和角色绑定提供；调用策略、RAG、Web 搜索、代码索引和仓库互助仍由各自配置分组管理。

## 全局配置页 `/config`

超级管理员的全部非 AI 配置收敛到单页 `/config`：左侧锚点导航 + 分组卡片，平铺动态键与策略/标签节表单同页呈现。

| 分区 | 内容 |
|---|---|
| 平铺键组（一个表单整体提交 `/config/general/save`） | 审查任务基础（`review_basic`，含 `protocol_repair_max_attempts`）、Web 搜索（`web_search`）、Issue 分析、Agent 专家团队，以及其余全部 DYNAMIC 动态配置组 |
| 策略节表单（各自提交 `/config/strategies/save`） | `strategy.*` 七节：策略分级（quick/standard/deep/large，分级条件可配置）、文件过滤、上下文增强（含 Sakura 记忆嵌套节卡片）、审查政策、Issue 分析分类、PR 依赖图、PR 总结模板 |
| 标签节表单 | `label.*` 三节：标签定义、推荐设置、冲突规则 |

- 旧 URL 均 302 重定向到 `/config` 对应锚点：`/config/general` → `#section-basic`、`/config/strategies` → `#section-strategy-strategies`、`/config/labels` → `#section-label-definitions`。
- 旧「Agent 专家团队」页的配置面板已并入 `/config`（团队页仅保留任务/工作区功能）；「AI 配置」「系统核心配置」页保持独立（A1）。
- 保存链路不变：平铺键走 `/config/general/save` 通用逐键 upsert 循环，节表单走各自节保存端点；均带 super_admin + CSRF 校验与管理员审计。
- `protocol_repair_max_attempts` 的重复保存路径已合一（此前同时挂在 review_basic 与 issue_analysis 两组）；`pr_dependency_graph_mode` 由策略节表单单键单写，旧平铺键仅保持兼容读取。

## 已移除的配置项

以下键在第二阶段已从 Settings、动态配置注册与消费点中删除；**DB 旧行惰性保留、种子自动消失，无迁移**。旧备份（v1/v2）中仍含这些键时导入会被宽容跳过（见「配置备份」）。

### 删除检查/循环上限 → 真·无限制

| 键 | 移除后行为 |
|---|---|
| `issue_max_directory_depth` | 原为死配置（无消费点），直接删除 |
| `agent_team_max_lines_changed` | 原为死配置（无消费点），直接删除 |
| `agent_team_max_tool_rounds` / `agent_team_reviewer_max_tool_rounds` | 全栈专家与审查专家工具循环不设轮次与时长上限，依赖模型自然停止（无工具调用即交付）与手动取消 |
| `context_enhancement.max_tool_iterations`（节叶） | PR 审查工具循环不设轮次上限，时长由 `review_timeout_seconds` 软超时兜底 |
| `issue_max_tool_iterations` | Issue 分析工具循环不设轮次与时长上限（删除了直查 DB 的旁路读取），依赖模型自然停止（无工具调用即交付）；同一 `review_timeout_seconds` 软超时也适用于 Issue 分析与仓库扫描 |
| `agent_team_max_files_changed` | 修改文件数硬检查删除（顺带删除 pr_service 中硬编码 >20 文件检查），Agent 不再因改动规模被拒 |
| `fetch_url_max_calls_per_session` | 会话抓取计数删除，不再限制抓取次数 |
| `issue_max_analysis_versions` | 分析版本永不归档，行自然增长 |
| `max_file_count`（1000）/ `max_line_count`（50000） | PR 超限直接拒绝的硬性门删除，超大 PR 照常进入流程，由策略分级决定审查深度 |

### 输出 max_tokens → 折叠到全局 `ai_max_tokens`

| 键 | 移除后行为 |
|---|---|
| `incremental_history_summary_max_tokens` | 历史摘要输出上限跟随「AI 配置」页全局 `ai_max_tokens` |
| `star_aid_summary_max_tokens` | 仓库互助 AI 摘要输出上限跟随全局 `ai_max_tokens` |

### 固定 0（机制已支持 0=不限）

| 键 | 移除后行为 |
|---|---|
| `issue_max_comments_in_context` | 评论条数不限制，全部纳入上下文 |
| `scan_max_tokens_per_repo` | 单仓库扫描 Token 预算固定为 0（不限） |
| `star_aid_summary_readme_budget` | README 全文传入，永不截断 |

### 行为开关 → 固定开启

| 键 | 移除后行为 |
|---|---|
| `issue_auto_comment` | 分析完成后固定自动发布分析报告评论 |
| `auto_index_pr_changes` | PR 变更自动索引固定开启（总开关 `enable_code_index` 保留） |
| `scan_auto_create_issue` | 扫描发现（total_findings > 0）固定自动创建 Issue 报告 |
| `enable_incremental_history_context` | 增量审查历史上下文固定启用 |
| `incremental_history_max_reviews` | 历史审查记录全量查询（不再限制轮数） |

### Sakura 平铺键 → 合并入 `strategy.context_enhancement.sakura_memory` 嵌套节

`sakura_memory_enabled`、`sakura_reflection_enabled`、`sakura_issue_reflection_enabled`、`sakura_consolidation_interval`、`sakura_max_memory_chars`、`sakura_max_sakura_chars`、`sakura_auto_init`、`sakura_auto_create_subdirs`、`sakura_consolidation_partial_commit`、`sakura_knowledge_extraction_enabled`、`sakura_extraction_min_reflections` 共 11 键——单一事实源改为节存储嵌套节（见「项目记忆系统」节，全局配置页「上下文增强」卡片内编辑）；`sakura_extraction_max_iterations` / `sakura_consolidation_max_iterations` 2 键删除轮次上限（依赖模型自然停止，不设时长上限）。

## 配置备份

WebUI「配置管理 → 备份」支持按节导出/恢复 `app_config`：

| 备份节 | 内容 |
|---|---|
| `global` / `ai` / `system` | 动态配置三节（原有行为） |
| `strategy` | 全部 `strategy.*` 节键（审查策略、文件过滤、上下文增强、审查政策、Issue 分析、PR 总结与依赖图模板） |
| `label` | 全部 `label.*` 节键（标签定义、推荐设置、冲突规则） |

恢复为事务式精确节替换：节内多出的键删除、缺失的键跳过，未导出的节不受影响；数据库连接串等部署期字段受保护、拒绝导入。

**宽容恢复**：旧版备份（v1/v2）中已被移除的历史配置键在导入时自动跳过并记录 warning，不阻断整份导入（例如含 `issue_max_tool_iterations`、`sakura_memory_enabled` 等旧平铺键的备份可完整恢复）；键仍存在但节归属不符时视为数据损坏、报错。

## 时间与应用时区

超级管理员可在「系统核心配置 → 应用配置」修改 `app_timezone`：

- 默认值 `system`：每次进程启动时读取运行环境的系统 IANA 时区并冻结，例如 `Asia/Shanghai` 或 `America/New_York`。
- 也可填写明确的 IANA 名称或 `UTC`。不接受 `CST`、`EST`、任意 `UTC+08:00` 等歧义缩写/固定 offset。
- 保存后必须重启应用；当前进程不会半热切换，所有进程在重启后使用同一解析结果。
- 该设置只影响 Sakura AI 的日志正文、WebUI、Telegram/CSV 等用户可见日历显示，不修改宿主机物理时钟、NTP 或系统时区，也不会执行系统命令。
- 数据库、API、SSE、配置/用户备份和 updater 协议中的时间点统一为 aware UTC，机器可读格式为 RFC3339 `Z`；超时、deadline、uptime 使用 monotonic 时钟。
- `datetime-local` 表单值属于应用时区；夏令时不存在的时间会被拒绝，重复小时必须选择较早或较晚 offset。

---

## AI 模型与账号

| 位置 | 键名 / 入口 | 说明 |
|---|---|---|
| WebUI「AI 配置」 | 账号管理 | 保存 OpenAI、Anthropic、Gemini、DeepSeek、Qwen、GLM、MiniMax、Kimi、Grok、Mistral、聚合网关、本地模型或自定义兼容账号（provider、protocol、region、base URL、API Key、默认模型） |
| WebUI「AI 配置」 | 角色绑定 | 为 `main`、`summary`、`agent_team` 配置主账号与故障转移链；模型列表可被发现并持久化，标签可快速选择 |
| WebUI 模型高级配置 | 每模型独立 | 上下文窗口、最大输出、图片多模态、思考模式/等级、temperature/top_p/top_k 能力/默认值 |
| WebUI「AI 配置」 | `ai_api_timeout_seconds` | 单次请求超时 |
| WebUI「AI 配置」 | `ai_api_total_timeout_seconds` | 一次 AI 调用重试循环的最长总耗时 |

**endpoint 约束**：内置远程账号仅允许官方 HTTPS endpoint；`custom` / `custom-anthropic` 可配置 HTTPS 公网 endpoint，以及 HTTP/HTTPS 本机或私网兼容 endpoint。

**角色跟随规则**：`summary` 与 `agent_team` 仅在绑定明确为 `account="main"` 或 `model="follow"` 时跟随主角色；缺少绑定、禁用账号、无效 endpoint 或空候选链会明确失败，不会回退到旧配置。

**历史旧键**：历史 `openai_*`、`summary_*`、`ai_provider` 和旧 Agent Team 供应商键可保留在数据库中，但系统不会读取、写入、迁移或将其作为回退来源。

详见 [模型上下文管理](MODEL_CONTEXT_FEATURE.md)。

---

## PR 审查

| 位置 | 键名 | 说明 |
|---|---|---|
| WebUI 配置管理 | `enable_auto_review` | PR webhook（opened/synchronize/reopened）是否自动入队；关闭后仍可命令或手动触发 |
| WebUI 审查策略 | 四种策略 | 快速 / 标准 / 深度 / 大 PR |
| WebUI 审查策略 | 文件过滤 | 跳过的文件扩展名和路径 |
| WebUI 审查策略 | `review_policy` | 审查批准阈值与仓库级覆盖 |
| WebUI 配置管理 | `enable_pr_summary` | PR 变更自动总结 |
| WebUI 配置管理 | `enable_inline_comments` | 是否在 PR diff 上发布行内评论，默认开启 |
| —（固定开启） | `enable_incremental_history_context`（已移除） | 增量审查历史，AI 自动学习历史审查记录；不再可关闭，历史记录全量读取，摘要输出上限跟随全局 `ai_max_tokens` |
| WebUI 配置管理 | `review_price_per_1k_prompt` / `review_price_per_1k_completion` | Token 消耗与成本追踪 |

### PR 依赖图

| 位置 | 键名 | 说明 |
|---|---|---|
| WebUI 配置管理 | `enable_pr_dependency_graph` | 总开关 |
| 全局配置页「PR 依赖图」节表单 | `pr_dependency_graph_mode` | `ai` 模型分析 / `static` 静态 import 解析（更省成本）；旧平铺键仅保持兼容读取 |
| WebUI 配置管理 | `pr_dependency_graph_max_nodes` | 最大节点数 |
| WebUI 配置管理 | `pr_dependency_graph_max_files` | 最大文件数 |

详见 [PR 功能指南](PR_FEATURES_GUIDE.md)、[审查批准功能](APPROVAL_FEATURE_SUMMARY.md)、[审查协议规范](PR_REVIEW_PROTOCOL.md)。

---

## Check Runs 与外部 CI

| 位置 | 键名 | 说明 |
|---|---|---|
| WebUI 配置管理 | `enable_check_runs` | 审查进度同步到 GitHub Checks 面板（默认开启，需 `checks:write` 权限） |
| WebUI 配置管理 | `enable_analysis_check` | 是否创建副 Analysis Check |
| WebUI 配置管理 | `enable_findings_check` | 是否创建副 Findings Check |
| WebUI 配置管理 | `analysis_min_interval_sec` | Analysis 快照写入最小间隔，避免高频更新烧 API 配额 |
| WebUI 审查策略 | `context_enhancement.ci_failure_injection` | 外部 CI 失败注入：开关、记录保留天数、单次审查最多失败记录数、每条失败最多 annotations 数 |

**Check external_id 格式**：`sakura-ai:v1:<review_job_id>:<check_kind>`。跨进程恢复优先读 DB 持久化的 `check_run_id`，缺失时按 `head_sha + name` 列举兜底。建议只将主 Check `Sakura AI Review` 纳入分支保护 required status check——副 Check 可能不出现，配为 required 会阻塞合并。

**外部 CI 注入依赖**：GitHub App 需订阅 `check_run` / `workflow_job` webhook，并授予 Checks 与 Actions 读取权限。

---

## 上下文治理与工具

| 位置 | 键名 | 说明 |
|---|---|---|
| WebUI「AI 配置」 | 每模型上下文窗口 | 按模型设置；自动压缩策略同页配置 |
| WebUI「AI 配置」 | `enable_context_compression` | 自动压缩开关 |
| WebUI「AI 配置」 | `context_compression_threshold` | 压缩触发阈值 |
| WebUI 配置管理 | `enable_ai_tools` | AI 工具开关 |
| —（无上限） | `context_enhancement.max_tool_iterations`（已移除） | PR 审查工具循环不设轮次上限，依赖模型自然停止，整体时长由 `review_timeout_seconds` 软超时兜底 |
| WebUI 配置管理 | `web_search_provider` | `duckduckgo`（免费，使用 `duckduckgo-search`）/ `tavily`（高级） |
| WebUI 审查策略 | `context_enhancement.search_in_files` | 跨文件搜索：GitHub Search API 优先策略、上下文行数、最大结果数 |
| WebUI 审查策略 | `context_enhancement.git_tools` | Git 信息工具：默认分支、提交返回数量 |

> 当初始 diff 过大时，审查自动使用 compact diff 工具模式；历史上下文由当前候选模型 AI 摘要压缩。上下文窗口按模型配置（自动发现优先，可手动覆盖），替代旧的全局单值。

详见 [模型上下文管理](MODEL_CONTEXT_FEATURE.md)。

---

## Issue 分析

| 位置 | 键名 | 说明 |
|---|---|---|
| 标签配置·推荐设置（PR 与 Issue 统一） | `label.recommendation.auto_create` | 自动创建标签开关（原 `issue_auto_create_labels` 已并入） |
| 标签配置·推荐设置（PR 与 Issue 统一） | `label.recommendation.confidence_threshold` | 标签置信度阈值（原 `issue_confidence_threshold` 已并入） |
| WebUI 配置管理 | `issue_auto_assign` | Issue 自动指派开关 |
| WebUI 配置管理 | `issue_assignee_confidence_threshold` | 指派置信度阈值 |
| WebUI 配置管理 | `max_concurrent_issues` | 同时进行的最大 Issue 分析任务数，超出排队 |
| WebUI 配置管理 | `issue_auto_rewrite_title` | Issue 标题自动改写 |
| WebUI 配置管理 | `enable_semantic_issue_linking` | 语义 Issue 关联开关 |
| WebUI 配置管理 | `semantic_issue_similarity_threshold` | 语义相似度阈值 |

---

## 标签推荐

| 位置 | 键名 | 说明 |
|---|---|---|
| 标签配置·推荐设置（PR 与 Issue 统一） | `label.recommendation.enabled` / `confidence_threshold` | 开关与置信度 |
| 标签配置·推荐设置（PR 与 Issue 统一） | `label.recommendation.auto_create` | 自动创建标签（原 `issue_auto_create_labels` 已并入） |

---

## Agent 专家团队

| 位置 | 键名 | 说明 |
|---|---|---|
| 全局配置页「Agent 专家团队」组 | `agent_team_enabled` | 总开关 |
| 全局配置页「Agent 专家团队」组 | `agent_team_workspace_root` | 工作区根目录 |
| 全局配置页「Agent 专家团队」组 | `agent_team_repo_allowlist` | 仓库白名单（普通用户仅能操作自己名下且匹配的仓库） |
| 全局配置页「Agent 专家团队」组 | `agent_team_enable_context_compression` 等 | 上下文压缩 |
| —（无上限） | `agent_team_max_tool_rounds` / `agent_team_reviewer_max_tool_rounds`（已移除） | 全栈专家与审查专家工具循环不设轮次与时长上限，依赖模型自然停止与手动取消 |
| —（无上限） | `agent_team_max_files_changed` / `agent_team_max_lines_changed`（已移除） | 修改文件数/行数不再受限（原硬检查已删除，含 PR 服务 >20 文件硬编码检查） |
| 全局配置页「Agent 专家团队」组 | `agent_team_auto_install_deps` | 自动安装依赖 |
| 全局配置页「Agent 专家团队」组 | `agent_team_execution_backend` | `sandbox` 为默认执行后端；`local` 只允许显式源码开发模式，镜像或未知部署模式会 fail-closed |
| 全局配置页「Agent 专家团队」组 | `agent_team_pr_closed_loop_enabled` | PR 审查闭环开关 |
| 全局配置页「Agent 专家团队」组 | `agent_team_max_iterations_per_task` | 单任务最大自动迭代次数 |
| 全局配置页「Agent 专家团队」组 | `agent_team_pr_review_pass_score` | PR 审查通过分数线 |
| 全局配置页「Agent Skills」组 | `agent_team_skills_enabled` | Agent 是否可加载技能 |
| 全局配置页「Agent Skills」组 | `agent_team_skills_root` | 技能本地存储根目录 |

> Agent Team 的 AI 调用固定使用 `agent_team` 角色绑定，上下文压缩使用 `summary` 角色绑定，**不支持**独立 endpoint、API Key 或模型配置。普通用户入口校验仓库归属和 `agent_team_repo_allowlist` 并消耗 Agent 配额；`/agent` 评论可从已分析 Issue 或扫描报告 Issue 创建任务。模型驱动的 shell、grep 和依赖 hook 使用 `AGENT` / `DEPENDENCY` profile 进入 sandboxd；clone、fetch、worktree、commit 和 push 保持为固定 argv 的可信 Git 控制面。

下列设置属于部署安全边界，不通过 WebUI 或数据库动态修改：

| 环境变量 / Settings | 默认 / 来源 | 说明 |
|---|---|---|
| `AGENT_TEAM_SANDBOX_SOCKET` / `agent_team_sandbox_socket` | `/run/sakura-ai-sandbox/sandboxd.sock` | 独立 sandboxd UDS；不得与 updater socket 共用 |
| `AGENT_TEAM_SANDBOX_RUNTIME` | `docker` | Backend health admission 期望的运行时 |
| `AGENT_TEAM_SANDBOX_RUNNER_IMAGE_DIGEST` | Release 的 `agent-sandbox-manifest.json` | runner 的不可变镜像引用；生产必须是 `name@sha256:...` |
| `AGENT_TEAM_SANDBOX_EXPECTED_INSTANCE_ID` | `start.sh` 持久化并注入 | 绑定当前受管 sandboxd 实例，缺失或不匹配即拒绝 Agent 执行 |
| `AGENT_TEAM_SANDBOX_EXPECTED_WORKSPACE_ROOT` | `start.sh` 计算的宿主绝对路径 | 仅作为 daemon 身份；Web 实际访问路径仍是 `/app/workplace` |
| `AGENT_TEAM_SANDBOX_TIMEOUT_SECONDS` | 900 | Backend 请求上限；daemon 仍使用更严格的服务端 clamp |
| `AGENT_TEAM_SANDBOX_MAX_OUTPUT_BYTES` | 1 MiB | stdout + stderr 合计字节上限 |

生产 sandboxd 的镜像、runner 镜像、Docker 参数、网络、mount、UID/GID、capabilities、资源限制和宿主 workspace root 均由部署侧控制，不能由模型请求或 Web 动态配置覆盖。Web 与 runner 不挂载 Docker socket；只有独立 sandboxd 容器持有该 socket。

详见 [Agent Skills 实现](agent-skills-python-implementation.md)、[Agent 文件工具实现](agent-file-tools-python-implementation.md)。

---

## 项目记忆系统

| 位置 | 键名 | 说明 |
|---|---|---|
| 全局配置页「上下文增强」卡片 | `context_enhancement.sakura_memory.enabled` | 记忆系统总开关 |
| 全局配置页「上下文增强」卡片 | `context_enhancement.sakura_memory.reflection.enabled` | 审查后反思 |
| 全局配置页「上下文增强」卡片 | `context_enhancement.sakura_memory.issue_reflection.enabled` | Issue 分析后反思 |
| 全局配置页「上下文增强」卡片 | `context_enhancement.sakura_memory.consolidation.interval` | 合并触发的反思轮数（默认 5） |
| 全局配置页「上下文增强」卡片 | `context_enhancement.sakura_memory.consolidation.max_memory_chars` / `max_sakura_chars` | memory.md / SAKURA.md 最大字符数（默认 2000 / 3000） |
| 全局配置页「上下文增强」卡片 | `context_enhancement.sakura_memory.consolidation.partial_commit` | 一个文件生成失败时是否仍提交另一个成功生成的文件 |
| 全局配置页「上下文增强」卡片 | `context_enhancement.sakura_memory.initialization.auto_init` | 自动初始化 `.sakura/` 目录 |
| 全局配置页「上下文增强」卡片 | `context_enhancement.sakura_memory.directory_convention.auto_create_subdirs` | 自动创建 rules/docs/plans 子目录 |
| 全局配置页「上下文增强」卡片 | `context_enhancement.sakura_memory.knowledge_extraction.enabled` / `min_reflections` | 自动知识提取开关与触发间隔（默认 15 轮反思） |
| 全局配置页「上下文增强」卡片 | `context_enhancement.sakura_memory.reflection.max_comments` / `max_changed_files` / `max_new_commits` | 反思 prompt 包含的最大评论/变更文件/新增提交条数（默认 30/30/20） |

> 反思、合并与知识提取由 `main` 或 `summary` 角色绑定决定实际账号和模型，不支持在该功能中另配凭据或模型。评论正文与 PR 描述完整传入、不截断。提取/合并 Agent 的工具循环不设轮次与时长上限（依赖模型自然停止）。WebUI「Sakura 记忆管理」页面支持查看 / 编辑 / 删除记忆文件、手动触发合并和知识提取。

详见 [项目记忆系统使用指南](SAKURA_MEMORY_GUIDE.md)。

---

## 仓库互助

| 位置 | 键名 | 说明 |
|---|---|---|
| WebUI 配置管理 | `star_aid_enabled` | 全局入口开关（关闭后页面只读） |
| WebUI 配置管理 | `star_aid_auto_star_enabled` | 是否执行自动点星（关闭后仅保留手动点星与展示） |
| WebUI 配置管理 | `star_aid_scheduler_enabled` | 后台调度器 |
| WebUI 配置管理 | `star_aid_min_interval_minutes` / `star_aid_max_interval_minutes` | 单成员两次自动点星的随机间隔区间 |
| WebUI 配置管理 | `star_aid_batch_size` | 每轮调度最大处理成员数 |
| WebUI 配置管理 | `star_aid_user_daily_limit` / `star_aid_repo_daily_limit` | 每用户 / 每仓库每日上限 |
| WebUI 配置管理 | `star_aid_summary_enabled` / `star_aid_summary_language` | 展示仓库 AI 摘要（README 全文传入不截断，输出上限跟随全局 `ai_max_tokens`） |
| WebUI 配置管理 | `star_aid_github_app_client_id` / `star_aid_github_app_client_secret` / `star_aid_github_app_callback_url` | 仓库互助 GitHub App user-to-server 凭据（可复用审查 App） |
| WebUI 配置管理 | `star_aid_token_encryption_key` | token 加密密钥 |

---

## 安全与 MFA

| 位置 | 键名 | 说明 |
|---|---|---|
| WebUI 安全中心 | 全局 / 单用户强制 MFA | 开启 MFA 要求 |
| WebUI 安全中心 | 重置 TOTP / 恢复码、删除 Passkeys | 管理员操作 |
| WebUI 配置管理 | `mfa_lockout_threshold` | MFA 失败锁定阈值（动态） |
| WebUI 配置管理 | `mfa_lockout_duration_minutes` | 锁定时长 |
| WebUI 配置管理 | `passkeys_allowed_origins` | WebAuthn 额外允许 Origin |
| WebUI 配置管理 | `mobile_oauth_allowed_redirect_uris` | 移动端 OAuth 回调白名单 |

> 用户可在个人设置中启用 TOTP、生成恢复码、注册 Passkeys/WebAuthn；支持 API Passkey 二次验证；WebAuthn 支持多个允许 Origin 与 Android App Links。

详见 [安全与 MFA 指南](SECURITY_MFA_GUIDE.md)。

---

## 支付与配额

| 位置 | 键名 | 说明 |
|---|---|---|
| WebUI 配置管理 | `payment_enabled` | 付费配额系统总开关 |
| WebUI 配置管理 | `stripe_*` / `paddle_*` / `alipay_*` / `nowpayments_*` / `tron_*` | 各支付网关参数 |
| WebUI 套餐管理 | 套餐计划、兑换码 | CRUD + 批量操作、管理员手动充值，支持一次性包和订阅，可为 PR/Issue/Agent 发放权益 |
| WebUI 配置管理 | 注册配额组 | 新用户注册初始配额 |

> 支持外部支付订单、回调验签、退款申请和超级管理员退款审核。

详见 [配额系统指南](QUOTA_SYSTEM_GUIDE.md)。

---

## Telegram Bot

| 位置 | 键名 | 说明 |
|---|---|---|
| Setup Wizard 第 3 步 / WebUI「系统核心配置」 | `telegram_bot_token` | Bot Token；**修改后需重启服务生效**（Bot 实例在服务启动时构造） |
| 环境变量（启动默认值） | `TELEGRAM_ADMIN_USER_IDS` | 超级管理员 Telegram ID（逗号分隔多个） |
| 环境变量（启动默认值） | `TELEGRAM_DEFAULT_CHAT_ID` | 默认通知聊天 ID |

> 注意：`telegram_admin_user_ids` / `telegram_default_chat_id` 不是 WebUI 动态配置键，以启动时环境变量 / Setup 配置为准。Bot 设置、权限体系与命令参考详见 [Telegram Bot 集成指南](TELEGRAM_SETUP.md)。

## 国际化

| 位置 | 键名 | 说明 |
|---|---|---|
| WebUI 个人设置 | 界面语言 | 中英文切换 |
| 全局配置 | `OUTPUT_LANGUAGE` | AI 输出语言 |
| 用户配置 | `output_language` | 用户级覆盖（`zh-CN` / `en` / 跟随全局） |

> 评论模板自动匹配所选语言。

---

## RAG 与代码索引

| 位置 | 入口 | 说明 |
|---|---|---|
| WebUI 配置管理 | 嵌入模型 | 支持 BAAI/bge-m3 等 |
| WebUI 配置管理 | 重排序模型 | — |
| WebUI 配置管理 | ChromaDB | 向量库连接 |
| WebUI 配置管理 | 代码分块 / 支持语言 / 核心目录 | PR 代码索引 |

---

*最后更新：2026-8-26 · 发现错误？[提 Issue](https://github.com/Sakura520222/Sakura-AI/issues)*
