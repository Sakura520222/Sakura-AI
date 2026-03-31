# WebUI 功能设计文档

## 1. 项目概述

为 Sakura AI Reviewer 项目添加现代化的 WebUI 管理界面。项目当前是一个基于 FastAPI 的 GitHub PR 自动审查服务，通过 GitHub Webhook 接收 PR 事件，使用 Telegram Bot 作为管理界面，无任何 Web 前端。WebUI 将在此基础上提供可视化的 PR 审查管理、用户管理、仓库管理和配置管理功能。

### 设计原则

- **复用优先**：基于现有 FastAPI 应用、SQLAlchemy 模型、MySQL 数据库扩展，不引入新的运行时依赖
- **渐进交付**：按 P0/P1/P2 三阶段实施，每阶段独立可用
- **安全隔离**：AI API Key 等敏感配置仅通过 `.env` 管理，WebUI 不可感知

## 2. 技术选型

### 2.1 后端技术栈（复用现有）

| 技术       | 版本    | 说明                    |
| ---------- | ------- | ----------------------- |
| FastAPI    | 0.109.0 | 现代异步 Web 框架，已有 |
| SQLAlchemy | 2.0.25  | 异步 ORM，已有          |
| Alembic    | 1.13.1  | 数据库迁移，已有        |
| Pydantic   | 2.5.3   | 数据验证，已有          |
| loguru     | 0.7.2   | 日志，已有              |
| Redis      | 5.0.1   | 缓存，已有              |

### 2.2 前端技术栈（新增）

| 技术             | 说明                                         |
| ---------------- | -------------------------------------------- |
| **Jinja2**       | 服务端模板引擎（FastAPI 原生支持）           |
| **HTMX**         | 通过 HTML 属性实现动态页面更新，无需构建工具 |
| **Tailwind CSS** | 实用优先 CSS 框架                            |
| **Alpine.js**    | 轻量级 JS 框架，处理少量客户端交互（可选）   |

### 2.3 需新增的依赖

```
jinja2                           # 模板渲染
python-jose[cryptography]        # JWT 令牌
itsdangerous                     # CSRF 保护
```

### 2.4 认证方案

- **GitHub OAuth**: 用户登录 WebUI
- **JWT Token**: WebUI 会话认证
- 现有 GitHub App 认证（JWT + Installation Token）保持不变，用于 PR 操作

## 3. 系统架构

### 3.1 整体架构

```
┌─────────────────────────────────────────────────────────┐
│                    WebUI 层                           │
│  ┌─────────────┐  ┌─────────────┐  ┌─────────────┐     │
│  │   HTMX      │  │  Tailwind   │  │   模板      │     │
│  │  页面       │  │   CSS       │  │  引擎      │     │
│  └─────────────┘  └─────────────┘  └─────────────┘     │
└─────────────────────────────────────────────────────────┘
                              │
┌─────────────────────────────────────────────────────────┐
│                   API 层                              │
│  ┌─────────────┐  ┌─────────────┐  ┌─────────────┐     │
│  │   FastAPI   │  │   路由      │  │   中间件    │     │
│  │  路由处理   │  │   管理      │  │   认证      │     │
│  └─────────────┘  └─────────────┘  └─────────────┘     │
└─────────────────────────────────────────────────────────┘
                              │
┌─────────────────────────────────────────────────────────┐
│                   业务层                              │
│  ┌─────────────┐  ┌─────────────┐  ┌─────────────┐     │
│  │  审查服务   │  │ 配置服务   │  │ 用户服务    │     │
│  │   管理      │   管理      │   管理      │     │
│  └─────────────┘  └─────────────┘  └─────────────┘     │
└─────────────────────────────────────────────────────────┘
                              │
┌─────────────────────────────────────────────────────────┐
│                   数据层                              │
│  ┌─────────────┐  ┌─────────────┐  ┌─────────────┐     │
│  │   数据库    │  │   缓存      │  │   文件      │     │
│  │ (MySQL/PG)  │  │   (Redis)   │  │   存储      │     │
│  └─────────────┘  └─────────────┘  └─────────────┘     │
└─────────────────────────────────────────────────────────┘
```

### 3.2 用户认证流程

WebUI 认证与现有 Telegram 用户体系打通，复用 `telegram_users` 表：

1. 用户访问 WebUI
2. 点击"GitHub 登录"
3. 重定向到 GitHub OAuth 授权页面
4. 授权后获取 GitHub 用户信息（github_username）
5. 通过 `github_username` 匹配现有 `telegram_users` 记录
6. 已匹配：创建 JWT Token，跳转仪表板
7. 未匹配：提示用户先通过 Telegram Bot 注册

## 4. 页面路由设计

按阶段标注交付优先级，基于现有数据库表设计。

```
/                        - 仪表板（总览统计）                   [P0] ✅
├── /auth                - GitHub OAuth 认证
│   ├── /login           - 登录页面                             [P0] ✅
│   ├── /github          - GitHub OAuth 授权跳转                [P0] ✅
│   ├── /callback        - OAuth 回调处理                       [P0] ✅
│   └── /logout          - 登出                                 [P0] ✅
├── /pr                  - PR 审查管理
│   ├── /                - PR 列表（搜索、过滤、分页）          [P0] ✅
│   ├── /export-csv      - 导出 PR 列表为 CSV                  [P4] ✅
│   ├── /:id             - PR 详情（审查结果、评论）            [P0] ✅
│   └── /:id/files       - 文件级审查（按文件分组评论）        [P3] ✅
├── /config              - 配置管理（仅超级管理员）
│   ├── /strategies      - 审查策略（读写 strategies.yaml）     [P2] ✅
│   ├── /labels          - 标签配置                             [P2] ✅
│   └── /general         - 全局配置                             [P5] ✅
├── /users               - 用户和权限管理
│   ├── /                - 用户列表                             [P1] ✅
│   ├── /:id             - 用户详情（角色、配额）               [P1] ✅
│   └── /roles           - 角色权限管理                         [P1] 已集成（在用户详情页内）
├── /repos               - 仓库管理
│   ├── /                - 仓库列表（repo_subscriptions）       [P1] ✅
│   └── /add             - 添加仓库到白名单                     [P1] ✅（内联表单）
├── /logs                - 审查日志
│   ├── /                - 审查日志（pr_reviews）               [P1] ✅
│   └── /actions         - 操作日志                             [P5] ✅
├── /queue               - 审查队列监控（管理员）
│   └── /                - 队列状态 + 过滤 + 统计              [P3] ✅
└── /settings            - 系统设置
    ├── /                - 个人设置                             [P1] ✅
    └── /about           - 关于                                 [P5] ✅
```

### 各阶段数据来源

| 阶段 | 页面                   | 主要数据来源（现有表）                                     |
| ---- | ---------------------- | ---------------------------------------------------------- |
| P0   | 仪表盘、PR 列表/详情   | `pr_reviews`, `review_comments`                            |
| P1   | 用户、仓库、日志、设置 | `telegram_users`, `repo_subscriptions`, `quota_usage_logs` |
| P2   | 配置管理               | `config/strategies.yaml`, `app_config`                     |

## 5. 数据模型设计

基于现有数据库表（`backend/models/database.py`）扩展，不新建与现有功能重叠的表。

### 5.1 现有表复用（无需修改）

以下表已在生产中使用，WebUI 直接读取：

| 现有表               | WebUI 用途                                   |
| -------------------- | -------------------------------------------- |
| `pr_reviews`         | PR 审查列表、详情、统计（仪表盘、PR 管理页） |
| `review_comments`    | PR 文件级审查评论（PR 详情页）               |
| `telegram_users`     | 用户列表、角色管理、配额管理（用户管理页）   |
| `repo_subscriptions` | 仓库白名单管理（仓库管理页）                 |
| `quota_usage_logs`   | 配额使用记录（用户详情页）                   |
| `review_queue`       | 审查任务队列状态（仪表盘）                   |

### 5.2 现有表扩展

**telegram_users 表新增字段**（用于 GitHub OAuth 登录）：

```python
# 在现有 TelegramUser 模型上扩展
github_oauth_id = Column(String(50), nullable=True)   # GitHub OAuth ID
avatar_url = Column(String(255), nullable=True)        # 用户头像 URL
webui_last_login = Column(DateTime, nullable=True)     # WebUI 最后登录时间
```

### 5.3 新增表

**webui_configs** — 用户偏好设置：

```python
class WebUIConfig(Base):
    """用户 WebUI 偏好设置，每个用户一条记录"""
    __tablename__ = "webui_configs"

    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey("telegram_users.id"), unique=True)
    theme = Column(String(10), default='light')          # light / dark
    language = Column(String(10), default='zh-CN')
    items_per_page = Column(Integer, default=20)

    user = relationship("TelegramUser", backref="webui_config")
```

### 5.4 设计说明

- **不新建 `users` 表**：复用 `telegram_users`，通过新增的 `github_oauth_id` 字段关联 GitHub OAuth
- **不新建 `github_installs` 表**：GitHub App 配置通过环境变量管理，安装状态通过 GitHub API 实时查询
- **`app_config` 表**：现有键值对配置表继续使用，WebUI 配置管理页面对 `config/strategies.yaml` 的编辑通过文件 I/O + 热更新实现

## 6. API 接口设计

所有 API 以 `/api/webui/` 为前缀，与现有 `/api/webhook/github` 不冲突。

### 6.1 认证相关 API

- `GET /api/webui/auth/login` - GitHub OAuth 登录（重定向）
- `GET /api/webui/auth/callback` - GitHub OAuth 回调
- `POST /api/webui/auth/logout` - 登出
- `GET /api/webui/auth/me` - 当前登录用户信息

### 6.2 PR 审查管理 API [P0]

数据来源：`pr_reviews` + `review_comments` 表

- `GET /api/webui/prs` - PR 列表（分页、搜索、按仓库/状态/决策过滤）
- `GET /api/webui/prs/{id}` - PR 详情（审查摘要、评分、决策）
- `GET /api/webui/prs/{id}/comments` - PR 审查评论列表
- `GET /api/webui/stats` - 仪表盘统计数据

### 6.3 用户管理 API [P1]

数据来源：`telegram_users` + `quota_usage_logs` 表

- `GET /api/webui/users` - 用户列表（分页）
- `GET /api/webui/users/{id}` - 用户详情（角色、配额使用情况）
- `PUT /api/webui/users/{id}/role` - 更新用户角色（仅管理员）
- `PUT /api/webui/users/{id}/quota` - 更新用户配额（仅管理员）
- `PUT /api/webui/users/{id}/status` - 启用/禁用用户（仅管理员）

### 6.4 仓库管理 API [P1]

数据来源：`repo_subscriptions` 表

- `GET /api/webui/repos` - 仓库列表
- `POST /api/webui/repos` - 添加仓库到白名单（仅管理员）
- `DELETE /api/webui/repos/{id}` - 移除仓库（仅管理员）
- `PUT /api/webui/repos/{id}/status` - 启用/禁用仓库（仅管理员）

### 6.5 配置管理 API [P2]

数据来源：`config/strategies.yaml` + `app_config` 表

> **注意**：AI API Key 等敏感配置仅通过 `.env` 文件管理，WebUI 不提供读取和修改接口。

- `GET /api/webui/config/strategies` - 获取审查策略配置（仅超级管理员）
- `PUT /api/webui/config/strategies` - 更新审查策略配置（仅超级管理员，写入 YAML 并触发热更新）
- `GET /api/webui/config/labels` - 获取标签推荐配置（仅超级管理员）
- `PUT /api/webui/config/labels` - 更新标签配置（仅超级管理员）

## 7. 权限设计

复用现有 `UserRole` 枚举（`backend/models/telegram_models.py`）：`super_admin` / `admin` / `user`。

| 角色            | 权限说明                           | 可访问页面                |
| --------------- | ---------------------------------- | ------------------------- |
| **user**        | 查看与自己相关的 PR 审查结果       | 仪表板、我的 PR、个人设置 |
| **admin**       | 查看所有 PR、管理用户和仓库        | P0 + P1 所有页面          |
| **super_admin** | 最高权限，可管理审查策略和标签配置 | 所有页面含 P2 配置管理    |

### 配置管理权限约束

- **AI API Key**（`OPENAI_API_KEY` 等）：仅通过 `.env` 文件管理，所有角色均无法通过 WebUI 读取或修改
- **审查策略**（`strategies.yaml`）：仅 `super_admin` 可编辑
- **标签规则**：仅 `super_admin` 可编辑
- **全局配置**（`app_config` 表中的非敏感项）：仅 `super_admin` 可编辑

## 8. 安全考虑

1. **HTTPS**: 生产环境必须使用 HTTPS
2. **CSRF 保护**: HTMX 请求需携带 CSRF Token（基于 `itsdangerous` 签名）
3. **XSS 防护**: Jinja2 模板自动转义 + 输入验证
4. **JWT 认证**: HttpOnly Cookie 存储 Token，防止 XSS 窃取
5. **权限检查**: FastAPI 依赖注入实现路由级权限校验
6. **敏感配置隔离**: `.env` 中的 API Key、私钥等不进入 WebUI 数据流
7. **日志记录**: 使用 loguru 记录所有管理操作（用户变更、配置修改等）

## 9. 性能优化

1. **数据库索引**: 复用现有索引，为 WebUI 新增的查询模式按需添加
2. **缓存策略**: 使用现有 Redis 缓存仪表盘统计数据和配置
3. **分页加载**: 所有列表页面默认分页（每页 20 条）
4. **静态资源**: Tailwind CSS 通过 CDN 引入，无需本地构建

## 10. 部署方案

### 10.1 新增依赖安装

```bash
pip install jinja2 python-jose[cryptography] itsdangerous
```

### 10.2 数据库迁移

```bash
# 新增 webui_configs 表 + telegram_users 扩展字段
alembic revision --autogenerate -m "add_webui_support"
alembic upgrade head
```

### 10.3 生产环境

- 基于现有 Docker Compose 扩展，无需新增容器
- 现有 `backend/main.py` 注册 WebUI 路由和 Jinja2 模板目录
- Nginx 配置新增 WebUI 相关 location 规则（如需独立域名）
- 新增环境变量：`GITHUB_OAUTH_CLIENT_ID`、`GITHUB_OAUTH_CLIENT_SECRET`、`WEBUI_SECRET_KEY`

## 11. 监控和日志

1. **应用日志**: 使用 loguru 记录应用日志
2. **访问日志**: 记录所有 API 访问
3. **错误监控**: 集成 Sentry 或类似服务
4. **性能监控**: 监控 API 响应时间和数据库查询

## 12. 测试策略

1. **单元测试**: 测试各个业务逻辑
2. **集成测试**: 测试 API 接口
3. **端到端测试**: 测试完整用户流程
4. **性能测试**: 测试系统负载能力

## 13. 扩展性考虑

1. **微服务架构**: 未来可拆分为独立服务
2. **插件系统**: 支持自定义审查插件
3. **多租户支持**: 支持多组织使用
4. **国际化**: 支持多语言

## 14. 实现计划

### P0 — 核心功能（仪表盘 + PR 管理 + GitHub OAuth）✅ 已完成

**目标**：WebUI 基础框架 + GitHub OAuth 认证 + PR 审查可视化

**已完成功能**：

1. ✅ 搭建 WebUI 项目结构：Jinja2 模板目录、Tailwind CSS CDN 引入、HTMX 2.0 配置
2. ✅ GitHub OAuth 认证流程（对接 `telegram_users.github_username` 匹配）
3. ✅ JWT 认证（HttpOnly Cookie）+ CSRF 保护（itsdangerous 签名）
4. ✅ 基础布局模板：导航栏、侧边栏、明暗主题切换（Alpine.js）
5. ✅ 仪表盘页面：审查统计卡片、最近审查列表
6. ✅ PR 列表页面：HTMX 动态搜索、按仓库/状态/决策过滤、分页
7. ✅ PR 详情页面：审查摘要、评分、评论列表

**已实现文件**：

| 文件                                | 说明                                                                  |
| ----------------------------------- | --------------------------------------------------------------------- |
| `backend/webui/auth.py`             | JWT 令牌创建/解码（python-jose）                                      |
| `backend/webui/deps.py`             | 依赖注入：模板引擎、数据库会话、CSRF、认证/权限                       |
| `backend/webui/routes/__init__.py`  | 路由聚合（`/webui` 前缀）                                             |
| `backend/webui/routes/auth.py`      | OAuth 登录/回调/登出                                                  |
| `backend/webui/routes/dashboard.py` | 仪表盘 + 统计 API                                                     |
| `backend/webui/routes/pr.py`        | PR 列表/详情 + HTMX 片段                                              |
| `backend/webui/templates/`          | Jinja2 模板（base, login, dashboard, pr_list, pr_detail, components） |

### P1 — 管理功能（用户 + 仓库 + 日志 + 设置）✅ 已完成

**目标**：完整的管理后台

**已完成功能**：

1. ✅ 个人设置：语言偏好、每页条数偏好（`webui_configs` 表 upsert）
2. ✅ 用户管理：列表（搜索/角色过滤/分页）、详情（配额进度条/使用历史）、角色变更、配额管理、启用/禁用
3. ✅ 仓库管理：列表（搜索/状态过滤/分页）、内联添加仓库、启用/禁用、删除（含确认弹窗）
4. ✅ 审查日志：多条件过滤（搜索/仓库/状态/日期范围）、展开详情、权限过滤（普通用户仅看已启用仓库）

**已实现文件**：

| 文件                                                          | 说明                                         |
| ------------------------------------------------------------- | -------------------------------------------- |
| `backend/webui/routes/settings.py`                            | 个人设置路由（GET/POST）                     |
| `backend/webui/routes/users.py`                               | 用户管理路由（6 个端点，含角色层级保护）     |
| `backend/webui/routes/repos.py`                               | 仓库管理路由（5 个端点，正则验证 repo_name） |
| `backend/webui/routes/logs.py`                                | 审查日志路由（3 个端点，权限过滤）           |
| `backend/webui/templates/settings.html`                       | 个人设置页                                   |
| `backend/webui/templates/users.html`                          | 用户列表页                                   |
| `backend/webui/templates/user_detail.html`                    | 用户详情页                                   |
| `backend/webui/templates/repos.html`                          | 仓库列表页                                   |
| `backend/webui/templates/logs.html`                           | 审查日志页                                   |
| `backend/webui/templates/components/user_list_fragment.html`  | 用户列表 HTMX 片段                           |
| `backend/webui/templates/components/repo_list_fragment.html`  | 仓库列表 HTMX 片段                           |
| `backend/webui/templates/components/log_filters.html`         | 日志过滤表单                                 |
| `backend/webui/templates/components/log_list_fragment.html`   | 日志列表 HTMX 片段                           |
| `backend/webui/templates/components/log_detail_fragment.html` | 日志详情 HTMX 片段                           |

**修改文件**：`routes/__init__.py`（注册 4 个路由）、`sidebar.html`（4 个导航链接）、`deps.py`（新增 `get_user_preferences` 依赖）

**关联 commit**：`2c378a3`、`997e1db`（2026-03-27）

### P2 — 配置管理（超级管理员）✅ 已完成

**目标**：审查策略和标签规则的可视化编辑

**已完成功能**：

1. ✅ 审查策略页面：展示和编辑 `strategies.yaml` 中的策略配置（strategies / file_filters / batch / context_enhancement / review_policy 五个 tab）
2. ✅ 标签配置页面：展示和编辑标签定义及推荐设置（`config/labels.yaml`）
3. ✅ YAML 热更新机制：修改后自动调用 `reload_strategy_config()` / `reload_label_config()` 同步到内存
4. ✅ 原子写入 + 异步锁：防止并发写竞态，round-trip YAML 验证

**已实现文件**：

| 文件                                             | 说明                                                                    |
| ------------------------------------------------ | ----------------------------------------------------------------------- |
| `backend/webui/routes/config.py`                 | 配置管理路由（6 个端点：策略页/保存策略、标签页/保存标签/保存推荐设置） |
| `backend/webui/templates/config_strategies.html` | 审查策略配置页（5 个 tab 切换）                                         |
| `backend/webui/templates/config_labels.html`     | 标签配置页（标签定义 + 推荐设置）                                       |

**权限**：仅 `super_admin` 可访问（使用 `require_super_admin` 依赖），侧边栏链接仅 super_admin 可见

**关联 commit**：`b37a525`（2026-03-28）、`4b50198`（2026-03-28）

### P3 — 文件级审查 + 队列监控 ✅ 已完成

**目标**：补全 PR 文件级审查页面，新增审查队列只读监控

**已完成功能**：

1. ✅ PR 文件级审查页：左右分栏布局，按 `file_path` 分组 `ReviewComment`，severity 彩色圆点统计，点击文件 HTMX 加载评论面板，总体评论独立区域
2. ✅ 审查队列监控页：基于 `PRReview` 表的只读监控，统计卡片（待处理/处理中/已完成/失败/平均耗时），多条件过滤（搜索/仓库/状态/日期），分页表格

**已实现文件**：

| 文件                                                                | 说明                                         |
| ------------------------------------------------------------------- | -------------------------------------------- |
| `backend/webui/routes/queue.py`                                     | 队列监控路由（3 个端点：页面/统计卡片/列表） |
| `backend/webui/templates/pr_files.html`                             | 文件审查容器页（左右分栏）                   |
| `backend/webui/templates/queue.html`                                | 队列监控容器页                               |
| `backend/webui/templates/components/pr_file_list_fragment.html`     | 文件列表 HTMX 片段（severity 圆点）          |
| `backend/webui/templates/components/pr_file_comments_fragment.html` | 文件评论 HTMX 片段                           |
| `backend/webui/templates/components/queue_stats_cards.html`         | 统计卡片组件                                 |
| `backend/webui/templates/components/queue_list_fragment.html`       | 队列列表 HTMX 片段（过滤 + 表格 + 分页）     |

**修改文件**：`routes/pr.py`（+3 文件审查路由）、`routes/__init__.py`（注册 queue）、`pr_detail.html`（入口按钮）、`sidebar.html`（审查队列链接，admin 可见）

### P4 — 仪表盘图表 + CSV 导出 ✅ 已完成

**目标**：数据可视化增强和导出功能

**已完成功能**：

1. ✅ 仪表盘图表：审查趋势折线图（30 天）、审查决策环形图、仓库审查量 Top 10 横向柱状图（Chart.js 4.x CDN）
2. ✅ CSV 导出：PR 列表页支持按当前筛选条件导出 CSV，UTF-8 BOM 兼容 Excel，上限 1000 条

**已实现文件**：

| 文件 | 说明 |
| ---- | ---- |
| `backend/webui/routes/dashboard.py` | 新增图表数据 API（`/api/webui/chart-data`） |
| `backend/webui/templates/components/dashboard_charts.html` | 图表容器组件（3 个 canvas） |
| `backend/webui/templates/components/dashboard_charts.js` | Chart.js 渲染脚本（暗色模式适配） |

**修改文件**：`base.html`（Chart.js CDN）、`dashboard.html`（引入图表组件）、`routes/pr.py`（CSV 导出端点）、`pr_filters.html`（导出按钮）、`pr_list.html`（exportCSV JS）

### P5 — 管理员操作日志 + 全局配置 + 关于页面 ✅ 已完成

**目标**：补全所有剩余原始设计项

**已完成功能**：

1. ✅ 管理员操作日志：新建 `admin_action_logs` 表，记录用户管理/仓库管理/配置变更等管理员操作，操作日志页面支持按操作类型/管理员/日期范围过滤和分页
2. ✅ 全局配置页：展示和编辑 `app_config` 表中的非敏感配置项（最大并发审查数、审查超时时间、自动审查开关），仅超级管理员可访问
3. ✅ 关于页面：显示应用版本号（从代码 `APP_VERSION` 读取）、运行环境信息

**已实现文件**：

| 文件 | 说明 |
| ---- | ---- |
| `backend/models/admin_action_log.py` | AdminActionLog 模型（admin_id/action/target_type/target_id/detail/created_at） |
| `backend/webui/helpers/admin_log.py` | log_admin_action 辅助函数 |
| `backend/webui/routes/action_logs.py` | 操作日志路由（页面/列表 API，admin 可访问） |
| `backend/webui/templates/action_logs.html` | 操作日志容器页 |
| `backend/webui/templates/components/action_log_list_fragment.html` | 操作日志 HTMX 片段 |
| `backend/webui/templates/config_general.html` | 全局配置页（super_admin 可访问） |
| `backend/webui/templates/about.html` | 关于页面 |

**修改文件**：`routes/config.py`（+全局配置 GET/POST 端点）、`routes/settings.py`（+关于页路由）、`routes/users.py`（+操作日志注入）、`routes/repos.py`（+操作日志注入）、`routes/__init__.py`（注册 action_logs）、`sidebar.html`（操作日志/全局配置/关于链接 + 权限控制）

**关联 commit**：`a4203ec`（2026-03-29）

### P6 — 体验增强（Toast 通知 + 确认弹窗 + Loading 状态）✅ 已完成

**目标**：零新功能，全面提升交互体验

**已完成功能**：

1. ✅ Toast 通知系统：Alpine.js 组件，4 种类型（success/error/warning/info），自动消失，暗色模式适配，滑入滑出动画
2. ✅ 确认弹窗组件：自定义 modal 替代浏览器 `confirm()`，支持警告文字、ESC/遮罩关闭、加载状态
3. ✅ data-confirm 机制：8 处敏感操作已添加（仓库删除/切换、用户禁用/角色/配额、配置保存、登出）
4. ✅ 按钮 Loading 状态：所有 POST 表单提交时按钮禁用 + spinner + "处理中..."，防重复提交，bfcache 回退恢复
5. ✅ HTMX 错误处理：全局拦截 `htmx:responseError`，401/403/422/500 各有专属错误 toast
6. ✅ toast_redirect 辅助函数：5 个路由文件 32 处 `RedirectResponse` 迁移为带具体消息的 toast 通知
7. ✅ 旧 Flash 消息清理：7 个模板 17 处 `{% if request.query_params.get('saved') %}` 块已删除

**已实现文件**：

| 文件 | 说明 |
| ---- | ---- |
| `backend/webui/deps.py` | 新增 `toast_redirect()` 辅助函数 |

**修改文件**：`base.html`（Toast 容器 + 确认弹窗 + Alpine 组件 + 事件监听 + CSS）、`deps.py`、`routes/repos.py`、`routes/users.py`、`routes/config.py`、`routes/settings.py`、`routes/auth.py`、`repo_list_fragment.html`、`user_detail.html`、`navbar.html`、`repos.html`、`users.html`、`config_strategies.html`、`config_labels.html`、`config_general.html`、`settings.html`

**关联 commit**：`df2ba5a`（2026-03-29）

---

文档创建时间：2024-03-27
最后更新：2026-03-29（P0 + P1 + P2 + P3 + P4 + P5 + P6 全部完成）
版本：8.0
作者：Sakura AI Reviewer Team
