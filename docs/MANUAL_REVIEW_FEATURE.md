# 手动触发 PR 全量审查

> 在 GitHub PR 中发布 `/full-review` 评论触发审查，通过 GitHub、WebUI 或 API 查看结果。

← [文档索引](README.md) · [README](../README.md)

---

## 使用方法

1. 打开需要审查的 GitHub Pull Request。
2. 确认 PR 处于打开状态、已退出草稿状态且尚未合并。
3. 在 PR 的普通评论区发布一条新评论，内容为：

   ```text
   /full-review
   ```

4. 查看 Sakura AI 在 PR 中回复的提交确认，以及后续 Check Runs 和审查报告。

命令作用于当前 PR，无需附带 PR URL。修改已有评论不会触发审查，需要发布新评论。该命令在 `enable_auto_review` 关闭时仍可使用。

全量审查会清理 Bot 之前发布的评论、撤回旧 Review，并尝试清理对应的本地旧审查记录，再提交新一轮审查。需要保留旧结果时，请先保存报告或通过 WebUI/API 导出。

## 权限与前置条件

- 触发者必须是 PR 作者，或拥有目标仓库 `write` / `admin` 权限的 GitHub 协作者。
- 权限按 GitHub PR 作者身份和仓库权限判断，不要求触发者具有 Sakura AI 超级管理员角色。
- GitHub App 已安装到目标仓库，且有读取 PR、发布审查报告所需的权限。
- GitHub App 的 Webhook 已订阅 `Issue comment` 事件，部署的 Webhook 地址和签名配置正确。
- AI 模型配置可用，Sakura AI 服务正常运行。

GitHub 权限查询暂时失败时，系统不会跳过权限检查继续审查；请根据 PR 中的提示稍后发布新评论重试。

## 查看进度与结果

| 入口 | 使用方式 |
|---|---|
| GitHub PR | 查看提交确认、Checks 中的进度，以及 Bot 发布的 Review 和评论 |
| WebUI | 登录后打开「PR 审查」页面（`/pr/`），搜索仓库或 PR，进入审查详情 |
| API | 使用已登录账号的 Bearer Token 或 WebUI Cookie 查询 `/api/v1/reviews` 等接口 |

API 的常用查询接口：

| 接口 | 用途 |
|---|---|
| `GET /api/v1/reviews` | 查询可访问的审查记录列表 |
| `GET /api/v1/reviews/{review_id}` | 查看审查详情与评论 |
| `GET /api/v1/reviews/{review_id}/files` | 查看文件级问题统计 |
| `GET /api/v1/reviews/{review_id}/comments` | 查看审查评论 |
| `GET /api/v1/reviews/export` | 导出可访问的审查记录 |

`review_id` 是审查记录 ID，可从列表响应中获取。WebUI 和 API 按登录用户的数据范围过滤记录，具体认证与响应格式见 [API v1 参考](api-v1-reference.md)。这些入口用于查看和导出；当前手动发起 PR 全量审查使用上述 GitHub 评论命令。

## Telegram 功能边界

Telegram Bot 仅注册 `/start` 与 `/bind`，用于可选通知端点绑定。历史 `/review <pr_url>` 命令已停用，旧版 PR 审查开始和完成的 Telegram 专用通知也已移除。

手动审查无需配置 Telegram Bot 或绑定 Telegram。Telegram Provider 仍支持通过统一通知服务投递公告，绑定说明见 [Telegram Bot 集成指南](TELEGRAM_SETUP.md)。

## 故障排查

| 现象 | 检查方式 |
|---|---|
| 发布命令后没有确认或新审查记录 | 确认发布位置是 PR 普通评论区、命令为 `/full-review`，并检查 GitHub App 的 Webhook 投递记录和服务日志 |
| 提示没有权限 | 使用 PR 作者账号，或由拥有仓库 `write` / `admin` 权限的协作者发布命令 |
| 提示权限检查暂不可用 | 检查 GitHub App 连接状态，恢复后发布新评论重试 |
| PR 被跳过 | 确认 PR 已打开、已退出草稿状态且尚未合并 |
| 无法读取仓库或 PR | 检查 GitHub App 的安装范围与目标仓库权限 |
| 审查执行失败或结果未发布 | 查看 GitHub Checks、WebUI 审查详情和服务日志，检查 AI/GitHub 连接及配置 |
| WebUI/API 中看不到记录 | 确认登录账号有对应数据的访问权限，并核对搜索条件和审查记录 ID |

## 相关文档

- [PR 功能指南](PR_FEATURES_GUIDE.md)：自动审查开关、审查策略与配置。
- [审查协议规范](PR_REVIEW_PROTOCOL.md)：审查报告的结构与输出契约。
- [部署指南](DEPLOYMENT.md)：GitHub App、Webhook 和服务部署。

---

*最后更新：2026-10-02 · 发现错误？[提 Issue](https://github.com/Sakura520222/Sakura-AI/issues)*
