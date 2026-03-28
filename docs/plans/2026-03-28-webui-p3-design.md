# WebUI P3 阶段设计文档：文件级审查 + 审查队列监控

## 1. Context

Sakura AI Reviewer WebUI 的 P0-P2 阶段已全部完成，提供了完整的 PR 管理、用户/仓库管理、审查日志和配置管理功能。P3 阶段聚焦两个核心模块：

1. **PR 文件级审查页** — P0 设计中规划但未实际实现的功能，按文件分组展示审查评论
2. **审查队列监控页** — 基于现有 `PRReview` 表数据构建只读监控视图

两个模块均为纯 WebUI 层面改动，不修改核心处理流程，不引入新依赖。

## 2. 模块 1：PR 文件级审查页

### 2.1 数据源

- `ReviewComment` 表 — 按 `file_path` 分组聚合评论，每个文件计算 severity 分布
- `PRReview` 表 — `file_count`、`line_count`、`code_file_count` 展示文件统计
- 评论类型 `comment_type`：overall（总体）、file（文件级）、line（行级）
- 严重程度 `severity`：critical、major、minor、suggestion

### 2.2 页面路由

| 路由 | 说明 |
| ---- | ---- |
| `GET /webui/pr/{review_id}/files` | 文件级审查页（容器） |
| `GET /webui/pr/{review_id}/files/file-fragment` | HTMX 片段：选中文件的评论列表 |

### 2.3 页面布局

左右分栏设计：

- **左栏（文件列表）**：
  - 每个文件显示：路径、评论总数、severity 分布（彩色圆点 🔴🟠🟡💚）
  - 文件按评论数降序排列，有评论的文件优先
  - 点击文件 → HTMX 加载右栏评论
  - 底部显示"总体评论"区域（`file_path IS NULL` 的评论）

- **右栏（评论面板）**：
  - 当前文件路径 + 修改行数信息
  - 评论列表：按 `line_number` 排序，显示 severity 标签 + 行号 + 内容
  - 无评论文件提示"该文件无审查评论"

### 2.4 新建文件

| 文件 | 说明 |
| ---- | ---- |
| `backend/webui/templates/pr_files.html` | 文件审查页容器（左右分栏布局） |
| `backend/webui/templates/components/pr_file_list_fragment.html` | 文件列表 HTMX 片段（含 severity 统计） |
| `backend/webui/templates/components/pr_file_comments_fragment.html` | 选中文件的评论 HTMX 片段 |

### 2.5 修改文件

| 文件 | 修改内容 |
| ---- | -------- |
| `backend/webui/routes/pr.py` | 新增 2 个路由端点（files 页面 + file-fragment） |
| `backend/webui/templates/pr_detail.html` | 在 PR 详情页添加"文件审查"入口按钮 |

## 3. 模块 2：审查队列监控页

### 3.1 数据源

基于 `PRReview` 表的 status/timing/error 字段构建监控视图，不激活 `ReviewQueue` 表。

- `status`：pending、processing、completed、failed
- `created_at` / `completed_at`：计算处理耗时
- `error_message`：失败原因展示

### 3.2 页面路由

| 路由 | 说明 |
| ---- | ---- |
| `GET /webui/queue/` | 审查队列监控页（管理员专用） |
| `GET /webui/queue/list-fragment` | HTMX 片段：队列列表 + 过滤 + 分页 |

### 3.3 页面布局

- **顶部统计卡片**：待处理数、处理中数、已完成数、失败数、平均处理耗时
- **过滤栏**：搜索（PR 标题）、仓库下拉、状态下拉、日期范围
- **数据表格**：PR 标题、仓库名、状态标签（彩色 badge）、创建时间、完成时间、耗时、错误信息
- **分页**：HTMX 驱动

### 3.4 统计卡片数据

| 指标 | 计算方式 |
| ---- | -------- |
| 待处理 | `SELECT COUNT(*) WHERE status = 'pending'` |
| 处理中 | `SELECT COUNT(*) WHERE status = 'processing'` |
| 已完成 | `SELECT COUNT(*) WHERE status = 'completed'` |
| 失败 | `SELECT COUNT(*) WHERE status = 'failed'` |
| 平均耗时 | `AVG(completed_at - created_at) WHERE status = 'completed' AND completed_at IS NOT NULL` |

### 3.5 新建文件

| 文件 | 说明 |
| ---- | ---- |
| `backend/webui/templates/queue.html` | 队列监控页容器 |
| `backend/webui/templates/components/queue_list_fragment.html` | 队列列表 HTMX 片段（含过滤 + 表格 + 分页） |
| `backend/webui/templates/components/queue_stats_cards.html` | 统计卡片组件 |

### 3.6 修改文件

| 文件 | 修改内容 |
| ---- | -------- |
| `backend/webui/routes/__init__.py` | 注册 queue router |
| `backend/webui/templates/components/sidebar.html` | 添加"审查队列"导航链接（管理员可见） |

### 3.7 权限

仅管理员（`require_admin`）可访问，与用户管理、仓库管理保持一致。

## 4. 关键约定（严格遵循 P0-P2 已有模式）

- **路由模式**：`router = APIRouter(prefix="/xxx", tags=["..."])`, `templates = get_templates()`
- **模板上下文**：始终包含 `request`, `current_user`, `csrf_token`, `active_page`, `user_prefs`
- **权限**：`require_auth` / `require_admin` / `require_super_admin`（来自 deps.py）
- **DB 查询**：异步 SQLAlchemy，`scalar() or 0`，LIKE 转义 `%` 和 `_`
- **HTMX**：页面用 spinner 容器 `hx-trigger="load"` 加载片段
- **卡片样式**：`bg-white dark:bg-gray-800 rounded-xl border border-gray-200 dark:border-gray-700 shadow-sm p-6`
- **按钮主色**：`bg-pink-500 hover:bg-pink-600 text-white rounded-lg text-sm font-medium`
- **不引入新依赖**，不修改数据库模型，不修改核心处理流程

## 5. 完整文件清单

### 新建（6 个文件）

| 文件 | 模块 |
| ---- | ---- |
| `backend/webui/templates/pr_files.html` | PR 文件级审查 |
| `backend/webui/templates/components/pr_file_list_fragment.html` | PR 文件级审查 |
| `backend/webui/templates/components/pr_file_comments_fragment.html` | PR 文件级审查 |
| `backend/webui/templates/queue.html` | 审查队列监控 |
| `backend/webui/templates/components/queue_list_fragment.html` | 审查队列监控 |
| `backend/webui/templates/components/queue_stats_cards.html` | 审查队列监控 |

### 修改（4 个文件）

| 文件 | 修改内容 |
| ---- | -------- |
| `backend/webui/routes/pr.py` | 新增文件审查 2 个路由 |
| `backend/webui/templates/pr_detail.html` | 添加"文件审查"入口按钮 |
| `backend/webui/routes/__init__.py` | 注册 queue router |
| `backend/webui/templates/components/sidebar.html` | 添加"审查队列"导航链接 |

## 6. 验证方案

1. **文件级审查**：打开一个有多文件评论的 PR 详情页，点击"文件审查"按钮，确认文件列表正确分组、severity 统计准确、点击文件加载评论、总体评论正确显示
2. **审查队列**：管理员登录，查看队列监控页，确认统计卡片数据准确、过滤/分页正常、耗时计算正确、失败记录显示错误信息
3. **权限**：普通用户访问 `/webui/queue/` 应被 403 拒绝
4. **暗色模式**：两个新页面在暗色模式下样式正确
5. **响应式**：移动端文件列表与评论面板垂直堆叠

---

文档创建时间：2026-03-28
版本：1.0
作者：Sakura AI Reviewer Team
