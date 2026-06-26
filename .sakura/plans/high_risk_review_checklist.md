# 高风险审查清单

## 元数据
- PR 描述与 diff 是否一致？是否声称文档、迁移、health API、release 内容但未体现？
- 描述与实际变更的架构影响是否存在量级差异？（描述说"优化配置"实际是架构变更=major）
- 增量审查是否清楚声明仅覆盖本轮 diff？

## 权限与范围
- `scope_user`、tenant、org、repo、owner 是否使用 `is not None`，而非 truthiness？
- Dashboard / activity / scan endpoint 是否统一权限校验？

## 时间与运行态
- duration 是否用 monotonic/perf_counter？
- timezone-aware datetime 是否与 DB/JSON/template/test 兼容？

## DB/状态机
- schema 变更是否有迁移或自动建表说明？
- 新增状态是否有转移矩阵和非法顺序测试？
- **新增DB表须对照查询验证索引覆盖**
- **状态机每个中间状态必须有超时/异常逃脱路径，无逃脱=major**

## Webhook/SSE/worker
- webhook 新分支是否仍经过签名验证？
- 重复、乱序、延迟事件是否幂等？
- worker early return/finally 是否清理资源？
- **队列服务固定维度：入队幂等、消费并发、消息确认/重试、死信处理、积压监控**

## 缓存与状态
- 实例级缓存是否有显式生命周期声明？
- 布尔标志参数（如finalize）是否全路径传播验证？
- 终态方法是否调用了缓存清理逻辑？
- 缓存作为状态层是否有逃脱路径（crash恢复、重启冷启动）？

## 提示词/格式契约
- 提示词格式约束修改是否追踪了下游解析代码的隐式假设？
- 格式解析失败是否有独立降级？回退值是否标记"分析失败"？
- 外部内容嵌入GitHub markdown须评估注入破坏渲染结构

## 输出质量
- major/critical 是否与 request_changes 对齐？
- review comments 是否只包含可定位、可执行问题？
- 是否过滤摘要、评分、无问题声明和重复评论？
- **存在历史major未清零时评分≤8**
