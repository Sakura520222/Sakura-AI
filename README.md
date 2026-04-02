# 🌸 Sakura AI Reviewer

> 基于 AI 的智能 GitHub Pull Request 代码审查与 Issue 分析机器人，具备主动探索代码库的能力

[![Python](https://img.shields.io/badge/Python-3.11+-blue.svg)](https://www.python.org/)
[![FastAPI](https://img.shields.io/badge/FastAPI-Latest-green.svg)](https://fastapi.tiangolo.com/)
[![License](https://img.shields.io/badge/License-AGPLv3-yellow.svg)](LICENSE)
[![Live Demo](https://img.shields.io/badge/🌐_免费体验-Online-success.svg)](https://pr-bot.firefly520.top/webui)

---

## ✨ 核心特性

- **AI 推理模式**：利用 AI 推理能力进行深度代码分析，主动调用工具查看项目结构和任意文件
- **跨文件依赖理解**：通过多轮对话理解模块间的复杂依赖关系，具备"全域视野"
- **自适应审查策略**：根据 PR 规模自动选择快速/标准/深度审查模式
- **结构化审查报告**：整体评分 + 分类问题（🔴严重/🟡重要/💡优化）+ 折叠详情
- **智能审查批准**：基于 AI 评分自动决策 APPROVE / REQUEST_CHANGES / COMMENT
- **智能标签推荐**：AI 自动分类并推荐 PR 标签，高置信度自动应用
- **Issue 智能分析**：自动分类、优先级判定、标签推荐、重复检测、关联 PR 发现
- **PR-Issue 关联**：自动解析 Issue 引用，注入上下文增强审查精度
- **AI 工具系统**：read_file、list_directory、search_web，AI 按需主动调用
- **仓库级知识库（RAG）**：向量语义检索项目文档，为 AI 审查提供规范上下文
- **PR 代码自动索引**：语法感知分块 + 语义搜索，AI 可精准定位相关代码
- **Telegram Bot**：实时通知、三级权限体系（超级管理员/管理员/普通用户）、配额管理
- **WebUI 管理界面**：仪表盘、PR 管理、用户管理、仓库白名单、配置管理、队列监控
- **GitHub OAuth 登录**：与 Telegram 用户体系打通，明暗主题切换

---

## 🏗️ 技术架构

```
┌─────────────────────────────────────────────────────────────┐
│                        GitHub PR                             │
└──────────┬───────────────────────────────┬──────────────────┘
           │ Webhook                       │ OAuth / API
           ▼                               ▼
┌─────────────────────────────────────────────────────────────┐
│                    FastAPI Web Server                        │
│  ┌──────────────┐  ┌──────────────┐  ┌──────────────┐      │
│  │   Webhook    │  │   PR 分析器   │  │  评论服务    │      │
│  │   Handler    │  │  (策略选择)   │  │  (发布结果)  │      │
│  │ (PR+Issue)   │  │             │  │             │      │
│  └──────────────┘  └──────────────┘  └──────────────┘      │
│  ┌──────────────────────────────────────────────────────┐   │
│  │              WebUI 管理界面 (Jinja2 + HTMX)          │   │
│  └──────────────────────────────────────────────────────┘   │
└──────────────────────┬──────────────────────────────────────┘
                       │
                       ▼
┌─────────────────────────────────────────────────────────────┐
│                     AI 审查引擎                              │
│  ┌────────────┐  ┌────────────┐  ┌────────────┐            │
│  │ read_file  │  │list_dir    │  │ search_web │            │
│  └────────────┘  └────────────┘  └────────────┘            │
└──────────────────────┬──────────────────────────────────────┘
                       │
                       ▼
┌─────────────────────────────────────────────────────────────┐
│                    数据存储层                                │
│  ┌──────────────┐  ┌──────────────┐  ┌──────────────┐      │
│  │    MySQL     │  │    Redis     │  │  ChromaDB    │      │
│  │  (审查记录)   │  │   (队列)     │  │  (向量检索)  │      │
│  └──────────────┘  └──────────────┘  └──────────────┘      │
└─────────────────────────────────────────────────────────────┘
```

**技术栈**：FastAPI (Python 3.11+) · Jinja2 + Tailwind CSS + HTMX · DeepSeek-R1 / OpenAI 兼容 API · MySQL 8.0 + Redis + ChromaDB · GitHub App (PyGithub) + OAuth · Docker + Docker Compose

---

## 🚀 快速开始

### 1. 环境要求

- Linux 服务器（推荐 Ubuntu 20.04+）
- Docker 和 Docker Compose
- 公网 IP 和域名
- GitHub 账号
- DeepSeek API Key（或其他 OpenAI 兼容 API）

### 2. 克隆与配置

```bash
git clone https://github.com/Sakura520222/Sakura-AI-Reviewer.git
cd Sakura-AI-Reviewer
cp .env.example .env
```

编辑 `.env` 文件，主要配置项：

```env
# GitHub App
GITHUB_APP_ID=your_app_id
GITHUB_PRIVATE_KEY="-----BEGIN RSA PRIVATE KEY-----\n...\n-----END RSA PRIVATE KEY-----"
GITHUB_WEBHOOK_SECRET=your_webhook_secret

# AI 模型
OPENAI_API_BASE=https://api.deepseek.com
OPENAI_API_KEY=your_api_key
OPENAI_MODEL=deepseek-reasoner

# 数据库（Docker 容器通过 host.docker.internal 访问宿主机）
DATABASE_URL=mysql+aiomysql://root:your_password@host.docker.internal:3306/sakura-pr
REDIS_URL=redis://host.docker.internal:6379/0

# 应用
APP_DOMAIN=your-domain.com
APP_PORT=8000
WEBUI_SECRET_KEY=your-random-secret-key

# GitHub OAuth（WebUI 登录）
GITHUB_OAUTH_CLIENT_ID=your_client_id
GITHUB_OAUTH_CLIENT_SECRET=your_client_secret
GITHUB_OAUTH_REDIRECT_URI=https://your-domain.com/webui/auth/callback
```

### 3. 创建 GitHub App

1. 访问 [GitHub Apps 设置](https://github.com/settings/apps)，点击 **New GitHub App**
2. 填写名称、Homepage URL
3. **Repository permissions**：Pull requests `Read and write`，Contents `Read-only`，Issues `Read and write`（可选）
4. **Webhook URL**：`https://your-domain.com:8000/api/webhook/github`，填写 Webhook secret
5. **Webhook events**：勾选 Pull requests、Pull request reviews、Issues（可选）、Issue comments（可选）
6. 创建后，在 App 页面底部 **Generate a private key**，下载 `.pem` 文件并转为单行格式填入 `.env`
7. 点击左侧 **Install App**，选择要启用审查的仓库

> WebUI 登录需额外创建 [OAuth App](https://github.com/settings/developers)，回调地址设为 `https://your-domain.com/webui/auth/callback`

### 4. 准备数据库

在宿主机安装并启动 MySQL 和 Redis：

```bash
sudo apt update && sudo apt install mysql-server redis-server -y
sudo systemctl start mysql && sudo systemctl start redis
sudo mysql -e "CREATE DATABASE IF NOT EXISTS \`sakura-pr\` CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;"
sudo mysql -e "CREATE USER IF NOT EXISTS 'root'@'%' IDENTIFIED BY 'your_password';"
sudo mysql -e "GRANT ALL PRIVILEGES ON *.* TO 'root'@'%';"
sudo mysql -e "FLUSH PRIVILEGES;"
```

### 5. 启动服务

```bash
cd docker
docker-compose up -d
```

### 6. 验证部署

```bash
curl http://your-domain.com:8000/health
# {"status":"healthy","service":"Sakura AI Reviewer"}
```

WebUI：`https://your-domain.com/webui/`

---

## 📖 使用说明

### PR 审查

在已安装 App 的仓库中创建 PR，AI 会自动审查并发布结构化报告。在 PR 中可使用以下命令：

- `/full-review` — 清理旧评论并触发全量重新审查（PR 作者或协作者）
- `/revoke` — 一键撤回所有 AI 评论和 Review（仅管理员）

### Issue 分析

- **自动分析**：Issue opened/edited/reopened 时自动触发，发布分类、优先级、标签建议
- **手动触发**：在 Issue 中评论 `/analyze`

### WebUI 管理

访问 `https://your-domain.com/webui/`，使用 GitHub 账号登录（需先在 Telegram Bot 中注册）。支持仪表盘、PR 管理、用户管理、仓库白名单、配置管理、审查队列监控等功能。

### Telegram Bot

提供实时通知（审查开始/完成）、配额管理、权限控制（三级体系）和丰富的管理命令。详见 [Telegram Bot 集成指南](docs/TELEGRAM_SETUP.md)。

---

## ⚙️ 配置说明

所有配置遵循优先级：**数据库 app_config > .env > config/*.yaml**

- **审查策略**：编辑 `config/strategies.yaml`，支持快速/标准/深度/大PR 四种策略
- **文件过滤**：在 `config/strategies.yaml` 中配置跳过的文件扩展名和路径
- **AI 工具**：`.env` 中 `ENABLE_AI_TOOLS` / `MAX_TOOL_ITERATIONS`
- **标签推荐**：`.env` 中 `ENABLE_LABEL_RECOMMENDATION` / `LABEL_CONFIDENCE_THRESHOLD`
- **审查批准**：`config/strategies.yaml` 中 `review_policy` 配置阈值和仓库级覆盖
- **RAG 知识库**：`.env` 中配置嵌入模型、重排序模型、ChromaDB 等
- **PR 代码索引**：`.env` 中配置代码分块、支持语言、核心目录等
- **模型上下文**：`.env` 中配置上下文窗口、自动压缩等，详见 [模型上下文管理](docs/MODEL_CONTEXT_FEATURE.md)

---

## 🖥️ 效果展示

<div align="center">

<img src="res/发送正在审查中和自动打标.png" width="80%" alt="审查进行中">

<img src="res/PR审查完成示例.png" width="80%" alt="审查报告">

<img src="res/Issues分析.png" width="80%" alt="Issue分析">

<img src="res/WebUI.png" width="80%" alt="WebUI管理界面">

<img src="res/Telegram通知-1.png" width="80%" alt="Telegram通知">

<img src="res/Telegram通知-2.png" width="80%" alt="Telegram通知">

</div>

---

## 🛠️ 开发指南

### 本地开发

```bash
pip install -r requirements.txt
cp .env.example .env
python -m backend.main
```

### 代码检查

```bash
python run_ruff.py
```

### 代码结构

```
Sakura-AI-Reviewer/
├── backend/
│   ├── api/               # API 路由
│   ├── core/              # 核心配置
│   ├── models/            # 数据模型
│   ├── services/          # 业务逻辑
│   │   ├── ai_reviewer/   # AI 审查引擎 + 工具系统
│   │   ├── pr_analyzer.py # PR 分析器（策略选择）
│   │   ├── issue_analyzer.py  # Issue 分析引擎
│   │   ├── decision_engine.py # 审查决策引擎
│   │   └── comment_service.py # 评论服务
│   ├── webui/             # WebUI（Jinja2 + HTMX）
│   ├── workers/           # 后台任务（review_worker, issue_worker）
│   └── telegram/          # Telegram Bot
├── config/                # YAML 配置文件
├── docker/                # Docker Compose
└── docs/                  # 项目文档
```

---

## 📚 详细文档

| 文档 | 说明 |
| ---- | ---- |
| [Telegram Bot 集成指南](docs/TELEGRAM_SETUP.md) | Bot 设置、权限体系、命令参考 |
| [审查批准功能](docs/APPROVAL_FEATURE_SUMMARY.md) | 智能审查批准系统详细说明 |
| [手动审查功能](docs/MANUAL_REVIEW_FEATURE.md) | 超级管理员手动触发审查 |
| [模型上下文管理](docs/MODEL_CONTEXT_FEATURE.md) | AI 模型上下文和压缩功能 |
| [WebUI 设计文档](docs/plans/2026-03-27-webui-design.md) | WebUI 设计规范 |

---

## 🤝 贡献

1. Fork 本项目
2. 创建特性分支 (`git checkout -b feature/AmazingFeature`)
3. 提交更改 (`git commit -m 'feat: add some amazing feature'`)
4. 推送到分支 (`git push origin feature/AmazingFeature`)
5. 开启 Pull Request

---

## 📄 许可证

[GNU Affero General Public License v3.0 (AGPLv3)](LICENSE) — 自由使用、修改和分发，网络服务需提供源代码。

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

**Sakura AI Reviewer** — 让代码审查更智能、更高效

Made with 🌸 by [Sakura520222](https://github.com/Sakura520222)

问题反馈：[Issues](https://github.com/Sakura520222/Sakura-AI-Reviewer/issues) · 邮箱：<Sakura520222@outlook.com>

</div>
