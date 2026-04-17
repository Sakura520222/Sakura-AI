# Sakura AI Reviewer API v1 参考文档

> 面向 Android 开发者的完整 API 参考。所有端点前缀为 `/api/v1`。

---

## 1. 基础信息

### Base URL

```
https://pr-bot.firefly520.top/api/v1
```

### 认证方式

采用 **双模认证**，优先 Bearer Token，回退 Cookie：

| 方式 | Header / Cookie |
|------|-----------------|
| Bearer Token（推荐，移动端） | `Authorization: Bearer <jwt_token>` |
| Cookie（WebUI 兼容） | `Cookie: webui_token=<jwt_token>` |

JWT Token 通过 OAuth 登录流程获取，有效期 24 小时（86400 秒）。

### 认证级别

| 级别 | 要求角色 | 说明 |
|------|---------|------|
| **免认证** | 无 | Setup Wizard 相关端点 |
| **auth** | 任意已登录用户 | `user` / `admin` / `super_admin` |
| **admin** | `admin` 或 `super_admin` | 管理员操作 |
| **super_admin** | `super_admin` | 超级管理员操作 |

### 统一响应格式

#### 成功响应

```json
{
  "success": true,
  "message": "ok",
  "data": { ... }
}
```

#### 分页响应

```json
{
  "success": true,
  "message": "ok",
  "data": {
    "items": [ ... ],
    "total": 100,
    "page": 1,
    "total_pages": 5,
    "per_page": 20
  }
}
```

分页参数说明：

| 参数 | 类型 | 默认值 | 范围 | 说明 |
|------|------|--------|------|------|
| `page` | int | 1 | >= 1 | 页码 |
| `per_page` | int | 20 | 1-100 | 每页数量 |

#### 错误响应

```json
{
  "success": false,
  "error": "错误描述",
  "detail": "详细信息（可选）",
  "code": "错误代码（可选）"
}
```

### 错误码说明

| HTTP 状态码 | 说明 |
|-------------|------|
| 400 | 请求参数错误 |
| 401 | 未提供认证凭证 / 凭证无效或已过期 |
| 403 | 权限不足（角色不满足要求） |
| 404 | 资源不存在 |
| 409 | 资源冲突（如正在索引中） |
| 500 | 服务器内部错误 |
| 502 | 上游服务错误（如 GitHub API） |
| 504 | 上游服务超时 |

### 限流说明

| 端点 | 限流策略 |
|------|---------|
| 健康检查 `GET /health` | 10 次/秒 |
| OAuth 回调 `POST /auth/callback` | 5 次/分钟 |
| 登出 `POST /auth/logout` | 10 次/分钟 |
| Setup 连接测试 `POST /setup/test-connection` | 10 次/分钟 |
| Setup 完成 `POST /setup/complete` | 3 次/分钟 |
| 扫描触发 `POST /scans/trigger` | 3 次/分钟 |
| 其他端点 | 无额外限流（按服务器默认策略） |

---

## 2. 端点总览

| # | 方法 | 路径 | 认证级别 | 说明 |
|---|------|------|---------|------|
| 1 | GET | `/health` | 免认证 | 健康检查 |
| 2 | GET | `/auth/github` | 免认证 | 获取 GitHub OAuth 授权 URL |
| 3 | GET | `/auth/github/mobile` | 免认证 | 获取移动端 GitHub OAuth 授权 URL |
| 4 | POST | `/auth/callback` | 免认证 | OAuth 回调换取 Token |
| 5 | POST | `/auth/logout` | auth | 登出 |
| 6 | GET | `/auth/me` | auth | 获取当前用户信息 |
| 7 | GET | `/setup/state` | 免认证 | 获取 Setup 状态 |
| 8 | POST | `/setup/test-connection` | 免认证 | 测试连接配置 |
| 9 | POST | `/setup/save-step` | 免认证 | 保存单步配置 |
| 10 | POST | `/setup/complete` | 免认证 | 完成 Setup 全流程 |
| 11 | GET | `/dashboard/stats` | auth | 仪表盘统计 |
| 12 | GET | `/dashboard/recent-reviews` | auth | 最近审查列表 |
| 13 | GET | `/dashboard/chart-data` | auth | 仪表盘图表数据 |
| 14 | POST | `/dashboard/cache/refresh` | auth | 刷新仪表盘缓存 |
| 15 | GET | `/reviews` | auth | PR 审查列表（分页） |
| 16 | GET | `/reviews/export` | auth | 导出审查 CSV |
| 17 | GET | `/reviews/{review_id}` | auth | 审查详情（含评论） |
| 18 | GET | `/reviews/{review_id}/files` | auth | 审查文件级统计 |
| 19 | GET | `/reviews/{review_id}/comments` | auth | 审查评论列表 |
| 20 | GET | `/reviews/{review_id}/files/{file_path}` | auth | 特定文件评论 |
| 21 | GET | `/issues` | auth | Issue 分析列表（分页） |
| 22 | GET | `/issues/stats` | auth | Issue 统计 |
| 23 | GET | `/issues/{issue_id}` | auth | Issue 分析详情 |
| 24 | POST | `/issues/{issue_id}/reanalyze` | auth | 重新分析 Issue |
| 25 | GET | `/users` | admin | 用户列表（分页） |
| 26 | POST | `/users` | super_admin | 创建用户 |
| 27 | GET | `/users/{user_id}` | admin | 用户详情 |
| 28 | PATCH | `/users/{user_id}/role` | admin | 修改用户角色 |
| 29 | PATCH | `/users/{user_id}/quota` | admin | 修改用户 PR 配额 |
| 30 | PATCH | `/users/{user_id}/issue-quota` | admin | 修改用户 Issue 配额 |
| 31 | POST | `/users/{user_id}/toggle` | admin | 启用/禁用用户 |
| 32 | DELETE | `/users/{user_id}` | super_admin | 删除用户 |
| 33 | PATCH | `/users/{user_id}/info` | super_admin | 修改用户基本信息 |
| 34 | POST | `/users/{user_id}/reset-quota` | super_admin | 重置用户配额使用量 |
| 35 | GET | `/repos` | admin | 仓库列表 |
| 36 | POST | `/repos/{repo_name}/index-docs` | admin | 触发文档索引 |
| 37 | POST | `/repos/{repo_name}/index-code` | admin | 触发代码索引 |
| 38 | POST | `/repos/{repo_name}/index-issues` | admin | 触发 Issues 索引 |
| 39 | POST | `/repos/{repo_name}/scan` | super_admin | 触发仓库扫描 |
| 40 | GET | `/config/general` | super_admin | 获取全局配置 |
| 41 | PATCH | `/config/general` | super_admin | 更新全局配置 |
| 42 | GET | `/config/strategies` | super_admin | 获取策略配置 |
| 43 | PATCH | `/config/strategies/{section}` | super_admin | 更新策略配置 section |
| 44 | GET | `/config/labels` | super_admin | 获取标签配置 |
| 45 | PUT | `/config/labels` | super_admin | 更新标签定义 |
| 46 | PATCH | `/config/labels/recommendation` | super_admin | 更新标签推荐设置 |
| 47 | GET | `/logs/reviews` | auth | 审查日志列表（分页） |
| 48 | GET | `/logs/reviews/{review_id}` | auth | 审查日志详情 |
| 49 | GET | `/logs/actions` | admin | 操作日志列表（分页） |
| 50 | GET | `/logs/actions/{log_id}` | admin | 操作日志详情 |
| 51 | GET | `/queue/stats` | admin | 队列统计 |
| 52 | GET | `/queue/items` | admin | 队列列表（分页） |
| 53 | GET | `/queue/items/{item_id}` | admin | 队列项详情 |
| 54 | POST | `/queue/items/{item_id}/retry` | admin | 重试队列项 |
| 55 | DELETE | `/queue/items/{item_id}` | admin | 删除队列项 |
| 56 | POST | `/queue/purge` | admin | 批量清理队列 |
| 57 | GET | `/scans` | auth | 扫描列表（分页） |
| 58 | GET | `/scans/stats` | auth | 扫描统计 |
| 59 | GET | `/scans/{scan_id}` | auth | 扫描详情 |
| 60 | POST | `/scans/trigger` | super_admin | 手动触发扫描 |
| 61 | POST | `/scans/{scan_id}/retry` | super_admin | 重试扫描 |
| 62 | POST | `/scans/{scan_id}/cancel` | super_admin | 取消扫描 |
| 63 | GET | `/settings` | auth | 获取个人设置 |
| 64 | PATCH | `/settings` | auth | 更新个人设置 |
| 65 | GET | `/settings/about` | auth | 获取系统版本信息 |
| 66 | GET | `/events` | auth | SSE 事件流 |

---

## 3. 详细文档

---

### 3.1 健康检查

#### GET /health

健康检查端点，无需认证。

> **注意**：此端点直接返回 JSON 对象，不使用统一响应包装格式（无 `success`/`message` 字段）。

**认证级别**：免认证

**响应示例**：

```json
{
  "status": "ok",
  "version": "v1"
}
```

---

### 3.2 认证 (Auth)

#### GET /auth/github

获取 GitHub OAuth 授权 URL（Web 端）。返回 JSON 格式的授权链接，不会自动重定向。

**认证级别**：免认证

**响应字段**：

| 字段 | 类型 | 说明 |
|------|------|------|
| `data.authorization_url` | string | GitHub OAuth 授权 URL |
| `data.state` | string | CSRF 防护 state 值 |

**响应示例**：

```json
{
  "success": true,
  "message": "ok",
  "data": {
    "authorization_url": "https://github.com/login/oauth/authorize?client_id=...",
    "state": "abc123..."
  }
}
```

---

#### GET /auth/github/mobile

获取移动端 GitHub OAuth 授权 URL，支持自定义 `redirect_uri`。

**认证级别**：免认证

**查询参数**：

| 参数 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `redirect_uri` | string | 否 | 移动端回调 URI，默认使用系统配置值 |

**响应示例**：

```json
{
  "success": true,
  "message": "ok",
  "data": {
    "authorization_url": "https://github.com/login/oauth/authorize?client_id=...&redirect_uri=...",
    "state": "xyz789..."
  }
}
```

> **Android 接入建议**：使用 `redirect_uri` 参数指定 App 的 Deep Link（如 `myapp://oauth/callback`），用户在浏览器完成 GitHub 授权后会回调到该 URI 并携带 `code` 和 `state` 参数。

---

#### POST /auth/callback

移动端 OAuth 回调：用授权码换取 JWT access_token。

**认证级别**：免认证

**限流**：5 次/分钟

**请求体**：

| 字段 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `code` | string | 是 | GitHub 授权码 |
| `state` | string | 是 | 与授权 URL 中一致的 state 值 |

**请求示例**：

```json
{
  "code": "ghp_abc123",
  "state": "xyz789..."
}
```

**响应字段**：

| 字段 | 类型 | 说明 |
|------|------|------|
| `data.access_token` | string | JWT Token |
| `data.token_type` | string | 固定 `"bearer"` |
| `data.expires_in` | int | 有效期（秒），固定 86400 |
| `data.user.sub` | string | GitHub 用户名 |
| `data.user.role` | string | 用户角色 |
| `data.user.user_id` | int | 系统用户 ID |
| `data.user.github_id` | int \| null | GitHub 用户 ID |
| `data.user.avatar_url` | string \| null | 头像 URL |

**响应示例**：

```json
{
  "success": true,
  "message": "ok",
  "data": {
    "access_token": "eyJhbGciOiJIUzI1NiIs...",
    "token_type": "bearer",
    "expires_in": 86400,
    "user": {
      "sub": "octocat",
      "role": "user",
      "user_id": 1,
      "github_id": 12345,
      "avatar_url": "https://avatars.githubusercontent.com/u/12345?v=4"
    }
  }
}
```

> **Android 接入建议**：获取 `access_token` 后存储到本地安全存储（如 EncryptedSharedPreferences），后续所有请求通过 `Authorization: Bearer <access_token>` 携带。

---

#### POST /auth/logout

登出。API 模式下客户端需自行删除本地存储的 Token。

**认证级别**：auth

**限流**：10 次/分钟

**响应示例**：

```json
{
  "success": true,
  "message": "已退出登录"
}
```

---

#### GET /auth/me

获取当前认证用户信息。

**认证级别**：auth

**响应字段**：

| 字段 | 类型 | 说明 |
|------|------|------|
| `data.sub` | string | GitHub 用户名 |
| `data.role` | string | 用户角色（`user` / `admin` / `super_admin`） |
| `data.user_id` | int | 系统用户 ID |
| `data.github_id` | int \| null | GitHub 用户 ID |
| `data.avatar_url` | string \| null | 头像 URL |

**响应示例**：

```json
{
  "success": true,
  "message": "ok",
  "data": {
    "sub": "octocat",
    "role": "user",
    "user_id": 1,
    "github_id": 12345,
    "avatar_url": "https://avatars.githubusercontent.com/u/12345?v=4"
  }
}
```

---

### 3.3 Setup Wizard

> Setup Wizard 端点仅在系统首次启动（bootstrap 模式）下可用，系统初始化完成后将返回 403 错误。

#### GET /setup/state

获取当前 Setup 状态。

**认证级别**：免认证

**响应字段**：

| 字段 | 类型 | 说明 |
|------|------|------|
| `data.state` | string | `"completed"` 或 `"in_progress"` |
| `data.current_step` | int | 当前步骤索引（-1 表示已完成） |
| `data.missing_fields` | string[] | 缺失的配置字段列表 |
| `data.field_groups` | object | 字段分组定义（仅 bootstrap 模式） |

**响应示例（已完成）**：

```json
{
  "success": true,
  "message": "ok",
  "data": {
    "state": "completed",
    "current_step": -1,
    "missing_fields": []
  }
}
```

**响应示例（进行中）**：

```json
{
  "success": true,
  "message": "ok",
  "data": {
    "state": "in_progress",
    "current_step": 1,
    "missing_fields": ["GITHUB_APP_ID", "GITHUB_PRIVATE_KEY"],
    "field_groups": { "database": [...], "github": [...], "ai": [...], "rag": [...], "admin": [...] }
  }
}
```

---

#### POST /setup/test-connection

测试各类连接配置。

**认证级别**：免认证（需 bootstrap 模式）

**限流**：10 次/分钟

**请求体**：

| 字段 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `type` | string | 是 | 测试类型：`database` / `redis` / `github` / `openai` / `telegram` |
| `database_url` | string | 条件 | 数据库连接 URL（type=database） |
| `redis_url` | string | 条件 | Redis 连接 URL（type=redis） |
| `app_id` | string | 条件 | GitHub App ID（type=github） |
| `private_key` | string | 条件 | GitHub 私钥（type=github） |
| `api_key` | string | 条件 | OpenAI API Key（type=openai） |
| `api_base` | string | 条件 | OpenAI API Base URL（type=openai） |
| `bot_token` | string | 条件 | Telegram Bot Token（type=telegram） |

**请求示例**：

```json
{
  "type": "database",
  "database_url": "mysql+aiomysql://user:pass@localhost:3306/sakura"
}
```

**响应示例**：

```json
{
  "success": true,
  "message": "ok",
  "data": {
    "success": true,
    "message": "数据库连接成功"
  }
}
```

---

#### POST /setup/save-step

保存单步配置。

**认证级别**：免认证（需 bootstrap 模式）

**请求体**：

| 字段 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `values` | object | 是 | 键值对，键为环境变量名，值为配置值 |

**请求示例**：

```json
{
  "values": {
    "DATABASE_URL": "mysql+aiomysql://user:pass@localhost:3306/sakura",
    "REDIS_URL": "redis://localhost:6379/0"
  }
}
```

**响应示例**：

```json
{
  "success": true,
  "message": "已保存 2 项配置",
  "data": {
    "saved_count": 2
  }
}
```

---

#### POST /setup/complete

完成 Setup 全流程，触发服务重启。

**认证级别**：免认证（需 bootstrap 模式）

**限流**：3 次/分钟

**请求体**（所有字段均为可选，按需填写）：

| 字段 | 类型 | 说明 |
|------|------|------|
| `DATABASE_URL` | string | 数据库连接 URL |
| `REDIS_URL` | string | Redis 连接 URL |
| `GITHUB_APP_ID` | string | GitHub App ID |
| `GITHUB_PRIVATE_KEY` | string | GitHub App 私钥 |
| `GITHUB_WEBHOOK_SECRET` | string | GitHub Webhook 密钥 |
| `OPENAI_API_KEY` | string | OpenAI API Key |
| `OPENAI_API_BASE` | string | OpenAI API Base URL |
| `OPENAI_MODEL` | string | OpenAI 模型名 |
| `TELEGRAM_BOT_TOKEN` | string | Telegram Bot Token |
| `APP_DOMAIN` | string | 应用域名 |
| `APP_PORT` | string | 应用端口 |
| `LOG_LEVEL` | string | 日志级别 |
| `ADMIN_GITHUB_USERNAME` | string | 管理员 GitHub 用户名 |
| `ADMIN_TELEGRAM_ID` | string | 管理员 Telegram ID |
| `GITHUB_OAUTH_CLIENT_ID` | string | GitHub OAuth Client ID |
| `GITHUB_OAUTH_CLIENT_SECRET` | string | GitHub OAuth Client Secret |
| `GITHUB_OAUTH_REDIRECT_URI` | string | GitHub OAuth 回调地址 |
| `EMBEDDING_API_KEY` | string | Embedding API Key |
| `EMBEDDING_BASE_URL` | string | Embedding Base URL |
| `EMBEDDING_MODEL` | string | Embedding 模型名 |
| `EMBEDDING_PROVIDER` | string | Embedding 提供商 |
| `EMBEDDING_DIMENSION` | string | Embedding 维度 |
| `RERANK_API_KEY` | string | Rerank API Key |
| `RERANK_BASE_URL` | string | Rerank Base URL |
| `RERANK_MODEL` | string | Rerank 模型名 |
| `RERANK_PROVIDER` | string | Rerank 提供商 |

**响应示例**：

```json
{
  "success": true,
  "message": "系统初始化完成，正在重启...",
  "data": {
    "success": true,
    "message": "系统初始化完成，正在重启..."
  }
}
```

---

### 3.4 仪表盘 (Dashboard)

#### GET /dashboard/stats

获取仪表盘统计数据。按用户权限范围过滤（普通用户仅看到自己仓库的数据）。

**认证级别**：auth

**响应字段**：

| 字段 | 类型 | 说明 |
|------|------|------|
| `data.total` | int | 审查总数 |
| `data.completed` | int | 已完成数 |
| `data.reviewing` | int | 审查中数 |
| `data.pending` | int | 待处理数 |
| `data.failed` | int | 失败数 |
| `data.approved` | int | 已通过数 |
| `data.changes_requested` | int | 需修改数 |
| `data.avg_score` | float | 平均评分 |
| `data.comment_count` | int | 评论总数 |
| `data.total_prompt_tokens` | int | Prompt Token 总消耗 |
| `data.total_completion_tokens` | int | Completion Token 总消耗 |
| `data.total_estimated_cost` | int | 预估总费用 |

**响应示例**：

```json
{
  "success": true,
  "message": "ok",
  "data": {
    "total": 150,
    "completed": 130,
    "reviewing": 3,
    "pending": 5,
    "failed": 12,
    "approved": 95,
    "changes_requested": 35,
    "avg_score": 7.8,
    "comment_count": 420,
    "total_prompt_tokens": 1500000,
    "total_completion_tokens": 800000,
    "total_estimated_cost": 2300
  }
}
```

---

#### GET /dashboard/recent-reviews

获取最近 10 条审查记录。

**认证级别**：auth

**响应字段**（items 数组中每个元素）：

| 字段 | 类型 | 说明 |
|------|------|------|
| `id` | int | 审查 ID |
| `pr_id` | int | PR ID |
| `repo_name` | string | 仓库名 |
| `repo_owner` | string | 仓库所有者 |
| `title` | string | PR 标题 |
| `author` | string | PR 作者 |
| `status` | string | 审查状态 |
| `overall_score` | int \| null | 综合评分 |
| `decision` | string \| null | 审查决策 |
| `strategy` | string \| null | 审查策略 |
| `created_at` | string | 创建时间（ISO 8601） |
| `completed_at` | string \| null | 完成时间（ISO 8601） |

**响应示例**：

```json
{
  "success": true,
  "message": "ok",
  "data": [
    {
      "id": 42,
      "pr_id": 128,
      "repo_name": "my-org/my-repo",
      "repo_owner": "my-org",
      "title": "feat: add user authentication",
      "author": "octocat",
      "status": "completed",
      "overall_score": 8,
      "decision": "approve",
      "strategy": "standard",
      "created_at": "2026-04-17T10:30:00",
      "completed_at": "2026-04-17T10:31:30"
    }
  ]
}
```

---

#### GET /dashboard/chart-data

获取仪表盘图表数据，包含 4 组图表。

**认证级别**：auth

**响应字段**：

| 字段 | 类型 | 说明 |
|------|------|------|
| `data.trend` | object | 审查趋势（最近 30 天） |
| `data.trend.labels` | string[] | 日期标签（MM-DD 格式） |
| `data.trend.completed` | int[] | 每天完成数 |
| `data.trend.failed` | int[] | 每天失败数 |
| `data.decisions` | object | 决策分布 |
| `data.decisions.labels` | string[] | 决策名称（中文） |
| `data.decisions.counts` | int[] | 各决策数量 |
| `data.top_repos` | object | 仓库排行 Top 10 |
| `data.top_repos.labels` | string[] | 仓库名 |
| `data.top_repos.counts` | int[] | 审查数量 |
| `data.tokens` | object | Token 消耗趋势（最近 30 天） |
| `data.tokens.labels` | string[] | 日期标签 |
| `data.tokens.tokens` | int[] | 每天消耗 Token 数 |

**响应示例**：

```json
{
  "success": true,
  "message": "ok",
  "data": {
    "trend": {
      "labels": ["03-18", "03-19", "..."],
      "completed": [5, 3, "..."],
      "failed": [0, 1, "..."]
    },
    "decisions": {
      "labels": ["通过", "需修改"],
      "counts": [95, 35]
    },
    "top_repos": {
      "labels": ["my-org/my-repo", "my-org/other-repo"],
      "counts": [80, 70]
    },
    "tokens": {
      "labels": ["03-18", "03-19", "..."],
      "tokens": [50000, 30000, "..."]
    }
  }
}
```

---

#### POST /dashboard/cache/refresh

手动刷新仪表盘缓存（仅清除当前用户）。

**认证级别**：auth

**响应示例**：

```json
{
  "success": true,
  "message": "缓存已刷新"
}
```

---

### 3.5 PR 审查 (Reviews)

#### GET /reviews

PR 审查列表，支持分页、搜索和过滤。

**认证级别**：auth

**查询参数**：

| 参数 | 类型 | 默认值 | 说明 |
|------|------|--------|------|
| `search` | string | `""` | 搜索关键词（匹配标题、仓库名、作者） |
| `status` | string | `""` | 按状态过滤（`pending` / `reviewing` / `completed` / `failed`） |
| `decision` | string | `""` | 按决策过滤（`approve` / `request_changes` / `comment` / `skip`） |
| `page` | int | 1 | 页码 |
| `per_page` | int | 20 | 每页数量（1-100） |

**响应字段**（items 数组中每个元素）：

| 字段 | 类型 | 说明 |
|------|------|------|
| `id` | int | 审查 ID |
| `pr_id` | int | PR ID |
| `repo_name` | string \| null | 仓库名 |
| `repo_owner` | string \| null | 仓库所有者 |
| `author` | string \| null | PR 作者 |
| `title` | string \| null | PR 标题 |
| `branch` | string \| null | 分支名 |
| `file_count` | int \| null | 文件数 |
| `line_count` | int \| null | 行数 |
| `code_file_count` | int \| null | 代码文件数 |
| `strategy` | string \| null | 审查策略 |
| `status` | string \| null | 状态 |
| `error_message` | string \| null | 错误信息 |
| `review_summary` | string \| null | 审查摘要 |
| `overall_score` | int \| null | 综合评分 |
| `decision` | string \| null | 决策 |
| `decision_reason` | string \| null | 决策理由 |
| `prompt_tokens` | int \| null | Prompt Token 数 |
| `completion_tokens` | int \| null | Completion Token 数 |
| `estimated_cost` | int \| null | 预估费用 |
| `created_at` | string \| null | 创建时间 |
| `updated_at` | string \| null | 更新时间 |
| `completed_at` | string \| null | 完成时间 |
| `comments` | array \| null | 评论列表（列表接口为 null） |

---

#### GET /reviews/export

导出 PR 审查列表为 CSV 文件。

**认证级别**：auth

**查询参数**：

| 参数 | 类型 | 默认值 | 说明 |
|------|------|--------|------|
| `search` | string | `""` | 搜索关键词 |
| `status` | string | `""` | 按状态过滤 |
| `decision` | string | `""` | 按决策过滤 |

**响应**：返回 `text/csv` 文件流，最多 1000 条记录。文件名格式 `pr_reviews_YYYYMMDD.csv`，UTF-8 BOM 编码。

---

#### GET /reviews/{review_id}

审查详情，含评论列表。

**认证级别**：auth

**路径参数**：

| 参数 | 类型 | 说明 |
|------|------|------|
| `review_id` | int | 审查记录 ID |

**响应字段**：

与列表接口相同，额外包含 `comments` 数组：

| 字段 | 类型 | 说明 |
|------|------|------|
| `comments[].id` | int | 评论 ID |
| `comments[].file_path` | string \| null | 文件路径 |
| `comments[].line_number` | int \| null | 行号 |
| `comments[].comment_type` | string \| null | 评论类型 |
| `comments[].severity` | string \| null | 严重程度 |
| `comments[].content` | string \| null | 评论内容 |
| `comments[].created_at` | string \| null | 创建时间 |

**响应示例**：

```json
{
  "success": true,
  "message": "ok",
  "data": {
    "id": 42,
    "pr_id": 128,
    "repo_name": "my-org/my-repo",
    "status": "completed",
    "overall_score": 8,
    "decision": "approve",
    "comments": [
      {
        "id": 101,
        "file_path": "src/main.py",
        "line_number": 42,
        "comment_type": "suggestion",
        "severity": "minor",
        "content": "建议使用 async with 代替 with",
        "created_at": "2026-04-17T10:31:00"
      }
    ]
  }
}
```

---

#### GET /reviews/{review_id}/files

审查文件级统计，按 `file_path` 分组，含严重程度分布。

**认证级别**：auth

**路径参数**：

| 参数 | 类型 | 说明 |
|------|------|------|
| `review_id` | int | 审查记录 ID |

**响应字段**（items 数组中每个元素）：

| 字段 | 类型 | 说明 |
|------|------|------|
| `file_path` | string | 文件路径（`__overall__` 表示总体统计） |
| `severity_counts` | object | 严重程度计数 |
| `severity_counts.critical` | int | 严重问题数 |
| `severity_counts.major` | int | 主要问题数 |
| `severity_counts.minor` | int | 次要问题数 |
| `severity_counts.suggestion` | int | 建议数 |
| `comment_count` | int | 评论总数 |

**响应示例**：

```json
{
  "success": true,
  "message": "ok",
  "data": [
    {
      "file_path": "src/main.py",
      "severity_counts": {
        "critical": 1,
        "major": 2,
        "minor": 3,
        "suggestion": 5
      },
      "comment_count": 11
    }
  ]
}
```

---

#### GET /reviews/{review_id}/comments

审查评论列表，可按 `file_path` 过滤。

**认证级别**：auth

**路径参数**：

| 参数 | 类型 | 说明 |
|------|------|------|
| `review_id` | int | 审查记录 ID |

**查询参数**：

| 参数 | 类型 | 默认值 | 说明 |
|------|------|--------|------|
| `file_path` | string | `""` | 文件路径过滤（`__overall__` 表示总体评论） |

**响应字段**（items 数组中每个元素）：

| 字段 | 类型 | 说明 |
|------|------|------|
| `id` | int | 评论 ID |
| `file_path` | string \| null | 文件路径 |
| `line_number` | int \| null | 行号 |
| `comment_type` | string \| null | 评论类型 |
| `severity` | string \| null | 严重程度 |
| `content` | string \| null | 评论内容 |
| `created_at` | string \| null | 创建时间 |

---

#### GET /reviews/{review_id}/files/{file_path}

获取特定文件的审查评论。

**认证级别**：auth

**路径参数**：

| 参数 | 类型 | 说明 |
|------|------|------|
| `review_id` | int | 审查记录 ID |
| `file_path` | string | 文件路径（URL 编码，支持多级路径） |

**响应字段**：

| 字段 | 类型 | 说明 |
|------|------|------|
| `data.file_path` | string | 文件路径 |
| `data.comment_count` | int | 评论总数 |
| `data.comments[]` | array | 评论列表 |
| `data.comments[].id` | int | 评论 ID |
| `data.comments[].line_number` | int \| null | 行号 |
| `data.comments[].comment_type` | string \| null | 评论类型 |
| `data.comments[].severity` | string \| null | 严重程度 |
| `data.comments[].content` | string \| null | 评论内容 |
| `data.comments[].created_at` | string \| null | 创建时间 |

**响应示例**：

```json
{
  "success": true,
  "message": "ok",
  "data": {
    "file_path": "src/main.py",
    "comment_count": 3,
    "comments": [
      {
        "id": 101,
        "line_number": 42,
        "comment_type": "suggestion",
        "severity": "minor",
        "content": "建议使用 async with",
        "created_at": "2026-04-17T10:31:00"
      }
    ]
  }
}
```

---

### 3.6 Issue 分析

#### GET /issues

Issue 分析列表，支持分页、搜索和多维过滤。

**认证级别**：auth

**查询参数**：

| 参数 | 类型 | 默认值 | 说明 |
|------|------|--------|------|
| `search` | string | `""` | 搜索关键词（匹配标题、仓库名、作者） |
| `repo_name` | string | `""` | 按仓库过滤 |
| `category` | string | `""` | 按分类过滤 |
| `priority` | string | `""` | 按优先级过滤 |
| `status` | string | `""` | 按状态过滤 |
| `page` | int | 1 | 页码 |
| `per_page` | int | 20 | 每页数量（1-100） |

**响应字段**（items 数组中每个元素）：

| 字段 | 类型 | 说明 |
|------|------|------|
| `id` | int | 分析记录 ID |
| `issue_number` | int | Issue 编号 |
| `repo_name` | string \| null | 仓库名 |
| `repo_owner` | string \| null | 仓库所有者 |
| `author` | string \| null | Issue 作者 |
| `title` | string \| null | Issue 标题 |
| `category` | string \| null | 分类 |
| `priority` | string \| null | 优先级 |
| `summary` | string \| null | 摘要 |
| `feasibility` | string \| null | 可行性评估 |
| `suggested_title` | string \| null | 建议标题 |
| `suggested_assignees` | array | 建议指派人列表（JSON 已解析） |
| `suggested_labels` | array | 建议标签列表（JSON 已解析） |
| `suggested_milestone` | string \| null | 建议 Milestone |
| `duplicate_of` | int \| null | 重复 Issue 编号 |
| `related_prs` | array | 关联 PR 列表（JSON 已解析） |
| `analysis_detail` | string \| null | 分析详情 |
| `status` | string \| null | 状态 |
| `error_message` | string \| null | 错误信息 |
| `comment_posted` | int \| null | 是否已发评论 |
| `comment_url` | string \| null | 评论 URL |
| `labels_applied` | int \| null | 是否已打标签 |
| `applied_label_names` | string \| null | 已打标签名 |
| `created_at` | string \| null | 创建时间 |
| `completed_at` | string \| null | 完成时间 |

---

#### GET /issues/stats

Issue 统计数据。

**认证级别**：auth

**响应字段**：

| 字段 | 类型 | 说明 |
|------|------|------|
| `data.total` | int | 总数 |
| `data.by_category` | object | 按分类统计（键为分类名，值为数量） |
| `data.by_priority` | object | 按优先级统计（键为优先级名，值为数量） |
| `data.by_status` | object | 按状态统计（键为状态名，值为数量） |

**响应示例**：

```json
{
  "success": true,
  "message": "ok",
  "data": {
    "total": 50,
    "by_category": {"bug": 20, "feature": 15, "question": 10, "other": 5},
    "by_priority": {"high": 10, "medium": 25, "low": 15},
    "by_status": {"completed": 40, "pending": 5, "failed": 5}
  }
}
```

---

#### GET /issues/{issue_id}

Issue 分析详情。

**认证级别**：auth

**路径参数**：

| 参数 | 类型 | 说明 |
|------|------|------|
| `issue_id` | int | 分析记录 ID |

**响应字段**：与列表接口每个元素的字段相同，`suggested_labels`、`suggested_assignees`、`related_prs` 为已解析的 JSON 数组。

---

#### POST /issues/{issue_id}/reanalyze

重新分析 Issue。将提交到后台队列异步执行。

**认证级别**：auth

**路径参数**：

| 参数 | 类型 | 说明 |
|------|------|------|
| `issue_id` | int | 分析记录 ID |

**响应字段**：

| 字段 | 类型 | 说明 |
|------|------|------|
| `data.task_id` | string | 后台任务 ID |

**响应示例**：

```json
{
  "success": true,
  "message": "ok",
  "data": {
    "task_id": "task_abc123"
  }
}
```

---

### 3.7 用户管理 (Users)

#### GET /users

用户列表（管理员），支持分页和搜索。

**认证级别**：admin

**查询参数**：

| 参数 | 类型 | 默认值 | 说明 |
|------|------|--------|------|
| `search` | string | `""` | 搜索关键词（匹配 GitHub 用户名、Telegram ID） |
| `role` | string | `""` | 按角色过滤（`user` / `admin` / `super_admin`） |
| `page` | int | 1 | 页码 |
| `per_page` | int | 20 | 每页数量（1-100） |

**响应字段**（items 数组中每个元素）：

| 字段 | 类型 | 说明 |
|------|------|------|
| `id` | int | 用户 ID |
| `telegram_id` | int | Telegram ID |
| `github_username` | string \| null | GitHub 用户名 |
| `role` | string | 角色 |
| `daily_quota` | int \| null | 每日 PR 配额 |
| `weekly_quota` | int \| null | 每周 PR 配额 |
| `monthly_quota` | int \| null | 每月 PR 配额 |
| `daily_used` | int \| null | 每日已用 |
| `weekly_used` | int \| null | 每周已用 |
| `monthly_used` | int \| null | 每月已用 |
| `issue_daily_quota` | int \| null | 每日 Issue 配额 |
| `issue_weekly_quota` | int \| null | 每周 Issue 配额 |
| `issue_monthly_quota` | int \| null | 每月 Issue 配额 |
| `issue_daily_used` | int \| null | 每日 Issue 已用 |
| `issue_weekly_used` | int \| null | 每周 Issue 已用 |
| `issue_monthly_used` | int \| null | 每月 Issue 已用 |
| `is_active` | bool \| null | 是否启用 |
| `created_at` | string \| null | 创建时间 |
| `updated_at` | string \| null | 更新时间 |

---

#### POST /users

创建用户。

**认证级别**：super_admin

**请求体**：

| 字段 | 类型 | 必填 | 默认值 | 说明 |
|------|------|------|--------|------|
| `telegram_id` | int | 是 | - | Telegram ID（正整数） |
| `github_username` | string | 是 | - | GitHub 用户名（非空） |
| `role` | string | 否 | `"user"` | 角色（`user` / `admin` / `super_admin`） |
| `daily_quota` | int | 否 | 10 | 每日 PR 配额 |
| `weekly_quota` | int | 否 | 50 | 每周 PR 配额 |
| `monthly_quota` | int | 否 | 200 | 每月 PR 配额 |
| `issue_daily_quota` | int | 否 | 20 | 每日 Issue 配额 |
| `issue_weekly_quota` | int | 否 | 80 | 每周 Issue 配额 |
| `issue_monthly_quota` | int | 否 | 300 | 每月 Issue 配额 |

**响应示例**：

```json
{
  "success": true,
  "message": "用户 octocat 已成功添加",
  "data": {
    "id": 5,
    "telegram_id": 123456789,
    "github_username": "octocat",
    "role": "user",
    "daily_quota": 10,
    "weekly_quota": 50,
    "monthly_quota": 200,
    "issue_daily_quota": 20,
    "issue_weekly_quota": 80,
    "issue_monthly_quota": 300,
    "daily_used": 0,
    "weekly_used": 0,
    "monthly_used": 0,
    "issue_daily_used": 0,
    "issue_weekly_used": 0,
    "issue_monthly_used": 0,
    "is_active": true,
    "created_at": "2026-04-17T10:00:00",
    "updated_at": "2026-04-17T10:00:00"
  }
}
```

---

#### GET /users/{user_id}

用户详情，含配额使用历史（最近 20 条）。

**认证级别**：admin

**路径参数**：

| 参数 | 类型 | 说明 |
|------|------|------|
| `user_id` | int | 用户 ID |

**响应字段**：包含用户基本字段（同列表） + 额外 `usage_logs` 数组：

| 字段 | 类型 | 说明 |
|------|------|------|
| `usage_logs[].id` | int | 日志 ID |
| `usage_logs[].quota_type` | string | 配额类型 |
| `usage_logs[].used_count` | int | 使用次数 |
| `usage_logs[].created_at` | string \| null | 创建时间 |

---

#### PATCH /users/{user_id}/role

修改用户角色。

**认证级别**：admin（修改 admin/super_admin 角色或设置为 super_admin 需要 super_admin 权限）

**路径参数**：

| 参数 | 类型 | 说明 |
|------|------|------|
| `user_id` | int | 用户 ID |

**请求体**：

| 字段 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `role` | string | 是 | 新角色（`user` / `admin` / `super_admin`） |

**响应示例**：

```json
{
  "success": true,
  "message": "用户角色已更改为 admin"
}
```

---

#### PATCH /users/{user_id}/quota

修改用户 PR 配额。

**认证级别**：admin

**路径参数**：

| 参数 | 类型 | 说明 |
|------|------|------|
| `user_id` | int | 用户 ID |

**请求体**：

| 字段 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `daily_quota` | int | 是 | 每日配额（>= 0） |
| `weekly_quota` | int | 是 | 每周配额（>= 0） |
| `monthly_quota` | int | 是 | 每月配额（>= 0） |

**响应示例**：

```json
{
  "success": true,
  "message": "用户配额已更新"
}
```

---

#### PATCH /users/{user_id}/issue-quota

修改用户 Issue 配额。

**认证级别**：admin

**路径参数**：

| 参数 | 类型 | 说明 |
|------|------|------|
| `user_id` | int | 用户 ID |

**请求体**：

| 字段 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `issue_daily_quota` | int | 是 | 每日配额（>= 0） |
| `issue_weekly_quota` | int | 是 | 每周配额（>= 0） |
| `issue_monthly_quota` | int | 是 | 每月配额（>= 0） |

**响应示例**：

```json
{
  "success": true,
  "message": "Issue 配额已更新"
}
```

---

#### POST /users/{user_id}/toggle

启用/禁用用户（切换 `is_active` 状态）。

**认证级别**：admin（操作 admin/super_admin 需要 super_admin 权限）

**路径参数**：

| 参数 | 类型 | 说明 |
|------|------|------|
| `user_id` | int | 用户 ID |

**响应示例**：

```json
{
  "success": true,
  "message": "用户 octocat 已禁用"
}
```

---

#### DELETE /users/{user_id}

删除用户。

**认证级别**：super_admin

**路径参数**：

| 参数 | 类型 | 说明 |
|------|------|------|
| `user_id` | int | 用户 ID |

**响应示例**：

```json
{
  "success": true,
  "message": "用户 octocat 已删除"
}
```

---

#### PATCH /users/{user_id}/info

修改用户基本信息（Telegram ID 和 GitHub 用户名）。

**认证级别**：super_admin

**路径参数**：

| 参数 | 类型 | 说明 |
|------|------|------|
| `user_id` | int | 用户 ID |

**请求体**：

| 字段 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `telegram_id` | int | 是 | 新 Telegram ID（正整数） |
| `github_username` | string | 是 | 新 GitHub 用户名（非空） |

**响应示例**：

```json
{
  "success": true,
  "message": "用户基本信息已更新"
}
```

---

#### POST /users/{user_id}/reset-quota

重置用户所有配额使用量为 0，并重置所有定时器。

**认证级别**：super_admin

**路径参数**：

| 参数 | 类型 | 说明 |
|------|------|------|
| `user_id` | int | 用户 ID |

**响应示例**：

```json
{
  "success": true,
  "message": "用户 octocat 的配额使用量已重置"
}
```

---

### 3.8 仓库管理 (Repos)

#### GET /repos

仓库列表（含统计数据，如审查数量、索引状态等）。

**认证级别**：admin

**响应字段**：返回安装信息列表，具体结构由 GitHub App Installation 数据决定。

---

#### POST /repos/{repo_name}/index-docs

触发文档索引（RAG）。

**认证级别**：admin

**路径参数**：

| 参数 | 类型 | 说明 |
|------|------|------|
| `repo_name` | string | 仓库名（支持多级路径，如 `owner/repo`） |

**前置条件**：RAG 功能需在设置中启用。

**响应示例**：

```json
{
  "success": true,
  "message": "文档索引已启动: my-org/my-repo"
}
```

---

#### POST /repos/{repo_name}/index-code

触发代码索引。

**认证级别**：admin

**路径参数**：

| 参数 | 类型 | 说明 |
|------|------|------|
| `repo_name` | string | 仓库名（支持多级路径） |

**前置条件**：代码索引功能需在设置中启用。

**响应示例**：

```json
{
  "success": true,
  "message": "代码索引已启动: my-org/my-repo"
}
```

---

#### POST /repos/{repo_name}/index-issues

触发 Issues 索引。

**认证级别**：admin

**路径参数**：

| 参数 | 类型 | 说明 |
|------|------|------|
| `repo_name` | string | 仓库名（支持多级路径） |

**前置条件**：语义 Issue 关联功能需在设置中启用。

**响应示例**：

```json
{
  "success": true,
  "message": "Issues 索引已启动: my-org/my-repo"
}
```

---

#### POST /repos/{repo_name}/scan

触发仓库安全扫描。

**认证级别**：super_admin

**路径参数**：

| 参数 | 类型 | 说明 |
|------|------|------|
| `repo_name` | string | 仓库名（支持多级路径） |

**响应示例**：

```json
{
  "success": true,
  "message": "仓库扫描已启动: my-org/my-repo"
}
```

---

### 3.9 配置管理 (Config)

> 所有配置端点需要 super_admin 权限。敏感字段（含 `secret`、`key`、`token`、`password`、`credential`）会被脱敏为 `****` 格式。

#### GET /config/general

获取全局配置。

**认证级别**：super_admin

**响应字段**：

| 字段 | 类型 | 说明 |
|------|------|------|
| `data.configs` | object | 键值对（键为配置名，值为配置值，敏感值已脱敏） |

**响应示例**：

```json
{
  "success": true,
  "message": "ok",
  "data": {
    "configs": {
      "OPENAI_API_KEY": "sk-1****abcd",
      "OPENAI_MODEL": "gpt-4o",
      "APP_DOMAIN": "example.com"
    }
  }
}
```

---

#### PATCH /config/general

更新全局配置。

**认证级别**：super_admin

**请求体**：

| 字段 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `configs` | object | 是 | 键值对，键为配置名，值为新值 |

**请求示例**：

```json
{
  "configs": {
    "OPENAI_MODEL": "gpt-4o-mini",
    "LOG_LEVEL": "DEBUG"
  }
}
```

**响应示例**：

```json
{
  "success": true,
  "message": "配置已更新"
}
```

---

#### GET /config/strategies

获取策略配置。

**认证级别**：super_admin

**响应字段**：

| 字段 | 类型 | 说明 |
|------|------|------|
| `data.strategies` | object | 策略配置（来自 `config/strategies.yaml`） |

---

#### PATCH /config/strategies/{section}

更新策略配置的某个 section。

**认证级别**：super_admin

**路径参数**：

| 参数 | 类型 | 说明 |
|------|------|------|
| `section` | string | 策略 section 名称（如 `standard`、`strict`） |

**请求体**：

| 字段 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `data` | object | 是 | 要更新的键值对 |

**请求示例**：

```json
{
  "data": {
    "max_files": 30,
    "focus_areas": ["security", "performance"]
  }
}
```

**响应示例**：

```json
{
  "success": true,
  "message": "策略配置 standard 已更新"
}
```

---

#### GET /config/labels

获取标签配置。

**认证级别**：super_admin

**响应字段**：

| 字段 | 类型 | 说明 |
|------|------|------|
| `data.labels` | array | 标签定义列表 |
| `data.recommendation` | object | 标签推荐设置 |

---

#### PUT /config/labels

更新标签定义（全量覆盖）。

**认证级别**：super_admin

**请求体**：

| 字段 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `labels` | array | 是 | 新的标签定义列表（非空） |

**响应示例**：

```json
{
  "success": true,
  "message": "标签定义已更新"
}
```

---

#### PATCH /config/labels/recommendation

更新标签推荐设置。

**认证级别**：super_admin

**请求体**：

| 字段 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `recommendation` | object | 是 | 推荐设置（非空） |

**响应示例**：

```json
{
  "success": true,
  "message": "标签推荐设置已更新"
}
```

---

### 3.10 日志查询 (Logs)

#### GET /logs/reviews

审查日志列表，支持分页、搜索和多维过滤。

**认证级别**：auth

**查询参数**：

| 参数 | 类型 | 默认值 | 说明 |
|------|------|--------|------|
| `search` | string | `""` | 搜索关键词（匹配标题、仓库名、作者） |
| `repo` | string | `""` | 按仓库过滤 |
| `status` | string | `""` | 按状态过滤 |
| `date_from` | string | `""` | 开始日期（YYYY-MM-DD） |
| `date_to` | string | `""` | 结束日期（YYYY-MM-DD） |
| `page` | int | 1 | 页码 |
| `per_page` | int | 20 | 每页数量（1-100） |

**响应字段**（items 数组中每个元素）：

| 字段 | 类型 | 说明 |
|------|------|------|
| `id` | int | 审查 ID |
| `pr_id` | int | PR ID |
| `repo_name` | string | 仓库名 |
| `repo_owner` | string | 仓库所有者 |
| `title` | string | PR 标题 |
| `author` | string | 作者 |
| `status` | string | 状态 |
| `decision` | string | 决策 |
| `overall_score` | int | 评分 |
| `strategy` | string | 策略 |
| `created_at` | string | 创建时间 |
| `completed_at` | string | 完成时间 |

---

#### GET /logs/reviews/{review_id}

审查日志详情。

**认证级别**：auth

**路径参数**：

| 参数 | 类型 | 说明 |
|------|------|------|
| `review_id` | int | 审查 ID |

**响应字段**：

| 字段 | 类型 | 说明 |
|------|------|------|
| `data.id` | int | 审查 ID |
| `data.pr_id` | int | PR ID |
| `data.repo_name` | string | 仓库名 |
| `data.repo_owner` | string | 仓库所有者 |
| `data.title` | string | PR 标题 |
| `data.author` | string | 作者 |
| `data.status` | string | 状态 |
| `data.decision` | string | 决策 |
| `data.overall_score` | int | 评分 |
| `data.review_summary` | string | 审查摘要 |
| `data.error_message` | string | 错误信息 |
| `data.strategy` | string | 策略 |
| `data.prompt_tokens` | int | Prompt Token 数 |
| `data.completion_tokens` | int | Completion Token 数 |
| `data.created_at` | string | 创建时间 |
| `data.completed_at` | string | 完成时间 |
| `data.comments[]` | array | 评论列表 |
| `data.comments[].id` | int | 评论 ID |
| `data.comments[].file_path` | string | 文件路径 |
| `data.comments[].line_number` | int | 行号 |
| `data.comments[].severity` | string | 严重程度 |
| `data.comments[].content` | string | 内容 |

---

#### GET /logs/actions

操作日志列表（管理员）。

**认证级别**：admin

**查询参数**：

| 参数 | 类型 | 默认值 | 说明 |
|------|------|--------|------|
| `action` | string | `""` | 操作类型过滤 |
| `start_date` | string | `""` | 开始日期 |
| `end_date` | string | `""` | 结束日期 |
| `page` | int | 1 | 页码 |
| `per_page` | int | 20 | 每页数量（1-100） |

**响应字段**（items 数组中每个元素）：

| 字段 | 类型 | 说明 |
|------|------|------|
| `id` | int | 日志 ID |
| `admin_id` | int | 操作人 ID |
| `action` | string | 操作类型 |
| `target_type` | string | 目标类型 |
| `target_id` | string | 目标 ID |
| `detail` | string | 详细信息 |
| `created_at` | string | 创建时间 |

---

#### GET /logs/actions/{log_id}

操作日志详情。

**认证级别**：admin

**路径参数**：

| 参数 | 类型 | 说明 |
|------|------|------|
| `log_id` | int | 日志 ID |

**响应字段**：

| 字段 | 类型 | 说明 |
|------|------|------|
| `data.id` | int | 日志 ID |
| `data.admin_id` | int | 操作人 ID |
| `data.admin_username` | string \| null | 操作人 GitHub 用户名 |
| `data.action` | string | 操作类型 |
| `data.target_type` | string | 目标类型 |
| `data.target_id` | string | 目标 ID |
| `data.detail` | string | 详细信息 |
| `data.created_at` | string | 创建时间 |

---

### 3.11 队列监控 (Queue)

#### GET /queue/stats

队列统计数据。

**认证级别**：admin

**响应字段**：

| 字段 | 类型 | 说明 |
|------|------|------|
| `data.pending` | int | 待处理数 |
| `data.processing` | int | 处理中数 |
| `data.completed` | int | 已完成数 |
| `data.failed` | int | 失败数 |
| `data.total` | int | 总数 |

**响应示例**：

```json
{
  "success": true,
  "message": "ok",
  "data": {
    "pending": 5,
    "processing": 2,
    "completed": 150,
    "failed": 3,
    "total": 160
  }
}
```

---

#### GET /queue/items

队列列表，支持分页和过滤。

**认证级别**：admin

**查询参数**：

| 参数 | 类型 | 默认值 | 说明 |
|------|------|--------|------|
| `search` | string | `""` | 搜索关键词（匹配仓库名） |
| `repo` | string | `""` | 仓库名过滤 |
| `status` | string | `""` | 状态过滤 |
| `page` | int | 1 | 页码 |
| `per_page` | int | 20 | 每页数量（1-100） |

**响应字段**（items 数组中每个元素）：

| 字段 | 类型 | 说明 |
|------|------|------|
| `id` | int | 队列项 ID |
| `pr_id` | int | PR ID |
| `repo_name` | string | 仓库名 |
| `action` | string | 操作类型 |
| `priority` | int | 优先级 |
| `status` | string | 状态 |
| `retry_count` | int | 重试次数 |
| `max_retries` | int | 最大重试次数 |
| `error_message` | string | 错误信息 |
| `created_at` | string | 创建时间 |
| `updated_at` | string | 更新时间 |

---

#### GET /queue/items/{item_id}

队列项详情。

**认证级别**：admin

**路径参数**：

| 参数 | 类型 | 说明 |
|------|------|------|
| `item_id` | int | 队列项 ID |

**响应字段**：与列表接口每个元素的字段相同。

---

#### POST /queue/items/{item_id}/retry

重试失败的队列项。

**认证级别**：admin

**路径参数**：

| 参数 | 类型 | 说明 |
|------|------|------|
| `item_id` | int | 队列项 ID |

**前置条件**：队列项状态必须为 `failed`。

**响应示例**：

```json
{
  "success": true,
  "message": "队列项已重新加入队列"
}
```

---

#### DELETE /queue/items/{item_id}

删除队列项。

**认证级别**：admin

**路径参数**：

| 参数 | 类型 | 说明 |
|------|------|------|
| `item_id` | int | 队列项 ID |

**响应示例**：

```json
{
  "success": true,
  "message": "队列项已删除"
}
```

---

#### POST /queue/purge

批量清理已完成或失败的队列项。

**认证级别**：admin

**查询参数**：

| 参数 | 类型 | 默认值 | 说明 |
|------|------|--------|------|
| `status` | string | `"completed"` | 清理状态（`completed` / `failed`） |

**响应字段**：

| 字段 | 类型 | 说明 |
|------|------|------|
| `data.deleted` | int | 已删除数量 |

**响应示例**：

```json
{
  "success": true,
  "message": "已清理 50 个已完成的队列项",
  "data": {
    "deleted": 50
  }
}
```

---

### 3.12 扫描管理 (Scans)

#### GET /scans

扫描列表，支持分页和过滤。

**认证级别**：auth

**查询参数**：

| 参数 | 类型 | 默认值 | 说明 |
|------|------|--------|------|
| `search` | string | `""` | 搜索关键词（匹配仓库名） |
| `repo_name` | string | `""` | 按仓库过滤 |
| `status` | string | `""` | 按状态过滤 |
| `page` | int | 1 | 页码 |
| `per_page` | int | 20 | 每页数量（1-100） |

**响应字段**（items 数组中每个元素）：

| 字段 | 类型 | 说明 |
|------|------|------|
| `id` | int | 扫描 ID |
| `repo_name` | string | 仓库名 |
| `repo_owner` | string | 仓库所有者 |
| `trigger_type` | string | 触发类型 |
| `status` | string | 状态 |
| `progress` | int | 进度百分比 |
| `total_findings` | int | 发现总数 |
| `overall_health_score` | int | 健康评分 |
| `created_at` | string | 创建时间 |
| `completed_at` | string | 完成时间 |

---

#### GET /scans/stats

扫描统计数据。

**认证级别**：auth

**响应字段**：

| 字段 | 类型 | 说明 |
|------|------|------|
| `data.total` | int | 扫描总数 |
| `data.by_status` | object | 按状态统计 |
| `data.avg_health_score` | float \| null | 平均健康评分 |

**响应示例**：

```json
{
  "success": true,
  "message": "ok",
  "data": {
    "total": 25,
    "by_status": {
      "completed": 20,
      "pending": 2,
      "failed": 3
    },
    "avg_health_score": 78.5
  }
}
```

---

#### GET /scans/{scan_id}

扫描详情，含所有发现（findings）。

**认证级别**：auth

**路径参数**：

| 参数 | 类型 | 说明 |
|------|------|------|
| `scan_id` | int | 扫描 ID |

**响应字段**：

| 字段 | 类型 | 说明 |
|------|------|------|
| `data.id` | int | 扫描 ID |
| `data.repo_name` | string | 仓库名 |
| `data.repo_owner` | string | 仓库所有者 |
| `data.trigger_type` | string | 触发类型 |
| `data.triggered_by` | string | 触发者 |
| `data.commit_sha` | string | 提交 SHA |
| `data.status` | string | 状态 |
| `data.progress` | int | 进度 |
| `data.current_phase` | string | 当前阶段 |
| `data.error_message` | string | 错误信息 |
| `data.file_count` | int | 文件数 |
| `data.code_file_count` | int | 代码文件数 |
| `data.total_findings` | int | 发现总数 |
| `data.critical_count` | int | 严重问题数 |
| `data.major_count` | int | 主要问题数 |
| `data.minor_count` | int | 次要问题数 |
| `data.suggestion_count` | int | 建议数 |
| `data.overall_health_score` | int | 健康评分 |
| `data.report_issue_number` | int | 报告 Issue 编号 |
| `data.report_issue_url` | string | 报告 Issue URL |
| `data.created_at` | string | 创建时间 |
| `data.started_at` | string | 开始时间 |
| `data.completed_at` | string | 完成时间 |
| `data.findings[]` | array | 发现列表 |
| `data.findings[].id` | int | 发现 ID |
| `data.findings[].file_path` | string | 文件路径 |
| `data.findings[].line_start` | int | 起始行 |
| `data.findings[].line_end` | int | 结束行 |
| `data.findings[].severity` | string | 严重程度 |
| `data.findings[].category` | string | 分类 |
| `data.findings[].title` | string | 标题 |
| `data.findings[].description` | string | 描述 |
| `data.findings[].suggestion` | string | 建议 |
| `data.findings[].confidence` | int | 置信度 |

---

#### POST /scans/trigger

手动触发扫描。自动选择未在冷却期内的仓库（最多 5 个）。

**认证级别**：super_admin

**限流**：3 次/分钟

**响应字段**：

| 字段 | 类型 | 说明 |
|------|------|------|
| `data.triggered` | array | 已触发的扫描列表 |
| `data.triggered[].repo` | string | 仓库名 |
| `data.triggered[].scan_id` | int | 扫描 ID |
| `data.count` | int | 已触发数量 |

**响应示例**：

```json
{
  "success": true,
  "message": "已触发 3 个仓库扫描",
  "data": {
    "triggered": [
      {"repo": "my-org/repo-a", "scan_id": 101},
      {"repo": "my-org/repo-b", "scan_id": 102},
      {"repo": "my-org/repo-c", "scan_id": 103}
    ],
    "count": 3
  }
}
```

---

#### POST /scans/{scan_id}/retry

重试失败的扫描。

**认证级别**：super_admin

**路径参数**：

| 参数 | 类型 | 说明 |
|------|------|------|
| `scan_id` | int | 扫描 ID |

**前置条件**：扫描状态必须为 `failed`。

**响应示例**：

```json
{
  "success": true,
  "message": "扫描已重新触发"
}
```

---

#### POST /scans/{scan_id}/cancel

取消正在进行的扫描。

**认证级别**：super_admin

**路径参数**：

| 参数 | 类型 | 说明 |
|------|------|------|
| `scan_id` | int | 扫描 ID |

**前置条件**：扫描状态必须为 `pending`、`indexing`、`analyzing` 或 `reporting`。

**响应示例**：

```json
{
  "success": true,
  "message": "扫描已取消"
}
```

---

### 3.13 个人设置 (Settings)

#### GET /settings

获取个人偏好设置。

**认证级别**：auth

**响应字段**：

| 字段 | 类型 | 说明 |
|------|------|------|
| `data.theme` | string | 主题（`"light"` / `"dark"` / `"system"`） |
| `data.language` | string | 语言（如 `"zh-CN"`） |
| `data.items_per_page` | int | 每页数量（10 / 20 / 50 / 100） |

**响应示例**：

```json
{
  "success": true,
  "message": "ok",
  "data": {
    "theme": "system",
    "language": "zh-CN",
    "items_per_page": 20
  }
}
```

---

#### PATCH /settings

更新个人偏好设置。

**认证级别**：auth

**请求体**（所有字段均为可选，仅传需要更新的字段）：

| 字段 | 类型 | 可选值 | 说明 |
|------|------|--------|------|
| `theme` | string | `"light"` / `"dark"` / `"system"` | 主题 |
| `language` | string | 任意 | 语言设置 |
| `items_per_page` | int | 10 / 20 / 50 / 100 | 每页数量 |

**请求示例**：

```json
{
  "theme": "dark",
  "items_per_page": 50
}
```

**响应示例**：

```json
{
  "success": true,
  "message": "设置已更新"
}
```

---

#### GET /settings/about

获取系统版本信息。

**认证级别**：auth

**响应字段**：

| 字段 | 类型 | 说明 |
|------|------|------|
| `data.version` | string | 应用版本号 |
| `data.build_date` | string | 构建日期（YYYY-MM-DD） |

**响应示例**：

```json
{
  "success": true,
  "message": "ok",
  "data": {
    "version": "2.8.1",
    "build_date": "2026-04-17"
  }
}
```

---

### 3.14 SSE 事件流 (Events)

#### GET /events

SSE（Server-Sent Events）实时事件流。

**认证级别**：auth

**连接方式**：
- 通过 URL 查询参数传递 Token：`GET /api/v1/events?token=<jwt_token>`
- 或通过 `Authorization: Bearer <token>` Header

> **Android 接入建议**：使用 OkHttp 或 Kotlin Coroutines Flow 建立 SSE 连接。注意 SSE 连接是长连接，需在后台妥善管理生命周期。

**响应头**：

```
Content-Type: text/event-stream
Cache-Control: no-cache
Connection: keep-alive
X-Accel-Buffering: no
```

**事件格式**（标准 SSE）：

```
event: message
data: {"type": "review_completed", "review_id": 42, ...}

event: message
data: {"type": "scan_progress", "scan_id": 101, "progress": 50, ...}
```

---

## 4. Android 接入快速指南

### OAuth 登录流程

```
1. GET /api/v1/auth/github/mobile?redirect_uri=myapp://oauth/callback
   -> 获取 authorization_url 和 state

2. 打开浏览器，加载 authorization_url
   -> 用户在 GitHub 授权

3. GitHub 回调到 myapp://oauth/callback?code=xxx&state=xxx
   -> App 接收 code 和 state

4. POST /api/v1/auth/callback
   Body: {"code": "xxx", "state": "xxx"}
   -> 获取 access_token

5. 后续请求: Authorization: Bearer <access_token>
```

### 常用请求模板

```kotlin
// OkHttp 请求构建
val request = Request.Builder()
    .url("https://your-domain/api/v1/dashboard/stats")
    .header("Authorization", "Bearer $token")
    .build()
```

### SSE 连接示例

```kotlin
// 使用 OkHttp SSE
val request = Request.Builder()
    .url("https://your-domain/api/v1/events")
    .header("Authorization", "Bearer $token")
    .build()

val sseSource = EventSource.Factory.create(request, eventListener)
```

> **SSE 认证方式**：支持三种认证 —— `Authorization: Bearer <token>` Header、`Cookie: webui_token=<token>`、以及 `?token=<jwt_token>` 查询参数（适用于部分不支持自定义 Header 的 SSE 客户端）。

---

> 文档版本：v1.0 | 最后更新：2026-04-17 | 端点总数：66
