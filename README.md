<div align="center">

# 🌸 Sakura AI

<img src="res/cover_1.png" alt="Sakura AI Cover" width="100%">

> 基于 AI 的智能 GitHub Pull Request 代码审查与 Issue 分析机器人，具备主动探索代码库的能力

[English](README_EN.md) | **中文**

[![Version](https://img.shields.io/badge/Version-3.2.0-blue.svg)](https://github.com/Sakura520222/Sakura-AI/releases)
[![CI](https://github.com/Sakura520222/Sakura-AI/actions/workflows/ci.yml/badge.svg)](https://github.com/Sakura520222/Sakura-AI/actions/workflows/ci.yml)
[![Python](https://img.shields.io/badge/Python-3.14+-blue.svg)](https://www.python.org/)
[![FastAPI](https://img.shields.io/badge/FastAPI-Latest-green.svg)](https://fastapi.tiangolo.com/)
[![License](https://img.shields.io/badge/License-AGPLv3-yellow.svg)](LICENSE)
[![Stars](https://img.shields.io/github/stars/Sakura520222/Sakura-AI)](https://github.com/Sakura520222/Sakura-AI/stargazers)
[![Last Commit](https://img.shields.io/github/last-commit/Sakura520222/Sakura-AI)](https://github.com/Sakura520222/Sakura-AI/commits)
[![Live Demo](https://img.shields.io/badge/🌐_免费体验-Online-success.svg)](https://ai.firefly520.top/)
[![Android App](https://img.shields.io/badge/Android_App-🚧_开发中-orange.svg)](https://github.com/Sakura520222/Sakura-AI-APP)

</div>

---

## 官方服务

**官方服务平台**：[https://ai.firefly520.top/](https://ai.firefly520.top/)

- **免费额度**：注册即赠免费体验额度，可立即使用 PR 审查、Issue 分析等核心功能
- **完整功能**：体验全部功能，包括 PR 审查、Issue 分析、Agent 任务委派、仓库互助等
- **无需部署**：开箱即用，无需自行搭建服务器和配置环境

> 如果你想自建实例或进行二次开发，请参考下方的 [快速开始](#快速开始) 部分。

---

## 核心特性

### 审查能力

- **AI 推理模式** — 深度代码分析，主动调用工具查看项目结构与任意文件
- **跨文件依赖理解** — 多轮对话理解模块间依赖，具备全域视野
- **自适应审查策略** — 按 PR 规模自动选择快速 / 标准 / 深度模式
- **大型 PR 精简审查** — diff 接近阈值时切 compact 模式，AI 按需查看变更
- **结构化审查报告** — 整体评分 + 分类问题（严重 / 重要 / 优化）+ 折叠详情
- **增量审查续跑** — 恢复上一轮 reviewer 完整消息历史，而非摘要注入
- **运行中增量入队** — 审查中新提交入队合并，不并行启动新审查
- **按需 diff 控制** — 不引入硬编码截断，靠工具按需读取与上下文压缩
- **智能审查批准** — 按评分自动决策 APPROVE / REQUEST_CHANGES / COMMENT
- **严格审查输出契约** — `<SAKURA_REVIEW>` 协议 + 字段校验 + 多轮自动修复 + 安全降级，全链路可观测
- **PR 变更自动总结** — AI 生成并在 PR 更新时增量更新
- **PR 依赖图生成** — AI / 静态双模式 Mermaid 图，增量叠加保留历史
- **Token 消耗追踪** — 实时追踪所有 AI API 调用的 token 与成本
- **新版实时监控** — 对话优先控制台，Session + Thread，Provider Attempt / 工具 / 压缩全链路投影，加密存储与审计解密
- **一键撤回** — `/revoke` 一键撤回所有 AI 评论与 Review
- **辅助模型支持** — 独立配置轻量模型处理摘要、标签等任务
- **行内评论开关** — `enable_inline_comments` 控制 PR diff 行内评论
- **可控自动审查** — `enable_auto_review` 控制 webhook 自动入队
- **Check Runs 进度可视化** — 主 Check 5 步流程 + 副 Analysis / Findings，展示语义不阻塞合并
- **外部 CI 失败注入** — 采集其他 CI（GitHub Actions / Codecov / lint App）失败作为不可信证据
- **审查评论标签交互** — 报告含标签复选框，勾选自动应用 / 移除
- **AI 生成 PR 描述** — Agent 创建 PR 时自动生成带元数据标记的描述

### AI 工具与知识库

- **AI 工具系统** — read_file / list_directory / search_in_files / get_git_info / list_commits / search_web / read_sakura_docs 等，按需调用
- **跨文件代码搜索** — 定位函数 / 变量 / 类的所有使用位置
- **Git 信息查询** — 仓库信息、分支列表、提交历史
- **Web 搜索增强** — DuckDuckGo / Tavily
- **URL 抓取** — 扩展审查所需外部上下文
- **仓库级知识库（RAG）** — 向量语义检索项目文档
- **PR 代码自动索引** — 语法感知分块 + 语义搜索精准定位
- **项目记忆系统** — 基于 `.sakura/` 的自我反思与知识积累。详见 [项目记忆系统使用指南](docs/SAKURA_MEMORY_GUIDE.md)

### 仓库扫描

- **AI 全仓库扫描** — 定期全面扫描，发现代码质量与安全问题
- **严格扫描输出契约** — `<SAKURA_SCAN>` 协议信封 + 多轮格式修复 + 安全降级
- **实时扫描对话监控** — 扫描期间的 AI 对话与工具调用实时记录到活动观测，WebUI 活动页可查看
- **自动创建 Issue** — AI 总结 + 趋势对比 + 严重性/类别矩阵 + 热点文件 + 折叠明细；自动关闭被取代的旧报告 Issue
- **灵活扫描配置** — 间隔、冷却、Token 预算、并发等；扫描提示词 focus 在统一配置页 `strategy.scan` 节编辑
- **扫描管理界面** — WebUI 查看列表、详情、统计
- **扫描通知** — Telegram Bot 推送完成通知（含 AI 总结）

### Issue 分析

- **Issue 智能分析** — 自动分类、优先级、标签推荐、重复检测、关联 PR 发现
- **图片多模态分析** — Issue 正文与评论中的截图经 GitHub 凭据安全下载，超过 5 MiB 时优先压缩至 500 KiB，再作为多模态输入交给 AI（需模型高级配置勾选"支持图片多模态"，并在统一配置页开启）
- **严格 Issue 输出契约** — `<SAKURA_ISSUE_ANALYSIS>` 协议 + 多轮修复 + 安全降级
- **Issue 自动打标** — 高置信度标签自动应用
- **Issue 自动指派** — 指派给合适的仓库协作者
- **Issue 标题改写** — 自动优化模糊标题
- **分析评论发布** — 自动发布结果并报告状态
- **PR-Issue 关联** — 解析 Issue 引用注入上下文
- **语义 Issue 关联** — 向量相似度发现并关联相关 Issue

### Agent 专家团队

- **多入口任务创建** — 超级管理员手动启动、Issue 评论 `/agent` 委派、PR 评论 `/agent` 一键修复
- **多分支并行工作区** — 每任务独立 Git worktree 隔离，同仓库多任务并行
- **双 Agent 协作** — 全栈专家负责计划与修改，专业审查负责推送前复核
- **上下文压缩与恢复** — 长任务自动压缩历史，持久化检查点支持失败续跑
- **OS 级工具隔离** — Agent shell、搜索和依赖安装进入一次性非 root 容器；默认断网、只读根文件系统、丢弃 capabilities，并只挂载当前任务 worktree
- **自动依赖与验证** — 在相同沙箱边界内检测安装 `pyproject.toml` / `requirements.txt` 依赖并运行项目测试，不再依赖高误报命令黑名单
- **Sakura 知识集成** — 浏览 `.sakura/` 知识与反思辅助修复
- **Agent Skills 与内置 Ruff** — 从文件 / ZIP / GitHub 安装技能，内置 Ruff lint / format
- **实时管理员干预** — WebUI Live View 注入指导意见
- **任务取消支持** — 随时取消并安全释放工作区
- **PR 创建闭环** — Draft PR + Sakura PR 审查 + 人工反馈迭代，不自动合并
- **普通用户权限控制** — 仓库白名单 + 独立 Agent 配额

### 仓库互助

- **互助点星计划** — 授权后代表你为其他成员的展示仓库点星，互相引流
- **GitHub App user-to-server 授权** — 加密密文存储 token，日志不打印原文
- **展示仓库选择** — 成员选择公开仓库参与展示，AI 生成仓库摘要
- **自动点星调度** — 随机间隔执行，受每用户 / 每仓库每日上限控制
- **幂等与审计** — star / unstar / skip / fail 全部审计，同 (actor, target, action) 保留最终态
- **手动点星** — 列表中手动点星，与自动共用幂等逻辑
- **成员与权限治理** — 加入 / 退出 / 暂停 / 封禁，违规仓库可禁用
- **安全校验** — 拒绝跨用户 state 复用，GitHub 账号必须与登录用户一致
- **WebUI 管理页** — 成员 / 展示仓库 / 今日用量 / 功能开关

### 管理与运维

- **Setup Wizard** — 首次启动分步引导，支持断点续配
- **系统核心配置管理** — 运行时修改基础设施配置（数据库、GitHub App、Telegram、域名等），审计记录；超级管理员可设置应用 IANA 时区（保存后重启）
- **动态配置管理** — 普通 WebUI 配置修改即时生效；应用时区等重启键保存后按提示重启
- **AI API 超时治理** — `ai_api_timeout_seconds` + `ai_api_total_timeout_seconds`
- **用户级配置覆盖** — UserConfig → AppConfig → Settings 逐级回退
- **AI Provider 注册表** — 内置 20+ 厂商，协议族感知模型发现与上下文窗口
- **AI 账号持久化配置页** — 多账号 + 角色绑定 + 回退链，每模型独立覆盖能力
- **多协议适配层** — OpenAI / Anthropic / Gemini 原生 / 兼容端点统一运行时
- **跨协议故障转移** — 退避重试 + 跨厂商切换 + 上下文超限压缩
- **GitHub App 安装管理** — 自动同步仓库授权状态
- **安全中心与多因素认证** — TOTP / 恢复码 / Passkeys / 全局或单用户强制 MFA / 失败锁定
- **SSE 实时推送** — 基于 Redis Pub/Sub 的多进程实时通信
- **配额制访问控制** — 用户自注册 + UTC 日 / 周 / 月自动重置
- **付费配额系统** — 套餐计划与兑换码 CRUD + 管理员手动充值
- **外部支付与退款** — Stripe / Paddle / 支付宝 / NOWPayments / TRON USDT 直收
- **法律页面** — 内置服务条款、隐私政策、退款政策、定价页
- **管理员操作审计** — 完整操作日志
- **WebUI 管理界面** — 仪表盘、PR、用户、配置、队列、扫描、Agent、记忆、仓库互助、向量库管理
- **批量 Issue 索引** — 向量缓存刷新 + AI 元数据增强
- **健康检查端点** — `/health` + Docker Compose 自动健康检测
- **Telegram Bot** — 实时通知、按钮菜单、三级权限体系
- **GitHub OAuth 登录** — 与 Telegram 用户体系打通，明暗主题切换

---

## 快速开始

### 在线体验（最快）

访问 [https://ai.firefly520.top/](https://ai.firefly520.top/)，注册即赠免费额度，无需部署。

### Docker 一键部署（推荐自建）

**Linux 全量部署**（Web + MySQL + Redis + Host Updater + Agent sandboxd）：

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

`sudo ./start.sh --prod` 会自动生成部署状态，解析当前 Release 的 Web、sandboxd 与 Agent runner 三个不可变镜像引用，先启动并验证独立 sandboxd，再启动 Web/MySQL/Redis，最后安装 Host Updater。只有 sandboxd 持有 Docker socket；Web 与一次性 runner 均不持有。按 `Ctrl+C` 只退出进度查看，后台部署仍会继续。新版本会自动检查，但安装需超级管理员确认；稳定版更新以三镜像事务完成预检、拉取、sidecar 重建、Web 激活与失败回滚，Updater 不可用时不会退回 Web-only 更新。macOS、Windows 和仅容器部署不提供该 Linux OS 沙箱或 Host Updater；细节见[部署指南](docs/DEPLOYMENT.md)。

> **WebUI 更新后的 Updater 同步：** WebUI 的稳定版更新事务会一起更新 Web、sandboxd 和 Agent runner，但不会替换正在运行的 Host Updater 二进制。应用更新完成并确认 `/health` 已返回新版本后，可在 `/opt/sakura-ai` 执行以下命令，使 Updater 二进制也与当前 Release 保持一致：
>
> ```bash
> sudo ./start.sh updater reinstall
> ```
>
> `reinstall` 会先通过 updater 内部锁原子关闭新任务提交并确认没有活动任务，再依次停止、安装、启动并输出新状态；安装失败时会尝试恢复原有 daemon。旧版 updater 若不支持原子维护门禁，命令会 fail-closed，并要求管理员先显式停止旧 daemon。安装器按部署状态选择具体 Sakura AI Release，但它本身不会强制检查应用健康状态。请把“`/health` 成功返回预期新版本”作为必须人工确认的前置条件；如果健康检查失败、不可用或版本不符，请勿执行。完整验证方法见[部署指南的 Host Updater 章节](docs/DEPLOYMENT.md#webui-更新后同步-host-updater)。

卸载分两级：标准卸载保留数据库等 Docker 数据卷，可随时重新部署；显式 `--purge` 完全卸载会删除数据卷、全部镜像（Web/MySQL/Redis/sandboxd/Agent runner）和 `.deploy` 部署状态。两种模式共用同一确认词 `UNINSTALL`：

```bash
sudo ./start.sh uninstall          # 标准卸载：保留数据，可重新部署
sudo ./start.sh uninstall --purge  # 完全卸载：永久删除数据卷、镜像与部署状态
```

**仅 Web 镜像**（MySQL/Redis 自备）：

```bash
docker run -d -p 8000:8000 \
  -e DATABASE_URL=mysql+asyncmy://user:pass@host:3306/sakura_ai \
  -e REDIS_URL=redis://host:6379/0 \
  -v $(pwd)/config:/app/config \
  ghcr.io/sakura520222/sakura-ai:latest
```

此方式不包含 Host Updater，只提供版本检查，不支持在 WebUI 中执行更新。

`latest` 始终代表正式稳定版。开发版仅通过 WebUI 版本管理器的“开发版”通道按明确风险确认选择；开发构建由 GHCR 的不可变 `dev-...` tag 与 manifest digest 标识，`edge` 只是移动别名，不是部署目标。

首次启动后访问 `http://localhost:8000/setup`。应用会在启动日志中打印一次性验证 Token，需在 `/setup/verify` 输入后才能进入向导（Token 每次启动重新生成）：

```bash
# 在 /opt/sakura-ai 下查看实时日志 / 提取首次部署 Token
docker compose --env-file .deploy/deployment.env --project-name sakura-ai \
  -f docker/docker-compose.prod.yml logs -f --tail=200 web
```

落盘 DEBUG 日志、错误过滤等更多查看方式见[部署指南 · 查看运行日志](docs/DEPLOYMENT.md#八查看运行日志)。

### 源码开发

> 源码开发平台：Linux x86_64/arm64（glibc ≥ 2.28，非 musl；Alpine 不支持）或 Apple Silicon macOS 14+。其余平台因上游 onnxruntime 未发布对应 Python 3.14 wheel（且无 sdist）无法安装依赖，pip 方式同样受限。

**uv 方式（推荐）**：

```bash
git clone https://github.com/Sakura520222/Sakura-AI.git
cd Sakura-AI
uv sync                # 自动创建 .venv 并安装全部依赖(含 updater)
uv run python -m backend.main
```

**传统 pip 方式（无 uv）**：

```bash
pip install -r requirements.txt
pip install -e './updater[dev]'
python -m backend.main
```

> 本地开发模式下，`backend/` 内的代码改动会在应用子进程内做模块级热重载（不重启进程）；`backend/main.py`、数据库模型等进程级模块的改动会提示手动重启。应用内重启请求（Setup 完成、管理员重启按钮）仍由监督循环整进程重新拉起。

> 部署细节（镜像 Tag、固定版本、GitHub App 创建、数据库准备、Setup Wizard 全流程、Host Updater 守护进程、升级与密码轮换）详见 [部署指南](docs/DEPLOYMENT.md)。

---

## 效果展示

<div align="center">

<img src="res/发送正在审查中和自动打标.png" width="1901" alt="审查进行中">

<img src="res/Issues分析.png" width="1707" alt="Issue分析">

<img src="res/WebUI.png" width="1707" alt="WebUI管理界面">

<img src="res/Telegram通知-1.png" width="627" alt="Telegram通知">

<img src="res/Telegram通知-2.png" width="537" alt="Telegram通知">

</div>

---

## 技术架构

```
GitHub (PR / Issue / OAuth)
        │ Webhook / OAuth / API
        ▼
FastAPI Web Server ── Webhook Handler · PR 分析器 · 评论服务
        │              WebUI (Jinja2 + HTMX + Alpine.js) · SSE 实时推送
        ▼
AI 审查引擎 ── read_file · list_dir · search_files · git_info · commits
              search_web · RAG · 代码索引 · read_sakura_docs/memory
        ▼
数据存储 ── MySQL (业务) · Redis (队列/PubSub) · ChromaDB (向量)
```

**技术栈**：FastAPI (Python 3.14+) · Jinja2 + Tailwind CSS + HTMX + Alpine.js · 多协议 AI（OpenAI / Anthropic / Gemini / 兼容） · MySQL 8.0 + Redis + ChromaDB · GitHub App + OAuth · Docker Compose

完整架构图、数据流、代码结构与交互式知识图谱详见 [技术架构](docs/ARCHITECTURE.md)。

---

## 开发指南

```bash
uv sync                              # 安装依赖(传统方式:pip install -r requirements.txt)
uv run python -m backend.main        # 启动应用(pip 环境用 python -m backend.main)
python run_ruff.py                   # 代码检查 + 修复 + 格式化
python run_ruff.py --check           # 只读检查
uv run python -m pytest -q           # 运行测试(pip 环境用 python -m pytest -q)
tail -f "$(ls -t logs/app_*.log | head -n1)"  # 查看最新运行日志（DEBUG）
```

首次启动进入 Bootstrap 模式，终端会打印一次性验证 Token，在 `/setup/verify` 输入后访问 `http://localhost:8000/setup` 完成配置。调试 Setup Wizard 流程可用 `py scripts/dev_bootstrap.py`（隔离 dev 配置，跳过后台任务）。

> 运行日志落盘在 `logs/app_*.log`（每次启动一个文件、500 MB 轮转、保留 10 天，自动脱敏密码与 Token）；Docker 部署的完整查看命令见[部署指南 · 查看运行日志](docs/DEPLOYMENT.md#八查看运行日志)。

> Updater 是独立的 Python 3.14+ 包（`updater/`），有自己的 `pyproject.toml`、测试与 PyInstaller native 构建链，开发方式见 [updater 文档](updater/)。其发布二进制在 Bookworm（glibc 2.36）环境构建，宿主机需 glibc ≥ 2.36（Debian 12+/Ubuntu 24.04+）。

---

## 文档

完整文档索引见 [docs/README.md](docs/README.md)，常用入口：

| 文档 | 说明 |
|---|---|
| [部署指南](docs/DEPLOYMENT.md) | Docker / 源码部署、GitHub App、Setup Wizard、Host Updater |
| [配置参考](docs/CONFIGURATION.md) | 全部配置项的位置、键名与说明 |
| [技术架构](docs/ARCHITECTURE.md) | 架构图、技术栈、代码结构 |
| [Telegram Bot 集成](docs/TELEGRAM_SETUP.md) | Bot 设置、权限体系、命令参考 |
| [审查协议规范](docs/PR_REVIEW_PROTOCOL.md) | `<SAKURA_REVIEW>` 协议、字段校验、修复降级 |
| [安全与 MFA 指南](docs/SECURITY_MFA_GUIDE.md) | TOTP、恢复码、Passkeys、安全中心 |
| [API v1 参考文档](docs/api-v1-reference.md) | RESTful API v1（移动端 OAuth、MFA、SSE、Billing） |
| [贡献者约定](AGENTS.md) | 自动化代理与贡献者项目约定 |

---

## 贡献

本项目使用标准 Gitflow 工作流：`main`（生产）← `release/*` / `hotfix/*`；`develop`（集成）← `feature/*`。

1. Fork 本项目
2. 基于 `develop` 创建特性分支：`git checkout develop && git checkout -b feature/amazing-feature`
3. 提交更改（英文 [Conventional Commits](https://www.conventionalcommits.org/)）：`git commit -m 'feat: add some amazing feature'`
4. 推送并开启 PR，目标分支选择 `develop`

发布与热修复由维护者执行：从 `develop` 创建 `release/x.y.z`（或从 `main` 创建 `hotfix/x.y.z`），合入 `main` 后自动发布 Release 并回合到 `develop`。自动化工作流会校验 PR 分支流向、运行 CI、清理已合并的临时分支。

---

## 许可证

[GNU Affero General Public License v3.0 (AGPLv3)](LICENSE) — 自由使用、修改和分发，网络服务需提供源代码。

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

**Sakura AI** — 让代码审查更智能、更高效

Made by [Sakura520222](https://github.com/Sakura520222)

问题反馈：[Issues](https://github.com/Sakura520222/Sakura-AI/issues) · 邮箱：<Sakura520222@outlook.com>

</div>
