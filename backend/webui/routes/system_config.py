"""WebUI 系统核心配置路由（超级管理员专用）

管理系统基础设施配置：数据库、Redis、GitHub App、GitHub OAuth、
Telegram Bot、WebUI 安全等。这些配置通常在 Setup Wizard 首次部署时设置，
此页面允许超级管理员在运行时修改。
"""
import asyncio

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse
from loguru import logger
from sqlalchemy.ext.asyncio import AsyncSession

from backend.core.config import get_settings
from backend.core.time_service import (
    InvalidTimezoneError,
    format_rfc3339,
    get_localzone_name,
    get_time_service,
    now_utc,
    resolve_timezone,
)
from backend.services.database_reset_runtime_service import (
    quiesce_database_reset_runtime,
)
from backend.services.database_reset_service import (
    DATABASE_RESET_CONFIRMATION,
    DatabaseResetError,
    database_reset_service,
)
from backend.services.system_config_service import (
    SYSTEM_CONFIG_GROUPS,
    SYSTEM_SENSITIVE_KEYS,
    system_config_service,
)
from backend.webui.deps import (
    get_csrf_serializer,
    get_db,
    get_user_preferences,
    render_template,
    require_csrf,
    require_csrf_header,
    require_super_admin,
    toast_redirect,
)
from backend.webui.helpers.admin_log import log_admin_action
from backend.webui.i18n import detect_language, make_translation_func

router = APIRouter(prefix="/system-config", tags=["WebUI System Config"])


@router.get("/")
async def system_config_page(
    request: Request,
    db: AsyncSession = Depends(get_db),
    user: dict = Depends(require_super_admin),
    user_prefs: dict = Depends(get_user_preferences),
):
    """渲染系统核心配置页面"""
    groups, _ = await system_config_service.load_grouped_configs(db)
    time_service = get_time_service()
    try:
        detected_system_timezone = get_localzone_name()
    except Exception:
        detected_system_timezone = "unavailable"
    time_status = {
        "configured_timezone": get_settings().app_timezone,
        "resolved_timezone": time_service.resolved_timezone,
        "system_timezone": detected_system_timezone,
        "current_utc": format_rfc3339(now_utc()),
        "current_local": time_service.format_display(now_utc()),
    }

    return render_template(
        "system_config.html",
        request,
        user_prefs=user_prefs,
        current_user=user,
        csrf_token=get_csrf_serializer().dumps({}),
        active_page="system_config",
        groups=groups,
        database_reset_confirmation=DATABASE_RESET_CONFIRMATION,
        time_status=time_status,
    )


@router.post("/save")
async def save_system_config(
    request: Request,
    db: AsyncSession = Depends(get_db),
    user: dict = Depends(require_super_admin),
    csrf_token: str = Depends(require_csrf),
):
    """保存系统核心配置"""
    try:
        form = await request.form()

        # 收集所有系统配置键
        all_system_keys = set()
        for group_def in SYSTEM_CONFIG_GROUPS:
            all_system_keys.update(group_def["keys"])

        # 收集并验证待更新的配置
        updates: dict[str, str] = {}
        for key in all_system_keys:
            is_sensitive = key in SYSTEM_SENSITIVE_KEYS

            # 敏感字段：检查 _changed 标记
            if is_sensitive:
                changed_flag = form.get(f"{key}_changed")
                if changed_flag != "true":
                    continue

            raw = form.get(key)
            if raw is None:
                continue

            val = str(raw).strip()
            if not val:
                continue

            # 数据库连接字符串验证（接受所有可规范化的异步驱动格式）
            if key == "database_url":
                if not val.startswith(
                    (
                        "mysql+aiomysql://",
                        "mysql+asyncmy://",
                        "mysql://",
                        "postgresql+asyncpg://",
                        "postgresql://",
                    )
                ):
                    return toast_redirect(
                        "/system-config/",
                        "system_config.invalid_db_url",
                        "error",
                        lang=detect_language(),
                    )

            # 端口号验证
            if key == "app_port":
                try:
                    port = int(val)
                    if not (1 <= port <= 65535):
                        raise ValueError
                    val = str(port)
                except ValueError, TypeError:
                    return toast_redirect(
                        "/system-config/",
                        "system_config.invalid_port",
                        "error",
                        lang=detect_language(),
                    )

            # 日志级别验证
            if key == "log_level":
                valid_levels = {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}
                if val.upper() not in valid_levels:
                    return toast_redirect(
                        "/system-config/",
                        "system_config.invalid_log_level",
                        "error",
                        lang=detect_language(),
                    )
                val = val.upper()

            if key == "app_timezone":
                try:
                    resolve_timezone(val)
                except InvalidTimezoneError:
                    return toast_redirect(
                        "/system-config/",
                        "system_config.invalid_timezone",
                        "error",
                        lang=detect_language(),
                    )

            updates[key] = val

        if not updates:
            return toast_redirect(
                "/system-config/",
                "toast.config_no_change",
                lang=detect_language(),
            )

        # 通过 Service 层写入数据库
        changed, needs_restart = await system_config_service.save_configs(db, updates)

        if not changed:
            return toast_redirect(
                "/system-config/",
                "toast.config_no_change",
                lang=detect_language(),
            )

        # 同步 Settings 单例
        await system_config_service.apply_live_settings(changed)

        logger.info(
            f"系统核心配置已更新, by={user['sub']}, changed={list(changed.keys())}"
        )

        # 记录审计日志
        log_changed = system_config_service.build_audit_log(changed)
        await log_admin_action(
            db, user["user_id"], "config_save", "system_core", None, log_changed
        )

        if needs_restart:
            return toast_redirect(
                "/system-config/",
                "system_config.saved_restart_required",
                lang=detect_language(),
            )
        return toast_redirect(
            "/system-config/",
            "system_config.saved",
            lang=detect_language(),
        )

    except ValueError:
        return toast_redirect(
            "/system-config/",
            "toast.invalid_param",
            "error",
            lang=detect_language(),
        )
    except Exception as e:
        logger.error(f"系统核心配置保存失败: {e}", exc_info=True)
        return toast_redirect(
            "/system-config/",
            "toast.save_failed",
            "error",
            lang=detect_language(),
        )


@router.post("/test-connection")
async def test_connection(
    request: Request,
    user: dict = Depends(require_super_admin),
    _csrf: str = Depends(require_csrf_header),
):
    """测试数据库或 Redis 连接"""
    body = await request.json()
    test_type = body.get("type", "")

    if test_type in ("database", "redis"):
        # 延迟导入：避免模块级别循环依赖
        from backend.core.setup_service import setup_service

        if test_type == "database":
            result = await setup_service.test_database_connection(body.get("url", ""))
        else:
            result = await setup_service.test_redis_connection(body.get("url", ""))
        return {
            "success": result.get("success", False),
            "message": result.get("message", ""),
        }

    return {"success": False, "message": "Unsupported test type"}


def _schedule_application_restart(delay_seconds: float = 2.0) -> None:
    """响应发出后复用 Setup 的应用重启机制（优雅停机，由监督者/容器拉起）。"""

    from backend.core.setup_service import setup_service

    asyncio.get_running_loop().call_later(
        delay_seconds,
        setup_service.trigger_restart,
    )


@router.post("/restart")
async def restart_application(
    db: AsyncSession = Depends(get_db),
    user: dict = Depends(require_super_admin),
    _csrf: str = Depends(require_csrf_header),
):
    """由超级管理员请求重启当前应用进程。"""

    logger.warning(
        "超级管理员请求重启应用: user_id={}, username={}",
        user["user_id"],
        user["sub"],
    )
    await log_admin_action(
        db,
        user["user_id"],
        "application_restart",
        "system_core",
        detail={"trigger": "webui"},
    )
    _schedule_application_restart()
    return JSONResponse(
        {"success": True, "restarting": True},
        status_code=202,
        headers={"Cache-Control": "no-store, max-age=0"},
    )


@router.post("/reset-database")
async def reset_database(
    request: Request,
    user: dict = Depends(require_super_admin),
    _csrf: str = Depends(require_csrf_header),
):
    """彻底清空数据库，重置 Setup 状态并安排应用重启。"""

    no_store_headers = {"Cache-Control": "no-store, max-age=0"}

    try:
        body = await request.json()
    except Exception:
        body = None
    requested_language = body.get("language") if isinstance(body, dict) else None
    language = (
        requested_language
        if requested_language in {"zh-CN", "en"}
        else detect_language()
    )
    translate = make_translation_func(language)
    if (
        not isinstance(body, dict)
        or body.get("confirmation") != DATABASE_RESET_CONFIRMATION
    ):
        return JSONResponse(
            {
                "success": False,
                "restarting": False,
                "message": translate(
                    "system_config.database_reset_invalid_confirmation"
                ),
            },
            status_code=400,
            headers=no_store_headers,
        )

    logger.info(
        "超级管理员请求彻底清空数据库: user_id={}, username={}",
        user["user_id"],
        user["sub"],
    )
    try:
        result = await database_reset_service.reset(
            before_drop=lambda: quiesce_database_reset_runtime(request.app),
        )
    except DatabaseResetError as exc:
        if exc.setup_state_reset:
            _schedule_application_restart()
        return JSONResponse(
            {
                "success": False,
                "restarting": exc.setup_state_reset,
                "message": translate(
                    "system_config.database_reset_failed_restarting"
                    if exc.setup_state_reset
                    else "system_config.database_reset_failed"
                ),
            },
            status_code=500,
            headers=no_store_headers,
        )

    _schedule_application_restart()
    return JSONResponse(
        {
            "success": True,
            "restarting": True,
            "message": translate(
                "system_config.database_reset_success",
                count=result.total_dropped,
            ),
            "dropped": {
                "tables": result.tables_dropped,
                "views": result.views_dropped,
                "materialized_views": result.materialized_views_dropped,
                "sequences": result.sequences_dropped,
            },
        },
        headers=no_store_headers,
    )
