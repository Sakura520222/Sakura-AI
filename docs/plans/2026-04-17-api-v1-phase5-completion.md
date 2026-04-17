# API v1 Phase 5: 补全所有缺失功能

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** 将 WebUI 所有尚未 API 化的功能补齐，包括 Setup Wizard、限流安全、扫描/队列管理增强、主题设置、版本信息等。

**Architecture:** 在现有 `backend/api/v1/` 模块上扩展。新增 `setup.py` 模块（免认证），修改已有模块增加缺失端点。slowapi 限流作为中间件集成到 `__init__.py`。

**Tech Stack:** FastAPI, slowapi, SQLAlchemy(async), Pydantic, httpx

---

## Task 1: Setup Wizard API（新增模块）

**Files:**
- Create: `backend/api/v1/setup.py`
- Modify: `backend/api/v1/__init__.py:5-23`（注册 setup router）

**Step 1: 创建 `backend/api/v1/setup.py`**

Setup Wizard 是免认证的（bootstrap 模式下系统还没有用户），所以不使用 `require_api_auth` 依赖。复用 `backend.core.setup_service.setup_service` 单例。

```python
"""API v1 Setup Wizard 端点（免认证，仅在 bootstrap 模式下可用）"""

from fastapi import APIRouter
from pydantic import BaseModel
from typing import Optional

from backend.core.bootstrap import is_bootstrap_mode
from backend.core.setup_service import setup_service

from backend.api.v1.responses import success_response, error_response

router = APIRouter(prefix="/setup", tags=["Setup"])


class TestConnectionRequest(BaseModel):
    """连接测试请求"""
    type: str  # database, redis, github, openai, telegram
    # database
    database_url: Optional[str] = None
    # redis
    redis_url: Optional[str] = None
    # github
    app_id: Optional[str] = None
    private_key: Optional[str] = None
    # openai
    api_key: Optional[str] = None
    api_base: Optional[str] = None
    # telegram
    bot_token: Optional[str] = None


class SaveStepRequest(BaseModel):
    """保存配置步骤请求"""
    values: dict[str, str]


class CompleteSetupRequest(BaseModel):
    """完成 Setup 请求"""
    # 所有配置项
    DATABASE_URL: Optional[str] = None
    REDIS_URL: Optional[str] = None
    GITHUB_APP_ID: Optional[str] = None
    GITHUB_PRIVATE_KEY: Optional[str] = None
    GITHUB_WEBHOOK_SECRET: Optional[str] = None
    OPENAI_API_KEY: Optional[str] = None
    OPENAI_API_BASE: Optional[str] = None
    OPENAI_MODEL: Optional[str] = None
    TELEGRAM_BOT_TOKEN: Optional[str] = None
    APP_DOMAIN: Optional[str] = None
    APP_PORT: Optional[str] = None
    LOG_LEVEL: Optional[str] = None
    ADMIN_GITHUB_USERNAME: Optional[str] = None
    ADMIN_TELEGRAM_ID: Optional[str] = None
    GITHUB_OAUTH_CLIENT_ID: Optional[str] = None
    GITHUB_OAUTH_CLIENT_SECRET: Optional[str] = None
    GITHUB_OAUTH_REDIRECT_URI: Optional[str] = None
    EMBEDDING_API_KEY: Optional[str] = None
    EMBEDDING_BASE_URL: Optional[str] = None
    EMBEDDING_MODEL: Optional[str] = None
    EMBEDDING_PROVIDER: Optional[str] = None
    EMBEDDING_DIMENSION: Optional[str] = None
    RERANK_API_KEY: Optional[str] = None
    RERANK_BASE_URL: Optional[str] = None
    RERANK_MODEL: Optional[str] = None
    RERANK_PROVIDER: Optional[str] = None


def _check_bootstrap():
    """检查是否处于 bootstrap 模式"""
    if not is_bootstrap_mode():
        return error_response("系统已完成初始化，Setup Wizard 不可用", status_code=403)
    return None


@router.get("/state")
async def get_setup_state():
    """获取当前 Setup 状态"""
    from backend.core.setup_service import ENV_FIELD_GROUPS

    if not is_bootstrap_mode():
        return success_response(data={
            "state": "completed",
            "current_step": -1,
            "missing_fields": [],
        })

    # 获取缺失字段
    from backend.core.config import get_settings
    settings = get_settings()
    missing = []
    for group, fields in ENV_FIELD_GROUPS.items():
        for field in fields:
            val = getattr(settings, field.lower(), None)
            if not val:
                missing.append(field)

    # 确定当前步骤
    step_order = ["database", "github", "ai", "rag", "admin"]
    current_step = 0
    for i, step in enumerate(step_order):
        step_fields = ENV_FIELD_GROUPS.get(step, [])
        if any(f in missing for f in step_fields):
            current_step = i
            break
    else:
        current_step = len(step_order) - 1

    return success_response(data={
        "state": "in_progress",
        "current_step": current_step,
        "missing_fields": missing,
        "field_groups": ENV_FIELD_GROUPS,
    })


@router.post("/test-connection")
async def test_connection(body: TestConnectionRequest):
    """测试各类连接配置"""
    bootstrap_error = _check_bootstrap()
    if bootstrap_error:
        return bootstrap_error

    if body.type == "database":
        result = await setup_service.test_database_connection(body.database_url or "")
    elif body.type == "redis":
        result = await setup_service.test_redis_connection(body.redis_url or "")
    elif body.type == "github":
        result = await setup_service.test_github_app(
            body.app_id or "", body.private_key or ""
        )
    elif body.type == "openai":
        result = await setup_service.test_openai_api(
            body.api_key or "", body.api_base or ""
        )
    elif body.type == "telegram":
        result = await setup_service.test_telegram_bot(body.bot_token or "")
    else:
        return error_response(f"不支持的测试类型: {body.type}", status_code=400)

    return success_response(data=result)


@router.post("/save-step")
async def save_step(body: SaveStepRequest):
    """保存单步配置"""
    bootstrap_error = _check_bootstrap()
    if bootstrap_error:
        return bootstrap_error

    try:
        values = body.values

        # 如果包含 DATABASE_URL，先初始化数据库
        if "DATABASE_URL" in values:
            db_url = values["DATABASE_URL"]
            test_result = await setup_service.test_database_connection(db_url)
            if not test_result["success"]:
                return error_response(test_result["message"], status_code=400)
            await setup_service.init_database(db_url)

        saved = await setup_service.save_configs_to_db(values)
        return success_response(
            data={"saved_count": saved},
            message=f"已保存 {saved} 项配置",
        )
    except Exception as e:
        return error_response(f"保存失败: {e}", status_code=500)


@router.post("/complete")
async def complete_setup(body: CompleteSetupRequest):
    """完成 Setup 全流程"""
    bootstrap_error = _check_bootstrap()
    if bootstrap_error:
        return bootstrap_error

    all_config = {
        k: str(v)
        for k, v in body.model_dump().items()
        if v is not None
    }

    result = await setup_service.complete_setup(all_config)

    if result["success"]:
        # 异步触发重启
        import asyncio
        asyncio.get_event_loop().call_later(
            2, setup_service.trigger_restart
        )

    if result["success"]:
        return success_response(data=result, message=result["message"])
    else:
        return error_response(result["message"], status_code=400)
```

**Step 2: 在 `__init__.py` 注册 setup router**

在 import 区添加 `setup`，在 router 注册区添加 `api_v1_router.include_router(setup.router)`。

---

## Task 2: 限流中间件（slowapi 集成）

**Files:**
- Modify: `backend/api/v1/__init__.py`（添加 limiter）

**Step 1: 在 `__init__.py` 中集成 slowapi**

```python
"""API v1 路由"""

from fastapi import APIRouter, Request
from slowapi import Limiter, _rate_limit_exceeded_handler
from slowapi.util import get_remote_address
from slowapi.errors import RateLimitExceeded

from backend.api.v1 import (
    auth, dashboard, reviews, issues, users, repos,
    config, logs, queue, scans, settings, events,
    setup,
)

# 限流器：按客户端 IP 限流
limiter = Limiter(key_func=get_remote_address)

api_v1_router = APIRouter()

# 限流异常处理
api_v1_router.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)

# 免认证模块
api_v1_router.include_router(setup.router)

# 需认证模块
api_v1_router.include_router(auth.router)
api_v1_router.include_router(dashboard.router)
api_v1_router.include_router(reviews.router)
api_v1_router.include_router(issues.router)
api_v1_router.include_router(users.router)
api_v1_router.include_router(repos.router)
api_v1_router.include_router(config.router)
api_v1_router.include_router(logs.router)
api_v1_router.include_router(queue.router)
api_v1_router.include_router(scans.router)
api_v1_router.include_router(settings.router)
api_v1_router.include_router(events.router)


@api_v1_router.get("/health", tags=["Health"])
@limiter.limit("10/second")
async def api_health(request: Request):
    """API v1 健康检查"""
    return {"status": "ok", "version": "v1"}
```

**Step 2: 为关键端点添加限流装饰器**

在以下端点上添加 `@limiter.limit(...)` 装饰器（注意 slowapi 需要将 `request: Request` 作为第一个参数）：

| 端点 | 限流规则 |
|------|----------|
| `POST /auth/callback` | `5/minute` |
| `POST /auth/logout` | `10/minute` |
| `POST /setup/test-connection` | `10/minute` |
| `POST /setup/complete` | `3/minute` |
| `POST /scans/trigger` | `3/minute` |
| `GET /health` | `10/second` |

其他端点使用默认不限流（内部使用需要灵活性）。

---

## Task 3: 扫描管理增强

**Files:**
- Modify: `backend/api/v1/scans.py`

**Step 1: 修复 trigger 端点，匹配 WebUI 完整逻辑**

当前 API 的 `trigger_scan` 简化了 WebUI 的逻辑（没获取候选仓库、没处理冷却期）。需要重写为与 WebUI 一致的行为：

```python
@router.post("/trigger")
async def trigger_scan(
    user: dict = Depends(require_api_super_admin),
):
    """手动触发扫描（超级管理员）"""
    import asyncio
    from backend.workers.scan_worker import ScanWorker

    try:
        worker = ScanWorker()
        result = await worker.get_scan_candidates()
        candidates = result["candidates"]

        if not candidates:
            total_active = result["total_active"]
            if total_active == 0:
                message = "当前无已安装的仓库，请确保 GitHub App 已安装到目标仓库"
            else:
                cooldown_hours = result["cooldown_hours"]
                message = (
                    f"所有 {total_active} 个仓库均在冷却期内"
                    f"（{cooldown_hours} 小时），请稍后重试"
                )
            return error_response(message, status_code=400)

        triggered = []
        for repo_name in candidates[:5]:
            try:
                scan_id = await worker.create_scan_record(
                    repo_name=repo_name,
                    trigger_type="manual_api",
                    triggered_by=f"api:{user['sub']}",
                )
                asyncio.create_task(worker.process_scan(scan_id))
                triggered.append({"repo": repo_name, "scan_id": scan_id})
            except Exception as e:
                from loguru import logger
                logger.error(f"触发扫描失败 ({repo_name}): {e}")

        return success_response(
            data={"triggered": triggered, "count": len(triggered)},
            message=f"已触发 {len(triggered)} 个仓库扫描",
        )
    except Exception as e:
        return error_response(str(e), status_code=500)
```

**Step 2: 添加扫描重试端点**

```python
@router.post("/{scan_id}/retry")
async def retry_scan(
    scan_id: int,
    db: AsyncSession = Depends(get_db),
    user: dict = Depends(require_api_super_admin),
):
    """重试失败的扫描"""
    import asyncio
    from backend.workers.scan_worker import ScanWorker

    result = await db.execute(select(RepoScan).where(RepoScan.id == scan_id))
    scan = result.scalar_one_or_none()
    if not scan:
        return error_response("扫描记录不存在", status_code=404)
    if scan.status not in ("failed",):
        return error_response("只能重试失败的扫描", status_code=400)

    # 重置状态并重新执行
    scan.status = "pending"
    scan.error_message = None
    await db.commit()

    try:
        worker = ScanWorker()
        asyncio.create_task(worker.process_scan(scan_id))
        return success_response(message="扫描已重新触发")
    except Exception as e:
        return error_response(str(e), status_code=500)
```

**Step 3: 添加扫描取消端点**

```python
@router.post("/{scan_id}/cancel")
async def cancel_scan(
    scan_id: int,
    db: AsyncSession = Depends(get_db),
    user: dict = Depends(require_api_super_admin),
):
    """取消正在进行的扫描"""
    result = await db.execute(select(RepoScan).where(RepoScan.id == scan_id))
    scan = result.scalar_one_or_none()
    if not scan:
        return error_response("扫描记录不存在", status_code=404)
    if scan.status not in ("pending", "indexing", "analyzing", "reporting"):
        return error_response("只能取消进行中的扫描", status_code=400)

    scan.status = "cancelled"
    scan.error_message = "用户手动取消"
    await db.commit()

    return success_response(message="扫描已取消")
```

---

## Task 4: 队列管理增强

**Files:**
- Modify: `backend/api/v1/queue.py`

**Step 1: 添加队列项详情端点**

```python
@router.get("/items/{item_id}")
async def get_queue_item(
    item_id: int,
    db: AsyncSession = Depends(get_db),
    user: dict = Depends(require_api_admin),
):
    """队列项详情"""
    result = await db.execute(
        select(ReviewQueue).where(ReviewQueue.id == item_id)
    )
    item = result.scalar_one_or_none()
    if not item:
        return error_response("队列项不存在", status_code=404)

    return success_response(data={
        "id": item.id,
        "pr_id": item.pr_id,
        "repo_name": item.repo_name,
        "action": item.action,
        "priority": item.priority,
        "status": item.status,
        "retry_count": item.retry_count,
        "max_retries": item.max_retries,
        "error_message": item.error_message,
        "created_at": item.created_at.isoformat() if item.created_at else None,
        "updated_at": item.updated_at.isoformat() if item.updated_at else None,
    })
```

**Step 2: 添加队列项重试端点**

```python
@router.post("/items/{item_id}/retry")
async def retry_queue_item(
    item_id: int,
    db: AsyncSession = Depends(get_db),
    user: dict = Depends(require_api_admin),
):
    """重试失败的队列项"""
    result = await db.execute(
        select(ReviewQueue).where(ReviewQueue.id == item_id)
    )
    item = result.scalar_one_or_none()
    if not item:
        return error_response("队列项不存在", status_code=404)
    if item.status != "failed":
        return error_response("只能重试失败的队列项", status_code=400)

    item.status = "pending"
    item.error_message = None
    await db.commit()

    return success_response(message="队列项已重新加入队列")
```

**Step 3: 添加队列项删除端点**

```python
@router.delete("/items/{item_id}")
async def delete_queue_item(
    item_id: int,
    db: AsyncSession = Depends(get_db),
    user: dict = Depends(require_api_admin),
):
    """删除队列项"""
    result = await db.execute(
        select(ReviewQueue).where(ReviewQueue.id == item_id)
    )
    item = result.scalar_one_or_none()
    if not item:
        return error_response("队列项不存在", status_code=404)

    await db.delete(item)
    await db.commit()

    return success_response(message="队列项已删除")
```

---

## Task 5: 主题设置写入

**Files:**
- Modify: `backend/api/v1/settings.py`
- Modify: `backend/api/v1/auth.py`（移除只读 theme 端点，迁移到 settings）

**Step 1: 在 `settings.py` 的 `update_settings` 中添加 theme 支持**

修改 `update_settings` 函数，接受 `theme` 字段：

```python
@router.patch("")
async def update_settings(
    body: dict,
    user: dict = Depends(require_api_auth),
    db: AsyncSession = Depends(get_db),
):
    """更新个人偏好设置"""
    theme = body.get("theme")
    items_per_page = body.get("items_per_page")
    language = body.get("language")

    result = await db.execute(
        select(WebUIConfig).where(WebUIConfig.user_id == user["user_id"])
    )
    config = result.scalar_one_or_none()

    if config:
        if theme is not None and theme in ("light", "dark", "system"):
            config.theme = theme
        if items_per_page is not None:
            config.items_per_page = int(items_per_page)
        if language is not None:
            config.language = language
    else:
        config = WebUIConfig(
            user_id=user["user_id"],
            theme=theme if theme in ("light", "dark", "system") else "system",
            language=language or "zh-CN",
            items_per_page=int(items_per_page) if items_per_page else 20,
        )
        db.add(config)

    await db.commit()
    invalidate_user_prefs_cache(user["user_id"])

    return success_response(message="设置已更新")
```

**Step 2: 从 `auth.py` 移除只读 theme 端点**

删除 `auth.py` 中的 `GET /auth/theme` 端点（已被 `settings.py` 的 `GET /settings` 包含）。

---

## Task 6: 版本/关于信息端点

**Files:**
- Modify: `backend/api/v1/settings.py`（或新建独立端点）

**Step 1: 在 settings.py 添加 about 端点**

```python
@router.get("/about")
async def get_about(
    user: dict = Depends(require_api_auth),
):
    """获取系统版本信息"""
    from datetime import datetime
    from backend.webui.routes.auth import APP_VERSION

    return success_response(data={
        "version": APP_VERSION,
        "build_date": datetime.utcnow().strftime("%Y-%m-%d"),
    })
```

---

## Task 7: 操作日志详情端点

**Files:**
- Modify: `backend/api/v1/logs.py`

**Step 1: 添加操作日志详情端点**

当前只有 `GET /logs/actions` 列表，缺少单条详情。同时补充 WebUI 中的 admin username 关联查询：

```python
@router.get("/actions/{log_id}")
async def get_action_log(
    log_id: int,
    db: AsyncSession = Depends(get_db),
    user: dict = Depends(require_api_admin),
):
    """操作日志详情"""
    result = await db.execute(
        select(AdminActionLog, TelegramUser.github_username)
        .outerjoin(TelegramUser, AdminActionLog.admin_id == TelegramUser.id)
        .where(AdminActionLog.id == log_id)
    )
    row = result.first()
    if not row:
        return error_response("操作日志不存在", status_code=404)

    log, admin_name = row
    return success_response(data={
        "id": log.id,
        "admin_id": log.admin_id,
        "admin_username": admin_name,
        "action": log.action,
        "target_type": log.target_type,
        "target_id": log.target_id,
        "detail": log.detail,
        "created_at": log.created_at.isoformat() if log.created_at else None,
    })
```

需要额外 import `TelegramUser`。

---

## Task 8: Ruff 代码检查 + 注册验证

**Step 1: 运行 ruff 检查所有新增/修改文件**

```bash
ruff check backend/api/v1/
```

**Step 2: 启动 FastAPI 应用，验证所有端点注册成功**

```bash
python -c "from backend.api.v1 import api_v1_router; print(len(api_v1_router.routes))"
```

预期：路由数量从 54 增加到 66。

---

## 执行顺序

1. Task 1: Setup Wizard API（新模块）
2. Task 2: 限流中间件
3. Task 3: 扫描管理增强
4. Task 4: 队列管理增强
5. Task 5: 主题设置写入
6. Task 6: 版本信息端点
7. Task 7: 操作日志详情
8. Task 8: Ruff 检查 + 验证

## 新增端点汇总

| # | 方法 | 路径 | 说明 | 权限 |
|---|------|------|------|------|
| 1 | GET | /setup/state | 获取 Setup 状态 | 免认证 |
| 2 | POST | /setup/test-connection | 测试连接 | 免认证 |
| 3 | POST | /setup/save-step | 保存配置步骤 | 免认证 |
| 4 | POST | /setup/complete | 完成 Setup | 免认证 |
| 5 | POST | /scans/{id}/retry | 重试扫描 | 超级管理员 |
| 6 | POST | /scans/{id}/cancel | 取消扫描 | 超级管理员 |
| 7 | GET | /queue/items/{id} | 队列项详情 | 管理员 |
| 8 | POST | /queue/items/{id}/retry | 重试队列项 | 管理员 |
| 9 | DELETE | /queue/items/{id} | 删除队列项 | 管理员 |
| 10 | POST | /queue/purge | 批量清理队列项 | 管理员 |
| 11 | GET | /settings/about | 版本信息 | 认证用户 |
| 12 | GET | /logs/actions/{id} | 操作日志详情 | 管理员 |
| 13 | GET | /reviews/{id}/files/{path} | 文件级审查评论 | 认证用户 |

**移除冗余端点：** `GET /auth/theme`（已合并到 `GET /settings`）

**最终：54 - 1（移除）+ 13（新增）= 66 个端点**
