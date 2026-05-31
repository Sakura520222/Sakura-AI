# Runtime、时间与 Dashboard 审查规则

## 耗时与时间语义
- 计算 duration/latency/elapsed/timeout 必须使用 `time.monotonic()` 或 `time.perf_counter()`。
- `time.time()`、`datetime.now(timezone.utc)` 只适合展示真实时间点。
- 启动信息建议同时保存：墙钟启动时间、单调时钟起点、启动耗时秒数。
- 从 `datetime.utcnow()` 迁移到 aware datetime 时，必须检查下游：DB 字段、JSON/template、naive/aware 比较、测试 mock。

## 启动与运行态信息
- 多 worker 部署下，模块级启动时间通常表示“当前 worker”，不是全局服务启动时间；API/文档应说明语义。
- health/dashboard API 新增字段须保持旧字段、状态码、结构兼容。
- 字段契约要稳定：如 `startup_duration_seconds` 用秒、数字、非负。

## main.py 依赖方向
- 路由、服务、工具模块不应依赖 `backend.main` 获取业务函数或运行态数据。
- 若出现 `from backend.main import ...`，优先抽取到 `backend/core/runtime.py`、`backend/services/system_info.py` 或工具模块。
- 延迟导入不能掩盖依赖方向错误；若确需延迟导入，必须注释循环依赖原因。

## Dashboard / system-info
- API 与 WebUI 返回相同系统信息时，应复用同一服务函数，避免两端重复构造 dict。
- 前端 fetch 必须检查 `response.ok`，缺字段/None 应降级展示。
- 后端已有格式化字符串时，前端不应重复实现不同格式规则，除非测试覆盖边界。
