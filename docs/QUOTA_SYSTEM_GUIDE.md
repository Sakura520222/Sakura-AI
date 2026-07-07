# 配额系统指南

Sakura AI 提供基于用户配额的访问控制，用于限制和管理 PR 审查、Issue 分析、Agent 专家团队任务等高成本操作的使用量。本文档说明配额统计、自动重置和管理方式。

## 配额类型

当前配额体系分别统计 PR 审查、Issue 分析与 Agent 任务用量，并维护以下周期维度：

| 维度 | 说明 |
| --- | --- |
| 日配额 | 按 UTC 自然日统计和重置 |
| 周配额 | 按 UTC 自然周统计和重置 |
| 月配额 | 按 UTC 自然月统计和重置 |

PR、Issue 与 Agent 使用量分别记录，避免 Issue 分析或 Agent 自动修复任务消耗 PR 审查额度，或反向影响。

### Agent 配额

Agent 配额用于限制 Agent 专家团队任务创建和重试等自动修复入口。2.12.0 起，以下场景会消耗 Agent 配额：

- 普通用户在 WebUI 中从 GitHub Issue 创建 Agent 任务。
- 普通用户重试 Agent 任务。
- Issue 评论 `/agent` 委派任务时，按仓库所有者对应用户消耗 Agent 配额。

WebUI 任务入口中管理员和超级管理员跳过 Agent 配额检查；普通用户入口会先校验仓库权限，再扣减 Agent 配额，避免无权限任务消耗额度。Issue 评论 `/agent` 会按仓库所有者对应的已注册用户检查配额，若该用户是管理员或超级管理员则跳过配额扣减。

Agent 配额初始化配置项：

| 配置项 | 说明 |
| --- | --- |
| `init_admin_agent_daily_quota` / `init_admin_agent_weekly_quota` / `init_admin_agent_monthly_quota` | Setup 初始管理员 Agent 配额 |
| `init_user_agent_daily_quota` / `init_user_agent_weekly_quota` / `init_user_agent_monthly_quota` | 自注册用户基础 Agent 配额 |

## 自动重置机制

配额重置由两层机制保障：

1. **定时批量重置**
   - 服务启动后，如果调度器启用，会启动配额重置调度任务。
   - 调度器按 UTC 时间每天 00:00 执行。
   - 任务会批量检查用户的日/周/月配额字段，并重置已经过期的用量。

2. **使用时懒重置**
   - 用户触发 PR 审查、Issue 分析或 Agent 任务等需要消耗配额的操作时，系统会在检查额度前刷新该用户的过期配额。
   - 即使定时任务未执行，使用时检查也能避免长期沿用过期用量。

> 配额周期按 UTC 计算，避免部署机器本地时区差异导致重置时间不一致。

## APScheduler 说明

定时批量重置依赖 APScheduler。如果运行环境未安装 APScheduler，服务会跳过定时调度器启动并记录警告日志。

这种情况下，使用时懒重置仍会在用户操作时刷新过期额度；但没有用户触发操作的账号不会被后台批量刷新，直到下次被访问或定时器恢复。

## 管理方式

管理员可通过以下入口管理配额：

- **WebUI 用户管理**：查看用户额度、使用量和权限状态。
- **WebUI 计费/套餐管理**：管理套餐、兑换码和手动充值；WebUI 套餐可配置 PR、Issue、Agent 三类权益。
- **Telegram Bot**：通过按钮菜单和管理命令处理配额相关操作。

具体可用入口取决于当前部署配置和用户权限。

## 常见问题

### 为什么用户额度没有在本地零点刷新？

配额按 UTC 时间重置，不按服务器本地时区或用户所在时区重置。请换算到 UTC 00:00 后再检查。

### 为什么日志提示 APScheduler 未安装？

说明定时批量重置任务未启动。可以安装 APScheduler 后重启服务，或依赖使用时懒重置在用户触发操作时刷新过期配额。

### PR、Issue 和 Agent 配额是否共用？

当前 PR、Issue 与 Agent 使用量分别统计，使用 Issue 分析或 Agent 自动修复不会直接消耗 PR 审查用量。

### 周配额什么时候重置？

周配额按 UTC 自然周计算。系统会在检测到当前时间进入新的周周期后重置周用量。

## 实现参考

- `backend/services/quota_service.py`：`QuotaService` 负责用户配额检查、消耗和日/周/月重置逻辑。
- `backend/services/telegram_service.py`：Telegram 用户服务负责 PR、Issue、Agent 配额消费入口。
- `backend/services/quota_scheduler.py`：`QuotaResetScheduler` 负责 UTC 00:00 定时批量重置。
- `backend/main.py`：应用生命周期中启动和停止配额重置调度器。
- `backend/webui/routes/users.py`、`backend/webui/routes/billing.py`：WebUI 中的用户和计费管理入口。
- `backend/webui/routes/agent_team.py`：Agent 任务创建、重试、仓库权限和 Agent 配额入口。
- `backend/telegram/handlers.py`：Telegram Bot 配额相关交互入口。
