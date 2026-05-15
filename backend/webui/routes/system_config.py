"""WebUI 系统核心配置路由（超级管理员专用）

管理系统基础设施配置：数据库、Redis、GitHub App、GitHub OAuth、
Telegram Bot、WebUI 安全等。这些配置通常在 Setup Wizard 首次部署时设置，
此页面允许超级管理员在运行时修改。
"""

from fastapi import APIRouter, Request, Depends
from sqlalchemy.ext.asyncio import AsyncSession
from loguru import logger

from backend.services.system_config_service import (
    SYSTEM_CONFIG_GROUPS,
    SYSTEM_SENSITIVE_KEYS,
    system_config_service,
)
from backend.webui.deps import (
    require_super_admin,
    get_db,
    get_csrf_serializer,
    require_csrf,
    require_csrf_header,
    get_user_preferences,
    toast_redirect,
    render_template,
)
from backend.webui.helpers.admin_log import log_admin_action
from backend.webui.i18n import detect_language

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

    return render_template(
        "system_config.html",
        request,
        user_prefs=user_prefs,
        current_user=user,
        csrf_token=get_csrf_serializer().dumps({}),
        active_page="system_config",
        groups=groups,
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

            # 数据库连接字符串验证
            if key == "database_url":
                if not val.startswith(("mysql+aiomysql://", "postgresql+asyncpg://")):
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
                except (ValueError, TypeError):
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
