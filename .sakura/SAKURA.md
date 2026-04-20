# Sakura AI Reviewer 项目概述

## 1. 项目简介
Sakura AI Reviewer 是一款基于大语言模型的智能 GitHub 代码审查与 Issue 分析机器人，具备主动探索代码库、跨文件依赖理解和全仓库扫描能力，旨在自动化提升代码质量管理与日常协作效率。

## 2. 技术栈
- **后端语言**：Python 3.11+
- **Web 框架**：FastAPI
- **前端界面**：HTML（自包含 WebUI）
- **数据库**：MySQL（关系型数据存储）、Redis（Pub/Sub 实时通信与缓存）
- **AI 与检索**：LLM API 对接、RAG 向量语义检索、DuckDuckGo/Tavily Web 搜索
- **基础设施**：Docker、Docker Compose
- **代码规范**：Ruff（Linter 与 Formatter）
- **集成渠道**：GitHub App（Webhook/OAuth）、Telegram Bot

## 3. 项目结构
项目采用典型的后端分层架构，前后端同仓管理，核心目录说明如下：

- **backend/**：后端核心代码
  - `api/`：RESTful API 路由定义
  - `core/`：核心配置、依赖注入与公共组件
  - `models/`：数据库模型与数据结构定义
  - `services/`：核心业务逻辑（AI 审查、Issue 分析、RAG 检索等）
  - `workers/`：后台异步任务处理（如仓库扫描、消息推送）
  - `telegram/`：Telegram Bot 交互与指令处理
  - `webui/`：前端静态页面资源（解释了 HTML 代码占比较高的原因）
- **config/**：外部化配置文件
  - `labels.yaml`：Issue 标签分类与映射策略
  - `strategies.yaml`：自适应审查策略配置
- **docker/**：容器化部署配置
  - 包含 Dockerfile、docker-compose.yml 及 MySQL 初始化脚本
- **docs/**：项目文档
  - 包含 API 参考手册、特性说明文档及未来规划
- **res/**：静态资源文件
  - 存放用于 README 展示的功能截图
- **.github/workflows/**：GitHub Actions CI/CD 流程配置
- **run_ruff.py**：Ruff 代码检查与格式化执行脚本
- **start.sh**：项目快速启动脚本

## 4. 开发约定
基于项目结构与配置文件推断，该项目遵循以下开发规范：

- **代码风格统一**：使用 Ruff 作为唯一的 Python 代码检查和格式化工具，通过 `run_ruff.py` 统一执行，未引入复杂的 Pre-commit 钩子配置。
- **分层架构模式**：后端严格遵循“API 路由 -> 服务层 -> 数据层”的解耦设计，业务逻辑高度内聚于 `services` 目录。
- **配置与代码分离**：业务规则（如标签策略、审查模式）通过 YAML 文件外部化配置，支持热更新，不硬编码在业务代码中。
- **前后端一体仓**：前端 WebUI 直接打包存放在 `backend/webui/` 下，由 FastAPI 托管静态资源，简化了跨仓发布和部署流程。
- **异步任务隔离**：耗时任务（如全仓库扫描、AI 推理）通过独立的 `workers` 模块处理，与主 API 进程分离，保障接口响应速度。
- **容器化交付**：环境依赖、数据库初始化均通过 Docker Compose 编排，确保开发、测试与生产环境的一致性。
- **文档双语化**：面向开源社区，提供中英文双语的 README，并具备详细的 API 参考与特性拆解文档。