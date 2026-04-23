# Sakura AI Reviewer 项目概述

## 1. 项目简介与技术栈
Sakura AI Reviewer 是一款基于大语言模型的智能 GitHub 代码审查与 Issue 分析机器人，具备主动探索代码库、跨文件依赖理解和全仓库扫描能力，旨在自动化提升代码质量管理与日常协作效率。

- **后端语言**：Python 3.11+
- **Web 框架**：FastAPI
- **前端界面**：HTML（自包含 WebUI）
- **数据库**：MySQL（关系型存储）、Redis（Pub/Sub 实时通信与缓存）
- **AI 与检索**：LLM API 对接、RAG 向量语义检索、DuckDuckGo/Tavily Web 搜索
- **基础设施**：Docker、Docker Compose
- **代码规范**：Ruff（Linter 与 Formatter）
- **集成渠道**：GitHub App（Webhook/OAuth）、Telegram Bot

## 2. 架构设计与关键决策
- **分层架构**：API