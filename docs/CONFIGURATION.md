# 配置参考

> Sakura AI 全部配置项的位置、键名与说明。配置修改通过 WebUI 即时生效，无需重启。

← [文档索引](README.md) · [README](../README.md)

---

## 配置优先级

- **全局配置**：数据库 `app_config`（WebUI 管理） > Settings 默认值
- **用户偏好**：UserConfig > `app_config` > Settings 默认值
- **YAML 文件**：`config/strategies.yaml`（审查策略、上下文增强）、`config/labels.yaml`（标签定义）

> **动态配置**：通过 WebUI 配置管理页面修改的配置项即时生效，无需重启服务。AI 运行时的账号、端点、凭据与模型仅由「AI 配置」中的账号和角色绑定提供；调用策略、RAG、Web 搜索、代码索引和仓库互助仍由各自配置分组管理。

---

## AI 模型与账号

| 位置 | 键名 / 入口 | 说明 |
|---|---|---|
| WebUI「AI 配置」 | 账号管理 | 保存 OpenAI、Anthropic、Gemini、DeepSeek、Qwen、GLM、MiniMax、Kimi、Grok、Mistral、聚合网关、本地模型或自定义兼容账号（provider、protocol、region、base URL、API Key、默认模型） |
| WebUI「AI 配置」 | 角色绑定 | 为 `main`、`summary`、`agent_team` 配置主账号与故障转移链；模型列表可被发现并持久化，标签可快速选择 |
| WebUI 模型高级配置 | 每模型独立 | 上下文窗口、最大输出、图片多模态、思考模式/等级、temperature/top_p/top_k 能力/默认值 |
| WebUI 配置管理 | `ai_api_timeout_seconds` | 单次请求超时 |
| WebUI 配置管理 | `ai_api_total_timeout_seconds` | 一次 AI 调用重试循环的最长总耗时 |

**endpoint 约束**：内置远程账号仅允许官方 HTTPS endpoint；`custom` / `custom-anthropic` 可配置 HTTPS 公网 endpoint，以及 HTTP/HTTPS 本机或私网兼容 endpoint。

**角色跟随规则**：`summary` 与 `agent_team` 仅在绑定明确为 `account="main"` 或 `model="follow"` 时跟随主角色；缺少绑定、禁用账号、无效 endpoint 或空候选链会明确失败，不会回退到旧配置。

**历史旧键**：历史 `openai_*`、`summary_*`、`ai_provider` 和旧 Agent Team 供应商键可保留在数据库中，但系统不会读取、写入、迁移或将其作为回退来源。

详见 [模型上下文管理](MODEL_CONTEXT_FEATURE.md)。

---

## PR 审查

| 位置 | 键名 | 说明 |
|---|---|---|
| WebUI 配置管理 | `enable_auto_review` | PR webhook（opened/synchronize/reopened）是否自动入队；关闭后仍可命令或手动触发 |
| `config/strategies.yaml` | 四种策略 | 快速 / 标准 / 深度 / 大 PR |
| `config/strategies.yaml` | 文件过滤 | 跳过的文件扩展名和路径 |
| `config/strategies.yaml` | `review_policy` | 审查批准阈值与仓库级覆盖 |
| WebUI 配置管理 | `enable_pr_summary` | PR 变更自动总结 |
| WebUI 配置管理 | `enable_inline_comments` | 是否在 PR diff 上发布行内评论，默认开启 |
| WebUI 配置管理 | `enable_incremental_history_context` | 增量审查历史，AI 自动学习历史审查记录 |
| WebUI 配置管理 | `review_price_per_1k_prompt` / `review_price_per_1k_completion` | Token 消耗与成本追踪 |

### PR 依赖图

| 位置 | 键名 | 说明 |
|---|---|---|
| WebUI 配置管理 | `enable_pr_dependency_graph` | 总开关 |
| WebUI 配置管理 | `pr_dependency_graph_mode` | `ai` 模型分析 / `static` 静态 import 解析（更省成本） |
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
| `config/strategies.yaml` | `context_enhancement.ci_failure_injection` | 外部 CI 失败注入：开关、记录保留天数、单次审查最多失败记录数、每条失败最多 annotations 数 |

**Check external_id 格式**：`sakura-ai:v1:<review_job_id>:<check_kind>`。跨进程恢复优先读 DB 持久化的 `check_run_id`，缺失时按 `head_sha + name` 列举兜底。建议只将主 Check `Sakura AI Review` 纳入分支保护 required status check——副 Check 可能不出现，配为 required 会阻塞合并。

**外部 CI 注入依赖**：GitHub App 需订阅 `check_run` / `workflow_job` webhook，并授予 Checks 与 Actions 读取权限。

---

## 上下文治理与工具

| 位置 | 键名 | 说明 |
|---|---|---|
| WebUI「AI 配置」 | 每模型上下文窗口 | 按模型设置；自动压缩策略同页配置 |
| WebUI 配置管理 | `enable_context_compression` | 自动压缩开关 |
| WebUI 配置管理 | `context_compression_threshold` | 压缩触发阈值 |
| WebUI 配置管理 | `enable_ai_tools` | AI 工具开关 |
| WebUI 配置管理 | `max_tool_iterations` | 工具调用最大迭代数 |
| WebUI 配置管理 | `web_search_provider` | `duckduckgo`（免费，使用 `duckduckgo-search`）/ `tavily`（高级） |
| `config/strategies.yaml` | `context_enhancement.search_in_files` | 跨文件搜索：GitHub Search API 优先策略、上下文行数、最大结果数 |
| `config/strategies.yaml` | `context_enhancement.git_tools` | Git 信息工具：默认分支、提交返回数量 |

> 当初始 diff 过大时，审查自动使用 compact diff 工具模式；历史上下文由当前候选模型 AI 摘要压缩。上下文窗口按模型配置（自动发现优先，可手动覆盖），替代旧的全局单值。

详见 [模型上下文管理](MODEL_CONTEXT_FEATURE.md)。

---

## Issue 分析

| 位置 | 键名 | 说明 |
|---|---|---|
| 全局配置 | `issue_auto_create_labels` | Issue 自动创建标签开关 |
| 全局配置 | `issue_confidence_threshold` | Issue 标签置信度阈值 |
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
| `config/labels.yaml` | PR 标签推荐 | 开关与置信度 |
| 全局配置 | `issue_auto_create_labels` / `issue_confidence_threshold` | Issue 标签 |

---

## Agent 专家团队

| 位置 | 键名 | 说明 |
|---|---|---|
| WebUI Agent Team 页 | `agent_team_enabled` | 总开关 |
| WebUI Agent Team 页 | `agent_team_workspace_root` | 工作区根目录 |
| WebUI Agent Team 页 | `agent_team_repo_allowlist` | 仓库白名单（普通用户仅能操作自己名下且匹配的仓库） |
| WebUI Agent Team 页 | `agent_team_enable_context_compression` 等 | 上下文压缩 |
| WebUI Agent Team 页 | `agent_team_max_tool_rounds` | 全栈专家工具轮数 |
| WebUI Agent Team 页 | `agent_team_reviewer_max_tool_rounds` | 审查专家工具轮数 |
| WebUI Agent Team 页 | `agent_team_auto_install_deps` | 自动安装依赖 |
| WebUI Agent Team 页 | 验证命令黑名单 | 控制可执行的验证命令 |
| WebUI Agent Team 页 | `agent_team_pr_closed_loop_enabled` | PR 审查闭环开关 |
| WebUI Agent Team 页 | `agent_team_max_iterations_per_task` | 单任务最大自动迭代次数 |
| WebUI Agent Team 页 | `agent_team_pr_review_pass_score` | PR 审查通过分数线 |
| WebUI Agent Skills 页 | `agent_team_skills_enabled` | Agent 是否可加载技能 |
| WebUI Agent Skills 页 | `agent_team_skills_root` | 技能本地存储根目录 |

> Agent Team 的 AI 调用固定使用 `agent_team` 角色绑定，上下文压缩使用 `summary` 角色绑定，**不支持**独立 endpoint、API Key 或模型配置。普通用户入口校验仓库归属和 `agent_team_repo_allowlist` 并消耗 Agent 配额；`/agent` 评论可从已分析 Issue 或扫描报告 Issue 创建任务。

详见 [Agent Skills 实现](agent-skills-python-implementation.md)、[Agent 文件工具实现](agent-file-tools-python-implementation.md)。

---

## 项目记忆系统

| 位置 | 键名 | 说明 |
|---|---|---|
| WebUI 配置管理 | `sakura_memory_enabled` | 记忆系统总开关 |
| WebUI 配置管理 | `sakura_reflection_enabled` | 审查后反思 |
| WebUI 配置管理 | `sakura_consolidation_interval` | 合并触发的反思轮数（默认 5） |
| WebUI 配置管理 | `sakura_auto_init` | 自动初始化 `.sakura/` 目录 |
| WebUI 配置管理 | `sakura_auto_create_subdirs` | 自动创建 rules/docs/plans 子目录 |
| WebUI 配置管理 | `sakura_knowledge_extraction_enabled` | 自动知识提取（三次串行 LLM 调用提取 rules/docs/plans） |
| `config/strategies.yaml` | `context_enhancement.sakura_memory.reflection` | 反思 prompt 包含的最大评论/变更文件/新增提交条数（`max_comments`/`max_changed_files`/`max_new_commits`，默认 30/30/20） |

> 反思、合并与知识提取由 `main` 或 `summary` 角色绑定决定实际账号和模型，不支持在该功能中另配凭据或模型。评论正文与 PR 描述完整传入、不截断。WebUI「Sakura 记忆管理」页面支持查看 / 编辑 / 删除记忆文件、手动触发合并和知识提取。

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
| WebUI 配置管理 | `star_aid_summary_enabled` / `star_aid_summary_language` / `star_aid_summary_readme_budget` / `star_aid_summary_max_tokens` | 展示仓库 AI 摘要 |
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

*最后更新：2026-8-10 · 发现错误？[提 Issue](https://github.com/Sakura520222/Sakura-AI/issues)*
