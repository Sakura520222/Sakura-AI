# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

# Sakura AI
## GitHub PR/Issue AI 审查机器人。
### Python 3.14+ / FastAPI / SQLAlchemy(async) / MySQL / Redis / ChromaDB。
### 异步优先，loguru 日志，中文英文双语注释，英文代码。
### Ruff 代码检查。
### 提交用 Conventional Commits (英文)。
### 配置：统一存数据库 app_config；超级管理员非 AI 配置在统一配置页 `/config` 编辑（旧 `/config/general|strategies|labels` 302 重定向；「AI 配置」「系统核心配置」页独立）；策略/标签等节键 `strategy.*`/`label.*` 入库、深度合并默认、移除键宽容恢复；config/ 仅剩 connection.json，默认值以 Settings 代码默认为单一来源。
### 最后更新两个README。
## 在此项目中优先使用中文交流

## 常用开发命令
### 首次安装
uv（推荐，自动创建 `.venv`，含 updater editable 安装与 `uv.lock` 锁定）：
```bash
uv sync
```
传统 pip 方式：
```bash
python -m pip install -r requirements.txt
python -m pip install -e './updater[dev]'
```
依赖单一来源是 `requirements.txt`（pip/Docker 权威）；根 `pyproject.toml` 逐条镜像它，由 `tests/test_uv_pyproject.py` 强制校验，改依赖时两处同步更新。开发依赖清单是 `requirements-dev.txt`（含 dev 组、updater 测试额外项与 SQLite 测试驱动，pip CI 安装它）。

源码开发平台：Windows 11 x86_64、Linux x86_64/aarch64（glibc ≥ 2.28、非 musl，Alpine 不支持）与 Apple Silicon macOS 14+；onnxruntime（chromadb 硬依赖）的 cp314 wheel 未覆盖 Intel Mac / macOS ≤13 / musl（Alpine）/ glibc <2.28，详见根 `pyproject.toml` 的 `[tool.uv]` 注释。可移植测试须在所有支持平台保持可用；平台前置检查只留给 POSIX 部署脚本、Linux 隔离/systemd 或链接权限类场景。

### 启动主应用
```bash
python -m backend.main
```
`python -m backend.main` 是监督循环模式：`backend/` 内代码改动在应用子进程内做模块级热重载（依赖闭包 reload + 路由引用替换，见 `backend/core/hot_reload.py`；`backend/main.py`、`backend/models` 等进程级模块改动提示手动重启）；应用内重启请求（Setup 完成、管理员重启按钮）会优雅停机并自动重新拉起。`python -m uvicorn backend.main:app ...` 直启也可运行，但重启请求只会退出进程，需外部环境（容器/手动）拉起。

Setup Wizard 调试模式会隔离连接配置、跳过后台任务并自动使用 `.venv`（如存在）：
```bash
python scripts/dev_bootstrap.py
```

### 测试
根目录测试递归发现 `tests/`、`updater/tests/` 与 `sandboxer/tests/`（sandboxer 由其 `tests/conftest.py` 自注入 `sandboxer/src`，无需单独安装）；执行前必须完成 updater editable 安装：
```bash
python -m pytest -q
python -m pytest tests/test_main.py -q
python -m pytest tests/test_main.py::test_format_duration_seconds -q
python -m pytest updater/tests -q
python -m pytest sandboxer/tests -q
```

uv 环境等效命令为 `uv run python -m pytest -q` 等（`uv run` 前缀，无需手动激活 `.venv`）。

### Ruff
```bash
ruff check .
python run_ruff.py --check
python run_ruff.py --check backend/services/pr_analyzer.py
python run_ruff.py
```

`run_ruff.py` 不带 `--check` 会修改并格式化文件；只读校验一律带 `--check`。

### Docker
开发 Compose 挂载源码并只启动 Web 服务：
```bash
docker compose -f docker/docker-compose.yml up --build
```

生产 Compose 启动 Web、MySQL 和 Redis；执行前需要准备 `.deploy/deployment.env`：
```bash
docker compose -f docker/docker-compose.prod.yml up -d
```

### 文档站点（VitePress）
`docs/` 同时是 Markdown 文档目录与 VitePress 文档站源码（Node 22，依赖锁定在 `docs/package-lock.json`）：
```bash
npm ci --prefix docs
npm run dev --prefix docs
npm run build --prefix docs
```
`.github/workflows/docs-pages.yml` 对目标为 develop/main 的 PR 做构建检查；push 到 main 自动部署 GitHub Pages（站点 base 为 `/Sakura-AI/`）。站点约定（sidebar 维护、`srcExclude` 排除项、链接仓库根文件用 GitHub 完整 URL）见 `docs/DOCS_MAINTENANCE.md`。

### Updater 构建与验证
`updater` 是独立的 Python 3.14+ 包（全仓统一使用 PEP 758 无括号 `except A, B:` 语法，updater 因此不再兼容 3.13-）。其 native 构建脚本要求在固定的 Python 3.14 Bookworm（glibc 2.36）构建环境中执行；产物 GLIBC 基线 2.36（宿主机需 Debian 12+/Ubuntu 24.04+），构建/运行镜像 pin 同步 `build.sh`、`updater-build.yml`、`release-on-pr-merge.yml` 三处：
```bash
bash updater/build/build.sh
bash updater/build/run-fresh-runtime-smoke.sh /absolute/path/to/binary
```

## 高层架构
- `backend/main.py` 创建 `FastAPI` 应用、注册生命周期管理、CORS、bootstrap 中间件、健康检查、WebUI、Webhook 和 `/api/v1` 路由。正常启动时先初始化异步数据库，再从数据库覆盖动态配置，随后启动可选 Telegram Bot、公告投递恢复、Redis SSE、活动观测 Outbox 和各类调度器；后台任务须经 `create_registered_background_task` 注册，由数据库重置运行时监督器（`backend/services/database_reset_runtime_service.py`）统一准入、优雅停机与重置隔离。
- `backend/core/` 提供配置、启动/bootstrap、日志、认证和跨模块基础设施。`get_settings()` 返回缓存的 `Settings` 单例；运行时配置的优先级是数据库 `app_config`（含 `strategy.*`/`label.*` 节键，经 `backend/core/config_sections.py` 深度合并）高于 Settings 代码默认，建库种子行也统一从 Settings 默认派生（`backend/models/database.py` 的 `_build_default_configs`）。
- 应用时间统一走 `backend/core/time_service.py` 与 `backend/models/time_types.py`（aware、DST 折叠安全）；禁止散用 `datetime.now()` 或 naive datetime，模型时间字段一律用这套类型。
- `backend/models/` 定义 SQLAlchemy 模型并封装异步数据库初始化、自动迁移和默认配置插入。业务服务位于 `backend/services/`，涵盖 GitHub PR/Issue 审查、AI agent 工具链、队列/调度、支付、安全、RAG 和 ChromaDB 向量索引。
- `backend/api/` 提供 Webhook 与认证后的 REST API；`backend/webui/` 提供根路径挂载的 HTML/WebUI 路由、认证、SSE 和页面辅助逻辑。前端静态资源与 WebUI 路由协同工作，修改页面行为时同时检查对应路由，用户可见文案须同步更新 `backend/webui/translations/zh-CN.yaml` 与 `en.yaml`。
- `backend/workers/` 执行异步审查等后台工作；GitHub Webhook 经 `backend/api/webhook.py` 进入审查链路，由 `backend/workers/review_worker.py` 协调 PR 分析、AI 审查、评论发布和 Check Run。`backend/telegram/` 仅保留可选通知绑定 Bot（/start、/bind 绑定握手，历史业务 handler 已不可达）；审查/Issue Worker 的 Telegram 业务通知已移除，公告通知统一走 `backend/services/notification_service.py` 的 Provider 注册表（web/email/telegram 渠道，含投递租约与重试）。Redis 用于 Pub/Sub 等跨进程事件，MySQL 通过 SQLAlchemy async 访问，ChromaDB 保存代码/RAG 向量数据。
- `docker/Dockerfile` 构建 Python 3.14 Bookworm 运行镜像（不含编译工具链），生产 Compose 通过卷提供配置、日志、工作区、Skills 和 ChromaDB 持久化。Agent 多语言工具链（Node 24 LTS / Go 1.27 / Rust stable / OpenJDK 25，trixie 基底）在独立 `sakura-ai-agent-runner` 镜像（`docker/Dockerfile.agent-sandbox`），由 sandboxd 经 digest 钉死的 OCI 容器执行 Agent 命令；sandboxd 守护进程源码在独立 `sandboxer/` 包（`sakura-ai-sandboxer`，src layout、自带测试），镜像为 `docker/Dockerfile.sandboxd`，`sandbox-quality.yml` CI 负责其单测、双镜像构建与真实隔离冒烟。
- `updater/` 使用 `src` layout，提供独立的 `sakura-ai-updater` CLI、Unix socket IPC、持久化 update state 和 backend daemon；它有自己的 `pyproject.toml`、测试和 PyInstaller native 构建链，不依赖主应用运行时。
- 根 `start.sh` 是宿主一键部署/更新入口：构建阶段后台 nohup 断线续跑（进度落 `.deploy/` 阶段文件）、`--prod` 拉取 GHCR 镜像、stable/development 频道切换与 `--status`/`--attach`/`--stop`/`--ps` 状态查询。
- `.sakura/` 是运行时项目记忆/知识目录（见 `docs/SAKURA_MEMORY_GUIDE.md`）；`docs/` 含架构、配置、部署等文档（`docs/README.md` 为 GitHub 与文档站共用的文档目录），`docs/superpowers/` 为开发流程产物（设计稿与实施计划，不生成公开站点页面）。
- Docker 镜像更新支持 stable/development 双频道：主应用 `backend/services/container_registry.py`（注册表查询）与 `backend/core/build_info.py`（当前频道/版本）配合 updater 的 `registry.py`（频道与 digest 解析）；修改更新/频道逻辑时这三处与 WebUI 版本管理页要同步。
## Claude Code 协作约束
### 小修复默认由主代理直接完成；仅在任务确实复杂、存在独立并行子任务或用户明确要求时使用子代理，禁止对同一小改动重复派遣或重复审查。
### Setup Wizard 备份恢复不得覆盖部署期连接配置（数据库连接串、Redis 地址）；`backend/core/setup_service.py` 显式保留这些字段，新增备份导入字段时同样要绕过它们。
### 本地完整测试优先使用 `python -m pytest -q`；根目录递归测试包含 `updater/tests`，执行前必须保证 updater 以 editable 方式可导入（`uv sync` 已含，或 `pip install -e './updater[dev]'`）。
### 单元测试须 mock 外部依赖（MySQL/Redis/GitHub/AI 提供商/ChromaDB）；CI 跑 Ruff 与 pytest，不设覆盖率门槛。
### 仓库根 `AGENTS.md` 是同一套贡献约定的跨 agent 版本（Codex 等读取），更新协作/风格/命令约定时两处同步。它还含本文件未展开的高风险领域契约：通知（Notification Maintenance）、计费定价（Pricing and Billing）、日志与脱敏（Logging and Observability）——改动这些领域前先读对应章节。
## CodeGraph 使用规范
### 本项目已配置 CodeGraph；除写入/修改文件外，优先使用 codegraph_* 工具完成大部分只读操作。
### 阅读代码、查找文件/符号、理解调用关系、分析影响范围、探索模块结构时，优先使用 CodeGraph，而不是直接 grep/read。
### 只有在需要查看具体文本内容、CodeGraph 未覆盖或结果不足时，再使用 read_file、grep_search 等原生读取/搜索工具。
## 永远都不要自主提交变更，也不允许子代理提交
## Gitflow 工作流规范
### 使用标准 Gitflow：main(生产) / develop(集成) / feature/* / release/* / hotfix/*。
### 日常开发：从 develop 创建 feature/<name>，PR 目标为 develop。
### 版本发布：从 develop 创建 release/x.y.z，更新版本号和两个 README，PR 目标为 main。
### 热修复：从 main 创建 hotfix/x.y.z，PR 目标为 main。
### 发布或热修复合入 main 后，必须将 main 回合到 develop。
### 不允许直接 push 到 main / develop。