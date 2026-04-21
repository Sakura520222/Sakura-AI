# Sakura AI Reviewer 项目概述

## 1. 项目简介
Sakura AI Reviewer 是一款基于大语言模型的智能 GitHub 代码审查与 Issue 分析机器人，具备主动探索代码库、跨文件依赖理解、全仓库扫描和项目记忆能力，旨在自动化提升代码质量管理与日常协作效率。

## 2. 技术栈
- **后端语言**：Python 3.11+
- **Web 框架**：FastAPI
- **前端界面**：HTML（自包含 WebUI）
- **数据库**：MySQL（关系型存储）、Redis（Pub/Sub 与缓存）
- **AI 与检索**：LLM API、RAG 向量检索、DuckDuckGo/Tavily 搜索
- **基础设施**：Docker、Docker Compose
- **代码规范**：Ruff（Linter & Formatter）
- **集成渠道**：GitHub App（Webhook/OAuth）、Telegram Bot

## 3. 架构设计
采用后端分层架构，前后端同仓管理：
- `backend/api/`：RESTful 路由
- `backend/core/`：配置、依赖注入
- `backend/models/`：数据库模型与数据结构
- `backend/services/`：核心业务逻辑（AI 审查、记忆系统、RAG 检索等）
- `backend/workers/`：异步任务（仓库扫描、消息推送）
- `backend/telegram/`：Telegram Bot 交互
- `backend/webui/`：前端静态资源
- `config/`：外部化配置（labels.yaml、strategies.yaml）

**关键决策**：
- 配置与代码分离，业务规则通过 YAML 外部化支持热更新
- 耗时任务通过 workers 模块与主 API 隔离，保障响应速度
- 容器化交付确保开发、测试与生产环境一致性

## 4. 已知问题与注意事项
### PR190 遗留缺陷（未闭环）
- **`github_write_service.py:76`**：多文件提交非原子性，存在数据一致性风险
- **`github_write_service.py:28-31`**：单例实现存在线程安全问题
- **`sakura_memory_service.py`**：819行"上帝服务"，承载核心逻辑与 Prompt 模板管理，职责过重

### 安全敏感点
- 路径处理禁用字符串检查（如 `if "../" in path`），必须使用 `pathlib.Path.resolve()` + 前缀验证
- 写操作服务必须审查权限边界与异常处理链路
- 安全相关问题禁止降级为 suggestion

## 5. 审查发现的重要模式
- **数据契约不严格**：跨模块传递混用 `dict` 与对象，缺乏类型注解，易引发运行时 `AttributeError`
- **Prompt 防御性编码**：将 `{xxx}` 占位符转为自然语言指令，消除 LLM 对元字符的歧义理解
- **渐进式修复**：聚焦单一职责的最小化改动是良好实践，但需警惕"点状修复"忽视全局同类问题

## 6. 团队约定与规范
### 代码规范
- 跨函数/模块传递复杂数据结构，禁止裸 `dict`，必须用 `Pydantic BaseModel`、`dataclass` 或 `TypedDict`
- 文件路径操作必须使用 `pathlib`，禁止字符串拼接/替换
- 建议引入 `mypy`/`pyright` 严格模式拦截隐式类型错误

### 审查规则（按优先级）
| 规则 | 级别 | 触发条件 |
|------|------|----------|
| 历史阻断机制 | P0 | 存在未闭环 error/critical 时，增量审查禁止 approve |
| PR-SIZE-001 | P0 | 文件数 >10 禁止 quick 策略 |
| REVIEW-OUTPUT-001 | P0 | 评分必须有评论支撑 |
| REVIEW-DECISION-001 | P0 | 禁止 unknown 决策 |
| SEC-PATH-001 | error | 文件路径使用字符串检查 `../` |
| SEC-WRITE-001 | error | 新增写服务未审查权限边界 |
| SEC-PATH-002 | warning | 文件操作未用 `pathlib.Path.resolve()` |
| ARCH-LARGE-FILE | warning | 单文件新增超 500 行 |
| PR-SCOPE-001 | P1 | 单PR同时含新子系统+配置变更+版本升级 |

### PR 审查原则
- 大 PR（>1000行）应按模块拆分审查：核心服务 → 安全敏感 → 配置/脚本
- 安全敏感模块（写操作、文件访问）强制提升审查级别
- 评分与决策解耦：评分反映增量质量，decision 反映 PR 整体可合并性
- 单PR不应同时包含新子系统、配置变更、版本升级等多关注点