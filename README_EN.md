<div align="center">

# 🌸 Sakura AI Reviewer

<img src="res/cover.png" alt="Sakura AI Reviewer Cover" width="100%">

> AI-powered intelligent GitHub Pull Request code review and Issue analysis bot with proactive codebase exploration capabilities

**English** | [中文](README.md)

[![Version](https://img.shields.io/badge/Version-2.10.1-blue.svg)](https://github.com/Sakura520222/Sakura-AI-Reviewer/releases)
[![Python](https://img.shields.io/badge/Python-3.11+-blue.svg)](https://www.python.org/)
[![FastAPI](https://img.shields.io/badge/FastAPI-Latest-green.svg)](https://fastapi.tiangolo.com/)
[![License](https://img.shields.io/badge/License-AGPLv3-yellow.svg)](LICENSE)
[![Live Demo](https://img.shields.io/badge/🌐_Free_Demo-Online-success.svg)](https://pr-bot.firefly520.top/)
[![Android App](https://img.shields.io/badge/Android_App-🚧_In_Development-orange.svg)](https://github.com/Sakura520222/Sakura-AI-Reviewer-APP)

</div>

---

## ✨ Core Features

### Review Capabilities

- **AI Reasoning Mode**: Leverages AI reasoning for in-depth code analysis, proactively invoking tools to inspect project structure and arbitrary files
- **Cross-file Dependency Understanding**: Understands complex inter-module dependencies through multi-turn dialogue with a "global view"
- **Adaptive Review Strategy**: Automatically selects quick/standard/deep review mode based on PR size
- **Large PR Compact Review**: Automatically switches to compact diff mode when the initial diff approaches the context threshold; AI inspects changes on demand through `get_file_diff` / `list_changed_files`
- **Structured Review Reports**: Overall score + categorized issues (🔴Critical / 🟡Important / 💡Suggestion) + `<details>` collapsible sections
- **Incremental Review Learning**: AI automatically summarizes historical review records, identifies scoring trends and issue hotspots, continuously improving review quality
- **Smart Review Approval**: Automatically decides APPROVE / REQUEST_CHANGES / COMMENT based on AI scores
- **PR Change Summary**: AI auto-generates PR change summaries with incremental updates when the PR is updated
- **PR Dependency Graph**: Supports both AI analysis and static import analysis modes to generate Mermaid-format visual dependency graphs
- **Token Consumption Tracking**: Real-time tracking of token usage and estimated costs across all AI API calls during review
- **One-click Revoke**: Admins can use `/revoke` to instantly withdraw all AI comments and reviews
- **Auxiliary Model Support**: Independently configure lightweight models for summarization, context compression, label recommendation, and other tasks to reduce inference costs
- **Inline Comments Toggle**: Control whether inline comments are posted on PR diffs via WebUI config `enable_inline_comments`, reducing review noise
- **Controlled Auto Review**: Use WebUI config `enable_auto_review` to control whether PR opened/synchronize/reopened events enqueue reviews automatically while keeping command and manual triggers available

### AI Tools & Knowledge Base

- **AI Tool System**: read_file, list_directory, search_in_files, get_git_info, list_commits, search_web, read_sakura_docs, list_sakura_directory, read_sakura_memory — AI proactively invokes tools on demand
- **Cross-file Code Search**: AI can search keywords across files in the repository, quickly locating all usages of functions, variables, and classes
- **Git Information Query**: AI can retrieve repository info, branch lists, and commit history to understand project evolution
- **Web Search Enhancement**: Supports DuckDuckGo / Tavily, allowing AI to actively search the internet to assist review decisions
- **Repository-level Knowledge Base (RAG)**: Vector semantic retrieval of project documentation, providing normative context for AI reviews
- **PR Code Auto-indexing**: Syntax-aware chunking + semantic search, enabling AI to precisely locate relevant code
- 🧠 **Project Memory System**: Self-reflection and knowledge accumulation based on `.sakura/` directory, AI reviews get smarter about your project over time. See [Project Memory Guide](docs/SAKURA_MEMORY_GUIDE.md) (Chinese)

### Repository Scanning

- **AI Full Repository Scan**: Periodic AI-powered code scanning across the entire repository, automatically detecting code quality issues and security vulnerabilities
- **Auto-create Issues**: Automatically creates GitHub Issues for discovered problems, with detailed descriptions and fix suggestions
- **Flexible Scan Configuration**: Configurable scan interval, cooldown time, token budget, concurrency, and more
- **Scan Management UI**: View scan list, scan details, and statistics in WebUI
- **Scan Notifications**: Sends notifications via Telegram Bot when scans complete

### Issue Analysis

- **Intelligent Issue Analysis**: Auto-classification, priority assessment, label recommendation, duplicate detection, linked PR discovery
- **Auto-labeling**: AI categorizes and recommends labels; high-confidence labels are applied automatically
- **Auto-assignment**: AI analyzes issue content and automatically assigns it to appropriate repository collaborators
- **Title Rewriting**: AI automatically improves vague or inaccurate issue titles
- **PR-Issue Linking**: Automatically parses issue references and injects context to enhance review precision
- **Semantic Issue Linking**: Discovers and links related issues based on vector semantic similarity

### Agent Expert Team

- **Super-admin Manual Launch**: Select candidate tasks from Issue analysis and repository scan findings, with natural language filtering, and start automated fix workflows on demand
- **Manual Issue Task Creation**: Paste a GitHub Issue URL or enter `owner/repo#123`; the system validates it and creates an Agent fix task directly
- **Smart Candidate Filtering**: Automatic deduplication, closed-issue filtering, score-based sorting, and AI natural language selection to match the most suitable candidate tasks
- **Two-agent Collaboration**: A full-stack expert plans and edits code, while a professional reviewer performs pre-push quality review
- **Context Compression & Resume**: Long-running tasks compress historical context automatically and persist conversation/message checkpoints for recovery
- **Isolated Git Workspaces**: Clone/fetch/checkout dedicated branches under `agent_team_workspace_root` without polluting the service runtime directory
- **Controlled Tool Execution**: File operations, search, and shell validation commands are scoped to the workspace; validation commands are controlled by an allowlist
- **Dependency Auto-install & Validation**: Can detect and install dependencies from `pyproject.toml` / `requirements.txt`, then run allowlisted tests or lint commands
- **Sakura Knowledge Integration**: Agents can browse and read `.sakura/` knowledge directory and reflection files via dedicated tools, leveraging accumulated review experience to assist code fixes
- **Agent Skills & Built-in Ruff**: Install skills from uploaded files, ZIP packages, or GitHub `SKILL.md`; a built-in Ruff lint/format skill can be loaded on demand
- **PR Creation Loop**: Supports AI-generated Conventional Commits-style PR titles, normal or Draft PR creation, then iterates through Sakura PR Review and human feedback; PRs are never merged automatically

### Management & Operations

- **Setup Wizard**: Automatically detects configuration status on first launch, guides you through GitHub App, database, AI model, and RAG setup step by step, with resume support
- **System Core Configuration**: Super admins can modify infrastructure settings (database, GitHub App/OAuth, Telegram, app domain, etc.) at runtime via WebUI — no need to re-run Setup Wizard; changes are automatically audit-logged
- **Dynamic Configuration**: Configuration changes via WebUI take effect immediately without service restart
- **AI API Timeout Control**: `ai_api_timeout_seconds` controls per-request timeout, and `ai_api_total_timeout_seconds` controls the total retry-loop duration for one AI call
- **Per-user Config Overrides**: Users can override allowed preference settings in WebUI or API (currently AI output language), with fallback order UserConfig → AppConfig → Settings defaults
- **AI Provider Registry**: Built-in OpenAI, DeepSeek, Qwen, Z.ai, Doubao, SiliconFlow, Gemini, Anthropic-compatible, and custom OpenAI-compatible providers, with automatic model list and context window discovery
- **GitHub App Installation Management**: Automatically handles GitHub App install/uninstall events, syncing repository authorization status
- **Security Center & MFA**: Supports TOTP, recovery codes, Passkeys/WebAuthn, global/per-user MFA enforcement, admin MFA reset, security event audit logs, MFA failure lockout (dynamic threshold and duration), and API Passkey second-factor authentication
- **SSE Real-time Push**: Multi-process real-time communication based on Redis Pub/Sub, with instant WebUI data updates
- **Quota-based Access Control**: Flexible quota-based access management system with user self-registration support and UTC daily/weekly/monthly auto-reset for PR and Issue usage
- **Paid Quota System**: Full CRUD management for plans and redeem codes (create/edit/delete/batch operations), admin manual grants, supports one-time packages and subscription plans
- **Admin Action Audit**: Complete operation logs covering configuration changes, user management, and other critical actions
- **WebUI Dashboard**: Dashboard charts, PR management, user management, configuration management, queue monitoring, action logs, repository scan management, Agent Expert Team, Agent Skills, and Sakura Memory management, with Markdown content rendering support
- **Telegram Bot**: Real-time notifications, interactive button menus, three-tier permission system (super admin / admin / user), quota management
- **GitHub OAuth Login**: Integrated with Telegram user system, light/dark theme switching

---

## 🏗️ Technical Architecture

```
┌─────────────────────────────────────────────────────────────┐
│                        GitHub                                │
│                    PR / Issue / OAuth                        │
└──────────┬───────────────────────────────┬──────────────────┘
           │ Webhook                       │ OAuth / API
           ▼                               ▼
┌─────────────────────────────────────────────────────────────┐
│                    FastAPI Web Server                        │
│  ┌──────────────┐  ┌──────────────┐  ┌──────────────┐      │
│  │   Webhook    │  │  PR Analyzer │  │   Comment    │      │
│  │   Handler    │  │  (Strategy)  │  │   Service    │      │
│  │ (PR+Issue)   │  │              │  │  (Publish)   │      │
│  └──────────────┘  └──────────────┘  └──────────────┘      │
│  ┌──────────────────────────────────────────────────────┐   │
│  │    WebUI (Jinja2 + HTMX + Alpine.js) · SSE Push      │   │
│  ├──────────────────────────────────────────────────────┤   │
│  │    Setup Wizard · Dynamic Config · Admin Audit        │   │
│  └──────────────────────────────────────────────────────┘   │
└──────────────────────┬──────────────────────────────────────┘
                       │
                       ▼
┌─────────────────────────────────────────────────────────────┐
│                     AI Review Engine                         │
│  ┌────────────┐  ┌────────────┐  ┌────────────┐            │
│  │ read_file  │  │ list_dir   │  │search_files│            │
│  └────────────┘  └────────────┘  └────────────┘            │
│  ┌────────────┐  ┌────────────┐  ┌────────────┐            │
│  │  git_info  │  │  commits   │  │ search_web │            │
│  └────────────┘  └────────────┘  └────────────┘            │
│  ┌────────────┐  ┌────────────┐  ┌────────────┐            │
│  │ RAG Search │  │ Code Index │  │  History   │            │
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
│                     Data Storage Layer                       │
│  ┌──────────────┐  ┌──────────────┐  ┌──────────────┐      │
│  │    MySQL     │  │    Redis     │  │  ChromaDB    │      │
│  │  (Business)  │  │(Queue/PubSub)│  │  (Vectors)   │      │
│  └──────────────┘  └──────────────┘  └──────────────┘      │
└─────────────────────────────────────────────────────────────┘
```

**Tech Stack**: FastAPI (Python 3.11+) · Jinja2 + Tailwind CSS + HTMX + Alpine.js · DeepSeek-R1 / OpenAI-compatible API · MySQL 8.0 + Redis (Queue/PubSub) + ChromaDB · GitHub App (PyGithub) + OAuth · Docker Compose · Optional Celery Worker

### Client Applications

- **Native Android App**: 🚧 Under
  development → [Sakura-AI-Reviewer-APP](https://github.com/Sakura520222/Sakura-AI-Reviewer-APP)
  Connects to the Sakura-AI-Reviewer backend via the [API v1 interface](docs/api-v1-reference.md) for mobile management

---

## 🚀 Quick Start

### 1. Requirements

- Linux server (Ubuntu 20.04+ recommended)
- Docker and Docker Compose
- Public IP and domain name
- GitHub account
- DeepSeek API Key (or other OpenAI-compatible API)

### 2. Clone the Repository

```bash
git clone https://github.com/Sakura520222/Sakura-AI-Reviewer.git
cd Sakura-AI-Reviewer
```

> All configuration (GitHub App, AI models, database, etc.) is done through the Setup Wizard web interface after first launch — no manual config file editing needed.

### 3. Create a GitHub App

1. Go to [GitHub Apps settings](https://github.com/settings/apps) and click **New GitHub App**
2. Fill in the name and Homepage URL
3. **Repository permissions**: Pull requests `Read and write`, Contents `Read and write`, Issues `Read and write` (optional)
4. **Webhook URL**: `https://your-domain.com:8000/api/webhook/github`, enter Webhook secret
5. **Webhook events**: Check Pull requests, Pull request reviews, Issues (optional), Issue comments (optional)
6. After creation, click **Generate a private key** at the bottom of the App page, download the `.pem` file (paste the full private key content in Setup Wizard)
7. Click **Install App** on the left sidebar, select the repositories to enable review

> WebUI login requires an additional [OAuth App](https://github.com/settings/developers) with callback URL set to `https://your-domain.com/auth/callback`

### 4. Prepare the Database

Install and start MySQL and Redis on the host:

```bash
sudo apt update && sudo apt install mysql-server redis-server -y
sudo systemctl start mysql && sudo systemctl start redis
sudo mysql -e "CREATE DATABASE IF NOT EXISTS \`sakura-pr\` CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;"
sudo mysql -e "CREATE USER IF NOT EXISTS 'root'@'%' IDENTIFIED BY 'your_password';"
sudo mysql -e "GRANT ALL PRIVILEGES ON *.* TO 'root'@'%';"
sudo mysql -e "FLUSH PRIVILEGES;"
```

### 5. Start the Service

```bash
cd docker
docker-compose up -d
```

### 6. Setup Wizard Configuration

After first launch, visit `https://your-domain.com/setup`. The Setup Wizard will guide you through all configuration steps (supports resume from breakpoint):

1. **Database Configuration**: Enter MySQL and Redis connection addresses, with online connection testing
2. **GitHub App Configuration**: Enter App ID, private key, and Webhook Secret; auto-verifies App connection
3. **AI Model & Notifications**: Configure AI API (supports auto-fetching model list) and Telegram Bot Token
4. **Admin & OAuth**: Set up admin account, application domain, and GitHub OAuth credentials

> Setup Wizard includes RAG embedding and reranking model configuration (collapsible), which can be skipped and configured later in WebUI.

### 7. Verify Deployment

```bash
curl http://your-domain.com:8000/health
# {"status":"healthy","service":"Sakura AI Reviewer"}
```

WebUI: `https://your-domain.com/`

---

## 📖 Usage

### PR Review

Create a PR in a repository with the App installed, and the AI will automatically review it and publish a structured report. Review reports use `<details>` collapsible sections to keep comments concise. Available commands in PRs:

- `/full-review` — Clear old comments and trigger a full re-review (PR author or collaborator)
- `/revoke` — One-click revoke all AI comments and reviews (admin only)

### Issue Analysis

- **Auto-analysis**: Triggered automatically on Issue opened/edited/reopened, posting classification, priority, and label suggestions
- **Auto-labeling**: AI recommends labels; high-confidence labels are applied automatically
- **Manual trigger**: Comment `/analyze` in an Issue
- **Duplicate detection**: Automatically identifies duplicate Issues and links to existing ones

### WebUI Management

Visit `https://your-domain.com/` and log in with your GitHub account (requires prior registration via Telegram Bot). Features include dashboard charts, PR management, user management, dynamic configuration, review queue monitoring, action logs, Security Center, and personal MFA/Passkey settings. Configuration changes take effect immediately without service restart.

### Telegram Bot

Provides real-time notifications (review started/completed), quota management, permission control (three-tier system), and rich admin commands. See [Telegram Bot Integration Guide](docs/TELEGRAM_SETUP.md) for details.

---

## ⚙️ Configuration

Global configuration follows this priority: **Database app_config (WebUI) > Settings defaults**. Per-user preference configuration follows **UserConfig > app_config > Settings defaults**. YAML config files (`config/strategies.yaml`, `config/labels.yaml`) manage review strategies and label definitions.

> **Dynamic Configuration**: Changes made via the WebUI configuration page take effect immediately without service restart. Supports multiple configuration groups including AI models, auxiliary models, RAG, web search, code indexing, and more.

- **AI Model**: Select a built-in AI Provider in WebUI configuration (OpenAI, DeepSeek, Qwen, Z.ai, Doubao, SiliconFlow, Gemini, Anthropic-compatible, or custom OpenAI-compatible), set API URL/API Key/model name, and optionally auto-fetch model lists and context window metadata
- **Auxiliary Model**: Set `summary_model`, `summary_api_base`, `summary_api_key` in WebUI configuration for lightweight tasks like summarization, context compression, and label recommendation; auto-falls back to main model if left empty
- **PR Auto Review**: `enable_auto_review` in WebUI configuration controls whether PR webhook events automatically trigger reviews; command and manual triggers remain available when disabled
- **AI API Timeout**: `ai_api_timeout_seconds` controls the per-request timeout, and `ai_api_total_timeout_seconds` controls the maximum total duration of one AI call retry loop
- **Security & MFA**: The WebUI Security Center can enforce MFA globally or per user, reset TOTP/recovery codes, delete Passkeys, and record security audit events; users can enable TOTP, generate recovery codes, and register Passkeys/WebAuthn in personal settings; supports MFA failure lockout (`mfa_lockout_threshold` / `mfa_lockout_duration_minutes`) and API Passkey second-factor authentication
- **Review Strategy**: Edit `config/strategies.yaml`, supports quick/standard/deep/large-PR four strategies
- **File Filtering**: Configure skipped file extensions and paths in `config/strategies.yaml`
- **AI Tools**: `enable_ai_tools` / `max_tool_iterations` in WebUI configuration
- **Label Recommendation**: `config/labels.yaml` for PR label recommendation toggle and confidence; Issue labels at `issue_auto_create_labels` / `issue_confidence_threshold` in global config
- **Review Approval**: `review_policy` in `config/strategies.yaml` for threshold and repository-level overrides
- **PR Change Summary**: `enable_pr_summary` in WebUI configuration
- **PR Dependency Graph**: `enable_pr_dependency_graph` / `pr_dependency_graph_mode` / `pr_dependency_graph_max_nodes` / `pr_dependency_graph_max_files` in WebUI configuration; `ai` mode uses model-based dependency analysis, while `static` mode uses static import parsing to reduce cost
- **Large PR Context Management**: `model_context_window` / `context_safety_threshold` / `enable_context_compression` / `context_compression_threshold` / `context_compression_keep_rounds` in WebUI configuration; when the initial diff is too large, review automatically uses compact diff tool mode
- **Token Cost Tracking**: `review_price_per_1k_prompt` / `review_price_per_1k_completion` in WebUI configuration for tracking review token consumption and costs
- **RAG Knowledge Base**: Configure embedding models (supports BAAI/bge-m3, etc.), reranking models, ChromaDB in WebUI configuration
- **PR Code Index**: Configure code chunking, supported languages, core directories in WebUI configuration
- **Issue Auto-assignment**: `issue_auto_assign` / `issue_assignee_confidence_threshold` in WebUI configuration
- **Issue Concurrency Control**: `max_concurrent_issues` in WebUI configuration — controls the maximum number of simultaneous Issue analysis tasks; excess tasks are queued
- **Issue Title Rewriting**: `issue_auto_rewrite_title` in WebUI configuration
- **Semantic Issue Linking**: `enable_semantic_issue_linking` / `semantic_issue_similarity_threshold` in WebUI configuration
- **Incremental Review History**: `enable_incremental_history_context` in WebUI configuration, AI auto-learns from historical review records
- **Inline Comments Toggle**: `enable_inline_comments` in WebUI configuration, controls whether inline comments are posted on PR diffs (default: enabled)
- **Web Search Tool**: `web_search_provider` in WebUI configuration (`duckduckgo` free or `tavily` premium)
- **Cross-file Search**: `context_enhancement.search_in_files` in `config/strategies.yaml` — configure GitHub Search API priority, context lines, max results, etc.
- **Git Info Tool**: `context_enhancement.git_tools` in `config/strategies.yaml` — configure default branch and commit return counts
- **Project Memory System**: `sakura_memory_enabled` to enable memory system, `sakura_reflection_enabled` to enable post-review reflection, `sakura_consolidation_interval` for consolidation trigger threshold (default 5), `sakura_auto_init` to auto-initialize `.sakura/` directory, `sakura_auto_create_subdirs` to auto-create rules/docs/plans subdirectories, `sakura_knowledge_extraction_enabled` to enable automatic knowledge extraction (extracts rules/docs/plans via three serial LLM calls), `sakura_extraction_provider` to configure extraction AI credentials (main/summary/custom) — all in WebUI configuration. WebUI provides a "Sakura Memory" management page for viewing/editing/deleting memory files and manually triggering consolidation and knowledge extraction. See [Project Memory Guide](docs/SAKURA_MEMORY_GUIDE.md) (Chinese)
- **Model Context**: Configure context window, auto-compression in WebUI configuration, see [Model Context Management](docs/MODEL_CONTEXT_FEATURE.md)
- **Agent Expert Team**: Configure `agent_team_enabled`, `agent_team_workspace_root`, `agent_team_repo_allowlist`, `agent_team_model_provider`, and other `agent_team_*` model/guardrail settings on the WebUI Agent Team page; supports context compression (`agent_team_enable_context_compression`, etc.), full-stack/reviewer tool-round limits (`agent_team_max_tool_rounds` / `agent_team_reviewer_max_tool_rounds`), dependency auto-install (`agent_team_auto_install_deps`), validation command allowlists, and the Draft PR switch; `agent_team_model_provider=main` reuses the main AI configuration, while independent Agent AI configuration is also supported
- **Agent Skills**: Install and toggle Skills on the WebUI Agent Skills page; `agent_team_skills_enabled` controls whether agents may load skills, and `agent_team_skills_root` configures the local storage root
- **Internationalization (i18n)**: WebUI supports Chinese/English interface switching (Settings page). AI output language can be controlled globally via `OUTPUT_LANGUAGE` or overridden per user through `output_language` (`zh-CN` / `en` / follow global). Comment templates automatically match the selected language.

---

## 🖥️ Screenshots

<div align="center">

<img src="res/发送正在审查中和自动打标.png" width="1901" alt="Review in progress">

<img src="res/Issues分析.png" width="1707" alt="Issue analysis">

<img src="res/WebUI.png" width="1707" alt="WebUI dashboard">

<img src="res/Telegram通知-1.png" width="627" alt="Telegram notification">

<img src="res/Telegram通知-2.png" width="537" alt="Telegram notification">

</div>

---

## 🛠️ Development Guide

### Local Development

```bash
pip install -r requirements.txt
python -m backend.main
```

> First launch will enter Bootstrap mode. Visit `http://localhost:8000/setup` to complete configuration via Setup Wizard.

To debug the first-run deployment / Setup Wizard flow locally, start with an isolated dev config:

```bash
py scripts/dev_bootstrap.py
```

The script uses `.sakura/dev/connection.json`, so it will not overwrite the production `config/connection.json`, and it skips Telegram, SSE, scan, and quota background tasks. To restart from step 0:

```bash
py scripts/dev_bootstrap.py --reset
```

### Code Linting

```bash
python run_ruff.py
```

### Project Structure

```
Sakura-AI-Reviewer/
├── backend/
│   ├── api/               # API routes (webhook, health, v1)
│   │   └── v1/            #   RESTful API v1 (mobile integration, including user_config/billing)
│   ├── core/              # Core config, dynamic configuration, AI provider registry
│   ├── models/            # Data models (SQLAlchemy)
│   ├── services/          # Business logic
│   │   ├── agent_team/    # Agent Expert Team, controlled workspace tools, PR creation, and Skills
│   │   ├── ai_reviewer/   # AI review engine
│   │   │   ├── tools/     #   AI tools (file reading, cross-file search, git info, web search, sakura memory)
│   │   │   └── compression/ # Context compression
│   │   ├── pr_analyzer.py # PR analyzer (strategy selection)
│   │   ├── issue_analyzer.py  # Issue analysis engine
│   │   ├── issue_service.py   # Issue service (labeling, assignment, rewriting)
│   │   ├── issue_embedding_service.py  # Issue vector embedding
│   │   ├── pr_issue_linker.py # PR-Issue linking
│   │   ├── decision_engine.py # Review decision engine
│   │   ├── comment_service.py # Comment service
│   │   ├── rag_service.py     # RAG knowledge base
│   │   ├── code_index_service.py  # Code indexing
│   │   ├── scan_prompt_builder.py # Repository scan prompt builder
│   │   ├── scan_report_service.py # Scan report service
│   │   ├── scan_scheduler.py      # Scan scheduler
│   │   ├── history_context_service.py  # Incremental review history
│   │   ├── sakura_memory_service.py    # .sakura/ project memory service
│   │   ├── sakura_consolidation_agent.py  # .sakura/ memory consolidation agent (tool-call driven)
│   │   ├── sakura_knowledge_extractor.py  # .sakura/ knowledge extraction agent
│   │   ├── github_write_service.py     # GitHub write operations service (.sakura/ writes)
│   │   ├── two_factor_service.py       # TOTP and recovery code service
│   │   ├── webauthn_service.py         # Passkeys/WebAuthn service
│   │   ├── security_admin_service.py   # Security Center admin service
│   │   └── security_audit_service.py   # Security audit service
│   ├── webui/             # WebUI management interface
│   │   ├── routes/        #   Routes (dashboard, config, users, ...)
│   │   ├── templates/     #   Jinja2 templates
│   │   ├── auth.py        #   GitHub OAuth authentication
│   │   └── sse.py         #   SSE real-time push
│   ├── workers/           # Background tasks (review_worker, issue_worker, scan_worker)
│   ├── telegram/          # Telegram Bot (notifications, commands, button menus, permissions)
│   └── bootstrap.py       # Setup Wizard guided configuration
├── config/                # YAML config files (strategies.yaml)
├── docker/                # Docker Compose deployment
└── docs/                  # Project documentation
```

---

## 📚 Documentation

| Document                                                       | Description                                     |
|----------------------------------------------------------------|-------------------------------------------------|
| [Telegram Bot Integration Guide](docs/TELEGRAM_SETUP.md)       | Bot setup, permission system, command reference |
| [Review Approval Feature](docs/APPROVAL_FEATURE_SUMMARY.md)    | Smart review approval system details            |
| [Manual Review Feature](docs/MANUAL_REVIEW_FEATURE.md)         | Super admin manual review triggering            |
| [Model Context Management](docs/MODEL_CONTEXT_FEATURE.md)      | AI model context and compression features       |
| [PR Features Guide](docs/PR_FEATURES_GUIDE.md)                 | PR change summary and dependency graph configuration |
| [Quota System Guide](docs/QUOTA_SYSTEM_GUIDE.md)               | PR/Issue quota usage tracking and auto-reset mechanism |
| [Security & MFA Guide](docs/SECURITY_MFA_GUIDE.md)             | TOTP, recovery codes, Passkeys/WebAuthn, and Security Center |
| [API v1 Reference](docs/api-v1-reference.md)                   | RESTful API documentation (mobile integration)  |
| [WebUI Design Document](docs/plans/2026-03-27-webui-design.md) | WebUI design specification                      |
| [Project Memory Guide](docs/SAKURA_MEMORY_GUIDE.md) | .sakura/ directory structure, lifecycle, configuration (Chinese) |
| [Project Memory System Design](docs/plans/2026-04-20-sakura-memory-design.md) | .sakura/ memory system architecture & config |
| [Agent Expert Team Mode](docs/plans/agent_expert_team_mode.md) | Agent automated fixes, controlled workspaces, and PR creation flow |
| [Agent Skills Implementation](docs/agent-skills-python-implementation.md) | Skill installation, indexing, toggling, and tool integration |
| [Agent File Tools Implementation](docs/agent-file-tools-python-implementation.md) | Agent workspace file tools, security boundaries, and implementation details |
| [Agents Project Guide](AGENTS.md)                              | Project conventions for automation agents and contributors |

---

## 🤝 Contributing

This project uses the standard Gitflow workflow:

- `main`: production release branch, only accepts merges from `release/*` and `hotfix/*`
- `develop`: daily integration branch and the target branch for regular features and fixes
- `feature/*`: feature branches created from `develop` and merged back into `develop`
- `release/*`: release preparation branches created from `develop` and merged into `main`
- `hotfix/*`: urgent production fix branches created from `main` and merged into `main`

Regular contribution flow:

1. Fork this repository
2. Create a feature branch from `develop` (`git checkout develop && git checkout -b feature/amazing-feature`)
3. Commit your changes (`git commit -m 'feat: add some amazing feature'`)
4. Push to the branch (`git push origin feature/amazing-feature`)
5. Open a Pull Request targeting `develop`

Release flow is handled by maintainers: create `release/x.y.z` from `develop`, finish version, documentation, and regression checks, then merge it into `main`; after the merge, Release is published automatically and `main` is merged back into `develop`.

Hotfix flow is handled by maintainers: create `hotfix/x.y.z` from `main`, merge it into `main` after the fix, publish the Release, then merge `main` back into `develop`.

Automation workflows help maintain Gitflow: PR branch policy checks prevent regular branches from being merged directly into `main`; CI runs Ruff and tests on `develop` / `main` PRs and major development branches; after `release/*` or `hotfix/*` is merged into `main`, Release is published automatically and `main` is automatically synced back into `develop` when possible; merged temporary Gitflow branches are cleaned up automatically.

Commit messages should follow the English [Conventional Commits](https://www.conventionalcommits.org/) format.

---

## 📄 License

[GNU Affero General Public License v3.0 (AGPLv3)](LICENSE) — Free to use, modify, and distribute; network services must provide source code.

---

## 🌟 Star History

<a href="https://star-history.com/#Sakura520222/Sakura-AI-Reviewer&Date">
 <picture>
   <source media="(prefers-color-scheme: dark)" srcset="https://api.star-history.com/svg?repos=Sakura520222/Sakura-AI-Reviewer&type=Date&theme=dark" />
   <source media="(prefers-color-scheme: light)" srcset="https://api.star-history.com/svg?repos=Sakura520222/Sakura-AI-Reviewer&type=Date" />
   <img alt="Star History Chart" src="https://api.star-history.com/svg?repos=Sakura520222/Sakura-AI-Reviewer&type=Date" />
 </picture>
</a>

---

<div align="center">

**Sakura AI Reviewer** — Smarter, more efficient code reviews

Made with 🌸 by [Sakura520222](https://github.com/Sakura520222)

Feedback: [Issues](https://github.com/Sakura520222/Sakura-AI-Reviewer/issues) · Email: <Sakura520222@outlook.com>

</div>
