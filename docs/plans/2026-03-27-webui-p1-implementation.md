# P1 阶段实现计划：WebUI 管理功能

## Context

Sakura AI Reviewer 的 WebUI P0 阶段（仪表盘、PR 管理、GitHub OAuth）已完成。现需实现 P1 阶段的 4 个管理模块：用户管理、仓库管理、审查日志、个人设置。所有实现严格复用现有代码模式和约定，不引入新依赖。

## 实现顺序

1. **个人设置** — 最简单，为其他模块无依赖
2. **用户管理** — 管理员核心功能
3. **仓库管理** — 与用户管理结构类似
4. **审查日志** — 复用过滤/分页模式

---

## 模块 1：个人设置

### 新建文件

**`backend/webui/routes/settings.py`**
- `GET /settings/` — 渲染设置页面，从 `webui_configs` 查询当前用户配置（不存在用默认值）
- `POST /settings/` — 保存设置：验证 CSRF，验证 theme/language/items_per_page 范围，upsert 写入 `webui_configs`，重定向带 `?saved=1`
- 使用 `require_auth` 依赖

**`backend/webui/templates/settings.html`**
- 继承 base.html，包含主题切换（radio 卡片）、语言下拉、每页条数下拉
- 保存成功/失败提示（URL 参数驱动）

### 修改文件
- `routes/__init__.py` — 注册 settings router
- `components/sidebar.html` — 添加"个人设置"链接（齿轮图标），`active_page="settings"`

---

## 模块 2：用户管理

### 新建文件

**`backend/webui/routes/users.py`**
- `GET /users/` — 用户列表页，`require_admin`
- `GET /users/list-fragment` — HTMX 片段：搜索(github_username/telegram_id)、角色过滤、分页
- `GET /users/{user_id}` — 用户详情页：基本信息、配额进度条、配额使用历史(QuotaUsageLog 最近20条)
- `POST /users/{user_id}/role` — 修改角色（验证 role 范围）
- `POST /users/{user_id}/quota` — 修改配额（daily/weekly/monthly，值 >= 0）
- `POST /users/{user_id}/toggle` — 启用/禁用（切换 is_active）
- 所有 POST 验证 CSRF，成功后 RedirectResponse

**`backend/webui/templates/users.html`** — 用户列表页容器

**`backend/webui/templates/user_detail.html`** — 用户详情：面包屑、基本信息卡片、配额进度条、修改角色表单、修改配额表单、启用/禁用按钮

**`backend/webui/templates/components/user_list_fragment.html`** — 用户列表 HTMX 片段：搜索/过滤表单、用户表格（用户名、角色标签、配额简览、状态圆点、注册时间）、分页

### 修改文件
- `routes/__init__.py` — 注册 users router
- `components/sidebar.html` — 添加"用户管理"链接（用户组图标）

### 数据模型
- `TelegramUser`（telegram_users 表）— 所有字段已存在，无需迁移

---

## 模块 3：仓库管理

### 新建文件

**`backend/webui/routes/repos.py`**
- `GET /repos/` — 仓库列表页，`require_admin`
- `GET /repos/list-fragment` — HTMX 片段：搜索(repo_name)、状态过滤(active/all)、分页
- `POST /repos/add` — 添加仓库：验证格式(含 `/`)、检查唯一约束、写入 RepoSubscription
- `POST /repos/{repo_id}/toggle` — 启用/禁用
- `POST /repos/{repo_id}/remove` — 删除仓库（带前端确认弹窗）

**`backend/webui/templates/repos.html`** — 仓库列表页容器

**`backend/webui/templates/components/repo_list_fragment.html`** — 仓库列表片段：内联添加表单、仓库列表（名称、状态标签、添加者、添加时间、操作按钮）、分页

### 修改文件
- `routes/__init__.py` — 注册 repos router
- `components/sidebar.html` — 添加"仓库管理"链接（仓库图标）

### 数据模型
- `RepoSubscription`（repo_subscriptions 表）— 所有字段已存在

---

## 模块 4：审查日志

### 新建文件

**`backend/webui/routes/logs.py`**
- `GET /logs/` — 审查日志页，`require_auth`
- `GET /logs/list-fragment` — HTMX 片段：搜索(PR标题/仓库名/作者)、仓库下拉(从 repo_subscriptions 查)、状态过滤、日期范围过滤、分页
- `GET /logs/{review_id}/detail-fragment` — 单条审查详情展开片段（PRReview + ReviewComment）
- 权限：管理员看全部，普通用户看已启用仓库的记录

**`backend/webui/templates/logs.html`** — 审查日志页容器

**`backend/webui/templates/components/log_filters.html`** — 过滤表单（搜索框、仓库下拉、状态下拉、日期范围、搜索按钮）

**`backend/webui/templates/components/log_list_fragment.html`** — 日志列表片段（含 filters）：每行可点击展开详情

**`backend/webui/templates/components/log_detail_fragment.html`** — 展开详情：审查摘要、决策、评分、文件统计、评论列表（前5条）

### 修改文件
- `routes/__init__.py` — 注册 logs router
- `components/sidebar.html` — 添加"审查日志"链接（时钟图标）

### 数据模型
- `PRReview` + `ReviewComment` — 所有字段已存在

---

## 全局修改汇总

### `backend/webui/routes/__init__.py`
新增 4 个 import 和 include_router：users, repos, logs, settings

### `backend/webui/templates/components/sidebar.html`
在"PR 审查"之后、"分隔线"之前插入：
- 审查日志 (`logs`) — 所有用户可见
- 分隔线
- 用户管理 (`users`) — 管理员功能
- 仓库管理 (`repos`) — 管理员功能
- 分隔线
- 个人设置 (`settings`) — 所有用户可见

---

## 关键约定（严格遵循）

- **路由模式**：`router = APIRouter(prefix="/xxx", tags=["..."])`, `templates = get_templates()`
- **模板上下文**：始终包含 `request`, `current_user`, `csrf_token`, `active_page`
- **权限**：`require_auth` / `require_admin` / `require_super_admin`（来自 deps.py）
- **CSRF**：所有 POST 表单含隐藏 `csrf_token` 字段，服务端调用 `validate_csrf_token()`
- **DB 查询**：异步 SQLAlchemy，`scalar() or 0`，LIKE 转义 `%` 和 `_`
- **HTMX**：页面用 spinner 容器 `hx-trigger="load"` 加载片段，写操作用传统 POST + 重定向
- **卡片样式**：`bg-white dark:bg-gray-800 rounded-xl border border-gray-200 dark:border-gray-700 shadow-sm p-6`
- **按钮主色**：`bg-pink-500 hover:bg-pink-600 text-white rounded-lg text-sm font-medium`
- **不引入新依赖**，不修改数据库模型

## 完整文件清单

### 新建（14 个文件）

| 文件 | 模块 |
|------|------|
| `backend/webui/routes/settings.py` | 个人设置 |
| `backend/webui/templates/settings.html` | 个人设置 |
| `backend/webui/routes/users.py` | 用户管理 |
| `backend/webui/templates/users.html` | 用户管理 |
| `backend/webui/templates/user_detail.html` | 用户管理 |
| `backend/webui/templates/components/user_list_fragment.html` | 用户管理 |
| `backend/webui/routes/repos.py` | 仓库管理 |
| `backend/webui/templates/repos.html` | 仓库管理 |
| `backend/webui/templates/components/repo_list_fragment.html` | 仓库管理 |
| `backend/webui/routes/logs.py` | 审查日志 |
| `backend/webui/templates/logs.html` | 审查日志 |
| `backend/webui/templates/components/log_filters.html` | 审查日志 |
| `backend/webui/templates/components/log_list_fragment.html` | 审查日志 |
| `backend/webui/templates/components/log_detail_fragment.html` | 审查日志 |

### 修改（2 个文件）

| 文件 | 修改内容 |
|------|---------|
| `backend/webui/routes/__init__.py` | 注册 4 个新路由模块 |
| `backend/webui/templates/components/sidebar.html` | 添加 4 个导航链接 + 调整分隔线 |

## 验证方案

每个模块完成后逐一验证：
1. **个人设置**：登录后访问设置页，修改主题/语言/每页条数并保存，刷新确认持久化
2. **用户管理**：管理员登录，查看列表、搜索、过滤、分页；进入详情修改角色/配额/启用状态
3. **仓库管理**：管理员登录，添加仓库、搜索、禁用/启用、删除（含确认弹窗）；重复添加验证唯一约束
4. **审查日志**：所有用户可访问，按时间/仓库/状态/关键词过滤；点击展开审查详情；普通用户权限限制
5. **权限**：普通用户访问管理页面应被 403 拒绝并重定向到登录页
6. **暗色模式**：所有新页面在暗色模式下样式正确
7. **响应式**：移动端侧边栏正常收起/展开
