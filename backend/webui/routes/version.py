"""Version & deployment info route.

Slice 1：只读展示当前版本与部署模式。
Slice 2：扩展 update_available/latest（Redis 缓读，derived update_available）+ /version/releases + /version/check + /version/manager 页面。

update_available 是 derived state：必须用当前进程 __version__ + cached latest_version
即时计算（is_newer_version），不信任缓存里可能陈旧的布尔值（外部升级后旧缓存会误报）。
"""

from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.requests import Request

from backend import __version__
from backend.core.build_info import get_build_info
from backend.core.config import get_settings
from backend.services.container_registry import (
    REPOSITORY as OFFICIAL_REGISTRY_REPOSITORY,
)
from backend.services.container_registry import (
    ContainerRegistryClient,
    ContainerRegistryError,
)
from backend.services.update_checker import is_newer_version, read_cache
from backend.services.updater_client import (
    UpdaterActionError,
    UpdaterClient,
    UpdaterProtocolError,
    UpdaterUnavailableError,
)
from backend.webui.deps import (
    get_csrf_serializer,
    get_db,
    get_user_preferences,
    render_template,
    require_auth,
    require_csrf_header,
    require_super_admin,
)
from backend.webui.helpers.admin_log import log_admin_action

router = APIRouter(tags=["Version"])

_VALID_MODES = {"image", "source"}
_registry_client: ContainerRegistryClient | None = None


def _get_registry_client() -> ContainerRegistryClient:
    """Reuse the short-TTL client so last-known-good survives requests."""

    global _registry_client
    if _registry_client is None or not isinstance(_registry_client, ContainerRegistryClient):
        _registry_client = ContainerRegistryClient(OFFICIAL_REGISTRY_REPOSITORY)
    return _registry_client


def build_version_info(
    deploy_mode: str,
    update_info: dict | None = None,
    updater_info: dict | None = None,
) -> dict:
    """构造版本与部署信息（纯函数）。

    Args:
        deploy_mode: 部署模式。非法值归一化为 "unknown"。
        update_info: 可选的更新检查缓存数据。None 时相关字段为 null。
        updater_info: 可选的 updater /v1/status envelope（连上时）。None 表示未连接。

    update_available 即时 derive：is_newer_version(__version__, latest)。
    - 无缓存（update_info=None）→ None
    - 有缓存且 latest 有值 → derive 布尔
    - 有缓存但 latest 为 None（空列表/失败无 last-known-good）→ False

    updater 连接状态：image 模式下 updater 已连且 protocol v1 兼容时，
    ``update_supported`` 为真；``update_available`` 仍只代表 Backend GitHub
    discovery，Host readiness 由 ``update_ready`` 单独表示。
    updater_info 的版本字段取自 envelope 顶层（spec §7.2，data 不重复）。
    """
    mode = deploy_mode if deploy_mode in _VALID_MODES else "unknown"

    updater_connected = updater_info is not None
    updater_version = updater_info.get("updater_version") if updater_info else None
    updater_protocol_version = (
        updater_info.get("protocol_version") if updater_info else None
    )

    updater_data = (
        updater_info.get("data", {})
        if isinstance(updater_info, dict)
        else {}
    )
    if not isinstance(updater_data, dict):
        updater_data = {}
    protocol_compatible = updater_protocol_version == 1
    update_supported = mode == "image" and updater_connected and protocol_compatible
    if mode == "source":
        reason = "source_updater_not_available"
    elif mode == "image":
        if not updater_connected:
            reason = "updater_not_connected"
        elif not protocol_compatible:
            reason = "updater_protocol_incompatible"
        else:
            reason = None
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
        "build": get_build_info(),
        "deployment_type": mode,
        "deployment_mode": mode,
        "update_supported": update_supported,
        "update_unsupported_reason": reason,
        "update_available": available,
        "latest_version": latest,
        "last_checked": ui.get("last_checked"),
        "check_error": ui.get("check_error"),
        "updater_connected": updater_connected,
        "updater_version": updater_version,
        "updater_protocol_version": updater_protocol_version,
        "updater_state": updater_data.get("state"),
        "has_active_job": bool(updater_data.get("has_active_job", False)),
        "active_job_id": updater_data.get("active_job_id"),
        "updater_deployment": updater_data.get("deployment"),
        "update_ready": bool(updater_data.get("update_ready", False)),
        # Host readiness is authoritative only when supplied by the updater's
        # most recent read-only check/preflight snapshot.  Keep the structured
        # checks and target available to callers instead of reducing readiness
        # to a permanently-false boolean when the daemon is freshly restarted.
        "readiness": updater_data.get("readiness"),
        "target": updater_data.get("target"),
    }


@router.get("/version/info")
async def get_version_info(user: dict = Depends(require_auth)):
    """当前版本 + 部署模式 + 更新可用性 + updater 连接状态（所有登录用户，驱动 navbar badge）。"""
    mode = get_settings().sakura_deploy_mode or "unknown"
    update_info = await read_cache()
    updater_info = await UpdaterClient().get_status()
    info = build_version_info(mode, update_info, updater_info)
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


@router.get("/version/images")
async def get_version_images(user: dict = Depends(require_super_admin)):
    """Return the read-only, digest-grouped official GHCR catalog."""

    try:
        payload = await _get_registry_client().list_images()
    except (ContainerRegistryError, ValueError):
        return JSONResponse(
            {"error": "registry_unavailable"},
            status_code=503,
            headers={"Cache-Control": "no-store, max-age=0"},
        )
    return JSONResponse(payload, headers={"Cache-Control": "no-store, max-age=0"})


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


def _updater_error(exc: Exception) -> JSONResponse:
    """Map typed updater client errors to Backend proxy responses."""

    if isinstance(exc, UpdaterUnavailableError):
        return JSONResponse({"error": "updater_unavailable"}, status_code=503)
    if isinstance(exc, UpdaterProtocolError):
        return JSONResponse({"error": "updater_protocol_error"}, status_code=502)
    if isinstance(exc, UpdaterActionError):
        body = exc.body if isinstance(exc.body, dict) else {"error": "updater_error"}
        if exc.status_code >= 500:
            code = body.get("error")
            if code in {"release_unavailable", "protocol_error", "updater_not_ready"}:
                payload = {"error": code}
                detail = body.get("detail")
                if (
                    isinstance(detail, str)
                    and len(detail) <= 96
                    and all(character.isalnum() or character in "._-" for character in detail)
                ):
                    payload["detail"] = detail
                status_code = 503 if code == "updater_not_ready" else 502
                return JSONResponse(payload, status_code=status_code)
            return JSONResponse({"error": "updater_internal_error"}, status_code=502)
        return JSONResponse(body, status_code=exc.status_code)
    return JSONResponse({"error": "updater_internal_error"}, status_code=502)


async def _read_action_body(request: Request) -> dict:
    try:
        data = await request.json()
    except (ValueError, TypeError):
        return {}
    return data if isinstance(data, dict) else {}


def _confirm_channel_switch(body: dict) -> tuple[bool | None, JSONResponse | None]:
    """Accept only a JSON boolean; an omitted field is the safe default false."""

    if "confirm_channel_switch" not in body:
        return False, None
    value = body["confirm_channel_switch"]
    if type(value) is not bool:
        return None, JSONResponse({"error": "invalid_confirm_channel_switch"}, status_code=422)
    return value, None


async def _resolve_catalog_target(body: dict) -> tuple[dict | None, JSONResponse | None]:
    """Re-resolve a browser snapshot against the current authoritative head."""

    target = body.get("target")
    if target is None:
        return None, None
    if not isinstance(target, dict) or target.get("channel") not in {"stable", "development"}:
        return None, JSONResponse({"error": "invalid_target"}, status_code=422)
    try:
        catalog = await _get_registry_client().list_images()
    except (ContainerRegistryError, ValueError):
        return None, JSONResponse({"error": "registry_unavailable"}, status_code=503)
    if catalog.get("stale"):
        return None, JSONResponse({"error": "stale_catalog"}, status_code=409)
    head = (catalog.get("heads") or {}).get(target.get("channel"))
    required = ("version", "tag", "digest")
    if target.get("channel") == "development":
        required += ("revision",)
    if not isinstance(head, dict) or any(target.get(key) != head.get(key) for key in required):
        return None, JSONResponse({"error": "target_not_selectable"}, status_code=409)
    return target, None


def _validate_target(target: object) -> bool:
    # Reuse the strict discovery parser; it accepts only X.Y.Z (no v prefix,
    # prerelease, or build metadata in P0).
    from backend.services.update_checker import _parse_semver

    return isinstance(target, str) and _parse_semver(target) is not None


@router.post("/version/readiness")
async def updater_readiness(
    user: dict = Depends(require_super_admin),
    _csrf: str = Depends(require_csrf_header),
):
    """Read-only host readiness (distinct from GitHub discovery state)."""

    try:
        return JSONResponse(await UpdaterClient().check(), headers={"Cache-Control": "no-store"})
    except Exception as exc:
        return _updater_error(exc)


@router.post("/version/preflight")
async def updater_preflight(
    request: Request,
    user: dict = Depends(require_super_admin),
    _csrf: str = Depends(require_csrf_header),
):
    body = await _read_action_body(request)
    confirm_channel_switch, confirm_error = _confirm_channel_switch(body)
    if confirm_error is not None:
        return confirm_error
    target_object, target_error = await _resolve_catalog_target(body)
    if target_error is not None:
        return target_error
    target = body.get("target_version")
    if target_object is None and not _validate_target(target):
        return JSONResponse({"error": "invalid_target_version"}, status_code=422)
    try:
        return JSONResponse(
            await UpdaterClient().preflight(
                target if isinstance(target, str) else None,
                target=target_object,
                confirm_channel_switch=confirm_channel_switch is True,
            ),
            headers={"Cache-Control": "no-store"},
        )
    except Exception as exc:
        return _updater_error(exc)


@router.post("/version/update")
async def updater_update(
    request: Request,
    db: AsyncSession = Depends(get_db),
    user: dict = Depends(require_super_admin),
    _csrf: str = Depends(require_csrf_header),
):
    body = await _read_action_body(request)
    confirm_channel_switch, confirm_error = _confirm_channel_switch(body)
    if confirm_error is not None:
        return confirm_error
    target_object, target_error = await _resolve_catalog_target(body)
    if target_error is not None:
        return target_error
    target = body.get("target_version")
    if target is not None and not _validate_target(target):
        return JSONResponse({"error": "invalid_target_version"}, status_code=422)
    try:
        payload = await UpdaterClient().update(
            target,
            target=target_object,
            confirm_channel_switch=confirm_channel_switch is True,
        )
        response = JSONResponse(
            payload,
            headers={"Cache-Control": "no-store"},
            status_code=202,
        )
        data = payload.get("data", {}) if isinstance(payload, dict) else {}
        job_id = data.get("job_id") if isinstance(data, dict) else None
        try:
            await log_admin_action(
                db,
                user["user_id"],
                "update_apply",
                "deployment",
                job_id,
                {
                    "target_version": target,
                    "target_channel": target_object.get("channel") if target_object else None,
                    "target_revision": target_object.get("revision") if target_object else None,
                    "target_digest": target_object.get("digest") if target_object else None,
                    "confirm_channel_switch": confirm_channel_switch is True,
                    "job_id": job_id,
                    "deployment_mode": get_settings().sakura_deploy_mode or "unknown",
                },
            )
        except Exception:
            # Audit failure must never roll back or mask a host updater job.
            pass
        return response
    except Exception as exc:
        return _updater_error(exc)


@router.get("/version/jobs/{job_id}")
async def updater_job(
    job_id: str,
    user: dict = Depends(require_super_admin),
):
    try:
        return JSONResponse(
            await UpdaterClient().get_job(job_id),
            headers={"Cache-Control": "no-store"},
        )
    except Exception as exc:
        return _updater_error(exc)


@router.get("/version/jobs/{job_id}/logs")
async def updater_job_logs(
    job_id: str,
    user: dict = Depends(require_super_admin),
):
    try:
        return JSONResponse(
            await UpdaterClient().get_job_logs(job_id),
            headers={"Cache-Control": "no-store"},
        )
    except Exception as exc:
        return _updater_error(exc)


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
    info = build_version_info(mode, update_info, await UpdaterClient().get_status())
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
