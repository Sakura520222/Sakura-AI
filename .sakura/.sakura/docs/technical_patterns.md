# 技术模式与实现细节

## 路径处理模式

### 前缀移除
```python
# 错误：使用 lstrip 按字符集语义
path.lstrip("./")  # 可能移除所有 . 和 / 字符

# 正确：使用 removeprefix 或 startswith + 切片
path.removeprefix("./")
# 或
while path.startswith("./"):
    path = path[2:]
```

### 路径别名解析
- **显式声明映射**：使用 `@/` 等路径别名时，须从 tsconfig/jsconfig/pyproject.toml 读取真实映射
- **假设条件注释**：若未读取真实映射，须在注释中说明假设条件（如"仅基于文件名后缀匹配"）

## 异步任务模式

### Fire-and-Forget 实现
```python
async def safe_create_task(coro):
    """安全创建后台任务并记录任务 ID"""
    task = asyncio.create_task(coro)
    task_id = id(task)
    logger.info(f"Created background task {task_id}")
    return task, task_id

# 使用示例
task, task_id = await safe_create_task(notify_user_async(user_id))
```

### 同步函数中的异步任务
```python
def sync_function_with_async_task():
    """同步函数中创建异步任务"""
    try:
        loop = asyncio.get_running_loop()
        loop.create_task(async_task())
    except RuntimeError:
        # 无运行循环，记录 warning 日志
        logger.warning("Cannot create async task in non-async context")
```

## 错误处理模式

### 可恢复错误定义
```python
# 使用常量集合定义可恢复错误类型
RECOVERABLE_ERRORS = {
    "max_rounds_reached_with_changes",
    "partial_failure",
    "timeout"
}

def can_review_partial_changes(error):
    return error in RECOVERABLE_ERRORS
```

### 日志规范
```python
# loguru 日志风格：使用 {} 占位符，禁止 f-string
logger.error("Error processing user {user_id}: {error}", user_id=user_id, error=str(e), exc_info=True)

# 日志级别规范
logger.info("Configuration not initialized, using default")  # 未初始化用 info
logger.warning("Configuration value invalid, using default: {value}", value=raw_value)  # 异常用 warning
```

## 配置管理模式

### 动态配置读取
```python
async def get_config_with_fallback(key, default=None):
    """获取动态配置，处理 None/非法值"""
    value = await get_dynamic_config(key)
    if value is None or not isinstance(value, int):
        logger.warning(f"Invalid config value for {key}: {value}, using default: {default}")
        return default
    return value
```

### 配置验证函数
```python
def validate_and_save_config(raw_value, min_val, max_val, default):
    """统一的配置验证和保存函数"""
    try:
        value = int(raw_value)
        if value < min_val or value > max_val:
            logger.warning(f"Config value {value} out of range [{min_val}, {max_val}], using default: {default}")
            return default
        return value
    except (ValueError, TypeError):
        logger.warning(f"Invalid config value: {raw_value}, using default: {default}")
        return default
```

## 数据库模式

### 事务边界
```python
# 服务层：不执行 commit，仅 flush
def service_method(session, data):
    session.add(data)
    session.flush()  # 不 commit
    return data

# 路由层：统一 commit
@router.post("/endpoint")
async def endpoint(data: DataModel, session: Session = Depends(get_session)):
    result = service_method(session, data)
    session.commit()
    return result
```

### 批量操作返回值
```python
def batch_operation(items):
    """批量操作返回结构化结果"""
    success = []
    skipped = []
    
    for item in items:
        try:
            process_item(item)
            success.append(item.id)
        except Exception as e:
            skipped.append({
                "id": item.id,
                "reason": {
                    "type": "processing_error",
                    "detail": str(e)
                }
            })
    
    return {
        "success": success,
        "skipped": skipped
    }
```

## 测试模式

### Mock 辅助类复用
```python
# 模块级 fixture 共享
@pytest.fixture
def mock_user_service():
    """Mock 用户服务的辅助类"""
    class MockUserService:
        def get_user(self, user_id):
            return {"id": user_id, "name": "Test User"}
    
    return MockUserService()

# 使用示例
def test_user_operation(mock_user_service):
    user = mock_user_service.get_user(123)
    assert user["id"] == 123
```

### 缓存清理
```python
@pytest.fixture(autouse=True)
def cleanup_cache():
    """自动清理全局缓存"""
    try:
        yield
    finally:
        # 清理所有缓存
        cache.clear()
```

## 前端模式

### JS 上下文转义
```html
<!-- 错误：仅使用 |e 转义 -->
<script>
    var userData = {{ user_data|e }};
</script>

<!-- 正确：使用 |tojson 转义 -->
<script>
    var userData = {{ user_data|tojson }};
</script>
```

### SVG 装饰图标
```html
<!-- 必须添加 aria-hidden 和 focusable 属性 -->
<svg aria-hidden="true" focusable="false" class="icon">
    <use href="#icon-name"></use>
</svg>
```