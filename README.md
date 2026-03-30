# 🌸 Sakura AI Reviewer

> 基于 AI 的智能 GitHub Pull Request 代码审查与 Issue 分析机器人，具备主动探索代码库的能力

[![Python](https://img.shields.io/badge/Python-3.11+-blue.svg)](https://www.python.org/)
[![FastAPI](https://img.shields.io/badge/FastAPI-Latest-green.svg)](https://fastapi.tiangolo.com/)
[![License](https://img.shields.io/badge/License-AGPLv3-yellow.svg)](LICENSE)

---

## ✨ 核心特性

### 🤖 AI 驱动的深度审查

- **AI 推理模式**：利用 AI 的推理能力进行深度代码分析
- **主动探索代码库**：AI 可以自主调用工具查看项目结构和任意文件
- **跨文件依赖理解**：通过多轮对话理解模块间的复杂依赖关系
- **上下文感知**：不局限于 PR diff，AI 具备"全域视野"

### 🏷️ 智能标签推荐

- **AI 自动分类**：基于代码变更内容自动推荐合适的 PR 标签
- **置信度评分**：为每个推荐标签提供置信度评分（0-100%）
- **存量标签感知**：自动识别仓库现有的自定义标签，优先匹配已有标签
- **智能应用**：高置信度标签自动应用，低置信度标签提供建议
- **可视化展示**：在审查报告中清晰展示标签推荐理由

### 🛠️ AI 函数工具系统

- **read_file**：查看任意文件的完整内容
- **list_directory**：列出目录结构，了解项目组织
- **智能决策**：AI 根据需要主动调用工具

### 🤖 Telegram Bot 集成

- **实时通知**：PR 开始审查、审查完成通知
- **配额管理**：每日/每周/每月配额系统
- **权限控制**：三级权限体系（超级管理员/管理员/普通用户）
- **命令管理**：丰富的管理命令支持
- **白名单机制**：仓库和用户白名单控制

### 🎯 智能审查批准

- **多维度决策**：基于AI评分和问题严重程度自动决策
- **三种审查状态**：APPROVE（批准）、REQUEST_CHANGES（请求变更）、COMMENT（评论）
- **智能阈值**：可配置的批准/阻断阈值
- **幂等性保护**：自动检查避免重复提交Review
- **灵活配置**：支持仓库级别的策略覆盖

### 📊 自适应审查策略

- **⚡️ 快速审查**：小改动（≤5 文件，≤200 行）
- **🔍 标准审查**：中等改动（≤20 文件，≤1000 行）
- **🔬 深度审查**：大改动（≤100 文件），分批处理
- **⏭️ 智能跳过**：文档、配置文件自动跳过

### 🎯 结构化审查报告

- **整体评分**：代码质量评分（1-10 分）
- **分类问题**：严重问题 🔴、重要建议 🟡、优化建议 💡
- **详细摘要**：变更概述和主要发现
- **可操作建议**：提供具体的改进方案

### 🐛 Issue 智能分析

- **自动触发**：Issue opened/edited/reopened 时自动 AI 分析
- **智能分类**：自动识别 bug、feature、question、enhancement 等类型
- **优先级判定**：基于关键词和 AI 推理判定 critical/high/medium/low
- **标签推荐**：高置信度标签自动应用，低置信度标签提供建议
- **重复检测**：基于 GitHub Search API 检测重复 Issue
- **关联 PR 发现**：自动查找与 Issue 相关的 PR
- **手动触发**：在 Issue 中评论 `/analyze` 即可手动触发分析
- **独立配额**：Issue 分析拥有独立的日/周/月配额体系

### 🔗 PR-Issue 智能关联

- **自动识别**：从 PR 描述中解析 Issue 引用（fixes #123、closes #456 等）
- **上下文注入**：关联 Issue 内容自动注入到 AI 审查 prompt
- **审查增强**：AI 结合 Issue 上下文提供更精准的审查意见

### 🖥️ WebUI 管理界面

- **📊 仪表盘**：统计概览、最近审查记录，一目了然
- **📋 PR 审查管理**：列表、详情、文件级评论查看，支持搜索和筛选
- **👥 用户与权限管理**：角色管理（超级管理员/管理员/普通用户）、配额设置
- **📂 仓库白名单管理**：可视化管理授权仓库
- **⚙️ 配置管理**：在线编辑审查策略和标签配置，无需修改 YAML
- **🔄 审查队列监控**：实时查看审查队列状态和统计
- **🔐 GitHub OAuth 登录**：安全登录，与 Telegram 用户体系打通
- **🌓 明暗主题切换**：支持 Light/Dark 主题，跟随系统偏好

### 📚 仓库级知识库（RAG）

- **文档语义检索**：基于向量相似度检索项目文档
- **增量更新**：文档变更自动索引，支持幂等性更新
- **上下文增强**：AI 审查时自动获取相关项目规范和文档
- **重排序优化**：使用重排序模型提升检索质量

### 🔍 PR 自动索引代码

- **自动触发**：PR 打开/更新时自动索引变更代码
- **语法感知分块**：根据编程语言特性智能分块
- **语义搜索**：AI 可通过语义搜索找到相关代码
- **丰富元数据**：包含函数名、类名、行号等详细信息

---

## 🎬 实际效果展示

### AI 审查流程实录

```
✅ 第 1 轮：调用 list_directory 查看项目结构
✅ 第 2 轮：调用 read_file 阅读 bot.py（主程序）
✅ 第 3 轮：调用 read_file 阅读 config.py（配置）
✅ 第 4 轮：调用 read_file 阅读 database.py（数据层）
✅ 第 5 轮：综合分析，生成 8 条有价值的审查评论
```

**AI 能力**：

- 🔍 主动探索项目架构
- 📖 阅读关键文件源码
- 🧠 理解跨文件依赖
- 💡 提供专业建议

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
│  ┌──────────────┐                                           │
│  │ Issue 分析器  │  (分类·优先级·标签·重复检测)               │
│  └──────────────┘                                           │
│  ┌──────────────────────────────────────────────────────┐   │
│  │              WebUI 管理界面 (Jinja2 + HTMX)          │   │
│  │  仪表盘 · PR管理 · 用户管理 · 仓库管理 · 配置管理   │   │
│  └──────────────────────────────────────────────────────┘   │
└──────────────────────┬──────────────────────────────────────┘
                       │
                       ▼
┌─────────────────────────────────────────────────────────────┐
│                     AI 审查引擎                              │
│  ┌──────────────────────────────────────────────────────┐   │
│  │         AI (推理模式)                       │   │
│  │  ┌────────────┐  ┌────────────┐  ┌────────────┐    │   │
│  │  │ read_file  │  │list_dir    │  │ 多轮对话   │    │   │
│  │  │   工具     │  │   工具      │  │   推理     │    │   │
│  │  └────────────┘  └────────────┘  └────────────┘    │   │
│  └──────────────────────────────────────────────────────┘   │
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

**技术栈**：

- **后端框架**：FastAPI (Python 3.11+)
- **前端界面**：Jinja2 + Tailwind CSS + HTMX
- **AI 模型**：DeepSeek-R1 / OpenAI 兼容 API（推理模式）
- **数据库**：MySQL 8.0 + Redis + ChromaDB（向量存储）
- **GitHub 集成**：GitHub App (PyGithub) + OAuth
- **部署**：Docker + Docker Compose

---

## 🚀 快速开始

### 1. 环境要求

- Linux 服务器（推荐 Ubuntu 20.04+）
- Docker 和 Docker Compose
- 公网 IP 和域名
- GitHub 账号
- DeepSeek API Key（或其他 OpenAI 兼容 API）

### 2. 克隆项目

```bash
git clone https://github.com/Sakura520222/Sakura-AI-Reviewer.git
cd Sakura-AI-Reviewer
```

### 3. 配置环境变量

```bash
cp .env.example .env
```

编辑 `.env` 文件：

```env
# GitHub App 配置
GITHUB_APP_ID=your_github_app_id
GITHUB_PRIVATE_KEY="-----BEGIN RSA PRIVATE KEY-----\n...\n-----END RSA PRIVATE KEY-----"
GITHUB_WEBHOOK_SECRET=your_webhook_secret

# GitHub App机器人用户名（可选）
# 用于幂等性检查，防止重复提交Review
# 如果不配置，系统会尝试从GitHub API自动获取
BOT_USERNAME=

# DeepSeek API 配置
OPENAI_API_BASE=https://api.deepseek.com
OPENAI_API_KEY=your_deepseek_api_key
OPENAI_MODEL=deepseek-reasoner

# 数据库配置
# 连接到宿主机的 MySQL（Docker 容器通过 host.docker.internal 访问宿主机）
DATABASE_URL=mysql+aiomysql://root:your_password@host.docker.internal:3306/sakura-pr

# Redis配置
# 连接到宿主机的 Redis
REDIS_URL=redis://host.docker.internal:6379/0

# 应用配置
APP_DOMAIN=your-domain.com
APP_PORT=8000

# WebUI 配置
WEBUI_SECRET_KEY=your-random-secret-key-change-in-production

# GitHub OAuth 配置（用于 WebUI 登录）
# 需要在 GitHub Settings > Developer settings > OAuth Apps 中创建
GITHUB_OAUTH_CLIENT_ID=your-github-oauth-client-id
GITHUB_OAUTH_CLIENT_SECRET=your-github-oauth-client-secret
GITHUB_OAUTH_REDIRECT_URI=https://your-domain.com/webui/auth/callback
```

### 4. 创建 GitHub App

#### 4.1 在 GitHub 上创建新 App

1. 访问 [GitHub Apps 设置页面](https://github.com/settings/apps)
2. 点击 **"New GitHub App"** 按钮
3. 填写基本信息：
   - **GitHub App name**: `Sakura AI Reviewer`（或你喜欢的名称）
   - **Homepage URL**: `https://your-domain.com`
   - **Application description**: `基于 AI 的智能代码审查机器人`

4. 配置权限（Repository permissions）：
   - **Pull requests**: `Read and write`
   - **Contents**: `Read-only`
   - **Issues**: `Read and write`（可选，用于在 Issue 中回复）
   - **Metadata**: `Read-only`

5. 配置 Webhook：
   - **Webhook URL**: `https://your-domain.com:8000/api/webhook/github`
   - **Webhook secret**: 生成一个随机字符串，保存到 `.env` 的 `GITHUB_WEBHOOK_SECRET`
   - 勾选 **Active**

6. 选择 Webhook 事件：
   - ✅ **Pull requests**
   - ✅ **Pull request reviews**
   - ✅ **Issues**（可选，用于 Issue 智能分析）
   - ✅ **Issue comments**（可选，用于 `/analyze` 命令）

7. 点击 **"Create GitHub App"**

#### 4.2 生成私钥

1. 在创建的 App 页面底部，找到 **"Private keys"** 部分
2. 点击 **"Generate a private key"** 按钮
3. 下载生成的 `.pem` 文件（文件名类似 `Sakura-AI-Reviewer.(timestamp).pem`）
4. 打开 `.pem` 文件，复制全部内容
5. 将私钥转换为单行格式，填入 `.env` 的 `GITHUB_PRIVATE_KEY`：

```bash
# Linux/Mac 格式化私钥（将换行符替换为 \n）
awk 'NF {sub(/\r/, ""); printf "%s\\n",$0;}' your-key.pem
```

或者手动将：

```
-----BEGIN RSA PRIVATE KEY-----
MIIEpAIBAAKCAQEA...
...
-----END RSA PRIVATE KEY-----
```

转换为：

```
-----BEGIN RSA PRIVATE KEY-----\nMIIEpAIBAAKCAQEA...\n...\n-----END RSA PRIVATE KEY-----\n
```

#### 4.3 获取 App ID

1. 在 App 页面顶部，找到 **"App ID"**（数字格式，如 `123456`）
2. 将其填入 `.env` 的 `GITHUB_APP_ID`

#### 4.4 安装 GitHub App

1. 在 App 页面左侧菜单，点击 **"Install App"**
2. 选择 **"Install to your account"** 或 **"Install to your organization"**
3. 选择要启用审查的仓库（可以选择"所有仓库"或特定仓库）
4. 点击 **"Install"** 完成安装

#### 4.5 创建 GitHub OAuth App（WebUI 登录）

WebUI 使用 GitHub OAuth 进行登录认证，需要额外创建一个 OAuth App：

1. 访问 [GitHub Developer Settings](https://github.com/settings/developers)
2. 点击 **"New OAuth App"** 按钮
3. 填写信息：
   - **Application name**: `Sakura AI Reviewer WebUI`
   - **Homepage URL**: `https://your-domain.com`
   - **Authorization callback URL**: `https://your-domain.com/webui/auth/callback`
4. 点击 **"Register application"**
5. 生成 **Client Secret**，将 `Client ID` 和 `Client Secret` 填入 `.env` 的对应配置项

> **注意**：WebUI 登录需要用户已在 Telegram Bot 中注册（通过 `/user_add` 命令添加）。

#### 4.6 验证配置

创建一个测试 Pull Request，检查是否收到 AI 审查评论：

- App 应该会在 PR 中发布一条"正在审查中..."的占位评论
- 几分钟后，占位评论会被替换为完整的审查报告

### 5. 准备数据库环境

由于项目使用 `host.docker.internal` 连接宿主机的数据库，您需要在宿主机上安装并启动 MySQL 和 Redis：

```bash
# Ubuntu/Debian 安装 MySQL 和 Redis
sudo apt update
sudo apt install mysql-server redis-server -y

# 启动服务
sudo systemctl start mysql
sudo systemctl start redis

# 创建数据库和用户（根据您的配置修改）
sudo mysql -e "CREATE DATABASE IF NOT EXISTS \`sakura-pr\` CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;"
sudo mysql -e "CREATE USER IF NOT EXISTS 'root'@'%' IDENTIFIED BY 'your_password';"
sudo mysql -e "GRANT ALL PRIVILEGES ON *.* TO 'root'@'%';"
sudo mysql -e "FLUSH PRIVILEGES;"

# 可选：导入初始化脚本（如果需要）
# sudo mysql < docker/mysql-init/init.sql
```

### 6. 启动服务

```bash
cd docker
docker-compose up -d
```

### 8. 验证部署

```bash
curl http://your-domain.com:8000/health
```

应返回：

```json
{
  "status": "healthy",
  "service": "Sakura AI Reviewer"
}
```

访问 WebUI：`https://your-domain.com/webui/`，使用 GitHub 账号登录。

---

## 📖 使用说明

### 安装 GitHub App

1. 在 GitHub 上创建并安装你的 App
2. 选择要启用审查的仓库
3. App 会自动接收 Webhook 事件

### 创建 PR

1. 在已安装 App 的仓库中创建 Pull Request
2. Sakura AI 会立即开始审查（会显示"正在审查中..."占位评论）
3. AI 可能会主动查看项目文件（通过工具调用）
4. 审查完成后，占位评论会被替换为完整的审查报告

### 查看审查报告

审查报告包含：

- **📊 整体评分**：代码质量评分（1-10 分）
- **🏷️ 标签建议**：AI 推荐的 PR 标签（含置信度和理由）
- **📝 审查摘要**：变更概述和主要发现
- **🔴 严重问题**：必须修复的问题
- **🟡 重要建议**：推荐改进
- **💡 优化建议**：代码优化建议

### Issue 智能分析

Sakura AI 会自动分析仓库中的 Issue，提供分类、优先级、标签建议等。

#### 自动分析

1. 在已安装 App 的仓库中创建或编辑 Issue
2. Sakura AI 会自动分析并发布评论报告
3. 如果启用了自动标签，高置信度标签会被自动应用

#### 手动触发

在任意 Issue 中评论 `/analyze` 即可手动触发分析。

#### 分析报告内容

- **📋 分类**: bug/feature/question/enhancement 等
- **⚡ 优先级**: critical/high/medium/low
- **📝 摘要**: AI 生成的 Issue 摘要
- **📐 可行性**: 基于代码库的修复难度评估
- **🏷️ 建议标签**: 含置信度和理由
- **👥 建议指派人**: 基于代码修改区域判断
- **⚠️ 重复检测**: 可能重复的 Issue
- **🔗 关联 PR**: 与该 Issue 相关的 PR

### 通过 WebUI 管理

除了通过 Telegram Bot 管理外，还可以使用 WebUI 管理界面：

1. 访问 `https://your-domain.com/webui/`
2. 使用 GitHub 账号登录（需先在 Telegram Bot 中注册）
3. 根据角色权限，可以使用以下功能：
   - **仪表盘**：查看审查统计和最近活动
   - **PR 管理**：浏览审查记录，查看文件级评论
   - **用户管理**（管理员）：添加/编辑用户、设置配额
   - **仓库管理**（管理员）：管理仓库白名单
   - **配置管理**（超级管理员）：在线编辑审查策略和标签
   - **审查队列**（管理员）：实时监控审查队列状态

### 标签推荐说明

Sakura AI 会自动分析 PR 的代码变更，推荐合适的标签：

#### 标签类型

- **bug** - 修复错误或缺陷
- **documentation** - 文档相关变更
- **enhancement** - 新功能或功能增强
- **refactor** - 代码重构（非功能性变更）
- **performance** - 性能优化
- **test** - 测试相关
- **dependencies** - 依赖更新
- **ci** - CI/CD 配置变更
- **style** - 代码风格调整
- **build** - 构建系统变更

#### 推荐逻辑

1. **自动应用**：置信度 ≥ 70% 的标签会自动添加到 PR
2. **建议确认**：置信度 < 70% 的标签会在评论中显示，需开发者手动确认
3. **自定义标签**：支持仓库的自定义标签，AI 会优先匹配已有标签

#### 配置选项

在 `.env` 中配置：

```env
# 启用标签推荐
ENABLE_LABEL_RECOMMENDATION=true

# 自动应用阈值（0.0-1.0）
LABEL_CONFIDENCE_THRESHOLD=0.7

# 是否自动创建不存在的标签
LABEL_AUTO_CREATE=false
```

---

## 🔧 配置说明

### 审查策略配置

编辑 `config/strategies.yaml`：

```yaml
strategies:
  quick:
    name: "⚡️ 快速审查"
    conditions:
      max_files: 5
      max_lines: 200
    prompt: |
      你是一个经验丰富的代码审查专家...
```

### 文件过滤规则

```yaml
file_filters:
  skip_extensions:
    - .md
    - .txt
    - .json
  skip_paths:
    - node_modules/
    - vendor/
    - .venv/
```

### AI 工具配置

在 `.env` 中配置：

```env
# 启用 AI 工具增强功能
ENABLE_AI_TOOLS=true

# 最大工具调用次数（防止无限循环）
MAX_TOOL_ITERATIONS=10
```

### 模型上下文配置

在 `.env` 中配置：

```env
# 模型上下文配置
MODEL_CONTEXT_WINDOW=0  # 自定义上下文窗口大小（K tokens），0 表示自动检测
AUTO_FETCH_MODEL_CONTEXT=true  # 是否自动从 API 获取模型上下文
CONTEXT_SAFETY_THRESHOLD=0.8  # 上下文安全阈值（0-1），默认使用 80%

# 上下文压缩配置
ENABLE_CONTEXT_COMPRESSION=true  # 是否启用上下文自动压缩
CONTEXT_COMPRESSION_THRESHOLD=0.85  # 压缩触发阈值（0-1），默认 85%
CONTEXT_COMPRESSION_KEEP_ROUNDS=2  # 保留最近几轮对话不压缩
```

**支持的模型**：

- OpenAI: GPT-4 (128K), GPT-4 Turbo (128K), GPT-3.5 Turbo (16K)
- DeepSeek: deepseek-chat (128K), deepseek-r1 (64K)
- Claude: Claude 3.5 Sonnet (200K), Claude 3 Opus (200K)
- Gemini: Gemini 1.5 Pro (1000K)

详细配置说明请参考：[模型上下文管理功能](docs/MODEL_CONTEXT_FEATURE.md)

---

## 🤖 Telegram Bot 集成

Sakura AI Reviewer 集成了完整的 Telegram Bot 功能，提供实时通知、配额管理和权限控制。

### 核心功能

#### 1. 实时通知系统

- 🔔 **PR 开始审查通知**：当 AI 开始审查时立即通知
- 🌸 **PR 审查完成通知**：包含评分、问题统计和审查摘要
- ⚠️ **配额不足提醒**：当用户配额即将用尽时自动提醒
- 🚫 **未授权提醒**：仓库或用户未授权时通知管理员

#### 2. 配额管理系统

- **每日配额**：每天 00:00 重置（默认：10次）
- **每周配额**：每周一 00:00 重置（默认：50次）
- **每月配额**：每月 1 日 00:00 重置（默认：200次）
- 管理员和超级管理员不受配额限制

#### 3. 三级权限体系

```
👑 超级管理员 (SUPER_ADMIN)
   ↓ 由环境变量 TELEGRAM_ADMIN_USER_IDS 定义
   
👤 管理员 (ADMIN)  
   ↓ 由超级管理员通过 /admin_add 添加
   
👥 普通用户 (USER)
   ↓ 由管理员通过 /user_add 添加
```

#### 4. 丰富的管理命令

**普通用户命令**：

- `/start` - 初始化 Bot
- `/help` - 查看帮助信息
- `/status` - 查看系统状态
- `/recent` - 查看最近审查记录
- `/myquota` - 查看我的配额使用情况

**管理员命令**：

- `/user_add <telegram_id> <github_username>` - 添加用户
- `/user_remove <github_username>` - 移除用户
- `/repo_add <owner/repo>` - 添加仓库到白名单
- `/repo_remove <owner/repo>` - 移除仓库
- `/quota_set <github_username> <daily|weekly|monthly> <limit>` - 设置配额
- `/users` - 列出所有用户
- `/repos` - 列出所有仓库

**超级管理员命令**：

- `/admin_add <telegram_id> <github_username>` - 添加管理员
- `/admin_remove <telegram_id>` - 移除管理员
- `/review <pr_url>` - 手动触发对任意 PR 的审查（超级管理员专属）

**使用示例**：

```bash
/review https://github.com/owner/repo/pull/123
```

详细说明请参考：[手动审查功能](docs/MANUAL_REVIEW_FEATURE.md)

### 快速配置

#### 步骤 1：创建 Telegram Bot

1. 在 Telegram 中找到 [@BotFather](https://t.me/BotFather)
2. 发送 `/newbot` 创建新机器人
3. 按提示设置机器人名称和用户名
4. 保存获得的 **Bot Token**

#### 步骤 2：获取你的 Telegram ID

1. 在 Telegram 中找到 [@userinfobot](https://t.me/userinfobot)
2. 发送任意消息获取你的 **Telegram ID**
3. 记录这个 ID（你将作为超级管理员）

#### 步骤 3：配置环境变量

在 `.env` 文件中添加或修改：

```env
# Telegram Bot配置
TELEGRAM_BOT_TOKEN=你的_Bot_Token
TELEGRAM_ADMIN_USER_IDS=你的_Telegram_ID
TELEGRAM_DEFAULT_CHAT_ID=你的_Telegram_ID
```

#### 步骤 4：重启服务

```bash
cd docker
docker-compose restart
```

#### 步骤 5：初始化系统

1. 在 Telegram 中找到你的 Bot
2. 发送 `/start` 命令
3. 你应该看到 "👑 超级管理员" 标识

#### 步骤 6：添加第一个管理员（可选）

```bash
/admin_add <管理员_Telegram_ID> <管理员_GitHub用户名>
```

#### 步骤 7：添加仓库到白名单

```bash
/repo_add owner/repo
```

#### 步骤 8：添加用户

```bash
/user_add <用户_Telegram_ID> <用户_GitHub用户名>
```

#### 步骤 9：设置用户配额（可选）

```bash
/quota_set <GitHub用户名> daily 20
```

### 使用流程

```
GitHub PR 创建
    ↓
Webhook 接收
    ↓
✅ 检查仓库是否在白名单
    ↓
✅ 检查 PR 作者是否已注册
    ↓
✅ 检查配额是否充足
    ↓
🔔 发送"开始审查"通知到 Telegram
    ↓
🤖 AI 审查进行中...
    ↓
🌸 发送"审查完成"通知到 Telegram
    ↓
✅ 审查完成
```

### 权限和配额规则

- **超级管理员**：所有权限 + 添加/删除管理员，无限配额
- **管理员**：添加/删除用户、管理仓库、设置配额，无限配额
- **普通用户**：使用配额进行审查、查看自己的配额

每次审查消耗 1 次所有配额（每日、每周、每月）。

### 拒绝场景

- ❌ 仓库未在白名单 → 静默跳过
- ❌ 用户未注册 → 静默跳过  
- ❌ 配额不足 → 发送拒绝通知到 Telegram

### 管理命令示例

```bash
# 添加管理员
/admin_add 123456789 john_doe

# 添加普通用户
/user_add 987654321 jane_smith

# 添加仓库
/repo_add facebook/react
/repo_add vuejs/vue

# 设置配额
/quota_set john_doe daily 20
/quota_set john_doe weekly 100
/quota_set john_doe monthly 500

# 查看所有用户
/users

# 查看所有仓库
/repos

# 手动触发审查
/review https://github.com/owner/repo/pull/123
```

### 数据库表结构

系统使用以下表管理 Telegram 功能：

- **telegram_users**：用户信息、角色、配额设置
- **repo_subscriptions**：仓库白名单
- **quota_usage_logs**：配额使用记录
- **issue_analyses**：Issue 分析记录（分类、优先级、AI 结果、Token 消耗）
- **pr_issue_links**：PR-Issue 关联关系
- **issue_analysis_queue**：Issue 分析任务队列

### 常见问题

**Q: 如何获取 Telegram ID？**  
A: 使用 [@userinfobot](https://t.me/userinfobot) 机器人获取

**Q: 配额如何重置？**  
A: 系统自动重置

- 每日：每天 00:00
- 每周：每周一 00:00
- 每月：每月 1 日 00:00

**Q: 管理员受配额限制吗？**  
A: 不受。超级管理员和管理员都有无限配额

**Q: Bot 没有响应怎么办？**  
A: 检查以下几点：

1. Bot Token 是否正确
2. 环境变量是否配置
3. 应用是否正常运行
4. 查看日志：`docker-compose logs -f`

### 详细文档

完整的 Telegram Bot 设置和配置指南请参考：[Telegram Bot 集成指南](docs/TELEGRAM_SETUP.md)

---

## 📚 仓库级知识库（RAG）

Sakura AI Reviewer 支持构建和管理仓库级知识库，通过向量索引技术实现文档的语义检索，为 AI 审查提供项目上下文增强。

### 功能特性

- **📖 多格式文档支持**：Markdown、README、代码规范、API 文档等
- **🔄 增量更新**：基于文件 Hash 的幂等性更新，避免重复索引
- **🔍 语义检索**：使用嵌入模型实现语义相似度搜索
- **⚡ 重排序优化**：使用重排序模型提升检索质量
- **🌐 多提供商支持**：SiliconFlow、OpenAI、Ollama、HuggingFace

### 配置说明

在 `.env` 中配置：

```env
# RAG 功能开关
ENABLE_RAG=true
CHROMA_PERSIST_DIR=./data/chroma

# 嵌入模型配置
EMBEDDING_PROVIDER=siliconflow  # openai|ollama|hf|siliconflow
EMBEDDING_MODEL=BAAI/bge-m3
EMBEDDING_BASE_URL=https://api.siliconflow.cn/v1
EMBEDDING_API_KEY=your_api_key
EMBEDDING_DIMENSION=1024
EMBEDDING_BATCH_SIZE=64

# 重排序模型配置
RERANK_PROVIDER=siliconflow
RERANK_MODEL=BAAI/bge-reranker-v2-m3
RERANK_BASE_URL=https://api.siliconflow.cn/v1/rerank
RERANK_API_KEY=your_rerank_key
RERANK_TOP_K=5
RERANK_SCORE_THRESHOLD=0.3

# 文档分块配置
CHUNK_SIZE=1000
CHUNK_OVERLAP=200
MAX_CHUNKS_PER_DOC=500
```

### 使用方法

#### 1. 创建文档库

在仓库根目录创建 `sakura` 文件夹，添加项目文档：

```
your-repo/
├── sakura/
│   ├── review-rules.md          # 审查规则
│   ├── coding-standards.md      # 编码规范
│   ├── architecture.md          # 架构文档
│   ├── api-documentation/       # API 文档
│   └── guides/                  # 指南文档
├── src/
└── README.md
```

#### 2. 自动索引

- 文档变更后会自动触发索引
- 支持定时任务和文件监控
- 基于文件 Hash 实现增量更新

#### 3. 检索增强

AI 审查时会自动：

1. 分析 PR 变更内容
2. 检索相关项目文档
3. 将文档内容作为上下文提供给 AI
4. 生成符合项目规范的审查意见

### 工作流程

```
┌─────────────────┐
│  PR 创建/更新    │
└────────┬────────┘
         │
         ▼
┌─────────────────┐
│  获取变更文件     │
└────────┬────────┘
         │
         ▼
┌─────────────────┐     ┌──────────────────┐
│  文档向量化索引   │────▶│  ChromaDB 存储    │
└────────┬────────┘     └──────────────────┘
         │
         ▼
┌─────────────────┐
│  语义相似度检索  │
└────────┬────────┘
         │
         ▼
┌─────────────────┐
│  重排序优化结果  │
└────────┬────────┘
         │
         ▼
┌─────────────────┐
│  AI 审查上下文   │
└─────────────────┘
```

### 支持的嵌入模型

| 模型                   | 维度 | 提供商      |
| ---------------------- | ---- | ----------- |
| BAAI/bge-m3            | 1024 | SiliconFlow |
| BAAI/bge-large-zh-v1.5 | 1024 | SiliconFlow |
| text-embedding-3-small | 1536 | OpenAI      |
| text-embedding-3-large | 3072 | OpenAI      |
| nomic-embed-text       | 768  | Ollama      |

### 支持的重排序模型

| 模型                               | 提供商      |
| ---------------------------------- | ----------- |
| BAAI/bge-reranker-v2-m3            | SiliconFlow |
| BAAI/bge-reranker-v2-m3            | HuggingFace |
| jina-reranker-v2-base-multilingual | SiliconFlow |

---

## 🔍 PR 自动索引代码

Sakura AI Reviewer 支持自动索引 PR 变更的代码，为 AI 审查提供精准的代码上下文支持。

### 功能特性

- **🚀 自动触发**：PR 打开/更新时自动索引变更文件
- **📈 增量索引**：只索引新增和修改的文件，高效更新
- **🧠 语法感知**：根据编程语言特性进行智能分块
- **🔎 向量检索**：支持语义相似度搜索相关代码
- **📋 元数据丰富**：包含函数名、类名、行号等详细信息

### 配置说明

在 `.env` 中配置：

```env
# 代码索引功能开关
ENABLE_CODE_INDEX=true
AUTO_INDEX_PR_CHANGES=true

# 代码分块配置
CODE_CHUNK_SIZE=500  # 代码块大小（字符数）
CODE_CHUNK_OVERLAP=50  # 代码块重叠大小

# 支持的编程语言（逗号分隔）
CODE_INDEX_LANGUAGES=python,javascript,typescript,go,java,rust,cpp,c,csharp,php,ruby,swift,kotlin

# 核心代码目录（逗号分隔）
CODE_INDEX_CORE_PATHS=src/,lib/,backend/,frontend/,app/,core/

# 依赖文件索引
CODE_INDEX_DEPENDENCY_FILES=true
```

### 支持的编程语言

| 语言       | 扩展名                     | 分块策略                    |
| ---------- | -------------------------- | --------------------------- |
| Python     | `.py`                      | 按类、函数、方法分块        |
| JavaScript | `.js`, `.jsx`, `.mjs`      | 按函数、类、模块分块        |
| TypeScript | `.ts`, `.tsx`              | 按函数、类、接口分块        |
| Go         | `.go`                      | 按函数、方法、结构体分块    |
| Java       | `.java`                    | 按类、方法分块              |
| Rust       | `.rs`                      | 按函数、结构体、impl 块分块 |
| C/C++      | `.c`, `.h`, `.cpp`, `.hpp` | 按函数分块                  |
| C#         | `.cs`                      | 按类、方法分块              |
| PHP        | `.php`                     | 按类、函数分块              |
| Ruby       | `.rb`                      | 按类、模块、方法分块        |
| Swift      | `.swift`                   | 按类、结构体、函数分块      |
| Kotlin     | `.kt`, `.kts`              | 按类、函数分块              |

### 工作流程

```
┌─────────────────┐
│  PR Webhook     │
│ (opened/sync/   │
│  reopened)      │
└────────┬────────┘
         │
         ▼
┌─────────────────┐
│  获取变更文件     │
│  (GitHub API)   │
└────────┬────────┘
         │
         ▼
┌─────────────────┐
│  过滤代码文件     │
│  (按扩展名)      │
└────────┬────────┘
         │
         ▼
┌─────────────────┐
│  下载文件内容     │
│  (HEAD分支)      │
└────────┬────────┘
         │
         ▼
┌─────────────────┐
│  语法感知分块     │
│  (AST解析)       │
└────────┬────────┘
         │
         ▼
┌─────────────────┐     ┌──────────────────┐
│  生成嵌入向量     │────▶│ ChromaDB 存储     │
└────────┬────────┘     │ (_code 后缀)      │
         │              └──────────────────┘
         ▼
┌─────────────────┐
│  记录索引状态     │
│  (数据库)        │
└─────────────────┘
```

### 代码分块策略

#### Python 分块示例

```python
# 原始代码
class UserService:
    def get_user(self, user_id: int) -> User:
        # ... 实现

    def create_user(self, data: dict) -> User:
        # ... 实现

# 分块结果
# Chunk 1: class UserService 定义
# Chunk 2: def get_user 方法
# Chunk 3: def create_user 方法
```

#### 上下文填充

每个代码块会添加语义上下文：

- 类名和方法签名
- 父类和继承关系
- 装饰器信息
- 相关导入语句

### AI 审查中的使用

1. **检索相关代码**：AI 可以通过语义搜索找到相关函数和类
2. **理解调用关系**：通过向量相似度发现代码关联
3. **一致性检查**：对比 PR 变更与现有代码模式
4. **最佳实践建议**：基于项目代码风格提供建议

### 数据库存储

- **向量存储**：ChromaDB，使用独立的 Collection（`{repo_name}_code`）
- **关系存储**：MySQL，记录索引状态和元数据
- **幂等性**：基于文件 Hash 避免重复索引
- **软删除**：支持标记删除，可恢复

---

## 🎯 智能审查批准

Sakura AI Reviewer 支持基于 AI 评分和问题严重程度的自动审查批准功能，可以显著提升团队研发效率。

### 决策逻辑

系统会根据 AI 评分和问题分类自动做出审查决策：

```
┌─────────────────────────────────────────┐
│         AI审查完成，获得评分和问题         │
└──────────────┬──────────────────────────┘
               │
               ▼
    ┌──────────────────────┐
    │  Critical > 0 ?      │──Yes──► REQUEST_CHANGES (一票否决)
    └──────────┬───────────┘
               │ No
               ▼
    ┌──────────────────────┐
    │  Score < 4 ?         │──Yes──► REQUEST_CHANGES (低分阻断)
    └──────────┬───────────┘
               │ No
               ▼
    ┌──────────────────────┐
    │  Score >= 8 &&       │
    │  Major <= 1 ?        │──Yes──► APPROVE (批准合并)
    └──────────┬───────────┘
               │ No
               ▼
          COMMENT (中立评论)
```

### 三种审查状态

#### 1. **APPROVE** ✅

- **条件**：评分 ≥ 8 分 且 无 Critical 问题 且 Major 问题 ≤ 1 个
- **含义**：代码质量优秀，可以合并
- **操作**：自动批准 PR，开发者可以直接合并

#### 2. **REQUEST_CHANGES** ❌

- **条件**：评分 < 4 分 或 存在 Critical 问题
- **含义**：发现严重问题或评分过低，必须修复
- **操作**：请求变更，开发者需要修复后重新提交

#### 3. **COMMENT** 💬

- **条件**：评分 4-7 分（中间状态）
- **含义**：需要人工复审
- **操作**：仅评论，不做批准或拒绝决策

### 配置选项

在 `config/strategies.yaml` 中配置：

```yaml
review_policy:
  # 是否启用自动批准功能（建议先设为false观察效果）
  enabled: false
  
  # 批准阈值：分数 >= 此值才考虑批准
  approve_threshold: 8
  
  # 阻断阈值：分数 < 此值自动请求变更
  block_threshold: 4
  
  # 是否在存在Critical问题时自动请求变更
  block_on_critical: true
  
  # 允许的Major问题数量上限
  max_major_issues: 1
  
  # 幂等性检查：是否检查已有Review避免重复提交
  enable_idempotency_check: true
```

### 仓库级别覆盖

支持为不同仓库设置不同的策略：

```yaml
review_policy:
  repo_overrides:
    "owner/core-project":
      approve_threshold: 9  # 核心项目更严格
      block_threshold: 5    # 更宽松
    "owner/experimental":
      approve_threshold: 7  # 实验项目更宽松
```

### 部署建议

#### 阶段1：观察期（推荐）

```yaml
review_policy:
  enabled: false  # 仅评论，不自动批准
```

观察 1-2 周，检查 AI 评分的准确性。

#### 阶段2：逐步启用

```yaml
review_policy:
  enabled: true
  approve_threshold: 9  # 先设置高阈值
  block_on_critical: true
```

#### 阶段3：正式运行

根据观察结果调整阈值。

### 幂等性保护

系统会自动检查是否已存在相同类型的 Review，避免重复提交：

```python
def has_existing_review(
    repo_owner, repo_name, pr_number, 
    bot_username, event
) -> bool
```

### 数据库记录

每个审查记录包含决策信息：

```sql
SELECT 
    id,
    overall_score,     -- AI评分 1-10
    decision,          -- approve/request_changes/comment
    decision_reason    -- 决策理由
FROM pr_reviews;
```

### 使用场景

#### 自动批准场景

- 评分 >= 8 分
- 无 Critical 问题
- Major 问题 <= 1 个

#### 自动阻断场景

- 评分 < 4 分
- 存在 Critical 问题

#### 人工复审场景

- 评分 4-7 分（中间状态）
- Major 问题过多

### 重要提示

1. **首次部署**：建议 `enabled: false`，先观察效果
2. **Critical 问题**：建议保持 `block_on_critical: true`
3. **测试环境**：先在测试仓库验证，再应用到生产
4. **监控日志**：密切关注决策引擎的日志输出
5. **人工复审**：COMMENT 状态的 PR 需要人工审查

### 详细文档

完整的审查批准功能实现细节请参考：[审查批准功能总结](docs/APPROVAL_FEATURE_SUMMARY.md)

---

## 📊 监控和日志

### 查看应用日志

```bash
# 查看所有日志
docker-compose logs -f

# 只查看 Web 服务日志
docker-compose logs -f web
```

### 查看 AI 工具调用

日志会显示 AI 的工具调用过程：

```
INFO | 执行工具 list_directory: {"directory": "."}
INFO | 执行工具 read_file: {"file_path": "bot.py"}
INFO | AI审查完成（使用了5轮对话）
```

### 数据库查询

由于数据库运行在宿主机上，直接使用以下命令：

```bash
mysql -u root -p sakura-pr
```

或者使用密码直接登录：

```bash
mysql -u root -pyour_password sakura-pr
```

查看审查记录：

```bash
mysql -u root -pyour_password -e "SELECT * FROM \`sakura-pr\`.pr_reviews ORDER BY created_at DESC LIMIT 10;"
```

---

## 🛠️ 故障排查

### 常见问题

#### 1. Webhook 签名验证失败

- 检查 `.env` 中的 `GITHUB_WEBHOOK_SECRET`
- 确保在 GitHub App 中设置了相同的 Secret

#### 2. AI 审查失败

- 检查 DeepSeek API 密钥是否有效
- 确认 API 端点地址可访问
- 查看应用日志获取详细错误信息

#### 3. 评论发布失败

- 确保 GitHub App 有写入权限
- 检查 PyGithub 版本兼容性
- 查看日志中的具体错误信息

---

## 🔐 安全建议

1. **保护敏感信息**
   - 不要将 `.env` 文件提交到版本控制
   - 使用强密码作为数据库和 Webhook 密钥
   - 定期轮换 API 密钥

2. **限制访问**
   - 使用防火墙限制数据库端口访问
   - 配置 Nginx 反向代理启用 HTTPS
   - 限制 GitHub App 的仓库访问权限

3. **定期更新**
   - 及时更新 Docker 镜像
   - 定期更新依赖包
   - 关注安全公告

---

## 📝 开发指南

### 本地开发

```bash
# 安装依赖
pip install -r requirements.txt

# 设置环境变量
cp .env.example .env

# 运行应用
python -m backend.main
```

### 代码结构

```
Sakura-AI-Reviewer/
├── backend/
│   ├── api/           # API 路由
│   ├── core/          # 核心配置
│   ├── models/        # 数据模型
│   ├── services/      # 业务逻辑
│   │   ├── ai_reviewer.py      # AI 审查引擎
│   │   ├── pr_analyzer.py      # PR 分析器
│   │   ├── issue_analyzer.py   # Issue AI 分析引擎
│   │   ├── issue_service.py    # Issue 管理服务
│   │   ├── pr_issue_linker.py  # PR-Issue 关联器
│   │   └── comment_service.py  # 评论服务
│   ├── webui/         # WebUI 管理界面
│   │   ├── routes/    # 页面路由（仪表盘、PR、用户、配置等）
│   │   ├── templates/ # Jinja2 HTML 模板
│   │   └── static/    # 静态资源
│   ├── workers/       # 后台任务
│   │   ├── review_worker.py    # PR 审查任务
│   │   └── issue_worker.py     # Issue 分析任务
│   └── telegram/      # Telegram Bot
├── config/            # 配置文件
├── docker/            # Docker 配置
└── docs/              # 项目文档
```

---

## 🎯 路线图

### 已完成 ✅

- [x] AI 推理模式支持
- [x] AI 函数工具系统（read_file, list_directory）
- [x] 多轮对话和上下文理解
- [x] 自适应审查策略
- [x] 结构化审查报告
- [x] 优雅的错误处理和降级机制
- [x] 智能标签推荐系统
- [x] Telegram Bot 集成（通知、配额、权限管理）
- [x] 智能审查批准（多维度决策引擎）
- [x] 行内评论（文件级代码审查）
- [x] 审查历史记录和趋势分析
- [x] WebUI 管理界面（仪表盘、PR管理、用户管理、配置管理、队列监控）
- [x] Issue 智能分析（自动分类、优先级判定、标签推荐、重复检测）
- [x] PR-Issue 智能关联（自动解析引用、上下文注入）

### 计划中 🚧

- [ ] 支持更多 AI 模型（Gemini、Claude）
- [ ] 审查结果导出（PDF/Markdown）
- [ ] 审批链（支持多个批准）

### 未来构想 💡

- [ ] 代码相似度检测
- [ ] 安全漏洞扫描
- [ ] 性能分析建议
- [ ] 多语言支持
- [ ] 自定义审查规则
- [ ] 智能调优（基于历史数据自动调整阈值）

---

## 📚 功能文档

- [Telegram Bot 集成指南](docs/TELEGRAM_SETUP.md) - 完整的 Bot 设置和配置指南
- [审查批准功能总结](docs/APPROVAL_FEATURE_SUMMARY.md) - 智能审查批准系统说明
- [手动审查功能](docs/MANUAL_REVIEW_FEATURE.md) - 超级管理员手动触发审查功能
- [模型上下文管理功能](docs/MODEL_CONTEXT_FEATURE.md) - AI 模型上下文和压缩功能说明
- [WebUI 设计文档](docs/plans/2024-03-27-webui-design.md) - WebUI 管理界面设计规范

---

## 🤝 贡献

欢迎提交 Issue 和 Pull Request！

### 贡献指南

1. Fork 本项目
2. 创建特性分支 (`git checkout -b feature/AmazingFeature`)
3. 提交更改 (`git commit -m 'Add some AmazingFeature'`)
4. 推送到分支 (`git push origin feature/AmazingFeature`)
5. 开启 Pull Request

---

## 📄 许可证

GNU Affero General Public License v3.0 (AGPLv3)

本项目采用 AGPLv3 许可证，这是一个自由软件许可证，特别适用于网络服务器软件。使用本软件时，请注意：

- 你可以自由使用、修改和分发本软件
- 如果你修改了软件并通过网络提供服务，必须向用户提供源代码
- 任何派生作品也必须使用相同的许可证

详见 [LICENSE](LICENSE) 文件。

---

## 🙏 致谢

- [DeepSeek](https://www.deepseek.com/) - 提供极具性价比的强大的 AI 推理能力
- [FastAPI](https://fastapi.tiangolo.com/) - 现代化的 Python Web 框架
- [PyGithub](https://github.com/PyGithub/PyGithub) - GitHub API 封装

---

## 📮 联系方式

- 问题反馈：[Issues](https://github.com/Sakura520222/Sakura-AI-Reviewer/issues)
- 邮箱：<Sakura520222@outlook.com>

---

## 🌟 Star History

如果这个项目对你有帮助，请给我们一个 Star ⭐

---

<div align="center">

**Sakura AI Reviewer** - 让代码审查更智能、更高效

Made with 🌸 by [Sakura520222](https://github.com/Sakura520222)

</div>
