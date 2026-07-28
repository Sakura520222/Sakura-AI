# GitHub Check Run 生命周期与身份约束

## 背景

审查执行链路为 Webhook → Worker → Check Run 服务。Check Run 同时承担 GitHub 可见状态与异步执行状态；因此其创建、接管、完成和回收是同一资源生命周期，不能按独立展示逻辑处理。

## 身份模型

以下概念不得混用：

- **执行实例 ID**：一次具体 worker/review job 的稳定持久化身份；
- **Check Run ID / external_id**：GitHub 侧资源及其与执行实例的映射；
- **占位标记**：Webhook 为即时反馈预创建的临时 Check 的来源/类型；
- **PR/head_sha/name**：定位范围字段，不足以唯一表示执行实例。

预创建的占位 Check 必须：
1. 被后续 worker 用同一稳定身份接管；或
2. 带有显式且可机器识别的占位类型，由专门回收逻辑取消并重建。

不得只因“同名、同 PR/head，且 external_id 不等于当前值”就取消活跃 Check：它可能属于并行、重试或乱序到达的合法执行实例。

## 状态与回收

典型状态：`queued → in_progress → completed`，并包含 `cancelled/failed` 逃脱路径。

- 每个中间态必须在成功、异常、超时、重试和 worker early-return 时有收敛路径；
- webhook 重放、乱序和 head 更新必须幂等；
- 同一 PR head 是否允许多个活跃 Check 必须被明确规定。若只允许一个，收敛条件应基于明确的实例/占位身份，而非排除式匹配；
- GitHub API 更新/取消失败时应记录可追踪错误，并避免留下无法接管的孤儿 Check。

## 测试最低集合

涉及创建、接管或 stale 清理的变更至少覆盖：

1. webhook 占位 Check 被正确接管或定向回收；
2. 并行/重试中的合法 Check 不会被取消；
3. 重复 webhook 与乱序事件保持幂等；
4. worker 失败、超时、重试后的终态与清理；
5. 同一 `head_sha` 下活跃 Check 的唯一性/并行策略；
6. 外部 GitHub API 失败后的残留与补偿行为。

## 设计建议

以显式字段（如 `run_instance_id`、`source_type`、`is_placeholder`）表达资源归属，优于依赖人类可读名称或字符串前缀。将状态转换与清理集中在服务层，避免 webhook 和 worker 分别以隐式规则操作同一 Check Run。