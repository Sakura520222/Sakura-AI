# 快速开始

这里提供最短的入门路径。更多平台差异、版本升级、备份及故障排查，请以[完整部署指南](DEPLOYMENT.md)为准。

## 方式一：直接使用官方服务

打开 [Sakura AI 在线服务](https://ai.firefly520.top/)，完成注册和登录后使用，无需自行维护服务器。

## 方式二：Linux Docker 自行部署

**适用场景：** 拥有可使用 Docker 与 systemd 的 Linux 服务器，希望部署 Web、MySQL、Redis、Host Updater 及 Agent 沙箱等完整能力。

**宿主机要求：** Linux x86_64/arm64、glibc ≥ 2.36（如 Debian 12+、Ubuntu 24.04+），已安装 Docker，且 systemd 作为 PID 1 正常运行。Ubuntu 20.04/22.04、Debian 11 和 Alpine 不满足此全量部署的 Host Updater 要求；这些宿主机可在支持官方镜像的 Docker 环境中使用[仅 Web 容器方案](DEPLOYMENT.md#web-only-deployment)，并手动更新镜像。其他 CPU 架构需改用受支持的 x86_64/arm64 主机。

运行前请先检查安装脚本内容，并了解它需要管理员权限。以下命令与仓库 [README](https://github.com/Sakura520222/Sakura-AI#readme) 中的稳定频道部署入口一致：

```bash
curl -fsSL https://raw.githubusercontent.com/Sakura520222/Sakura-AI/main/start.sh | sudo bash -s -- --prod
```

安装过程包含镜像检查、数据库与沙箱组件的准备，不能等同于简单的单容器启动。需要手动下载并检查脚本时，使用[部署指南中的分步方式](DEPLOYMENT.md#full-deployment)。

::: tip 版本选择
上述命令部署正式（stable）频道。开发版可能包含未发布或仍在变更的功能，生产环境应优先选用正式版。
:::

## 方式三：使用已有数据库

已有 MySQL 和 Redis、只需要 Web 服务，或需要在 Windows/macOS 上运行的读者，请阅读[其他 Docker 方案](DEPLOYMENT.md#web-only-deployment)。

单独运行 Web 容器不等于完整 Agent 沙箱部署。Windows/macOS 的仅容器环境不提供 Linux sandboxd 和 Host Updater。

## 初始化与首次登录

1. 访问部署站点的 `/setup`，例如本机默认的 `http://localhost:8000/setup`。
2. 使用首次部署验证 Token 完成 Setup Wizard。
3. 按向导配置数据库、GitHub App、AI 模型与必要的集成选项。
4. 登录 WebUI，按实际需要管理仓库、PR 审查、Issue 分析和 Agent。

::: warning 安全提醒
验证 Token、GitHub App 私钥、API Key、数据库密码及 Telegram Bot Token 都属于敏感凭据。不要提交到 Git 仓库或放到公开文档中。
:::

## 下一步

- [配置参考](CONFIGURATION.md)：查看 WebUI 配置项及生效方式。
- [PR 功能指南](PR_FEATURES_GUIDE.md)：了解审查与依赖图。
- [Agent Harness 2.0](AGENT_HARNESS_RUNTIME.md)：了解 Agent 运行机制与限制。
- [通知中心](NOTIFICATION_CENTER.md)：配置 Telegram、邮件或 Webhook。
- [API v1 参考](api-v1-reference.md)：开发客户端或自动化集成。
