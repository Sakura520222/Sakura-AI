"""Version & deployment info route.

Slice 1：只读展示当前版本与部署模式。
Slice 2：扩展 update_available/latest（Redis 缓读，derived update_available）+ /version/releases + /version/check + /version/manager 页面。

update_available 是 derived state：必须用当前进程 __version__ + cached latest_version
即时计算（is_newer_version），不信任缓存里可能陈旧的布尔值（外部升级后旧缓存会误报）。
"""

from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse
from starlette.requests import Request

from backend import __version__
from backend.core.config import get_settings
from backend.services.update_checker import is_newer_version, read_cache
from backend.webui.deps import (
    get_csrf_serializer,
    get_user_preferences,
    render_template,
    require_auth,
    require_csrf_header,
    require_super_admin,
)

router = APIRouter(tags=["Version"])

_VALID_MODES = {"image", "source"}


def build_version_info(
    deploy_mode: str, update_info: dict | None = None
) -> dict:
    """构造版本与部署信息（纯函数）。

    Args:
        deploy_mode: 部署模式。非法值归一化为 "unknown"。
        update_info: 可选的更新检查缓存数据。None 时相关字段为 null。

    update_available 即时 derive：is_newer_version(__version__, latest)。
    - 无缓存（update_info=None）→ None
    - 有缓存且 latest 有值 → derive 布尔
    - 有缓存但 latest 为 None（空列表/失败无 last-known-good）→ False
    """
    mode = deploy_mode if deploy_mode in _VALID_MODES else "unknown"

    update_supported = False
    if mode == "source":
        reason = "source_updater_not_available"
    elif mode == "image":
        reason = "updater_not_connected"  # Slice 4 接入 updater 后改判
    else:
        reason = "unknown_deployment"

    ui = update_info or {}
    latest = ui.get("latest_version")
    if ui:
        available = is_newer_version(__version__, latest) if latest else False
    else:
        available = None
    return {
        "current_version": __version__,
        "deployment_type": mode,
        "update_supported": update_supported,
        "update_unsupported_reason": reason,
        "update_available": available,
        "latest_version": latest,
        "last_checked": ui.get("last_checked"),
        "check_error": ui.get("check_error"),
    }


@router.get("/version/info")
async def get_version_info(user: dict = Depends(require_auth)):
    """当前版本 + 部署模式 + 更新可用性（所有登录用户，驱动 navbar badge）。"""
    mode = get_settings().sakura_deploy_mode or "unknown"
    update_info = await read_cache()
    info = build_version_info(mode, update_info)
    return JSONResponse(
        info,
        headers={"Cache-Control": "no-store, max-age=0"},
    )


@router.get("/version/releases")
async def get_version_releases(user: dict = Depends(require_super_admin)):
    """Release 列表（含 Markdown notes）— 版本管理器数据源，super_admin only。

    update_available 同样 derive，不裸返回缓存布尔值。
    """
    cache = await read_cache() or {}
    latest = cache.get("latest_version")
    available = is_newer_version(__version__, latest) if latest else False
    return JSONResponse(
        {
            "current_version": __version__,
            "latest_version": latest,
            "update_available": available,
            "last_checked": cache.get("last_checked"),
            "check_error": cache.get("check_error"),
            "releases": cache.get("releases", []),
        },
        headers={"Cache-Control": "no-store, max-age=0"},
    )


@router.post("/version/check")
async def trigger_check(
    request: Request,
    user: dict = Depends(require_super_admin),
    _csrf: str = Depends(require_csrf_header),
):
    """手动触发一次更新检查（super_admin + CSRF）。

    复用 app.state.update_checker（lifespan 创建的唯一实例，含 _check_lock）。
    后台任务未启动（dev/bootstrap）时返回 503。
    """
    checker = getattr(request.app.state, "update_checker", None)
    if checker is None:
        return JSONResponse(
            {"error": "update_checker_unavailable"},
            status_code=503,
            headers={"Cache-Control": "no-store, max-age=0"},
        )
    data = await checker.check_once()
    return JSONResponse(
        data,
        headers={"Cache-Control": "no-store, max-age=0"},
    )


@router.get("/version/manager")
async def version_manager_page(
    request: Request,
    user: dict = Depends(require_super_admin),
    user_prefs: dict = Depends(get_user_preferences),
):
    """版本管理器页面（super_admin only）。

    render_template 注入 i18n；csrf_token 供 recheck 按钮 X-CSRF-Token 用。
    """
    mode = get_settings().sakura_deploy_mode or "unknown"
    update_info = await read_cache()
    info = build_version_info(mode, update_info)
    return render_template(
        "version_manager.html",
        request,
        user_prefs=user_prefs,
        current_user=user,
        csrf_token=get_csrf_serializer().dumps({}),
        version_info=info,
        releases=(update_info or {}).get("releases", []),
        active_page="version_manager",
        page_title="版本管理",
    )
