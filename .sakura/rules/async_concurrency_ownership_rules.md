# 异步并发与所有权状态机规则

## 一、所有权锁与单一真值源规则 (Single Source of Ownership Truth)

### 1.1 严禁内联就地重写所有权判定 SQL
1. **单一真值源约束**：严禁在各业务服务（如 Reconciliation、Webhook Dispatch、Worker 等）中私自内联 `ServiceExecutionOwnership` 的活性判定 SQL。凡涉及任务抢占、所有权存活或租期过期的检查，必须强制统一调用 Capacity/Ownership 核心模块提供的公共接口。
2. **白名单状态匹配优先于黑名单**：状态判定必须采用显式白名单机制（如 `state.in_(("queued", "running"))`），严禁使用黑名单过滤（如 `state != "released"`）。黑名单逻辑在系统拓展新中间态或终态（如 `cancelled`、`timed_out`、`draining`）时极易被静默击穿导致并发状态撕裂。
3. **存在性查询物理限制**：使用 ORM 对活跃所有权做存在性探活时，强制追加 `.limit(1)` 或使用 `exists()` 语义，禁止使用 `.scalars().all()` 载入内存判定，防止高频探活引发性能与内存退化。

---

## 二、异步取消排空 (Cancellation Drain) 观测性规则

### 2.1 任务取消与排空异常保留
1. **禁止完全吞没异常诊断链**：在使用 `asyncio.shield` 循环排空长耗时或子进程任务时，若底层操作抛出异常并被捕获，必须在 `break` 并重抛 `asyncio.CancelledError` 前记录一条上下文诊断日志（`WARNING`/`ERROR`），严禁完全吞没底层故障信息。
2. **取消信号的生命周期优先**：必须保障 `CancelledError` 的正确传播，不可让排空期间业务异常覆盖协程取消语义，但必须保留诊断证据以供链路追踪。

---

## 三、检查点恢复与反序列化防御一致性 (Checkpoint Consistency Guard)

### 3.1 恢复反序列化同级防御
1. **检验与消费同级防御**：在恢复历史检查点或反序列化会话状态时，下游数据投影（Projection/Reconstruction）必须与前置 Validator 保持完全同等级别的防御策略（统一采用 `.get()` 或安全 Schema 解码），禁止因前置校验漏判而在下游硬下标访问（如 `call["function"]`）导致 `KeyError`/`TypeError`。
2. **废弃工具/模型防悬挂**：废弃历史持久化字段或运行时工具时，必须确保存量长周期未决任务或检查点回放不会触发未捕获的空指针或硬访问崩溃。
