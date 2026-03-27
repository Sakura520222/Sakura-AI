# 🤖 Telegram Bot 集成指南

## 📋 功能概述

Sakura AI Reviewer 现已集成 Telegram Bot，提供以下功能：

### ✨ 核心功能

1. **通知系统**
   - 🔔 PR 开始审查通知
   - 🌸 PR 审查完成通知（含评分、问题统计）
   - ⚠️ 配额不足提醒
   - 🚫 未授权仓库/用户提醒

2. **命令系统**
   - `/status` - 查看系统状态
   - `/recent` - 查看最近审查记录
   - `/myquota` - 查看我的配额
   - `/help` - 帮助信息

3. **管理功能**（管理员）
   - `/user_add <telegram_id> <github_username>` - 添加用户
   - `/user_remove <github_username>` - 移除用户
   - `/repo_add <owner/repo>` - 添加仓库到白名单
   - `/repo_remove <owner/repo>` - 移除仓库
   - `/quota_set <github_username> <daily|weekly|monthly> <limit>` - 设置配额
   - `/users` - 列出所有用户
   - `/repos` - 列出所有仓库

4. **超级管理员功能**
   - `/admin_add <telegram_id> <github_username>` - 添加管理员
   - `/admin_remove <telegram_id>` - 移除管理员
   - `/review <pr_url>` - 手动触发审查

## 🔐 权限体系

### 三级权限

```
👑 超级管理员 (SUPER_ADMIN)
   ↓ 由环境变量 TELEGRAM_ADMIN_USER_IDS 定义（唯一）
   
👤 管理员 (ADMIN)
   ↓ 由超级管理员通过 /admin_add 添加
   
👥 普通用户 (USER)
   ↓ 由管理员通过 /user_add 添加
```

### 权限说明

- **超级管理员**：所有权限 + 添加/删除管理员
- **管理员**：添加/删除用户、管理仓库、设置配额
- **普通用户**：使用配额进行审查、查看自己的配额

## 📦 配额系统

### 配额类型

- **每日配额**：每天 00:00 重置（默认：10次）
- **每周配额**：每周一 00:00 重置（默认：50次）
- **每月配额**：每月 1 日 00:00 重置（默认：200次）

### 配额规则

- 超级管理员和管理员不受配额限制
- 普通用户每次审查消耗 1 次所有配额
- 配额不足时自动拒绝审查并发送通知

## 🚀 快速开始

### 1. 创建 Telegram Bot

1. 在 Telegram 中找到 [@BotFather](https://t.me/BotFather)
2. 发送 `/newbot` 创建新机器人
3. 按提示设置机器人名称和用户名
4. 保存获得的 **Bot Token**

### 2. 获取你的 Telegram ID

1. 在 Telegram 中找到 [@userinfobot](https://t.me/userinfobot)
2. 发送任意消息获取你的 **Telegram ID**
3. 记录这个 ID（你将作为超级管理员）

### 3. 配置环境变量

在 `.env` 文件中添加：

```env
# Telegram Bot配置
TELEGRAM_BOT_TOKEN=你的_Bot_Token
TELEGRAM_ADMIN_USER_IDS=你的_Telegram_ID
TELEGRAM_DEFAULT_CHAT_ID=你的_Telegram_ID
```

### 4. 启动应用

```bash
cd docker
docker-compose up -d
```

### 5. 初始化系统

1. 在 Telegram 中找到你的 Bot
2. 发送 `/start` 命令
3. 你应该看到 "👑 超级管理员" 标识

### 6. 添加第一个管理员（可选）

```bash
/admin_add <管理员_Telegram_ID> <管理员_GitHub用户名>
```

### 7. 添加仓库到白名单

```bash
/repo_add owner/repo
```

### 8. 添加用户

```bash
/user_add <用户_Telegram_ID> <用户_GitHub用户名>
```

### 9. 设置用户配额（可选）

```bash
/quota_set <GitHub用户名> daily 20
```

## 📝 使用流程

### 审查流程

```
GitHub PR 创建
    ↓
Webhook 接收
    ↓
✅ 检查仓库是否在白名单
    ↓
✅ 检查 PR 作者是否已注册
    ↓
✅ 检查配额是否充足
    ↓
🔔 发送"开始审查"通知
    ↓
🤖 AI 审查进行中...
    ↓
🌸 发送"审查完成"通知
    ↓
✅ 审查完成
```

### 拒绝场景

- ❌ 仓库未在白名单 → 静默跳过
- ❌ 用户未注册 → 静默跳过
- ❌ 配额不足 → 发送拒绝通知

## 🔧 管理命令示例

### 添加管理员

```bash
/admin_add 123456789 john_doe
```

### 添加普通用户

```bash
/user_add 987654321 jane_smith
```

### 添加仓库

```bash
/repo_add facebook/react
/repo_add vuejs/vue
```

### 设置配额

```bash
# 设置每日配额为 20
/quota_set john_doe daily 20

# 设置每周配额为 100
/quota_set john_doe weekly 100

# 设置每月配额为 500
/quota_set john_doe monthly 500
```

### 查看所有用户

```bash
/users
```

### 查看所有仓库

```bash
/repos
```

## 📊 数据库表结构

### telegram_users

| 字段 | 说明 |
|------|------|
| id | 主键 |
| telegram_id | Telegram 用户 ID |
| github_username | GitHub 用户名 |
| role | 角色（super_admin/admin/user） |
| daily_quota | 每日配额限制 |
| weekly_quota | 每周配额限制 |
| monthly_quota | 每月配额限制 |
| daily_used | 已使用每日配额 |
| weekly_used | 已使用每周配额 |
| monthly_used | 已使用每月配额 |

### repo_subscriptions

| 字段 | 说明 |
|------|------|
| id | 主键 |
| repo_name | 仓库名称（owner/repo） |
| is_active | 是否激活 |
| added_by | 添加者 Telegram ID |

### quota_usage_logs

| 字段 | 说明 |
|------|------|
| id | 主键 |
| telegram_user_id | 用户 ID |
| repo_name | 仓库名称 |
| pr_number | PR 编号 |
| usage_type | 配额类型 |

## 🛡️ 安全建议

1. **保护 Bot Token**：不要将 `.env` 文件提交到版本控制
2. **限制管理员**：只授予必要的管理员权限
3. **监控配额**：定期检查配额使用情况
4. **白名单管理**：只添加需要审查的仓库

## ❓ 常见问题

### Q: 如何获取 Telegram ID？

A: 使用 [@userinfobot](https://t.me/userinfobot) 机器人获取

### Q: 配额如何重置？

A: 系统自动重置

- 每日：每天 00:00
- 每周：每周一 00:00
- 每月：每月 1 日 00:00

### Q: 管理员受配额限制吗？

A: 不受。超级管理员和管理员都有无限配额

### Q: 如何删除用户？

A: 使用 `/user_remove <github_username>` 命令

### Q: Bot 没有响应怎么办？

A: 检查以下几点：

1. Bot Token 是否正确
2. 环境变量是否配置
3. 应用是否正常运行
4. 查看日志：`docker-compose logs -f`

## 📚 更多信息

- 项目主页：[Sakura AI Reviewer](https://github.com/Sakura520222/Sakura-AI-Reviewer)
- 问题反馈：[Issues](https://github.com/Sakura520222/Sakura-AI-Reviewer/issues)

---

<div align="center">

**Sakura AI Reviewer** - 让代码审查更智能、更高效

Made with 🌸 by [Sakura520222](https://github.com/Sakura520222)

</div>
