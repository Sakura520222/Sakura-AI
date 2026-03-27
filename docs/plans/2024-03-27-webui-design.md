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
/                        - 仪表板（总览统计）                   [P0]
├── /pr                  - PR 审查管理
│   ├── /                - PR 列表（搜索、过滤、分页）          [P0]
│   ├── /:id             - PR 详情（审查结果、评论）            [P0]
│   └── /:id/files       - 文件级审查（只读展示）              [P0]
├── /config              - 配置管理（仅超级管理员）
│   ├── /rules           - 审查策略（读写 strategies.yaml）     [P2]
│   ├── /labels          - 标签配置                             [P2]
│   └── /general         - 全局配置                             [P2]
├── /users               - 用户和权限管理
│   ├── /                - 用户列表                             [P1]
│   ├── /:id             - 用户详情（角色、配额）               [P1]
│   └── /roles           - 角色权限管理                         [P1]
├── /repos               - 仓库管理
│   ├── /                - 仓库列表（repo_subscriptions）       [P1]
│   └── /add             - 添加仓库到白名单                     [P1]
├── /logs                - 审查日志
│   ├── /reviews         - 审查日志（pr_reviews）               [P1]
│   └── /actions         - 操作日志                             [P1]
└── /settings            - 系统设置
    ├── /profile         - 个人设置                             [P1]
    └── /about           - 关于                                 [P1]
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

### P0 — 核心功能（仪表盘 + PR 管理）

**目标**：WebUI 基础框架 + PR 审查可视化

1. 搭建 WebUI 项目结构：Jinja2 模板目录、Tailwind CSS 引入、HTMX 配置
2. 实现 GitHub OAuth 认证流程（对接 `telegram_users` 表）
3. JWT 认证中间件 + CSRF 保护
4. 基础布局模板：导航栏、侧边栏、页脚
5. 仪表盘页面：审查统计（总数/通过/拒绝/评分分布）、最近审查列表
6. PR 列表页面：搜索、按仓库/状态/决策过滤、分页
7. PR 详情页面：审查摘要、评分、评论列表

**关键文件**：

- `backend/webui/` — WebUI 路由和模板根目录
- `backend/webui/templates/` — Jinja2 模板
- `backend/webui/auth.py` — OAuth + JWT 认证
- `backend/webui/deps.py` — 权限依赖注入

### P1 — 管理功能（用户 + 仓库 + 日志 + 设置）

**目标**：完整的管理后台

1. 用户管理：列表、详情、角色变更、配额管理、启用/禁用
2. 仓库管理：白名单列表、添加/移除仓库
3. 审查日志：按时间/仓库/状态筛选
4. 个人设置：主题切换、语言偏好

**关键文件**：

- `backend/webui/routes/users.py` — 用户管理路由
- `backend/webui/routes/repos.py` — 仓库管理路由
- `backend/webui/routes/logs.py` — 日志路由

### P2 — 配置管理（超级管理员）

**目标**：审查策略和标签规则的可视化编辑

1. 审查策略页面：展示和编辑 `strategies.yaml` 中的策略配置
2. 标签配置页面：展示和编辑标签推荐相关配置
3. YAML 热更新机制：修改后自动同步到内存中的 `StrategyConfig`

**关键文件**：

- `backend/webui/routes/config.py` — 配置管理路由
- `backend/services/config_service.py` — 配置读写 + 热更新服务

---

文档创建时间：2024-03-27
版本：2.0（基于可行性审查修订）
作者：Sakura AI Reviewer Team
