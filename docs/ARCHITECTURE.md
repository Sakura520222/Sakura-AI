# 技术架构

> Sakura AI 的整体架构、技术栈、代码结构与客户端生态。

← [文档索引](README.md) · [README](../README.md)

---

## 整体架构

```
┌─────────────────────────────────────────────────────────────┐
│                        GitHub                                │
│                    PR / Issue / OAuth                        │
└──────────┬───────────────────────────────────┬──────────────┘
           │ Webhook                           │ OAuth / API
           ▼                                   ▼
┌─────────────────────────────────────────────────────────────┐
│                    FastAPI Web Server                        │
│  ┌──────────────┐  ┌──────────────┐  ┌──────────────┐      │
│  │   Webhook    │  │   PR 分析器   │  │  评论服务    │      │
│  │   Handler    │  │  (策略选择)   │  │  (发布结果)  │      │
│  │ (PR+Issue)   │  │              │  │              │      │
│  └──────────────┘  └──────────────┘  └──────────────┘      │
│  ┌──────────────────────────────────────────────────────┐   │
│  │    WebUI (Jinja2 + HTMX + Alpine.js) · SSE 实时推送   │   │
│  ├──────────────────────────────────────────────────────┤   │
│  │  Setup Wizard · 动态配置 · 仓库互助 · 法律页面 · 审计   │   │
│  └──────────────────────────────────────────────────────┘   │
└──────────────────────┬──────────────────────────────────────┘
                       │
                       ▼
┌─────────────────────────────────────────────────────────────┐
│                     AI 审查引擎                              │
│  ┌────────────┐  ┌────────────┐  ┌────────────┐            │
│  │ read_file  │  │ list_dir   │  │search_files│            │
│  └────────────┘  └────────────┘  └────────────┘            │
│  ┌────────────┐  ┌────────────┐  ┌────────────┐            │
│  │  git_info  │  │  commits   │  │ search_web │            │
│  └────────────┘  └────────────┘  └────────────┘            │
│  ┌────────────┐  ┌────────────┐  ┌────────────┐            │
│  │ RAG 检索   │  │ 代码索引    │  │  历史上下文  │            │
│  └────────────┘  └────────────┘  └────────────┘            │
│  ┌──────────────────────┐  ┌──────────────────────┐        │
│  │ read_sakura_docs     │  │ list_sakura_directory │        │
│  └──────────────────────┘  └──────────────────────┘        │
│  ┌──────────────────────┐                                   │
│  │ read_sakura_memory   │                                   │
│  └──────────────────────┘                                   │
└──────────────────────┬──────────────────────────────────────┘
                       │
                       ▼
┌─────────────────────────────────────────────────────────────┐
│                    数据存储层                                │
│  ┌──────────────┐  ┌──────────────┐  ┌──────────────┐      │
│  │    MySQL     │  │    Redis     │  │  ChromaDB    │      │
│  │  (业务数据)   │  │ (队列/PubSub)│  │  (向量检索)  │      │
│  └──────────────┘  └──────────────┘  └──────────────┘      │
└─────────────────────────────────────────────────────────────┘
```

### 数据流

1. **事件入口**：GitHub Webhook（PR / Issue / Check run / Workflow job）与 OAuth 登录进入 FastAPI。
2. **审查流水线**：Webhook Handler → PR 分析器按规模选择策略（快速 / 标准 / 深度 / 大 PR）→ AI 审查引擎调用工具与知识库 → 评论服务发布结构化报告。
3. **实时通信**：Redis Pub/Sub 承载 SSE，跨进程把审查进度、Provider Attempt、工具状态推送到 WebUI 与活动观测页。
4. **持久化**：MySQL 存业务数据与配置（`app_config`），Redis 做队列与 Pub/Sub，ChromaDB 存代码与 RAG 向量。
5. **后台工作**：`workers/` 执行审查、Issue 分析、扫描、Agent Team、仓库互助等异步任务。

---

## 技术栈

- **后端**：FastAPI（Python 3.14+）、SQLAlchemy（async）、PyGithub
- **前端**：Jinja2 + Tailwind CSS + HTMX + Alpine.js
- **AI**：多协议适配层（OpenAI Chat Completions / Anthropic Messages / Gemini 原生 / 兼容端点），支持 OpenAI、Anthropic Claude、Google Gemini、xAI Grok、DeepSeek、Qwen、GLM、MiniMax、Kimi、Mistral、Ollama、vLLM 等
- **存储**：MySQL 8.0（业务）、Redis（队列 / PubSub）、ChromaDB（向量检索）
- **集成**：GitHub App + OAuth、Telegram Bot、Stripe / Paddle / 支付宝 / NOWPayments / TRON USDT
- **部署**：Docker Compose、Host Updater 守护进程（独立二进制）
- **日志**：loguru；**代码检查**：Ruff；**注释**：中英双语；**提交规范**：Conventional Commits

---

## 代码结构

```
Sakura-AI/
├── backend/
│   ├── api/               # API 路由（webhook、health、v1）
│   │   └── v1/            #   RESTful API v1（移动端对接，含 user_config/billing/scans）
│   ├── core/              # 核心配置、动态配置、Setup Wizard、AI Provider 注册表、Redis
│   ├── models/            # 数据模型（SQLAlchemy 异步）
│   ├── services/          # 业务逻辑
│   │   ├── agent_team/    # Agent 专家团队、受控工作区工具、PR 创建与 Skills
│   │   │   └── tools/     #   Agent 工具（read/write/edit/grep/glob/shell/web/sakura/skill ...）
│   │   ├── ai_reviewer/   # AI 审查引擎
│   │   │   ├── tools/     #   AI 工具（文件读取、跨文件搜索、Git 信息、Web 搜索、diff、Sakura 记忆）
│   │   │   └── compression/ # 上下文压缩
│   │   ├── payment/        # 支付网关（Stripe / Paddle / Alipay / NOWPayments / TRON）
│   │   ├── pr_analyzer.py        # PR 分析器（策略选择）
│   │   ├── issue_analyzer.py     # Issue 分析引擎
│   │   ├── issue_service.py      # Issue 服务（打标、指派、改写）
│   │   ├── issue_embedding_service.py  # Issue 向量嵌入
│   │   ├── pr_issue_linker.py    # PR-Issue 关联
│   │   ├── decision_engine.py    # 审查决策引擎
│   │   ├── comment_service.py    # 评论服务
│   │   ├── check_run_service.py  # GitHub Check Runs 可视化
│   │   ├── ci_failure_service.py # 外部 CI 失败注入
│   │   ├── rag_service.py        # RAG 知识库
│   │   ├── code_index_service.py # 代码索引
│   │   ├── scan_prompt_builder.py    # 仓库扫描 Prompt 构建
│   │   ├── scan_report_service.py    # 扫描报告服务
│   │   ├── scan_scheduler.py         # 扫描调度器
│   │   ├── history_context_service.py     # 增量审查历史
│   │   ├── pr_review_incremental_queue.py # 增量审查入队
│   │   ├── activity_observability/    # Activity 可观测性、Canonical Transcript、Outbox 与 SSE
│   │   ├── sakura_memory_service.py          # .sakura/ 项目记忆服务
│   │   ├── sakura_consolidation_agent.py     # .sakura/ 记忆合并 Agent
│   │   ├── sakura_knowledge_extractor.py     # .sakura/ 知识提取 Agent
│   │   ├── star_aid_service.py           # 仓库互助业务服务
│   │   ├── star_aid_github_service.py    # 仓库互助 GitHub 协议
│   │   ├── star_aid_summary_service.py   # 仓库互助 AI 摘要
│   │   ├── star_aid_scheduler.py         # 仓库互助调度器
│   │   ├── payment_service.py            # 付费配额与退款服务
│   │   ├── quota_service.py              # 配额计费与重置
│   │   ├── github_write_service.py       # GitHub 写操作服务（.sakura/ 写入）
│   │   ├── two_factor_service.py         # TOTP 与恢复码服务
│   │   ├── webauthn_service.py           # Passkeys/WebAuthn 服务
│   │   ├── mfa_lockout_service.py        # MFA 失败锁定
│   │   ├── security_admin_service.py     # 安全中心管理服务
│   │   ├── security_audit_service.py     # 安全审计服务
│   │   ├── secret_crypto_service.py      # 敏感数据加解密（仓库互助 token 等）
│   │   └── system_config_service.py      # 系统核心配置管理
│   ├── webui/             # WebUI 管理界面
│   │   ├── routes/        #   路由（dashboard, config, users, agent_team, star_aid, billing, legal ...）
│   │   ├── templates/     #   Jinja2 模板
│   │   ├── translations/  #   i18n 文案（zh-CN / en）
│   │   ├── auth.py        #   GitHub OAuth 认证
│   │   └── sse.py         #   SSE 实时推送
│   ├── workers/           # 后台任务（review / issue / scan / agent_team / star_aid worker）
│   ├── telegram/          # Telegram Bot（通知、命令、按钮菜单、权限）
│   └── main.py            # FastAPI 应用入口
├── config/                # YAML 配置文件（strategies.yaml、labels.yaml）
├── docker/                # Docker Compose 部署
├── docs/                  # 项目文档
├── scripts/               # 辅助脚本（dev_bootstrap 等）
└── .understand-anything/  # 交互式知识图谱（Understand Anything）
```

### 模块职责

| 层 | 职责 |
|---|---|
| `backend/api/` | Webhook 入口、健康检查、RESTful API v1（移动端、user_config、billing、scans） |
| `backend/core/` | 配置单例 `get_settings()`、动态配置、Setup Wizard、bootstrap、AI Provider 注册表、Redis 基础设施、日志、认证 |
| `backend/models/` | SQLAlchemy 异步模型、数据库初始化、自动迁移、默认配置插入 |
| `backend/services/` | 全部业务逻辑：PR/Issue 审查、AI agent 工具链、队列调度、支付、安全、RAG、ChromaDB 索引 |
| `backend/webui/` | 根路径挂载的 HTML/WebUI 路由、认证、SSE、页面辅助、i18n 文案 |
| `backend/workers/` | 异步审查、Issue、扫描、Agent Team、仓库互助等后台工作 |
| `backend/telegram/` | Telegram Bot 通知与命令 |

### 运行时配置优先级

- 全局：**数据库 `app_config`（WebUI 管理） > Settings 默认值**
- 用户偏好：**UserConfig > `app_config` > Settings 默认值**
- YAML 文件（`config/strategies.yaml`、`config/labels.yaml`）管理审查策略与标签定义

详见 [配置参考](CONFIGURATION.md)。

---

## 客户端

### 原生 Android App

锐意开发中 → [Sakura-AI-APP](https://github.com/Sakura520222/Sakura-AI-APP)

通过 [API v1 接口](api-v1-reference.md) 与 Sakura-AI 后端对接，提供移动端管理体验。

---

## 交互式知识图谱

项目使用 [Understand Anything](https://github.com/Egonex-AI/Understand-Anything) 生成交互式代码知识图谱，包含架构层次、节点关系和学习路径，便于快速理解项目结构。

**生成 / 更新知识图谱**（在 Claude Code 中执行）：

```
/understand --language zh
```

**启动可视化仪表盘**：

```
/understand-dashboard
```

启动后自动在浏览器中打开交互式仪表盘，支持：

- 浏览架构层次和模块依赖关系
- 查看节点（文件、函数、类、端点）之间的调用和导入关系
- 按引导路径逐步了解项目架构
- 按类型、标签、层级筛选节点

知识图谱数据存储在 `.understand-anything/knowledge-graph.json`，支持增量更新——代码变更后重新执行 `/understand` 即可自动同步。

---

*最后更新：2026-8-10 · 发现错误？[提 Issue](https://github.com/Sakura520222/Sakura-AI/issues)*
