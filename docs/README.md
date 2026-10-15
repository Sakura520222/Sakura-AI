# Sakura AI 文档

欢迎使用 Sakura AI 官方文档。本站从当前仓库的 `docs/` 目录构建，文档内容与 Markdown 源文件保持同步。

- [文档站首页](index.md)
- [快速开始](quick-start.md)
- [项目仓库](https://github.com/Sakura520222/Sakura-AI)
- [报告文档问题](https://github.com/Sakura520222/Sakura-AI/issues/new/choose)

## 开始使用

| 文档 | 内容 |
| --- | --- |
| [快速开始](quick-start.md) | 在线体验、自行部署与初始化流程 |
| [部署指南](DEPLOYMENT.md) | Docker、源码、GitHub App、数据库、Updater 与 Agent 沙箱 |
| [配置参考](CONFIGURATION.md) | WebUI 配置项、优先级与生效方式 |
| [统一配置中心](configuration-center.md) | 配置项组织、默认值、变更生效与回滚机制 |

## 主要功能

| 文档 | 内容 |
| --- | --- |
| [PR 功能指南](PR_FEATURES_GUIDE.md) | PR 摘要、自动审查、依赖图 |
| [手动审查](MANUAL_REVIEW_FEATURE.md) | 评论命令及显式触发流程 |
| [审查批准](APPROVAL_FEATURE_SUMMARY.md) | 智能审查批准能力 |
| [审查协议](PR_REVIEW_PROTOCOL.md) | 审查输出协议与校验 |
| [模型上下文](MODEL_CONTEXT_FEATURE.md) | 上下文窗口与压缩 |
| [项目记忆（旧版参考）](SAKURA_MEMORY_GUIDE.md) | 历史记忆机制，使用时请结合当前运行代码核对 |

## Agent

| 文档 | 内容 |
| --- | --- |
| [Harness 2.0 运行机制](AGENT_HARNESS_RUNTIME.md) | 完成语义、并发、安全边界和持久化 |
| [仓库上下文和 Skills](AGENT_REPOSITORY_CONTEXT.md) | 仓库指令、技能和上下文 |
| [Agent 插件](AGENT_PLUGINS.md) | 插件能力与使用约束 |
| [Skills Python 实现参考](agent-skills-python-implementation.md) | 开发实现参考 |
| [文件操作工具实现参考](agent-file-tools-python-implementation.md) | 文件工具和操作边界 |

## 集成、账务与安全

| 文档 | 内容 |
| --- | --- |
| [统一通知中心](NOTIFICATION_CENTER.md) | WebUI、邮件、Telegram 与 Webhook 通知 |
| [Telegram 接入](TELEGRAM_SETUP.md) | Bot Token、绑定及通知端点 |
| [安全与 MFA](SECURITY_MFA_GUIDE.md) | TOTP、恢复码、Passkey |
| [配额系统](QUOTA_SYSTEM_GUIDE.md) | 配额统计与重置 |
| [Billing 2.0](billing-2.md) | 价格、权益、对账与迁移 |
| [旧权益迁移](billing-legacy-migration.md) | 历史权益和支付恢复 |

## 开发者参考

| 文档 | 内容 |
| --- | --- |
| [技术架构](ARCHITECTURE.md) | 系统架构、技术栈、模块和数据流 |
| [API v1](api-v1-reference.md) | REST API、鉴权、MFA、SSE |
| [重命名迁移](RENAME_MIGRATION_GUIDE.md) | 老版本部署或品牌迁移 |
| [贡献指南](https://github.com/Sakura520222/Sakura-AI/blob/develop/CONTRIBUTING.md) | Gitflow 和开发环境 |
| [Agent 规则](https://github.com/Sakura520222/Sakura-AI/blob/develop/AGENTS.md) | 代码贡献和自动化 Agent 约定 |

## 历史设计与验收资料

`docs/superpowers/` 中的设计和实施计划，以及部分研发验收记录属于开发工作产物，**保留在 GitHub 仓库中，但不参与公开文档站的页面生成和搜索索引**。可在 [GitHub docs 目录](https://github.com/Sakura520222/Sakura-AI/tree/develop/docs) 阅读它们。

文档发现不准确或过期时，欢迎通过文章底部的“在 GitHub 上编辑本页”提交修改；新文档请遵循 [文档维护说明](DOCS_MAINTENANCE.md)。
