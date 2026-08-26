<div align="center">

# 🌸 Sakura AI

<img src="res/cover_1.png" alt="Sakura AI Cover" width="100%">

> AI-powered intelligent GitHub Pull Request code review and Issue analysis bot with proactive codebase exploration capabilities

**English** | [中文](README.md)

[![Version](https://img.shields.io/badge/Version-3.2.0-blue.svg)](https://github.com/Sakura520222/Sakura-AI/releases)
[![CI](https://github.com/Sakura520222/Sakura-AI/actions/workflows/ci.yml/badge.svg)](https://github.com/Sakura520222/Sakura-AI/actions/workflows/ci.yml)
[![Python](https://img.shields.io/badge/Python-3.14+-blue.svg)](https://www.python.org/)
[![FastAPI](https://img.shields.io/badge/FastAPI-Latest-green.svg)](https://fastapi.tiangolo.com/)
[![License](https://img.shields.io/badge/License-AGPLv3-yellow.svg)](LICENSE)
[![Stars](https://img.shields.io/github/stars/Sakura520222/Sakura-AI)](https://github.com/Sakura520222/Sakura-AI/stargazers)
[![Last Commit](https://img.shields.io/github/last-commit/Sakura520222/Sakura-AI)](https://github.com/Sakura520222/Sakura-AI/commits)
[![Live Demo](https://img.shields.io/badge/🌐_Free_Demo-Online-success.svg)](https://ai.firefly520.top/)
[![Android App](https://img.shields.io/badge/Android_App-🚧_In_Development-orange.svg)](https://github.com/Sakura520222/Sakura-AI-APP)

</div>

---

## Official Service

**Official Service Platform**: [https://ai.firefly520.top/](https://ai.firefly520.top/)

- **Free Quota**: Register to receive free trial credits for PR review, Issue analysis, and other core features
- **Full Features**: Experience all features including PR review, Issue analysis, Agent task delegation, Repository Aid, and more
- **No Deployment Required**: Ready to use out of the box — no need to set up servers or configure environments

> If you want to self-host or contribute to development, refer to the [Quick Start](#quick-start) section below.

---

## Core Features

### Review Capabilities

- **AI Reasoning Mode** — Deep code analysis, proactively invoking tools to inspect project structure and arbitrary files
- **Cross-file Dependency Understanding** — Multi-turn dialogue with a global view of complex inter-module dependencies
- **Adaptive Review Strategy** — Auto-selects quick / standard / deep mode based on PR size
- **Large PR Compact Review** — Switches to compact diff mode near the context threshold; AI inspects changes on demand
- **Structured Review Reports** — Overall score + categorized issues (Critical / Important / Suggestion) + collapsible sections
- **Incremental Review Continuation** — Restores the previous reviewer's full ActivitySession message history instead of summary injection
- **In-flight Increment Queue** — New `synchronize` commits queued during an active review instead of launching a parallel review
- **On-demand Diff Control** — No hardcoded truncation; content size governed by tool-driven inspection and compression
- **Smart Review Approval** — Auto-decides APPROVE / REQUEST_CHANGES / COMMENT based on AI scores
- **Strict Review Output Contract** — `<SAKURA_REVIEW>` envelope + field validation + multi-round auto-repair + safe degradation, fully observable
- **PR Change Summary** — AI auto-generates and incrementally updates PR summaries
- **PR Dependency Graph** — AI / static dual-mode Mermaid graphs, incrementally stacked with history preserved
- **Token Consumption Tracking** — Real-time tracking of token usage and estimated costs across all AI calls
- **New Live Monitor** — Conversation-first console, Session + Thread, full-chain Provider Attempt / tool / compression projection, encrypted storage with audited decrypt
- **One-click Revoke** — `/revoke` instantly withdraws all AI comments and reviews
- **Auxiliary Model Support** — Independently configure lightweight models for summarization, label recommendation, etc.
- **Inline Comments Toggle** — `enable_inline_comments` controls inline comments on PR diffs
- **Controlled Auto Review** — `enable_auto_review` controls webhook auto-enqueueing
- **Check Runs Progress Visualization** — Main Check 5-step flow + sub Analysis / Findings, display-only semantics that never block merges
- **External CI Failure Injection** — Collects failures from other CI (GitHub Actions / Codecov / lint Apps) as untrusted evidence
- **Review Comment Label Interaction** — Report contains label checkboxes; checking applies/removes labels automatically
- **AI-generated PR Descriptions** — Agents auto-generate descriptions with metadata markers when creating PRs

### AI Tools & Knowledge Base

- **AI Tool System** — read_file / list_directory / search_in_files / get_git_info / list_commits / search_web / read_sakura_docs, invoked on demand
- **Cross-file Code Search** — Locates all usages of functions / variables / classes
- **Git Information Query** — Repository info, branch lists, commit history
- **Web Search Enhancement** — DuckDuckGo / Tavily
- **URL Fetching** — Expands external context needed for review
- **Repository-level Knowledge Base (RAG)** — Vector semantic retrieval of project documentation
- **PR Code Auto-indexing** — Syntax-aware chunking + semantic search for precise code location
- **Project Memory System** — Self-reflection and knowledge accumulation based on `.sakura/`. See [Project Memory Guide](docs/SAKURA_MEMORY_GUIDE.md) (Chinese)

### Repository Scanning

- **AI Full Repository Scan** — Periodic AI-powered scan detecting code quality issues and security vulnerabilities
- **Strict Scan Output Contract** — `<SAKURA_SCAN>` protocol envelope with multi-round format repair and safe degradation
- **Live Scan Conversation Monitoring** — AI dialogue and tool calls during scans are recorded into activity observability in real time, visible on the WebUI activity page
- **Auto-create Issues** — AI summary, trend comparison, severity/category matrix, hotspot files, and folded details; superseded report issues are closed automatically
- **Flexible Scan Configuration** — Interval, cooldown, token budget, concurrency, etc.; the scan prompt focus is editable in the unified config page `strategy.scan` section
- **Scan Management UI** — View scan list, details, and statistics in WebUI
- **Scan Notifications** — Telegram Bot notifications on completion (with AI summary)

### Issue Analysis

- **Intelligent Issue Analysis** — Auto-classification, priority, label recommendation, duplicate detection, linked PR discovery
- **Strict Issue Output Contract** — `<SAKURA_ISSUE_ANALYSIS>` envelope + multi-round repair + safe degradation
- **Auto-labeling** — High-confidence labels applied automatically
- **Auto-assignment** — Assigns to appropriate repository collaborators
- **Title Rewriting** — Auto-improves vague or inaccurate titles
- **Analysis Comment Publishing** — Auto-publishes results and reports status via user-scoped events
- **PR-Issue Linking** — Parses Issue references and injects context
- **Semantic Issue Linking** — Discovers related Issues via vector similarity

### Agent Expert Team

- **Multi-entry Task Creation** — Super-admin manual launch, Issue `/agent` delegation, PR `/agent` one-click fix
- **Multi-branch Parallel Workspaces** — Each task uses an isolated Git worktree, supporting parallel execution
- **Two-agent Collaboration** — Full-stack expert plans and edits; professional reviewer does pre-push quality review
- **Context Compression & Resume** — Long tasks auto-compress history and persist checkpoints for recovery
- **OS-level Tool Isolation** — Agent shell, search, and dependency installation run in one-shot non-root containers with no network, a read-only root filesystem, dropped capabilities, and only the current task worktree mounted
- **Dependency Auto-install & Validation** — Detects and installs `pyproject.toml` / `requirements.txt` dependencies and runs project tests inside the same sandbox boundary, without relying on high-false-positive command blacklists
- **Sakura Knowledge Integration** — Browses `.sakura/` knowledge and reflection files to assist fixes
- **Agent Skills & Built-in Ruff** — Install skills from files / ZIP / GitHub; built-in Ruff lint / format
- **Real-time Admin Intervention** — Inject guidance via WebUI Live View
- **Task Cancellation** — Cancel anytime with safe workspace cleanup
- **PR Creation Loop** — Draft PR + Sakura PR review + human feedback iteration; never auto-merges
- **Non-admin Access Control** — Repository allowlist + dedicated Agent quotas

### Repository Aid

- **Mutual Star Plan** — Stars other members' displayed repositories on your behalf after authorization
- **GitHub App User-to-Server Authorization** — Token stored as encrypted ciphertext, never printed in logs
- **Displayed Repository Selection** — Members choose public repositories to display, with AI-generated summaries
- **Auto-star Scheduling** — Randomized intervals, governed by per-user / per-repository daily limits
- **Idempotency & Audit** — star / unstar / skip / fail all audited; same (actor, target, action) keeps final state
- **Manual Star** — Manually star from the display list, sharing idempotency logic
- **Member & Permission Governance** — Join / leave / pause / ban; offending repositories can be disabled
- **Security Checks** — Rejects cross-user state reuse; GitHub account must match the logged-in user
- **WebUI Management Page** — Members / displayed repositories / today's usage / feature toggles

### Management & Operations

- **Setup Wizard** — First-launch step-by-step guidance with resume support
- **System Core Configuration** — Modify infrastructure settings at runtime via WebUI, audit-logged; super administrators can set an IANA application timezone (restart required)
- **Dynamic Configuration** — Ordinary WebUI changes take effect immediately; restart-required keys such as the application timezone are applied after restart
- **AI API Timeout Control** — `ai_api_timeout_seconds` + `ai_api_total_timeout_seconds`
- **Per-user Config Overrides** — UserConfig → AppConfig → Settings fallback
- **AI Provider Registry** — 20+ built-in vendors, protocol-family-aware model discovery and context windows
- **Persistent AI Account Configuration** — Multiple accounts + role bindings + fallback chains, per-model capability overrides
- **Multi-Protocol Adaptation Layer** — Unified runtime for OpenAI / Anthropic / Gemini native / compatible endpoints
- **Cross-Protocol Fallback** — Backoff retry + cross-vendor switch + context overflow compression
- **GitHub App Installation Management** — Auto-syncs repository authorization status
- **Security Center & MFA** — TOTP / recovery codes / Passkeys / global or per-user MFA enforcement / failure lockout
- **SSE Real-time Push** — Multi-process real-time communication via Redis Pub/Sub
- **Quota-based Access Control** — User self-registration + UTC daily / weekly / monthly auto-reset
- **Paid Quota System** — Plan and redeem code CRUD + admin manual grants
- **External Payments & Refunds** — Stripe / Paddle / Alipay / NOWPayments / TRON USDT
- **Legal Pages** — Built-in terms of service, privacy policy, refund policy, pricing page
- **Admin Action Audit** — Complete operation logs
- **WebUI Dashboard** — Dashboard, PR, user, config, queue, scan, Agent, memory, Repository Aid, vector storage management
- **Batch Issue Indexing** — Vector cache refresh + AI metadata enrichment
- **Health Check Endpoint** — `/health` + Docker Compose auto health detection
- **Telegram Bot** — Real-time notifications, button menus, three-tier permission system
- **GitHub OAuth Login** — Integrated with Telegram user system, light/dark theme switching

---

## Quick Start

### Online Demo (Fastest)

Visit [https://ai.firefly520.top/](https://ai.firefly520.top/) — register for free credits, no deployment needed.

### Docker One-Click Deployment (Recommended for Self-hosting)

**Linux full deployment** (Web + MySQL + Redis + Host Updater + Agent sandboxd):

```bash
sudo install -d -o root -g root -m 0755 /opt/sakura-ai/docker
sudo curl --fail --location \
  https://raw.githubusercontent.com/Sakura520222/Sakura-AI/main/docker/docker-compose.prod.yml \
  --output /opt/sakura-ai/docker/docker-compose.prod.yml
sudo curl --fail --location \
  https://raw.githubusercontent.com/Sakura520222/Sakura-AI/main/start.sh \
  --output /opt/sakura-ai/start.sh
sudo chmod 0644 /opt/sakura-ai/docker/docker-compose.prod.yml
sudo chmod 0755 /opt/sakura-ai/start.sh
cd /opt/sakura-ai
sudo ./start.sh --prod
```

`sudo ./start.sh --prod` creates deployment state, resolves immutable Web, sandboxd, and Agent runner image references for the current Release, starts and verifies the independent sandboxd first, then starts Web/MySQL/Redis and installs the Host Updater. Only sandboxd receives the Docker socket; neither Web nor one-shot runners do. Pressing `Ctrl+C` only detaches the progress view. Releases are checked automatically but require administrator confirmation. Stable updates use one three-image transaction for preflight, pulls, sidecar replacement, Web activation, and rollback; an unavailable Updater never falls back to a Web-only update. macOS, Windows, and container-only deployments do not provide this Linux OS sandbox or Host Updater; see the [Deployment Guide](docs/DEPLOYMENT.md).

> **Synchronizing Host Updater after a WebUI update:** A stable WebUI update transaction updates Web, sandboxd, and the Agent runner together, but it does not replace the currently running Host Updater binary. After the update succeeds and `/health` reports the new version, run the following command in `/opt/sakura-ai` to align the Updater binary with that Release:
>
> ```bash
> sudo ./start.sh updater reinstall
> ```
>
> `reinstall` first uses the updater's internal lock to atomically close new submissions and prove that no job is active, then stops, installs, starts, and reports the new daemon state; if installation fails, it attempts to restore the existing daemon. If an older updater does not support the atomic maintenance gate, the command fails closed and requires the administrator to stop that legacy daemon explicitly first. The installer selects a concrete Sakura AI Release from deployment state, but it does not enforce application health. Treat a successful `/health` response containing the expected new version as a mandatory manual prerequisite; do not continue if the health check fails, is unavailable, or reports a different version. See the [Host Updater section of the Deployment Guide](docs/DEPLOYMENT.md#webui-更新后同步-host-updater) for complete verification steps.

Uninstall preserves Docker data volumes by default. Only explicit `--purge` removes the volumes and `.deploy` state:

```bash
sudo ./start.sh uninstall          # Preserve data for a later redeployment
sudo ./start.sh uninstall --purge  # Permanently delete database/cache volumes and deployment state
```

**Web image only** (bring your own MySQL/Redis):

```bash
docker run -d -p 8000:8000 \
  -e DATABASE_URL=mysql+asyncmy://user:pass@host:3306/sakura_ai \
  -e REDIS_URL=redis://host:6379/0 \
  -v $(pwd)/config:/app/config \
  ghcr.io/sakura520222/sakura-ai:latest
```

This mode does not include the Host Updater. It can report available releases, but it cannot apply an update from the WebUI.

`latest` always means the stable production channel. Development builds are opt-in from the WebUI Version Manager and require an explicit risk confirmation; updates use the immutable GHCR `dev-...` tag plus manifest digest. `edge` is only a moving development alias and is never persisted as an update target.

After first start, visit `http://localhost:8000/setup`. The app prints a one-time verification token in the startup log; enter it at `/setup/verify` to access the wizard (the token is regenerated on every restart):

```bash
# In /opt/sakura-ai: tail live logs / extract the first-deploy token
docker compose --env-file .deploy/deployment.env --project-name sakura-ai \
  -f docker/docker-compose.prod.yml logs -f --tail=200 web
```

For persisted DEBUG logs, error filtering, and more see [Deployment Guide · View Runtime Logs](docs/DEPLOYMENT.md#八查看运行日志).

### Source Development

```bash
git clone https://github.com/Sakura520222/Sakura-AI.git
cd Sakura-AI
pip install -r requirements.txt
python -m backend.main
```

> In local development, changes under `backend/` are hot-reloaded at module level inside the app subprocess (no process restart); changes to process-level modules such as `backend/main.py` or database models log a hint to restart manually. In-app restart requests (Setup completion, admin restart button) still respawn the whole process via the supervision loop.

> Deployment details (image tags, pinned versions, GitHub App creation, database setup, full Setup Wizard flow, Host Updater daemon, upgrade and password rotation) are in the [Deployment Guide](docs/DEPLOYMENT.md) (Chinese).

---

## Screenshots

<div align="center">

<img src="res/发送正在审查中和自动打标.png" width="1901" alt="Review in progress">

<img src="res/Issues分析.png" width="1707" alt="Issue analysis">

<img src="res/WebUI.png" width="1707" alt="WebUI dashboard">

<img src="res/Telegram通知-1.png" width="627" alt="Telegram notification">

<img src="res/Telegram通知-2.png" width="537" alt="Telegram notification">

</div>

---

## Technical Architecture

```
GitHub (PR / Issue / OAuth)
        │ Webhook / OAuth / API
        ▼
FastAPI Web Server ── Webhook Handler · PR Analyzer · Comment Service
        │              WebUI (Jinja2 + HTMX + Alpine.js) · SSE Push
        ▼
AI Review Engine ── read_file · list_dir · search_files · git_info · commits
                   search_web · RAG · Code Index · read_sakura_docs/memory
        ▼
Data Storage ── MySQL (Business) · Redis (Queue/PubSub) · ChromaDB (Vectors)
```

**Tech Stack**: FastAPI (Python 3.14+) · Jinja2 + Tailwind CSS + HTMX + Alpine.js · Multi-protocol AI (OpenAI / Anthropic / Gemini / compatible) · MySQL 8.0 + Redis + ChromaDB · GitHub App + OAuth · Docker Compose

Full architecture diagram, data flow, code structure, and interactive knowledge graph are in the [Architecture Guide](docs/ARCHITECTURE.md) (Chinese).

---

## Development

```bash
pip install -r requirements.txt      # Install dependencies
python -m backend.main               # Start the app
python run_ruff.py                   # Lint + fix + format
python run_ruff.py --check           # Read-only check
python -m pytest -q                  # Run tests
tail -f "$(ls -t logs/app_*.log | head -n1)"  # Tail latest run log (DEBUG)
```

First launch enters Bootstrap mode — a one-time verification token is printed to the terminal; enter it at `/setup/verify`, then visit `http://localhost:8000/setup` to complete configuration. To debug the Setup Wizard flow, use `py scripts/dev_bootstrap.py` (isolated dev config, skips background tasks).

> Run logs are written to `logs/app_*.log` (one file per startup, 500 MB rotation, 10-day retention; passwords and tokens are auto-redacted). For Docker log-viewing commands see the [Deployment Guide · View Runtime Logs](docs/DEPLOYMENT.md#八查看运行日志).

> The Updater is an independent Python 3.14+ package (`updater/`) with its own `pyproject.toml`, tests, and PyInstaller native build chain — see the [updater directory](updater/) for development. Its release binaries are built on Bookworm (glibc 2.36) and require the host to run glibc ≥ 2.36 (Debian 12+ / Ubuntu 24.04+).

---

## Documentation

Full documentation index at [docs/README.md](docs/README.md). Common entries:

| Document | Description |
|---|---|
| [Deployment Guide](docs/DEPLOYMENT.md) | Docker / source deployment, GitHub App, Setup Wizard, Host Updater (Chinese) |
| [Configuration Reference](docs/CONFIGURATION.md) | All config options: location, key, description (Chinese) |
| [Architecture Guide](docs/ARCHITECTURE.md) | Architecture diagram, tech stack, code structure (Chinese) |
| [Telegram Bot Integration](docs/TELEGRAM_SETUP.md) | Bot setup, permission system, command reference (Chinese) |
| [Review Protocol Spec](docs/PR_REVIEW_PROTOCOL.md) | `<SAKURA_REVIEW>` protocol, validation, repair (Chinese) |
| [Security & MFA Guide](docs/SECURITY_MFA_GUIDE.md) | TOTP, recovery codes, Passkeys, Security Center (Chinese) |
| [API v1 Reference](docs/api-v1-reference.md) | RESTful API v1 (mobile OAuth, MFA, SSE, Billing) (Chinese) |
| [Contributor Conventions](AGENTS.md) | Project conventions for automation agents and contributors |

---

## Contributing

This project uses the standard Gitflow workflow: `main` (production) ← `release/*` / `hotfix/*`; `develop` (integration) ← `feature/*`.

1. Fork this repository
2. Create a feature branch from `develop`: `git checkout develop && git checkout -b feature/amazing-feature`
3. Commit changes (English [Conventional Commits](https://www.conventionalcommits.org/)): `git commit -m 'feat: add some amazing feature'`
4. Push and open a Pull Request targeting `develop`

Releases and hotfixes are handled by maintainers: create `release/x.y.z` from `develop` (or `hotfix/x.y.z` from `main`), merge into `main` to auto-publish a Release, then `main` is synced back to `develop`. Automation enforces PR branch flow, runs CI, and cleans up merged temporary branches.

---

## License

[GNU Affero General Public License v3.0 (AGPLv3)](LICENSE) — Free to use, modify, and distribute; network services must provide source code.

---

## Star History

<a href="https://star-history.com/#Sakura520222/Sakura-AI&Date">
 <picture>
   <source media="(prefers-color-scheme: dark)" srcset="https://api.star-history.com/svg?repos=Sakura520222/Sakura-AI&type=Date&theme=dark" />
   <source media="(prefers-color-scheme: light)" srcset="https://api.star-history.com/svg?repos=Sakura520222/Sakura-AI&type=Date" />
   <img alt="Star History Chart" src="https://api.star-history.com/svg?repos=Sakura520222/Sakura-AI&type=Date" />
 </picture>
</a>

---

<div align="center">

**Sakura AI** — Smarter, more efficient code reviews

Made by [Sakura520222](https://github.com/Sakura520222)

Feedback: [Issues](https://github.com/Sakura520222/Sakura-AI/issues) · Email: <Sakura520222@outlook.com>

</div>
