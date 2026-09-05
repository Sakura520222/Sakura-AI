# 🤖 Telegram Bot 集成指南

> Telegram 仅作为可选的通知渠道和一次性绑定入口。账号登录、GitHub 身份、角色权限、配额、仓库和系统配置均在 WebUI 中管理。

← [文档索引](README.md) · [README](../README.md)

---

## 📋 当前功能

Sakura AI 的 Telegram Bot 目前只注册以下两个命令：

| 命令 | 用途 |
|------|------|
| `/start` | 显示绑定提示；带有 WebUI 生成的深链接参数时，会直接进入绑定流程 |
| `/bind <一次性令牌>` | 在 Bot 私聊中消费一次性令牌并绑定通知端点 |

历史版本中的状态、配额、审查、用户和仓库管理命令已经不再挂载。旧处理函数仍可能保留在源码中以兼容导入，但不会被 Bot 路由调用，也不应继续写入部署脚本或用户操作手册。

## 🔐 身份与权限

- 使用 GitHub OAuth 登录或注册 Sakura AI 账号；GitHub 身份是登录和账号认领的依据。
- 超级管理员在 WebUI「用户管理」中维护用户、角色、启用状态、配额和 GitHub 用户名。
- 仓库白名单、订阅、审查和系统设置通过 WebUI/API 管理。
- Telegram ID 只用于标识通知目标，不参与登录、账号认领或角色判断；绑定 Telegram 不会提升权限。
- Telegram Bot 绑定只接受私聊中的正数用户/chat ID，并且绑定令牌短期有效且只能使用一次。

## 🚀 快速开始

### 1. 创建 Bot

1. 在 Telegram 中找到 [@BotFather](https://t.me/BotFather)。
2. 发送 `/newbot`，按提示设置名称和用户名。
3. 保存 BotFather 返回的 **Bot Token**，不要把它提交到 Git 仓库或公开日志。

### 2. 配置 Bot

首次部署时，在 Setup Wizard 的「AI 模型与通知」步骤填写 Bot Token。部署后，超级管理员可以在 WebUI「系统核心配置」的 Telegram 分组中设置：

- `telegram_enabled`：是否启用 Telegram Provider；
- `telegram_bot_token`：BotFather 生成的 Token；
- `telegram_bind_token_expire_seconds`：WebUI 绑定令牌的有效期。

修改启用状态或 Bot Token 后需要重启服务，新的 Bot 实例才会加载配置。若部署环境配置了默认系统通知聊天目标，请按部署文件中的 `TELEGRAM_DEFAULT_CHAT_ID` 设置；该目标是通知目的地，不是管理员身份来源。

```env
TELEGRAM_BOT_TOKEN=你的_Bot_Token
TELEGRAM_DEFAULT_CHAT_ID=可选的通知聊天ID
```

> 管理员账号和权限请通过 WebUI 配置。数据库动态配置和备份导入会自动兼容历史字段名，但新配置应使用上面的规范键名。

### 3. 启动并绑定通知

1. 启动应用并确认 Telegram Provider 已启用。
2. 使用 GitHub 登录 Sakura AI，打开 WebUI「个人设置」中的 Telegram 通知区域。
3. 点击生成一次性绑定链接，并在有效期内打开它；链接会跳转到 Bot 私聊中的 `/start <token>`。
4. 如果没有使用深链接，也可以在 Bot 私聊中发送 `/bind <token>`。
5. 看到“Telegram 通知绑定成功”后，回到 WebUI 检查通知端点状态。

绑定失败时，请回到 WebUI 重新生成令牌。令牌过期、重复使用、在群组中使用，或该 Telegram 已经绑定到其他账号，都会被拒绝。

## 🧭 日常管理入口

| 事项 | 入口 |
|------|------|
| 登录、GitHub 身份认领 | GitHub OAuth 登录页 |
| 绑定或解绑自己的 Telegram | WebUI「个人设置」 |
| 用户、角色、启用状态和配额 | 超级管理员 WebUI「用户管理」 |
| 仓库白名单与订阅 | WebUI 仓库/订阅管理 |
| Bot Token、Provider 开关 | 超级管理员 WebUI「系统核心配置」 |
| 审查、扫描和公告通知 | WebUI/API 及配置的通知渠道 |

Telegram Bot 不提供用户、管理员、仓库、配额或审查命令。需要执行这些操作时，请使用 WebUI；需要自动化时，请使用受保护的 API。

## 📦 通知目标说明

Telegram 通知目标分为两类：

1. 用户在 WebUI 个人设置中绑定的私聊通知端点；
2. 部署配置提供的系统级默认通知聊天目标，可用于扫描等系统通知。

群组或频道的聊天 ID 可能是负数，这是 Telegram 的正常格式。它只能作为显式配置的系统通知目标使用，不应伪装成某个用户的个人绑定端点。通知发送失败会记录在应用日志中，应用不会因此改变账号权限。

## 🛡️ 安全建议

1. 保护 Bot Token，不要提交 `.env`、备份文件或日志中的敏感值。
2. 通过 WebUI 最小化授予超级管理员和管理员角色。
3. 绑定链接只在本人 Telegram 私聊中使用，并在泄露时立即回 WebUI 重新生成。
4. 修改 Bot Token 或启用状态后重启服务，并确认日志中的 Provider 状态。
5. 生产环境使用 HTTPS，避免 WebUI 绑定令牌被中间人截获。

## ❓ 常见问题

### Q: Telegram 能直接登录 Sakura AI 吗？

A: 不能。请使用 GitHub OAuth 登录。Telegram 只接收通知，以及完成 WebUI 发起的一次性通知绑定。

### Q: 为什么历史 Telegram 命令没有响应？

A: 这些历史命令已经移除，不在当前 Bot 路由中注册。请使用 WebUI 或受保护 API 完成对应操作。

### Q: 绑定链接失效怎么办？

A: 绑定令牌只能使用一次且有有效期。回到 WebUI「个人设置」重新生成，并在 Bot 私聊中打开新链接。

### Q: 修改 Token 后 Bot 仍使用旧配置？

A: Telegram Bot 在服务启动时构造。保存配置后重启服务，再检查应用日志。

### Q: 如何查看 Telegram ID？

A: 绑定流程会使用 Telegram 私聊上下文自动识别 ID，一般不需要手工填写。只有配置系统级默认通知目标时，才需按部署入口填写目标 chat ID。

## 📚 更多信息

- 项目主页：[Sakura AI](https://github.com/Sakura520222/Sakura-AI)
- 问题反馈：[Issues](https://github.com/Sakura520222/Sakura-AI/issues)
- [部署指南](DEPLOYMENT.md)

---

<div align="center">

**Sakura AI** - 让代码审查更智能、更高效

Made with 🌸 by [Sakura520222](https://github.com/Sakura520222)

</div>

---

*最后更新：2026-09-04 · 发现错误？[提 Issue](https://github.com/Sakura520222/Sakura-AI/issues)*
