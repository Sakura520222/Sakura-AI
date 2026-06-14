# 资源生命周期与防御性编程规则

## 资源关闭循环防御

遍历列表关闭资源时，必须使用 `try...finally` 或 `contextlib.suppress` 确保单个资源关闭失败不影响剩余资源：
```python
for client in self._retired_clients:
    try:
        await client.close()
    except Exception:
        logger.warning("Failed to close client", exc_info=True)
self._retired_clients.clear()
```

## 模块级有状态缓存治理

审查中遇到模块级 `dict` 缓存（如 `_repo_locks`、对象池）时，必须同时拷问：
- 容量上限如何？是否有清理策略？
- 进程长运行时是否成为负担？
- 是否需要弱引用或定期驱逐？

## 动态参数白名单校验

从外部配置动态获取字段名用于 `getattr` 反射访问时，必须做白名单校验或在允许列表内，防止配置篡改导致非预期字段查询。

## 并发安全模式

- 模块级 `asyncio.Lock` 保护共享资源时，需检查锁的创建、清理和粒度。
- 状态机"先查询再操作"模式必须分析TOCTOU竞态风险。
- 资源字典（如 `_cancel_events`、`_background_tasks`）新增 `early return`/`finally` 时必须检查清理路径。

## 批量操作状态追踪

执行"循环应用标签/评论/文件"等批量操作时，若依赖"已有状态"做判断，必须在循环内维护 `effective_entities` 集合并实时更新，禁止仅使用初始快照。
