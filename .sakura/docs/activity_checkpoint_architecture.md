# Activity Checkpoint 架构

## 架构概述

Activity Checkpoint 是将运行过程持久化并实时展示的状态同步系统，而非简单的事件日志。

## 核心链路

```
Worker/Analyzer 
  → Event Callback 
  → CheckpointService 
  → DB Persistence 
  → SSE Publish 
  → Activity Route 
  → WebUI Display
```

## 核心组件

### CheckpointService
- **职责**：状态持久化 + SSE 发布
- **关键方法**：
  - `append_message()`
  - `mark_tool_call_running/completed/failed()`
  - `_publish()` SSE 事件

### 数据模型
- **activity_sessions**：会话级别状态
- **activity_messages**：消息序列
- **activity_tool_calls**：工具调用状态
- **UniqueConstraint**：保证 session 内 seq 唯一

### 前端同步
- **REST Snapshot**：初始化加载完整状态
- **SSE Incremental**：实时增量更新
- **去重与恢复**：处理重连、乱序、重复事件

## 关键设计决策

### 事务与事件顺序
- **推荐**：先提交数据库事务，再发布 SSE
- **理由**：避免前端看到幽灵数据
- **补偿**：SSE 发布失败仅记录日志，不影响主流程

### Seq 生成策略
- **数据库自增**：原生保证并发安全
- **应用层生成**：须评估并发冲突，使用锁或重试机制

### 数据保留策略
- **问题**：activity 表增长速度快
- **方案**：
  - 定期清理任务
  - 按完成时间归档
  - 大字段截断
  - 分页查询限制

## 状态机设计

### Tool Call 状态
- `created` → `running` → `completed/failed`
- **非法转换**：completed → failed, failed → completed
- **并发保护**：重复 tool_call id 冲突处理

### Session 状态
- `active` → `completed` → `archived`
- **幂等性**：重复标记相同状态不报错

## 测试要点

- 状态转换（正常/非法/重复/不存在）
- 并发写入（seq 冲突）
- 事务顺序（DB ↔ SSE）
- 前端恢复（重连/去重/初始化）
- 性能（大量 tool_calls 处理）
