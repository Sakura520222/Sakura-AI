# 数据库与测试补充规则

## DB schema 变更
任何新增/修改 DB 列、表、索引、唯一约束、枚举值，都必须检查：
- 是否有迁移文件或项目明确依赖自动建表；
- nullable/default 是否兼容旧数据；
- 唯一约束对历史数据是否可能失败；
- 索引/约束名称是否跨数据库兼容；
- PR 描述是否说明生产升级路径。

新增 activity/event/checkpoint/log/history 表还需说明数据保留策略、清理周期、最大保留量、归档或删除任务。

## 事务与并发
- `_exists` + insert 存在竞态，DB 唯一约束冲突必须捕获并安全返回。
- webhook 与 worker 同时更新同一 task 时，要检查覆盖写、隔离级别和终态保护。
- 捕获 DB 异常后必须 rollback/close，避免 session inactive 污染后续操作。

## Fake/Mock 测试
- Fake session 必须贴近生产 SQLAlchemy 行为：过滤、排序、limit、异常、唯一冲突。
- 如果 fake 难以模拟复杂 SQL，应补充 sqlite/真实 session 轻量集成测试。
- 测试断言应引用生产常量，避免硬编码与实现漂移。

## 公共服务测试最低集
新增公共服务类需覆盖：空数据、正常流、失败流、权限/范围过滤、边界值、异常日志、关键 fallback。
