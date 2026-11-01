"""超级管理员系统运维操作（连接测试 / GitHub App slug / 重启 / 数据库重置）。

Super-admin system maintenance operations shared by the versioned WebUI API:
connection testing, review GitHub App slug resolution, application restart and
full database reset. The API layer (``/api/v1/webui/system/*``) is the only
entry point; these helpers own no routes of their own.
"""

import asyncio

from fastapi import Depends, Request
from fastapi.responses import JSONResponse
from loguru import logger
from sqlalchemy.ext.asyncio import AsyncSession

from backend.core.config import get_settings
from backend.services.database_reset_runtime_service import (
    quiesce_database_reset_runtime,
)
from backend.services.database_reset_service import (
    DATABASE_RESET_CONFIRMATION,
    DatabaseResetError,
    database_reset_service,
)
from backend.webui.deps import (
    get_db,
    get_user_preferences,
    require_csrf_header,
    require_super_admin,
)
from backend.webui.helpers.admin_log import log_admin_action
from backend.webui.i18n import detect_language, make_translation_func


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

        url = body.get("url", "")
        if body.get("use_saved") is True and not url:
            # Tests use the active connection; credentials never return to the browser.
            url = getattr(
                get_settings(),
                "database_url" if test_type == "database" else "redis_url",
                "",
            )
        if test_type == "database":
            result = await setup_service.test_database_connection(url)
        else:
            result = await setup_service.test_redis_connection(url)
        return {
            "success": result.get("success", False),
            "message": result.get("message", ""),
        }

    return {"success": False, "message": "Unsupported test type"}


async def _resolve_review_github_app_slug(
    authorization_client_id: str,
) -> tuple[str | None, str | None]:
    """Resolve the review App slug only when it owns the authorization client ID."""
    from backend.core.github_app import GitHubAppClient

    try:
        slug, review_client_id = await asyncio.to_thread(
            GitHubAppClient().get_app_identity
        )
    except Exception as exc:
        logger.warning(
            "GitHub App slug lookup failed: {}",
            type(exc).__name__,
        )
        return None, "github_app_slug_lookup_failed"

    if not slug or slug == "unknown-bot" or not review_client_id:
        return None, "github_app_slug_lookup_failed"
    if review_client_id != authorization_client_id:
        # GitHub has no API for reverse-looking-up a separate App from only
        # its OAuth Client ID/Secret. Never fill this field with the review
        # App slug when those are different Apps.
        return None, "github_app_slug_requires_manual_entry"
    return slug.removesuffix("[bot]"), None


async def fetch_github_app_slug(
    request: Request,
    _user: dict = Depends(require_super_admin),
    user_prefs: dict = Depends(get_user_preferences),
    _csrf: str = Depends(require_csrf_header),
):
    """Auto-fill the App slug when the user-authorization App is the review App."""
    translate = make_translation_func(detect_language(user_prefs))
    try:
        body = await request.json()
    except Exception:
        body = None

    client_id = str(body.get("clientId", "")).strip() if isinstance(body, dict) else ""
    if not client_id:
        return {
            "success": False,
            "error_code": "github_app_slug_client_id_required",
            "message": translate("system_config.github_app_slug_client_id_required"),
        }

    slug, error_code = await _resolve_review_github_app_slug(client_id)
    if error_code:
        return {
            "success": False,
            "error_code": error_code,
            "message": translate(f"system_config.{error_code}"),
        }

    return {
        "success": True,
        "slug": slug,
        "message": translate("system_config.github_app_slug_filled"),
    }


def _schedule_application_restart(delay_seconds: float = 2.0) -> None:
    """响应发出后复用 Setup 的应用重启机制（优雅停机，由监督者/容器拉起）。"""

    from backend.core.setup_service import setup_service

    asyncio.get_running_loop().call_later(
        delay_seconds,
        setup_service.trigger_restart,
    )


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
