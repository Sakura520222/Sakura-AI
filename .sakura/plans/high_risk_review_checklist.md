# 高风险审查清单

## 元数据
- PR 描述与 diff 是否一致？是否声称文档、迁移、health API、release 内容但未体现？
- 增量审查是否清楚声明仅覆盖本轮 diff？

## 权限与范围
- `scope_user`、tenant、org、repo、owner 是否使用 `is not None`，而非 truthiness？
- Dashboard / activity / scan endpoint 是否统一权限校验？

## 时间与运行态
- duration 是否用 monotonic/perf_counter？
- timezone-aware datetime 是否与 DB/JSON/template/test 兼容？
- 多 worker 下 startup/runtime 字段语义是否说明？

## DB/状态机
- schema 变更是否有迁移或自动建表说明？
- 新增状态是否有转移矩阵和非法顺序测试？
- `_exists` + insert 是否仍捕获唯一约束冲突？

## Webhook/SSE/worker
- webhook 新分支是否仍经过签名验证？
- 重复、乱序、延迟事件是否幂等？
- DB commit 与 SSE publish 顺序/失败补偿是否明确？
- worker early return/finally 是否清理资源？

## 输出质量
- major/critical 是否与 request_changes 对齐？
- review comments 是否只包含可定位、可执行问题？
- 是否过滤摘要、评分、无问题声明和重复评论？
