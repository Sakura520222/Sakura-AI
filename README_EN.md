**English** | [中文](README.md)

# 🌸 Sakura AI Reviewer

> AI-powered intelligent GitHub Pull Request code review and Issue analysis bot with proactive codebase exploration capabilities

[![Version](https://img.shields.io/badge/Version-2.7.6-blue.svg)](https://github.com/Sakura520222/Sakura-AI-Reviewer/releases)
[![Python](https://img.shields.io/badge/Python-3.11+-blue.svg)](https://www.python.org/)
[![FastAPI](https://img.shields.io/badge/FastAPI-Latest-green.svg)](https://fastapi.tiangolo.com/)
[![License](https://img.shields.io/badge/License-AGPLv3-yellow.svg)](LICENSE)
[![Live Demo](https://img.shields.io/badge/🌐_Free_Demo-Online-success.svg)](https://pr-bot.firefly520.top/webui)

---

## ✨ Core Features

### Review Capabilities

- **AI Reasoning Mode**: Leverages AI reasoning for in-depth code analysis, proactively invoking tools to inspect project structure and arbitrary files
- **Cross-file Dependency Understanding**: Understands complex inter-module dependencies through multi-turn dialogue with a "global view"
- **Adaptive Review Strategy**: Automatically selects quick/standard/deep review mode based on PR size
- **Structured Review Reports**: Overall score + categorized issues (🔴Critical / 🟡Important / 💡Suggestion) + `<details>` collapsible sections
- **Incremental Review Learning**: AI automatically summarizes historical review records, identifies scoring trends and issue hotspots, continuously improving review quality
- **Smart Review Approval**: Automatically decides APPROVE / REQUEST_CHANGES / COMMENT based on AI scores
- **PR Change Summary**: AI auto-generates PR change summaries with incremental updates when the PR is updated
- **PR Dependency Graph**: AI analyzes import/module dependencies of changed files and generates Mermaid-format visual dependency graphs
- **Token Consumption Tracking**: Real-time tracking of token usage and estimated costs across all AI API calls during review
- **One-click Revoke**: Admins can use `/revoke` to instantly withdraw all AI comments and reviews
- **Auxiliary Model Support**: Independently configure lightweight models for summarization, context compression, label recommendation, and other tasks to reduce inference costs

### AI Tools & Knowledge Base

- **AI Tool System**: read_file, list_directory, search_web — AI proactively invokes tools on demand
- **Web Search Enhancement**: Supports DuckDuckGo / Tavily, allowing AI to actively search the internet to assist review decisions
- **Repository-level Knowledge Base (RAG)**: Vector semantic retrieval of project documentation, providing normative context for AI reviews
- **PR Code Auto-indexing**: Syntax-aware chunking + semantic search, enabling AI to precisely locate relevant code

### Issue Analysis

- **Intelligent Issue Analysis**: Auto-classification, priority assessment, label recommendation, duplicate detection, linked PR discovery
- **Auto-labeling**: AI categorizes and recommends labels; high-confidence labels are applied automatically
- **Auto-assignment**: AI analyzes issue content and automatically assigns it to appropriate repository collaborators
- **Title Rewriting**: AI automatically improves vague or inaccurate issue titles
- **PR-Issue Linking**: Automatically parses issue references and injects context to enhance review precision
- **Semantic Issue Linking**: Discovers and links related issues based on vector semantic similarity

### Management & Operations

- **Setup Wizard**: Automatically detects configuration status on first launch, guides you through GitHub App, database, AI model, and RAG setup step by step, with resume support
- **Dynamic Configuration**: Configuration changes via WebUI take effect immediately without service restart
- **GitHub App Installation Management**: Automatically handles GitHub App install/uninstall events, syncing repository authorization status
- **SSE Real-time Push**: Multi-process real-time communication based on Redis Pub/Sub, with instant WebUI data updates
- **Quota-based Access Control**: Flexible quota-based access management system with user self-registration support
- **Admin Action Audit**: Complete operation logs covering configuration changes, user management, and other critical actions
- **WebUI Dashboard**: Dashboard charts, PR management, user management, configuration management, queue monitoring, action logs
- **Telegram Bot**: Real-time notifications, three-tier permission system (super admin / admin / user), quota management
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
│  │ read_file  │  │ list_dir   │  │ search_web │            │
│  └────────────┘  └────────────┘  └────────────┘            │
│  ┌────────────┐  ┌────────────┐  ┌────────────┐            │
│  │ RAG Search │  │ Code Index │  │  History   │            │
│  └────────────┘  └────────────┘  └────────────┘            │
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
3. **Repository permissions**: Pull requests `Read and write`, Contents `Read-only`, Issues `Read and write` (optional)
4. **Webhook URL**: `https://your-domain.com:8000/api/webhook/github`, enter Webhook secret
5. **Webhook events**: Check Pull requests, Pull request reviews, Issues (optional), Issue comments (optional)
6. After creation, click **Generate a private key** at the bottom of the App page, download the `.pem` file (paste the full private key content in Setup Wizard)
7. Click **Install App** on the left sidebar, select the repositories to enable review

> WebUI login requires an additional [OAuth App](https://github.com/settings/developers) with callback URL set to `https://your-domain.com/webui/auth/callback`

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

WebUI: `https://your-domain.com/webui/`

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

Visit `https://your-domain.com/webui/` and log in with your GitHub account (requires prior registration via Telegram Bot). Features include dashboard charts, PR management, user management, dynamic configuration, review queue monitoring, action logs, and more. Configuration changes take effect immediately without service restart.

### Telegram Bot

Provides real-time notifications (review started/completed), quota management, permission control (three-tier system), and rich admin commands. See [Telegram Bot Integration Guide](docs/TELEGRAM_SETUP.md) for details.

---

## ⚙️ Configuration

All configuration follows this priority: **Database app_config (WebUI) > Settings defaults**. YAML config files (`config/strategies.yaml`, `config/labels.yaml`) manage review strategies and label definitions.

> **Dynamic Configuration**: Changes made via the WebUI configuration page take effect immediately without service restart. Supports multiple configuration groups including AI models, auxiliary models, RAG, web search, code indexing, and more.

- **AI Model**: Set API URL, API Key, and model name in WebUI configuration
- **Auxiliary Model**: Set `summary_model`, `summary_api_base`, `summary_api_key` in WebUI configuration for lightweight tasks like summarization, context compression, and label recommendation; auto-falls back to main model if left empty
- **Review Strategy**: Edit `config/strategies.yaml`, supports quick/standard/deep/large-PR four strategies
- **File Filtering**: Configure skipped file extensions and paths in `config/strategies.yaml`
- **AI Tools**: `enable_ai_tools` / `max_tool_iterations` in WebUI configuration
- **Label Recommendation**: `config/labels.yaml` for PR label recommendation toggle and confidence; Issue labels at `issue_auto_create_labels` / `issue_confidence_threshold` in global config
- **Review Approval**: `review_policy` in `config/strategies.yaml` for threshold and repository-level overrides
- **PR Change Summary**: `enable_pr_summary` in WebUI configuration
- **PR Dependency Graph**: `enable_pr_dependency_graph` / `pr_dependency_graph_max_nodes` / `pr_dependency_graph_max_files` in WebUI configuration
- **Token Cost Tracking**: `review_price_per_1k_prompt` / `review_price_per_1k_completion` in WebUI configuration for tracking review token consumption and costs
- **RAG Knowledge Base**: Configure embedding models (supports BAAI/bge-m3, etc.), reranking models, ChromaDB in WebUI configuration
- **PR Code Index**: Configure code chunking, supported languages, core directories in WebUI configuration
- **Issue Auto-assignment**: `issue_auto_assign` / `issue_assignee_confidence_threshold` in WebUI configuration
- **Issue Title Rewriting**: `issue_auto_rewrite_title` in WebUI configuration
- **Semantic Issue Linking**: `enable_semantic_issue_linking` / `semantic_issue_similarity_threshold` in WebUI configuration
- **Incremental Review History**: `enable_incremental_history_context` in WebUI configuration, AI auto-learns from historical review records
- **Web Search Tool**: `web_search_provider` in WebUI configuration (`duckduckgo` free or `tavily` premium)
- **Model Context**: Configure context window, auto-compression in WebUI configuration, see [Model Context Management](docs/MODEL_CONTEXT_FEATURE.md)

---

## 🖥️ Screenshots

<div align="center">

<img src="res/发送正在审查中和自动打标.png" width="80%" alt="Review in progress">

<img src="res/PR审查完成示例.png" width="80%" alt="Review report">

<img src="res/Issues分析.png" width="80%" alt="Issue analysis">

<img src="res/WebUI.png" width="80%" alt="WebUI dashboard">

<img src="res/Telegram通知-1.png" width="80%" alt="Telegram notification">

<img src="res/Telegram通知-2.png" width="80%" alt="Telegram notification">

</div>

---

## 🛠️ Development Guide

### Local Development

```bash
pip install -r requirements.txt
python -m backend.main
```

> First launch will enter Bootstrap mode. Visit `http://localhost:8000/setup` to complete configuration via Setup Wizard.

### Code Linting

```bash
python run_ruff.py
```

### Project Structure

```
Sakura-AI-Reviewer/
├── backend/
│   ├── api/               # API routes (webhook, health)
│   ├── core/              # Core config, dynamic configuration
│   ├── models/            # Data models (SQLAlchemy)
│   ├── services/          # Business logic
│   │   ├── ai_reviewer/   # AI review engine
│   │   │   ├── tools/     #   AI tools (file reading, web search)
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
│   │   └── history_context_service.py  # Incremental review history
│   ├── webui/             # WebUI management interface
│   │   ├── routes/        #   Routes (dashboard, config, users, ...)
│   │   ├── templates/     #   Jinja2 templates
│   │   ├── auth.py        #   GitHub OAuth authentication
│   │   └── sse.py         #   SSE real-time push
│   ├── workers/           # Background tasks (review_worker, issue_worker)
│   ├── telegram/          # Telegram Bot (notifications, commands, permissions)
│   └── bootstrap.py       # Setup Wizard guided configuration
├── config/                # YAML config files (strategies.yaml)
├── docker/                # Docker Compose deployment
└── docs/                  # Project documentation
```

---

## 📚 Documentation

| Document | Description |
| ---- | ---- |
| [Telegram Bot Integration Guide](docs/TELEGRAM_SETUP.md) | Bot setup, permission system, command reference |
| [Review Approval Feature](docs/APPROVAL_FEATURE_SUMMARY.md) | Smart review approval system details |
| [Manual Review Feature](docs/MANUAL_REVIEW_FEATURE.md) | Super admin manual review triggering |
| [Model Context Management](docs/MODEL_CONTEXT_FEATURE.md) | AI model context and compression features |
| [WebUI Design Document](docs/plans/2026-03-27-webui-design.md) | WebUI design specification |

---

## 🤝 Contributing

1. Fork this repository
2. Create a feature branch (`git checkout -b feature/AmazingFeature`)
3. Commit your changes (`git commit -m 'feat: add some amazing feature'`)
4. Push to the branch (`git push origin feature/AmazingFeature`)
5. Open a Pull Request

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
