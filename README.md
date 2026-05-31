<div align="center">

# 🌸 Sakura AI Reviewer

<img src="res/cover.png" alt="Sakura AI Reviewer Cover" width="100%">

> 基于 AI 的智能 GitHub Pull Request 代码审查与 Issue 分析机器人，具备主动探索代码库的能力

[English](README_EN.md) | **中文**

[![Version](https://img.shields.io/badge/Version-2.12.0-blue.svg)](https://github.com/Sakura520222/Sakura-AI-Reviewer/releases)
[![Python](https://img.shields.io/badge/Python-3.11+-blue.svg)](https://www.python.org/)
[![FastAPI](https://img.shields.io/badge/FastAPI-Latest-green.svg)](https://fastapi.tiangolo.com/)
[![License](https://img.shields.io/badge/License-AGPLv3-yellow.svg)](LICENSE)
[![Live Demo](https://img.shields.io/badge/🌐_免费体验-Online-success.svg)](https://pr-bot.firefly520.top/)
[![Android App](https://img.shields.io/badge/Android_App-🚧_开发中-orange.svg)](https://github.com/Sakura520222/Sakura-AI-Reviewer-APP)

</div>

---

## 🌐 官方服务

**官方服务平台**：[https://pr-bot.firefly520.top/](https://pr-bot.firefly520.top/)

- ✅ **免费额度**：注册即赠免费体验额度，可立即使用 PR 审查、Issue 分析等核心功能
- ✅ **完整功能**：体验全部功能，包括 PR 审查、Issue 分析、Agent 任务委派等
- ✅ **无需部署**：开箱即用，无需自行搭建服务器和配置环境

> 💡 如果你想自建实例或进行二次开发，请参考下方的 [快速开始](#-快速开始) 部分。

---

## ✨ 核心特性

### 2.12.0 更新亮点

- **多支付网关与退款闭环**：新增 Stripe、Paddle、支付宝、NOWPayments、TRON USDT 直收等外部支付网关，支持订单创建、状态查询、支付回调、用户退款申请、超级管理员审核与真实退款执行。
- **Agent 专家团队开放与配额化**：Agent 任务支持仓库协作者通过 Issue 评论 `/agent` 委派，非管理员需满足仓库归属/白名单约束并消耗独立 Agent 日/周/月配额。
- **移动端认证增强**：移动端 OAuth 支持白名单回调 URI，WebAuthn/Passkey 支持额外 Origin 与 Android App Links 场景，便于原生 Android App 接入。
- **Sakura 记忆知识提取升级**：项目记忆从一次性提取改为按反思轮次周期性沉淀 rules/docs/plans，长期维护项目规则、架构知识和经验计划。
- **审查与任务链路简化**：移除旧批处理模块，审查器直接使用 compact diff 工具模式处理大型 PR，并补强扫描报告 Issue 到 Agent 任务的闭环。

### 审查能力

- **AI 推理模式**：利用 AI 推理能力进行深度代码分析，主动调用工具查看项目结构和任意文件
- **跨文件依赖理解**：通过多轮对话理解模块间的复杂依赖关系，具备"全域视野"
- **自适应审查策略**：根据 PR 规模自动选择快速/标准/深度审查模式
- **大型 PR 精简审查**：当初始 diff 接近上下文阈值时自动切换 compact diff 模式，AI 通过 `get_file_diff` / `list_changed_files` 按需查看变更
- **结构化审查报告**：整体评分 + 分类问题（🔴严重/🟡重要/💡优化）+ `<details>` 折叠详情
- **增量审查学习**：AI 自动总结历史审查记录，识别评分趋势和问题热点，逐步提升审查质量
- **智能审查批准**：基于 AI 评分自动决策 APPROVE / REQUEST_CHANGES / COMMENT
- **PR 变更自动总结**：AI 自动生成 PR 变更摘要，并在 PR 更新时增量更新总结内容
- **PR 依赖图生成**：支持 AI 分析与静态 import 分析双模式，生成 Mermaid 格式可视化依赖关系图
- **Token 消耗追踪**：实时追踪审查中所有 AI API 调用的 token 消耗量与预估成本
- **一键撤回**：管理员使用 `/revoke` 命令一键撤回所有 AI 评论和 Review
- **辅助模型支持**：独立配置轻量级模型处理摘要、上下文压缩、标签推荐等任务，降低推理成本
- **行内评论开关**：通过 WebUI 配置 `enable_inline_comments`，控制是否在 PR diff 上发布行内评论，减少审查噪音
- **可控自动审查**：通过 WebUI 配置 `enable_auto_review` 控制 PR opened/synchronize/reopened 是否自动入队，保留命令和手动触发路径
- **审查评论标签交互**：审查报告中包含标签复选框，用户可在 GitHub PR 页面直接勾选/取消标签，AI 自动应用或移除对应标签
- **AI 生成 PR 描述**：Agent 创建 PR 时 AI 自动生成包含元数据标记的 PR 描述，后续审查可精确识别和更新 AI 注入区域

### AI 工具与知识库

- **AI 工具系统**：read_file、list_directory、search_in_files、get_git_info、list_commits、search_web、read_sakura_docs、list_sakura_directory、read_sakura_memory，AI 按需主动调用
- **跨文件代码搜索**：AI 可在仓库中跨文件搜索关键词，快速定位函数/变量/类的所有使用位置
- **Git 信息查询**：AI 可获取仓库基本信息、分支列表和提交历史，理解项目演进脉络
- **Web 搜索增强**：支持 DuckDuckGo / Tavily，AI 可主动检索互联网信息辅助审查决策
- **仓库级知识库（RAG）**：向量语义检索项目文档，为 AI 审查提供规范上下文
- **PR 代码自动索引**：语法感知分块 + 语义搜索，AI 可精准定位相关代码
- 🧠 **项目记忆系统**：基于 `.sakura/` 目录的自我反思和知识积累，AI 审查越来越了解你的项目。详见 [项目记忆系统使用指南](docs/SAKURA_MEMORY_GUIDE.md)

### 仓库扫描

- **AI 全仓库扫描**：定期对仓库进行全面的 AI 代码扫描，自动发现代码质量问题和安全隐患
- **自动创建 Issue**：扫描发现的问题自动创建 GitHub Issue，包含详细的问题描述和修复建议
- **灵活扫描配置**：可配置扫描间隔、冷却时间、Token 预算、并发数等参数
- **扫描管理界面**：WebUI 中查看扫描列表、扫描详情和统计数据
- **扫描通知**：扫描完成后通过 Telegram Bot 发送通知

### Issue 分析

- **Issue 智能分析**：自动分类、优先级判定、标签推荐、重复检测、关联 PR 发现
- **Issue 自动打标**：AI 自动分类并推荐标签，高置信度自动应用
- **Issue 自动指派**：AI 分析内容并自动指派给合适的仓库协作者
- **Issue 标题改写**：AI 自动优化模糊或不够准确的 Issue 标题
- **PR-Issue 关联**：自动解析 Issue 引用，注入上下文增强审查精度
- **语义 Issue 关联**：基于向量语义相似度发现并关联相关 Issue

### Agent 专家团队

- **超级管理员手动启动**：从 Issue 分析和仓库扫描发现中筛选候选任务，支持自然语言描述筛选条件，按需启动自动修复流程
- **手动 Issue 创建任务**：支持粘贴 GitHub Issue 链接或输入 `owner/repo#123`，验证后直接创建 Agent 修复任务
- **Issue 评论委派**：仓库管理员/写权限协作者可在已分析 Issue 或扫描报告 Issue 中评论 `/agent` 创建修复任务，可附加 `base:<branch>` 指定基础分支
- **普通用户仓库权限控制**：非管理员只能操作自己名下仓库，且仓库必须匹配 `agent_team_repo_allowlist`；任务创建、重试和 `/agent` 委派均消耗独立 Agent 配额
- **智能候选筛选**：自动去重、过滤已关闭 Issue、按评分排序，支持 AI 自然语言筛选匹配最合适的候选任务
- **双 Agent 协作**：内置全栈专家负责计划与代码修改，专业审查负责推送前质量复核
- **上下文压缩与任务恢复**：长任务自动压缩历史上下文，并持久化会话与消息检查点，支持失败后继续处理
- **独立 Git 工作区**：在 `agent_team_workspace_root` 下 clone/fetch/checkout 专用分支，避免污染服务运行目录
- **受控工具执行**：文件读写、搜索、shell 验证命令均限制在工作区内，验证命令受黑名单控制（阻止危险命令，允许其余命令）
- **自动依赖与验证**：可自动检测并安装 `pyproject.toml` / `requirements.txt` 依赖，随后运行白名单内测试或 lint 命令
- **Sakura 知识集成**：Agent 可通过专用工具浏览和读取 `.sakura/` 知识目录与反思文件，利用项目积累的审查经验辅助代码修复
- **Agent Skills 与内置 Ruff**：支持从上传文件、ZIP 或 GitHub `SKILL.md` 安装技能，并内置 Ruff lint/format 技能供 Agent 按需加载
- **实时管理员干预**：管理员可在任务执行过程中通过 WebUI Live View 注入指导意见，Agent 在下一轮迭代中消费并合并指导到后续流程
- **任务取消支持**：支持在任务执行过程中随时取消 Agent 任务，安全释放工作区资源
- **Web 搜索与 URL 抓取**：Agent 可使用 Web 搜索和 URL 抓取工具，扩展信息获取能力辅助代码修复
- **Token 消耗追踪**：实时追踪 Agent Team 中所有 AI API 调用的 token 消耗量与预估成本
- **目标分支选择**：创建任务时支持选择目标分支（develop/main 等），灵活控制合入方向
- **手动 Issue 任务预览/编辑**：WebUI 中支持预览和编辑 Issue 分析结果后再创建 Agent 任务
- **PR 创建闭环**：支持 AI 生成 Conventional Commits 风格 PR 标题、描述和提交信息，创建 Draft PR，并通过 Sakura PR 审查与人工反馈继续迭代；不会自动合并 PR
  - Agent Team 初始创建的是 Draft PR；Draft opened webhook 不会触发 Sakura PR Review
  - 当 Draft PR 被标记为 Ready for review 后，GitHub `ready_for_review` webhook 会自动触发 Sakura PR Review
  - Bot 自己创建的 PR 在 GitHub 侧只能发表普通评论；Agent 闭环使用 Sakura 内部结构化审查结果判定是否继续
  - 存在 critical / major 等配置为阻塞的审查项，或分数低于 `agent_team_pr_review_pass_score` 时，Agent 会在同一 `sakura-agent/*` 分支继续迭代
  - 首轮迭代包含内部 Professional Reviewer 审查；闭环后续迭代跳过内部审查，直接交给外部 Sakura PR Review，节省 token 和时间
  - Agent push 新 commit 后，GitHub `synchronize` webhook 会自动触发下一轮 Sakura PR Review
  - 自动迭代受 `agent_team_max_iterations_per_task` 限制；达到上限或无法安全继续时进入 `waiting_human`
  - `agent_team_pr_closed_loop_enabled` 可关闭闭环并恢复创建 PR 即完成的旧行为

### 管理与运维

- **首次部署引导（Setup Wizard）**：首次启动自动检测配置状态，分步引导完成 GitHub App、数据库、AI 模型、RAG 等配置，支持断点续配
- **系统核心配置管理**：超级管理员可在 WebUI 运行时修改基础设施配置（数据库、GitHub App/OAuth、Telegram、应用域名等），无需重新运行 Setup Wizard，变更自动审计记录
- **动态配置管理**：通过 WebUI 修改配置即时生效，无需重启服务
- **AI API 超时治理**：通过 `ai_api_timeout_seconds` 和 `ai_api_total_timeout_seconds` 分别控制单次请求超时与重试循环总耗时
- **用户级配置覆盖**：普通用户可在个人设置或 API 中覆盖允许的偏好配置（当前支持 AI 输出语言），按 UserConfig → AppConfig → Settings 默认值逐级回退
- **AI Provider 注册表**：内置 OpenAI、DeepSeek、Qwen、Z.ai、Doubao、SiliconFlow、Gemini、Anthropic 兼容与自定义 OpenAI 兼容厂商，支持自动获取模型列表和上下文窗口信息
- **GitHub App 安装管理**：自动处理 GitHub App 安装/卸载事件，同步仓库授权状态
- **安全中心与多因素认证**：支持 TOTP、恢复码、Passkeys/WebAuthn、全局/单用户强制 MFA、管理员重置 MFA 与安全事件审计、MFA 失败锁定（动态阈值与锁定时长）、API Passkey 二次验证；移动端 OAuth 支持自定义回调 URI 白名单，WebAuthn 支持多个允许 Origin 与 Android App Links
- **SSE 实时推送**：基于 Redis Pub/Sub 的多进程实时通信，WebUI 数据即时更新
- **配额制访问控制**：基于配额的灵活访问管理体系，支持用户自注册，并按 UTC 日/周/月自动重置 PR、Issue 与 Agent 用量
- **付费配额系统**：套餐计划与兑换码完整 CRUD 管理（创建/编辑/删除/批量操作）、管理员手动充值，支持一次性包和订阅模式，并可为 PR、Issue、Agent 三类用量发放权益
- **外部支付与退款**：支持 Stripe、Paddle、支付宝、NOWPayments、TRON USDT 直收、支付回调验签、订单取消/查询、用户退款申请、超级管理员审核和退款通知
- **管理员操作审计**：完整的操作日志，覆盖配置变更、用户管理等关键操作
- **WebUI 管理界面**：仪表盘、PR 管理、用户管理、配置管理、队列监控、操作日志、仓库扫描管理、Agent 专家团队与 Agent Skills、Sakura 记忆管理、向量存储与数据库管理，支持 Markdown 内容渲染
- **批量 Issue 索引**：支持在 WebUI 中批量索引仓库 Issue 并刷新向量缓存，AI 元数据增强嵌入质量
- **健康检查端点**：`/health` 端点用于 Docker 健康检查和部署验证，Docker Compose 内置自动健康检测
- **注册配额管理**：独立的注册配额配置组，控制新用户注册时赠送的初始配额
- **Telegram Bot**：实时通知、按钮菜单交互、三级权限体系（超级管理员/管理员/普通用户）、配额管理
- **GitHub OAuth 登录**：与 Telegram 用户体系打通，明暗主题切换

---

## 🏗️ 技术架构

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
│  │   Webhook    │  │   PR 分析器   │  │  评论服务    │      │
│  │   Handler    │  │  (策略选择)   │  │  (发布结果)  │      │
│  │ (PR+Issue)   │  │              │  │              │      │
│  └──────────────┘  └──────────────┘  └──────────────┘      │
│  ┌──────────────────────────────────────────────────────┐   │
│  │    WebUI (Jinja2 + HTMX + Alpine.js) · SSE 实时推送   │   │
│  ├──────────────────────────────────────────────────────┤   │
│  │    Setup Wizard · 动态配置管理 · 管理员操作审计        │   │
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
│  │ RAG 检索   │  │ 代码索引    │  │ 历史上下文  │            │
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

**技术栈**：FastAPI (Python 3.11+) · Jinja2 + Tailwind CSS + HTMX + Alpine.js · DeepSeek-R1 / OpenAI 兼容 API · MySQL 8.0 + Redis (队列/PubSub) + ChromaDB · GitHub App (PyGithub) + OAuth · Docker Compose · 可选 Celery Worker

### 客户端

- **原生 Android App**：🚧 锐意开发中 → [Sakura-AI-Reviewer-APP](https://github.com/Sakura520222/Sakura-AI-Reviewer-APP)
  通过 [API v1 接口](docs/api-v1-reference.md) 与 Sakura-AI-Reviewer 后端对接，提供移动端管理体验

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

> 所有配置（GitHub App、AI 模型、数据库等）通过首次启动后的 Setup Wizard 在 Web 界面完成，无需手动编辑配置文件。

### 3. 创建 GitHub App

1. 访问 [GitHub Apps 设置](https://github.com/settings/apps)，点击 **New GitHub App**
2. 填写名称、Homepage URL
3. **Repository permissions**：Pull requests `Read and write`，Contents `Read and write`，Issues `Read and write`（可选）
4. **Webhook URL**：`https://your-domain.com:8000/api/webhook/github`，填写 Webhook secret
5. **Webhook events**：勾选 Pull requests、Pull request reviews、Issues（可选）、Issue comments（可选）
6. 创建后，在 App 页面底部 **Generate a private key**，下载 `.pem` 文件（Setup Wizard 中需粘贴完整私钥内容）
7. 点击左侧 **Install App**，选择要启用审查的仓库

> WebUI 登录需额外创建 [OAuth App](https://github.com/settings/developers)，回调地址设为 `https://your-domain.com/auth/callback`

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

### 6. Setup Wizard 引导配置

首次启动后访问 `https://your-domain.com/setup`，Setup Wizard 将分步引导完成所有配置（支持断点续配）：

1. **数据库配置**：填写 MySQL 和 Redis 连接地址，提供在线连接测试
2. **GitHub App 配置**：填写 App ID、私钥和 Webhook Secret，自动验证 App 连接
3. **AI 模型与通知**：配置 AI API（支持自动获取模型列表）和 Telegram Bot Token
4. **管理员与 OAuth**：设置管理员账户、应用域名和 GitHub OAuth 凭证

> Setup Wizard 内置 RAG 嵌入与重排序模型配置（可折叠），可跳过后续在 WebUI 中配置。

### 7. 验证部署

```bash
curl http://your-domain.com:8000/health
# {"status":"healthy","service":"Sakura AI Reviewer"}
```

WebUI：`https://your-domain.com/`

---

## 📖 使用说明

### PR 审查

在已安装 App 的仓库中创建 PR，AI 会自动审查并发布结构化报告。审查报告使用 `<details>` 折叠详情，保持评论简洁。在 PR 中可使用以下命令：

- `/full-review` — 清理旧评论并触发全量重新审查（PR 作者或协作者）
- `/revoke` — 一键撤回所有 AI 评论和 Review（仅管理员）

### Issue 分析

- **自动分析**：Issue opened/edited/reopened 时自动触发，发布分类、优先级、标签建议
- **自动打标**：AI 推荐标签，高置信度自动应用到 Issue
- **手动触发**：在 Issue 中评论 `/analyze`
- **Agent 委派**：仓库管理员或写权限协作者可在已分析 Issue 或扫描报告 Issue 中评论 `/agent`，将问题交给 Agent 专家团队处理；可使用 `/agent base:develop` 指定基础分支
- **重复检测**：自动识别重复 Issue 并关联已有 Issue

### WebUI 管理

访问 `https://your-domain.com/`，使用 GitHub 账号登录（需先在 Telegram Bot 中注册）。支持仪表盘图表、PR 管理、用户管理、动态配置管理、审查队列监控、操作日志、安全中心、个人 MFA/Passkey 设置等功能。配置修改即时生效，无需重启服务。

### Telegram Bot

提供实时通知（审查开始/完成）、配额管理、权限控制（三级体系）和丰富的管理命令。详见 [Telegram Bot 集成指南](docs/TELEGRAM_SETUP.md)。

---

## ⚙️ 配置说明

所有全局配置遵循优先级：**数据库 app_config（WebUI 管理） > Settings 默认值**，用户级偏好配置遵循 **UserConfig > app_config > Settings 默认值**。YAML 配置文件（`config/strategies.yaml`、`config/labels.yaml`）管理审查策略和标签定义。

> **动态配置**：通过 WebUI 的配置管理页面修改的配置项即时生效，无需重启服务。支持 AI 模型、辅助模型、RAG、Web 搜索、代码索引等多个配置分组。

- **AI 模型**：WebUI 配置管理中选择内置 AI Provider（OpenAI、DeepSeek、Qwen、Z.ai、Doubao、SiliconFlow、Gemini、Anthropic 兼容、自定义 OpenAI 兼容），设置 API 地址、API Key 和模型名称，并可自动拉取模型列表与上下文窗口信息
- **辅助模型**：WebUI 配置管理中设置 `summary_model`、`summary_api_base`、`summary_api_key`，用于摘要生成、上下文压缩、标签推荐等轻量任务，留空则自动回退到主模型
- **PR 自动审查**：WebUI 配置管理中 `enable_auto_review` 控制 PR webhook 是否自动触发审查；关闭后仍可通过命令或手动入口触发
- **AI API 超时**：WebUI 配置管理中 `ai_api_timeout_seconds` 控制单次请求超时，`ai_api_total_timeout_seconds` 控制一次 AI 调用重试循环的最长总耗时
- **安全与 MFA**：WebUI 安全中心可开启全局 MFA 要求、为单个用户强制 MFA、重置 TOTP/恢复码、删除 Passkeys，并记录安全审计事件；用户可在个人设置中启用 TOTP、生成恢复码、注册 Passkeys/WebAuthn；支持 MFA 失败锁定（`mfa_lockout_threshold` / `mfa_lockout_duration_minutes`）、API Passkey 二次验证、`passkeys_allowed_origins` 额外 Origin 和 `mobile_oauth_allowed_redirect_uris` 移动端 OAuth 回调白名单
- **审查策略**：编辑 `config/strategies.yaml`，支持快速/标准/深度/大PR 四种策略
- **文件过滤**：在 `config/strategies.yaml` 中配置跳过的文件扩展名和路径
- **AI 工具**：WebUI 配置管理中 `enable_ai_tools` / `max_tool_iterations`
- **标签推荐**：`config/labels.yaml` 配置 PR 标签推荐开关与置信度；Issue 标签在全局配置页 `issue_auto_create_labels` / `issue_confidence_threshold`
- **审查批准**：`config/strategies.yaml` 中 `review_policy` 配置阈值和仓库级覆盖
- **PR 变更总结**：WebUI 配置管理中 `enable_pr_summary`
- **PR 依赖图**：WebUI 配置管理中 `enable_pr_dependency_graph` / `pr_dependency_graph_mode` / `pr_dependency_graph_max_nodes` / `pr_dependency_graph_max_files`；`ai` 模式使用模型分析依赖，`static` 模式使用静态 import 解析降低成本
- **大型 PR 上下文治理**：WebUI 配置管理中 `model_context_window` / `context_safety_threshold` / `enable_context_compression` / `context_compression_threshold` / `context_compression_keep_rounds`；当初始 diff 过大时会自动使用 compact diff 工具模式
- **Token 成本追踪**：WebUI 配置管理中 `review_price_per_1k_prompt` / `review_price_per_1k_completion`，追踪审查 Token 消耗与成本
- **支付网关**：WebUI 配置管理中 `payment_enabled` 启用付费配额系统，按需配置 `stripe_*`、`paddle_*`、`alipay_*`、`nowpayments_*`、`tron_*` 网关参数；支持外部支付订单、回调验签、退款申请和超级管理员退款审核
- **RAG 知识库**：WebUI 配置管理中配置嵌入模型（支持 BAAI/bge-m3 等）、重排序模型、ChromaDB 等
- **PR 代码索引**：WebUI 配置管理中配置代码分块、支持语言、核心目录等
- **Issue 自动指派**：WebUI 配置管理中 `issue_auto_assign` / `issue_assignee_confidence_threshold`
- **Issue 并发控制**：WebUI 配置管理中 `max_concurrent_issues`，控制同时进行的最大 Issue 分析任务数，超出排队等待
- **Issue 标题改写**：WebUI 配置管理中 `issue_auto_rewrite_title`
- **语义 Issue 关联**：WebUI 配置管理中 `enable_semantic_issue_linking` / `semantic_issue_similarity_threshold`
- **增量审查历史**：WebUI 配置管理中 `enable_incremental_history_context`，AI 自动学习历史审查记录
- **行内评论开关**：WebUI 配置管理中 `enable_inline_comments`，控制是否在 PR diff 上发布行内评论，默认开启
- **Web 搜索工具**：WebUI 配置管理中 `web_search_provider`（`duckduckgo` 免费或 `tavily` 高级）
- **跨文件搜索**：`config/strategies.yaml` 中 `context_enhancement.search_in_files`，配置 GitHub Search API 优先策略、上下文行数、最大结果数等
- **Git 信息工具**：`config/strategies.yaml` 中 `context_enhancement.git_tools`，配置默认分支和提交返回数量
- **项目记忆系统**：WebUI 配置管理中 `sakura_memory_enabled` 启用记忆系统，`sakura_reflection_enabled` 启用审查后反思，`sakura_consolidation_interval` 合并触发的反思轮数（默认 5），`sakura_auto_init` 自动初始化 `.sakura/` 目录，`sakura_auto_create_subdirs` 自动创建 rules/docs/plans 子目录，`sakura_knowledge_extraction_enabled` 启用自动知识提取（通过三次串行 LLM 调用分别提取 rules/docs/plans），`sakura_extraction_provider` 配置提取 AI 凭据来源（主AI/辅助AI/独立配置）。WebUI 提供「Sakura 记忆管理」页面，支持查看/编辑/删除记忆文件、手动触发合并和知识提取。详见 [项目记忆系统使用指南](docs/SAKURA_MEMORY_GUIDE.md)
- **模型上下文**：WebUI 配置管理中配置上下文窗口、自动压缩等，详见 [模型上下文管理](docs/MODEL_CONTEXT_FEATURE.md)
- **Agent 专家团队**：WebUI Agent Team 页面配置 `agent_team_enabled`、`agent_team_workspace_root`、`agent_team_repo_allowlist`、`agent_team_model_provider`、`agent_team_*` 模型与护栏参数；支持上下文压缩（`agent_team_enable_context_compression` 等）、全栈/审查工具轮数（`agent_team_max_tool_rounds` / `agent_team_reviewer_max_tool_rounds`）、自动安装依赖（`agent_team_auto_install_deps`）、验证命令黑名单、Draft PR 开关和 PR 审查闭环（`agent_team_pr_closed_loop_enabled`、`agent_team_max_iterations_per_task`、`agent_team_pr_review_pass_score`）；`agent_team_model_provider=main` 时复用主 AI 配置，也可选择独立 Agent AI 配置；普通用户入口会校验仓库归属和 `agent_team_repo_allowlist` 并消耗 Agent 配额，Issue 评论 `/agent` 可从已分析 Issue 或扫描报告 Issue 创建任务；支持 Web 搜索工具和 Token 消耗追踪
- **Agent Skills**：WebUI Agent Skills 页面安装和启停 Skills；通过 `agent_team_skills_enabled` 控制 Agent 是否可加载技能，通过 `agent_team_skills_root` 配置本地存储根目录
- **国际化（i18n）**：WebUI 支持中英文界面切换（个人设置页面），AI 输出语言可通过全局配置 `OUTPUT_LANGUAGE` 或用户级配置覆盖（`output_language`，`zh-CN` / `en` / 跟随全局）控制，评论模板自动匹配对应语言

---

## 🖥️ 效果展示

<div align="center">

<img src="res/发送正在审查中和自动打标.png" width="1901" alt="审查进行中">

<img src="res/Issues分析.png" width="1707" alt="Issue分析">

<img src="res/WebUI.png" width="1707" alt="WebUI管理界面">

<img src="res/Telegram通知-1.png" width="627" alt="Telegram通知">

<img src="res/Telegram通知-2.png" width="537" alt="Telegram通知">

</div>

---

## 🛠️ 开发指南

### 本地开发

```bash
pip install -r requirements.txt
python -m backend.main
```

> 首次启动将进入 Bootstrap 模式，访问 `http://localhost:8000/setup` 通过 Setup Wizard 完成配置。

如果只想在本地调试首次部署/Setup Wizard 流程，使用独立的 dev 配置文件启动：

```bash
py scripts/dev_bootstrap.py
```

该脚本会使用 `.sakura/dev/connection.json`，不会覆盖正式的 `config/connection.json`，并会跳过 Telegram、SSE、扫描、配额等后台任务。需要重新从第 0 步调试时：

```bash
py scripts/dev_bootstrap.py --reset
```

### 代码检查

```bash
python run_ruff.py
```

### 代码结构

```
Sakura-AI-Reviewer/
├── backend/
│   ├── api/               # API 路由（webhook、health、v1）
│   │   └── v1/            #   RESTful API v1（移动端对接，含 user_config/billing）
│   ├── core/              # 核心配置、动态配置管理、AI Provider 注册表
│   ├── models/            # 数据模型（SQLAlchemy）
│   ├── services/          # 业务逻辑
│   │   ├── agent_team/    # Agent 专家团队、受控工作区工具、PR 创建与 Skills
│   │   ├── ai_reviewer/   # AI 审查引擎
│   │   │   ├── tools/     #   AI 工具（文件读取、跨文件搜索、Git 信息、Web 搜索、Sakura 记忆）
│   │   │   └── compression/ # 上下文压缩
│   │   ├── pr_analyzer.py # PR 分析器（策略选择）
│   │   ├── issue_analyzer.py  # Issue 分析引擎
│   │   ├── issue_service.py   # Issue 服务（打标、指派、改写）
│   │   ├── issue_embedding_service.py  # Issue 向量嵌入
│   │   ├── pr_issue_linker.py # PR-Issue 关联
│   │   ├── decision_engine.py # 审查决策引擎
│   │   ├── comment_service.py # 评论服务
│   │   ├── rag_service.py     # RAG 知识库
│   │   ├── code_index_service.py  # 代码索引
│   │   ├── scan_prompt_builder.py # 仓库扫描 Prompt 构建
│   │   ├── scan_report_service.py # 扫描报告服务
│   │   ├── scan_scheduler.py      # 扫描调度器
│   │   ├── history_context_service.py  # 增量审查历史
│   │   ├── sakura_memory_service.py    # .sakura/ 项目记忆服务
│   │   ├── sakura_consolidation_agent.py  # .sakura/ 记忆合并 Agent（工具调用驱动）
│   │   ├── sakura_knowledge_extractor.py  # .sakura/ 知识提取 Agent
│   │   ├── github_write_service.py     # GitHub 写操作服务（.sakura/ 写入）
│   │   ├── two_factor_service.py       # TOTP 与恢复码服务
│   │   ├── webauthn_service.py         # Passkeys/WebAuthn 服务
│   │   ├── security_admin_service.py   # 安全中心管理服务
│   │   └── security_audit_service.py   # 安全审计服务
│   ├── webui/             # WebUI 管理界面
│   │   ├── routes/        #   路由（dashboard, config, users, ...）
│   │   ├── templates/     #   Jinja2 模板
│   │   ├── auth.py        #   GitHub OAuth 认证
│   │   └── sse.py         #   SSE 实时推送
│   ├── workers/           # 后台任务（review_worker, issue_worker, scan_worker）
│   ├── telegram/          # Telegram Bot（通知、命令、按钮菜单、权限）
│   └── bootstrap.py       # Setup Wizard 引导配置
├── config/                # YAML 配置文件（strategies.yaml）
├── docker/                # Docker Compose 部署
├── docs/                  # 项目文档
└── .understand-anything/  # 交互式知识图谱（Understand Anything）
```

### 交互式知识图谱

项目使用 [Understand Anything](https://github.com/Lum1104/Understand-Anything) 生成交互式代码知识图谱，包含架构层次、节点关系和学习路径，便于快速理解项目结构。

**生成/更新知识图谱**（在 Claude Code 中执行）：

```
/understand --language zh
```

**启动可视化仪表盘**：

```
/understand-dashboard
```

启动后会自动在浏览器中打开交互式仪表盘，支持以下功能：

- 浏览架构层次和模块依赖关系
- 查看节点（文件、函数、类、端点）之间的调用和导入关系
- 按引导路径逐步了解项目架构
- 按类型、标签、层级筛选节点

知识图谱数据存储在 `.understand-anything/knowledge-graph.json`，支持增量更新——代码变更后重新执行 `/understand` 即可自动同步。

---

## 📚 详细文档

| 文档                                                  | 说明                      |
|-----------------------------------------------------|-------------------------|
| [Telegram Bot 集成指南](docs/TELEGRAM_SETUP.md)         | Bot 设置、权限体系、命令参考        |
| [审查批准功能](docs/APPROVAL_FEATURE_SUMMARY.md)          | 智能审查批准系统详细说明            |
| [手动审查功能](docs/MANUAL_REVIEW_FEATURE.md)             | 超级管理员手动触发审查             |
| [模型上下文管理](docs/MODEL_CONTEXT_FEATURE.md)            | AI 模型上下文和压缩功能           |
| [PR 功能指南](docs/PR_FEATURES_GUIDE.md)                   | PR 变更总结与依赖图配置说明        |
| [配额系统指南](docs/QUOTA_SYSTEM_GUIDE.md)                 | PR/Issue 配额统计与自动重置机制    |
| [安全与 MFA 指南](docs/SECURITY_MFA_GUIDE.md)              | TOTP、恢复码、Passkeys/WebAuthn 与安全中心 |
| [API v1 参考文档](docs/api-v1-reference.md)             | RESTful API v1.3 接口文档（移动端 OAuth、MFA、SSE、Billing） |
| [WebUI 设计文档](docs/plans/2026-03-27-webui-design.md) | WebUI 设计规范              |
| [项目记忆系统使用指南](docs/SAKURA_MEMORY_GUIDE.md) | .sakura/ 目录结构、生命周期、配置说明 |
| [项目记忆系统设计](docs/plans/2026-04-20-sakura-memory-design.md) | .sakura/ 记忆系统架构与配置 |
| [Agent 专家团队模式](docs/plans/agent_expert_team_mode.md) | Agent 自动修复、受控工作区与 PR 创建流程 |
| [Agent Skills 实现](docs/agent-skills-python-implementation.md) | Skills 安装、索引、启停与工具集成说明 |
| [Agent 文件工具实现](docs/agent-file-tools-python-implementation.md) | Agent 工作区文件工具、安全边界与实现细节 |
| [Agents 项目指南](AGENTS.md)                               | 自动化代理与贡献者项目约定         |

---

## 🤝 贡献

本项目使用标准 Gitflow 工作流：

- `main`：生产发布分支，仅接收 `release/*` 与 `hotfix/*` 合并
- `develop`：日常集成分支，普通功能与修复的目标分支
- `feature/*`：功能分支，从 `develop` 创建并合回 `develop`
- `release/*`：发布准备分支，从 `develop` 创建并合入 `main`
- `hotfix/*`：线上紧急修复分支，从 `main` 创建并合入 `main`

日常贡献流程：

1. Fork 本项目
2. 基于 `develop` 创建特性分支 (`git checkout develop && git checkout -b feature/amazing-feature`)
3. 提交更改 (`git commit -m 'feat: add some amazing feature'`)
4. 推送到分支 (`git push origin feature/amazing-feature`)
5. 开启 Pull Request，目标分支选择 `develop`

发布流程由维护者执行：从 `develop` 创建 `release/x.y.z`，完成版本号、文档和回归检查后合并到 `main`；合并后自动发布 Release，并将 `main` 回合到 `develop`。

紧急修复流程由维护者执行：从 `main` 创建 `hotfix/x.y.z`，修复后合并到 `main` 并发布，再将 `main` 回合到 `develop`。

自动化工作流会协助维护 Gitflow：PR 分支流向校验会阻止普通分支直接合入 `main`；CI 会在 `develop` / `main` PR 和主要开发分支上运行 Ruff 与测试；`release/*` 或 `hotfix/*` 合入 `main` 后会自动发布 Release，并自动尝试将 `main` 回合到 `develop`；已合并的临时 Gitflow 分支会自动清理。

提交信息请使用英文 [Conventional Commits](https://www.conventionalcommits.org/) 格式。

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
