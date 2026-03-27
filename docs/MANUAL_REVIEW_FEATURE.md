# 手动触发审查功能说明

## 功能概述

超级管理员可以通过 Telegram Bot 手动触发对任意 GitHub Pull Request 的 AI 代码审查。

## 使用方法

### 命令格式

```
/review <pr_url>
```

### 参数说明

- `pr_url`: GitHub Pull Request 的完整 URL
  - 支持格式: `https://github.com/owner/repo/pull/123`
  - 也支持: `https://github.com/owner/repo/pull/123/files`
  - 不需要包含 `/files` 后缀

### 使用示例

```
/review https://github.com/Sakura520222/Sakura-AI-Reviewer/pull/42
```

## 权限要求

- **仅超级管理员可用** (SUPER_ADMIN)
- 超级管理员在 `.env` 文件的 `TELEGRAM_ADMIN_USER_IDS` 中配置
- 超级管理员可以审查任何仓库，不受仓库白名单限制
- 超级管理员不受配额限制

## 执行流程

### 1. URL 解析

- 从 PR URL 中提取 `owner`、`repo` 和 `pr_number`
- 支持多种 URL 格式

### 2. PR 信息获取

- 通过 GitHub API 获取 PR 详细信息
- 自动获取 `installation_id`（用于 GitHub App 认证）
- 验证 PR 是否存在

### 3. 状态检查

检查以下条件，任一不满足则拒绝审查：

- ❌ PR 必须是 `open` 状态
- ❌ PR 不能是草稿 (draft)
- ❌ PR 不能已合并 (merged)

### 4. 审查启动

- 发送 Telegram 通知（审查开始）
- 提交审查任务到异步队列
- 返回任务 ID 给用户

### 5. 异步执行

- 审查在后台异步执行（使用 `asyncio.create_task`）
- 不阻塞 Telegram Bot
- 执行时间: 通常 30s - 2min

### 6. 完成通知

- 审查完成后自动发送 Telegram 通知
- 包含评分、决策等信息

## 响应消息

### 成功提交

```
✅ 审查任务已提交

📋 PR: owner/repo#123
👤 作者: username
📝 标题: Fix bug in authentication...
🆔 任务ID: abc123-def456-...

⏳ 审查完成后将通过Telegram通知您
```

### 错误提示

#### URL 格式错误

```
❌ 无效的PR URL格式: https://example.com/bad-url
正确格式: https://github.com/owner/repo/pull/123
```

#### PR 未打开

```
❌ PR未打开

📋 PR: owner/repo#123
状态: closed
```

#### 草稿 PR

```
❌ 这是草稿PR，跳过审查

📋 PR: owner/repo#123
```

#### PR 已合并

```
❌ PR已合并，跳过审查

📋 PR: owner/repo#123
```

#### 无权限访问

```
❌ 无法访问仓库

可能原因：
• GitHub App 未安装到目标仓库
• 仓库不存在或无权限访问

错误详情: Failed to get installation...
```

#### PR 不存在

```
❌ PR不存在

请检查PR URL是否正确
错误详情: Not Found
```

## 技术实现

### 核心函数

#### `get_pr_info_from_url(pr_url: str)`

位置: `backend/core/github_app.py`

功能：

- 解析 PR URL
- 通过 GitHub API 获取 PR 信息
- 获取 `installation_id`
- 构造与 webhook 一致的 `pr_info` 字典

#### `cmd_review(update, context)`

位置: `backend/telegram/handlers.py`

功能：

- 权限检查
- 参数验证
- 调用 `get_pr_info_from_url()`
- 状态验证
- 提交审查任务
- 错误处理

### 数据流程

```
Telegram Bot
    ↓
cmd_review (handlers.py)
    ↓
get_pr_info_from_url (github_app.py)
    ↓
submit_review_task (review_worker.py)
    ↓
process_review_task (review_worker.py)
    ↓
AI Review & Submit to GitHub
    ↓
_send_review_complete_notification (review_worker.py)
    ↓
Telegram Notification
```

### 关键特性

#### 1. 等效性构造

手动触发的 `pr_info` 字典与 webhook payload 格式完全一致：

```python
{
    "action": "manual",  # 标记为手动触发
    "pr_id": ...,
    "pr_number": ...,
    "repo_owner": ...,
    "repo_name": ...,
    "repo_full_name": ...,
    "installation_id": ...,  # 关键字段
    "author": ...,
    "title": ...,
    "branch": ...,
    "base_branch": ...,
    "diff_url": ...,
    "patch_url": ...,
    "html_url": ...,
    "state": ...,
    "draft": ...,
    "merged": ...,
}
```

#### 2. 异步处理

使用 `asyncio.create_task()` 实现非阻塞：

```python
task_id = await submit_review_task(pr_info)
# 立即返回，不等待审查完成
```

#### 3. 完整的审查流程

手动触发与 webhook 触发使用相同的审查逻辑：

- PR 分析
- AI 审查
- 标签推荐
- 决策引擎
- 提交 Review 到 GitHub

## 前置条件

### 必需配置

1. **GitHub App 已安装到目标仓库**
   - 手动触发需要 GitHub App 有访问权限
   - 如果未安装，会提示"无法访问仓库"

2. **超级管理员配置**

   ```env
   TELEGRAM_ADMIN_USER_IDS=123456789,987654321
   ```

3. **Telegram Bot 配置**
   - Bot Token 已配置
   - Bot 已启动并运行

### 可选配置

- Telegram 通知配置（用于接收审查完成通知）

## 使用场景

### 适用场景

1. **重新审查已修改的 PR**
   - PR 作者修改后希望重新审查
   - 不需要关闭重开 PR

2. **测试审查功能**
   - 测试新的审查策略
   - 验证 AI 审查效果

3. **补充审查**
   - Webhook 触发失败时的补救措施
   - 手动触发跳过的 PR

4. **特殊仓库审查**
   - 审查未在白名单中的仓库
   - 临时需要审查某个 PR

### 不适用场景

- ❌ 已关闭的 PR
- ❌ 已合并的 PR
- ❌ 草稿 PR (Draft PR)
- ❌ 不存在的 PR

## 故障排查

### 常见问题

#### 1. "无法访问仓库"

**原因**: GitHub App 未安装到目标仓库

**解决**:

1. 访问 GitHub App 设置页面
2. 安装 App 到目标仓库
3. 确保有正确的权限

#### 2. "PR不存在"

**原因**: URL 错误或 PR 编码错误

**解决**:

1. 检查 URL 是否正确
2. 确认 PR 编号正确
3. 确认仓库存在

#### 3. 审查超时

**原因**: PR 太大或 API 响应慢

**解决**:

1. 检查 PR 规模是否超过限制
2. 查看 Bot 日志
3. 耐心等待（可能需要 2-5 分钟）

## 日志查看

审查过程的日志会记录到应用日志中：

```
[INFO] 超级管理员手动触发审查: 123456789 -> owner/repo#42
[INFO] 解析PR URL成功: owner/repo#42
[INFO] 成功获取PR信息: owner/repo#42, author=username, state=open
[INFO] 手动审查任务已提交: owner/repo#42, task_id=abc123, triggered_by=123456789
```

## 相关文档

- [Telegram Bot 设置](./TELEGRAM_SETUP.md)
- [审查策略配置](../config/strategies.yaml)
- [API 文档](../README.md)

## 更新历史

- **2026-03-06**: 初始版本
  - 实现基本的 `/review` 命令
  - 支持 URL 解析和 PR 信息获取
  - 完整的错误处理和用户反馈
  - 异步执行和 Telegram 通知
