# Sakura AI 文档

> 完整文档索引。入门请先读 [README](../README.md)，部署参考 [部署指南](DEPLOYMENT.md)。

---

## 快速上手

| 文档 | 说明 |
|---|---|
| [部署指南](DEPLOYMENT.md) | Docker 镜像、源码部署、GitHub App、数据库、Setup Wizard、WebUI 应用更新与 Host Updater 管理 |
| [配置参考](CONFIGURATION.md) | 全部配置项的位置、键名与说明 |
| [技术架构](ARCHITECTURE.md) | 整体架构图、技术栈、代码结构、客户端、交互式知识图谱 |

## 功能详解

| 文档 | 说明 |
|---|---|
| [审查协议规范](PR_REVIEW_PROTOCOL.md) | `<SAKURA_REVIEW>` 标签化审查输出协议、字段校验与修复降级 |
| [审查批准功能](APPROVAL_FEATURE_SUMMARY.md) | 智能审查批准系统详细说明 |
| [手动审查功能](MANUAL_REVIEW_FEATURE.md) | 超级管理员手动触发审查 |
| [模型上下文管理](MODEL_CONTEXT_FEATURE.md) | AI 模型上下文和压缩功能 |
| [PR 功能指南](PR_FEATURES_GUIDE.md) | PR 变更总结与依赖图配置说明 |
| [项目记忆系统使用指南](SAKURA_MEMORY_GUIDE.md) | `.sakura/` 目录结构、生命周期与配置说明 |
| [Agent Skills 实现](agent-skills-python-implementation.md) | Skills 安装、索引、启停与工具集成说明 |
| [Agent 文件工具实现](agent-file-tools-python-implementation.md) | Agent 工作区文件工具、安全边界与实现细节 |

## 集成与运维

| 文档 | 说明 |
|---|---|
| [Telegram Bot 集成指南](TELEGRAM_SETUP.md) | Bot 设置、权限体系、命令参考 |
| [安全与 MFA 指南](SECURITY_MFA_GUIDE.md) | TOTP、恢复码、Passkeys/WebAuthn 与安全中心 |
| [API v1 参考文档](api-v1-reference.md) | RESTful API v1 接口文档（移动端 OAuth、MFA、SSE、Billing） |
| [配额系统指南](QUOTA_SYSTEM_GUIDE.md) | PR / Issue 配额统计与自动重置机制 |

## 其他

| 文档 | 说明 |
|---|---|
| [改名迁移指南](RENAME_MIGRATION_GUIDE.md) | 项目品牌改名与配置迁移说明 |
| [贡献者约定](../AGENTS.md) | 自动化代理与贡献者项目约定（根目录） |

---

> `superpowers/` 目录是开发流程工作产物（设计稿、实施计划），不属于用户文档。

*最后更新：2026-8-10 · 发现错误？[提 Issue](https://github.com/Sakura520222/Sakura-AI/issues)*
