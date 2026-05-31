# 状态机、Webhook、Checkpoint 规则

## 状态机变更
涉及 task/status/worker/webhook/tool_call 的 PR，必须检查状态转移矩阵：
- 新增状态、允许前置状态、成功/失败/取消后状态；
- 重复事件、乱序事件、延迟事件、终态后事件；
- 非法顺序是否拒绝或幂等处理。

新增状态变更公共方法必须测试：正常流、重复调用、非法顺序、对象不存在。

## Webhook 与异步闭环
- webhook 新增分支必须确认签名验证仍覆盖。
- 检查 action/event 过滤、缺字段 payload、安全失败、重复投递幂等。
- 延迟或过期事件应通过 head_sha、timestamp、status 等防护。
- `_exists` 预检查不能替代 DB 唯一约束；insert 时仍要捕获唯一冲突。

## Worker 资源生命周期
- `_cancel_events`、background tasks、pending prompts 等资源在终态、异常、early return、finally 中都要清理。
- finally 内的 await/DB 更新也可能失败，应独立 try/except，避免掩盖原异常。

## Checkpoint / SSE 链路
同一服务同时写 DB 与发布 SSE/WebSocket 时，必须说明：
- 是否事务提交后发布；发布失败是否影响主事务；
- DB 成功但 SSE 失败是否可由 REST snapshot 补偿；
- SSE 已发布但事务回滚如何避免幽灵事件。

REST snapshot + SSE 增量更新必须具备去重、重连恢复、乱序容忍。新增 activity/checkpoint/log/history 表必须说明保留策略、分页、大字段裁剪和清理机制。

## 应用层 seq
若 seq/order 由“查询 max + 1”生成，必须评估并发写入、唯一键冲突、锁或重试机制。
