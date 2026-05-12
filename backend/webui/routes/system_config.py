"""WebUI 系统核心配置路由（超级管理员专用）

管理系统基础设施配置：数据库、Redis、GitHub App、GitHub OAuth、
Telegram Bot、WebUI 安全等。这些配置通常在 Setup Wizard 首次部署时设置，
此页面允许超级管理员在运行时修改。
"""

from fastapi import APIRouter, Request, Depends
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from loguru import logger

from backend.models.database import AppConfig
from backend.core.config import (
    CORE_CONFIG_KEYS,
    mask_sensitive_value,
    update_settings_field,
    get_settings,
    invalidate_dynamic_config_cache,
    get_all_dynamic_config_keys,
)
from backend.webui.deps import (
    require_super_admin,
    get_db,
    get_templates,
    get_csrf_serializer,
    require_csrf,
    get_user_preferences,
    toast_redirect,
    render_template,
)
from backend.webui.helpers.admin_log import log_admin_action
from backend.webui.i18n import detect_language

router = APIRouter(prefix="/system-config", tags=["WebUI System Config"])
templates = get_templates()

# 系统核心配置分组定义
SYSTEM_CONFIG_GROUPS = [
    {
        "id": "database",
        "keys": ["database_url", "redis_url"],
    },
    {
        "id": "github_app",
        "keys": [
            "github_app_id",
            "github_private_key",
            "github_webhook_secret",
        ],
    },
    {
        "id": "github_oauth",
        "keys": [
            "github_oauth_client_id",
            "github_oauth_client_secret",
            "github_oauth_redirect_uri",
        ],
    },
    {
        "id": "telegram",
        "keys": ["telegram_bot_token"],
    },
    {
        "id": "application",
        "keys": [
            "app_domain",
            "app_port",
            "log_level",
            "webui_secret_key",
            "bot_username",
        ],
    },
]

# 需要重启才能生效的配置键
RESTART_REQUIRED_KEYS = frozenset(
    {
        "database_url",
        "redis_url",
        "github_private_key",
        "webui_secret_key",
    }
)

# 敏感键（在页面显示时脱敏）
SYSTEM_SENSITIVE_KEYS = frozenset(
    {
        "github_private_key",
        "github_webhook_secret",
        "github_oauth_client_secret",
        "telegram_bot_token",
        "webui_secret_key",
    }
)


@router.get("/")
async def system_config_page(
    request: Request,
    db: AsyncSession = Depends(get_db),
    user: dict = Depends(require_super_admin),
    user_prefs: dict = Depends(get_user_preferences),
):
    """渲染系统核心配置页面"""
    settings = get_settings()

    # 从数据库读取所有核心配置
    result = await db.execute(
        select(AppConfig).where(AppConfig.key_name.in_(CORE_CONFIG_KEYS))
    )
    db_configs = result.scalars().all()
    config_map = {c.key_name: c.key_value for c in db_configs}

    # 构建分组数据
    groups = []
    for group_def in SYSTEM_CONFIG_GROUPS:
        group_id = group_def["id"]
        items = []
        for key in group_def["keys"]:
            # 数据库值优先，否则从 Settings 获取
            value = config_map.get(key, str(getattr(settings, key, "") or ""))
            is_sensitive = key in SYSTEM_SENSITIVE_KEYS
            display_value = (
                mask_sensitive_value(value) if (is_sensitive and value) else value
            )
            default_val = str(getattr(settings, key, "") or "")
            items.append(
                {
                    "key": key,
                    "value": display_value,
                    "default": (
                        mask_sensitive_value(default_val)
                        if (is_sensitive and default_val)
                        else default_val
                    ),
                    "sensitive": is_sensitive,
                    "requires_restart": key in RESTART_REQUIRED_KEYS,
                    "raw_value": value,
                }
            )
        groups.append({"id": group_id, "fields": items})

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
        changed = {}
        needs_restart = False

        # 收集所有系统配置键
        all_system_keys = set()
        for group_def in SYSTEM_CONFIG_GROUPS:
            all_system_keys.update(group_def["keys"])

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
                if not val.startswith(
                    ("mysql+aiomysql://", "postgresql+asyncpg://")
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

            # 写入数据库
            result = await db.execute(
                select(AppConfig).where(AppConfig.key_name == key)
            )
            cfg = result.scalar_one_or_none()
            mask_fn = lambda v: mask_sensitive_value(v) if is_sensitive and v else v  # noqa: E731

            if cfg is None:
                cfg = AppConfig(key_name=key, key_value=val, description=key)
                db.add(cfg)
                changed[key] = {
                    "old": "(无)",
                    "new": mask_fn(val),
                    "raw_new": val,
                }
            elif cfg.key_value != val:
                changed[key] = {
                    "old": mask_fn(cfg.key_value),
                    "new": mask_fn(val),
                    "raw_new": val,
                }
                cfg.key_value = val

            if key in RESTART_REQUIRED_KEYS:
                needs_restart = True

        if not changed:
            return toast_redirect(
                "/system-config/",
                "toast.config_no_change",
                lang=detect_language(),
            )

        await db.commit()

        # 同步 Settings 单例
        all_dynamic_keys = get_all_dynamic_config_keys()
        invalidate_dynamic_config_cache(all_dynamic_keys)
        for key, change in changed.items():
            if key in all_dynamic_keys or key in CORE_CONFIG_KEYS:
                update_settings_field(key, change.get("raw_new", change["new"]))

        logger.info(
            f"系统核心配置已更新, by={user['sub']}, changed={list(changed.keys())}"
        )

        # 构建脱敏日志
        log_changed = {}
        for k, v in changed.items():
            log_entry = {"old": v["old"], "new": v["new"]}
            if k in SYSTEM_SENSITIVE_KEYS:
                log_entry["old"] = mask_sensitive_value(str(log_entry["old"]))
                log_entry["new"] = mask_sensitive_value(str(log_entry["new"]))
            log_changed[k] = log_entry

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
):
    """测试数据库或 Redis 连接"""
    body = await request.json()
    test_type = body.get("type", "")

    if test_type == "database":
        from backend.core.setup_service import setup_service

        result = await setup_service.test_database_connection(body.get("url", ""))
        return {"success": result.get("success", False), "message": result.get("message", "")}
    elif test_type == "redis":
        from backend.core.setup_service import setup_service

        result = await setup_service.test_redis_connection(body.get("url", ""))
        return {"success": result.get("success", False), "message": result.get("message", "")}
    else:
        return {"success": False, "message": f"Unknown test type: {test_type}"}
