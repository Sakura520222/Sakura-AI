"""Versioned browser adapters for the existing updater control plane.

Slice 1：只读展示当前版本与部署模式。
Slice 2：扩展 update_available/latest（Redis 缓读，derived update_available）+ /version/releases + /version/check（旧 /version/manager 页面已由 React SPA /app/admin/version 取代）。

update_available 是 derived state：必须用当前进程 __version__ + cached latest_version
即时计算（is_newer_version），不信任缓存里可能陈旧的布尔值（外部升级后旧缓存会误报）。
"""

import json
from typing import Literal

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.ext.asyncio import AsyncSession

from backend import __version__
from backend.api.v1.deps import require_api_auth, require_api_super_admin
from backend.api.v1.private_routes import PrivateAPIRoute
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
from backend.webui.deps import get_db, require_csrf_header
from backend.webui.helpers.admin_log import log_admin_action

router = APIRouter(
    prefix="/version", tags=["Version Management"], route_class=PrivateAPIRoute
)


class VersionTarget(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    channel: Literal["stable", "development"]
    version: str = Field(min_length=1)
    tag: str = Field(min_length=1)
    digest: str = Field(min_length=1)
    revision: str | None = None


class VersionAction(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    target: VersionTarget | None = None
    target_version: str | None = None
    confirm_channel_switch: bool = False


_VALID_MODES = {"image", "source"}
_registry_client: ContainerRegistryClient | None = None


def _get_registry_client() -> ContainerRegistryClient:
    """Reuse the short-TTL client so last-known-good survives requests."""

    global _registry_client
    if _registry_client is None or not isinstance(
        _registry_client, ContainerRegistryClient
    ):
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

    stable 的 update_available 即时 derive：is_newer_version(__version__, latest)。
    development 则只信任 updater 最近一次同通道 readiness 快照中的
    target_newer（该值由当前 digest 与 edge digest 比较得出）。
    - 无缓存（update_info=None）→ None
    - 有缓存且 latest 有值 → derive 布尔
    - 有缓存但 latest 为 None（空列表/失败无 last-known-good）→ False

    updater 连接状态：image 模式下 updater 已连且 protocol v1 兼容时，
    ``update_supported`` 为真；stable 的 ``update_available`` 代表 Backend
    GitHub discovery，development 的该字段代表 updater digest discovery；
    Host 全部门槛是否通过仍由 ``update_ready`` 单独表示。
    updater_info 的版本字段取自 envelope 顶层（spec §7.2，data 不重复）。
    """
    mode = deploy_mode if deploy_mode in _VALID_MODES else "unknown"

    updater_connected = updater_info is not None
    updater_version = updater_info.get("updater_version") if updater_info else None
    updater_protocol_version = (
        updater_info.get("protocol_version") if updater_info else None
    )

    updater_data = (
        updater_info.get("data", {}) if isinstance(updater_info, dict) else {}
    )
    if not isinstance(updater_data, dict):
        updater_data = {}
    from sakura_ai_updater.contract import compatibility

    contract = compatibility(updater_info)
    protocol_compatible = updater_protocol_version == 1
    update_supported = mode == "image" and updater_connected and contract["compatible"]
    if mode == "source":
        reason = "source_updater_not_available"
    elif mode == "image":
        if not updater_connected:
            reason = "updater_not_connected"
        elif not protocol_compatible:
            reason = "updater_protocol_incompatible"
        elif not contract["compatible"]:
            reason = "updater_capability_incompatible"
        else:
            reason = None
    else:
        reason = "unknown_deployment"

    build = get_build_info()
    ui = update_info or {}
    channel_result = ui.get("channel_result")
    channel_data = (
        channel_result.get("data", {}) if isinstance(channel_result, dict) else {}
    )
    cached_target = (
        channel_data.get("target") if isinstance(channel_data, dict) else None
    )
    matching_channel_cache = (
        isinstance(cached_target, dict)
        and cached_target.get("channel") == build.get("channel")
        and ui.get("channel_build") == build
    )
    if matching_channel_cache:
        # This cache is written by both the scheduler and manual check. It is
        # also the last-known-good when the host/registry is temporarily down.
        updater_data = {**updater_data, **channel_data}
    if build.get("channel") == "development":
        # Stable Release discovery is not an update signal for a development
        # deployment. Only reuse an updater snapshot whose target belongs to
        # the same channel; a prior explicit stable preflight must not create a
        # development badge.
        target = updater_data.get("target")
        readiness = updater_data.get("readiness")
        matching_target = (
            target
            if (protocol_compatible or matching_channel_cache)
            and isinstance(target, dict)
            and target.get("channel") == "development"
            else {}
        )
        latest_value = matching_target.get("version")
        latest = latest_value if isinstance(latest_value, str) else None
        target_newer = (
            readiness.get("target_newer")
            if latest is not None and isinstance(readiness, dict)
            else None
        )
        available = target_newer if type(target_newer) is bool else None
        if (
            matching_channel_cache
            and type(channel_data.get("update_available")) is bool
        ):
            available = channel_data["update_available"]
    else:
        latest = ui.get("latest_version")
        if ui:
            available = is_newer_version(__version__, latest) if latest else False
        else:
            available = None
    if matching_channel_cache:
        latest_value = cached_target.get("version")
        latest = latest_value if isinstance(latest_value, str) else None
        available = channel_data.get("update_available")
        if type(available) is not bool:
            available = None
    return {
        "current_version": __version__,
        "build": build,
        "deployment_type": mode,
        "deployment_mode": mode,
        "update_supported": update_supported,
        "update_unsupported_reason": reason,
        "update_available": available,
        "latest_version": latest,
        "last_checked": ui.get("last_checked"),
        "check_error": ui.get("check_error"),
        "fetched_at": ui.get("channel_fetched_at") or ui.get("fetched_at"),
        "updater_connected": updater_connected,
        "updater_version": updater_version,
        "updater_compatibility": contract,
        "updater_capabilities": sorted(
            set((updater_info or {}).get("capabilities") or [])
        )
        if isinstance((updater_info or {}).get("capabilities"), list)
        and all(isinstance(cap, str) for cap in updater_info["capabilities"])
        else [],
        "updater_build_revision": (updater_info or {}).get("build_revision"),
        "updater_build_release": (updater_info or {}).get("build_release"),
        "updater_protocol_version": updater_protocol_version,
        "updater_state": updater_data.get("state"),
        "has_active_job": bool(updater_data.get("has_active_job", False)),
        "active_job_id": updater_data.get("active_job_id"),
        "updater_deployment": updater_data.get("deployment"),
        "update_ready": update_supported
        and bool(updater_data.get("update_ready", False)),
        # Host readiness is authoritative only when supplied by the updater's
        # most recent read-only check/preflight snapshot.  Keep the structured
        # checks and target available to callers instead of reducing readiness
        # to a permanently-false boolean when the daemon is freshly restarted.
        "readiness": updater_data.get("readiness"),
        "target": updater_data.get("target"),
    }


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
            if code in {
                "registry_unavailable",
                "release_unavailable",
                "protocol_error",
                "updater_not_ready",
            }:
                payload = {"error": code}
                detail = body.get("detail")
                if (
                    isinstance(detail, str)
                    and len(detail) <= 96
                    and all(
                        character.isalnum() or character in "._-"
                        for character in detail
                    )
                ):
                    payload["detail"] = detail
                status_code = (
                    503
                    if code in {"registry_unavailable", "updater_not_ready"}
                    else 502
                )
                return JSONResponse(payload, status_code=status_code)
            return JSONResponse({"error": "updater_internal_error"}, status_code=502)
        return JSONResponse(body, status_code=exc.status_code)
    return JSONResponse({"error": "updater_internal_error"}, status_code=502)


async def _read_action_body(request: Request) -> dict:
    try:
        data = await request.json()
    except ValueError, TypeError:
        return {}
    return data if isinstance(data, dict) else {}


def _confirm_channel_switch(body: dict) -> tuple[bool | None, JSONResponse | None]:
    """Accept only a JSON boolean; an omitted field is the safe default false."""

    if "confirm_channel_switch" not in body:
        return False, None
    value = body["confirm_channel_switch"]
    if type(value) is not bool:
        return None, JSONResponse(
            {"error": "invalid_confirm_channel_switch"}, status_code=422
        )
    return value, None


async def _resolve_catalog_target(
    body: dict,
) -> tuple[dict | None, JSONResponse | None]:
    """Re-resolve a browser snapshot against the current authoritative head."""

    target = body.get("target")
    if target is None:
        return None, None
    if not isinstance(target, dict) or target.get("channel") not in {
        "stable",
        "development",
    }:
        return None, JSONResponse({"error": "invalid_target"}, status_code=422)
    try:
        catalog = await _get_registry_client().list_images()
    except ContainerRegistryError, ValueError:
        return None, JSONResponse({"error": "registry_unavailable"}, status_code=503)
    if catalog.get("stale"):
        return None, JSONResponse({"error": "stale_catalog"}, status_code=409)
    head = (catalog.get("heads") or {}).get(target.get("channel"))
    required = ("version", "tag", "digest")
    if target.get("channel") == "development":
        required += ("revision",)
    if not isinstance(head, dict) or any(
        target.get(key) != head.get(key) for key in required
    ):
        return None, JSONResponse({"error": "target_not_selectable"}, status_code=409)
    return target, None


def _validate_target(target: object) -> bool:
    # Reuse the strict discovery parser; it accepts only X.Y.Z (no v prefix,
    # prerelease, or build metadata in P0).
    from backend.services.update_checker import _parse_semver

    return isinstance(target, str) and _parse_semver(target) is not None


async def _resolve_current_channel_target(
    *, force_refresh: bool = False
) -> tuple[dict | None, JSONResponse | None]:
    """Resolve the running image channel to its current authoritative head."""

    build = get_build_info()
    channel = build.get("channel") if isinstance(build, dict) else None
    mode = get_settings().sakura_deploy_mode or "unknown"
    if mode != "image" or channel not in {"stable", "development"}:
        return None, None
    try:
        catalog = await _get_registry_client().list_images(force_refresh=force_refresh)
    except ContainerRegistryError, ValueError:
        return None, JSONResponse({"error": "registry_unavailable"}, status_code=503)
    if catalog.get("stale"):
        return None, JSONResponse({"error": "stale_catalog"}, status_code=409)
    head = (catalog.get("heads") or {}).get(channel)
    required = ("version", "tag", "digest")
    if channel == "development":
        required += ("revision",)
    if not isinstance(head, dict) or any(not head.get(key) for key in required):
        return None, JSONResponse(
            {"error": "channel_head_unavailable"}, status_code=409
        )
    target = {
        "channel": channel,
        "version": head["version"],
        "tag": head["tag"],
        "digest": head["digest"],
    }
    if channel == "development":
        target["revision"] = head["revision"]
    return target, None


def _annotate_channel_readiness(envelope: dict, target: dict) -> dict:
    """Attach the exact channel target and availability to an updater envelope."""

    payload = dict(envelope)
    data = payload.get("data")
    if not isinstance(data, dict):
        return payload
    data = dict(data)
    checks = data.get("checks")
    target_newer = None
    if isinstance(checks, list):
        target_newer = next(
            (
                bool(item.get("passed"))
                for item in checks
                if isinstance(item, dict) and item.get("name") == "target_newer"
            ),
            None,
        )
    data["target"] = target
    data["update_ready"] = data.get("can_update") is True
    if target_newer is not None:
        data["update_available"] = target_newer
    payload["data"] = data
    return payload


async def _check_current_channel(*, force_refresh: bool = False) -> dict:
    target, target_error = await _resolve_current_channel_target(
        force_refresh=force_refresh
    )
    if target_error is not None:
        raise UpdaterActionError(
            target_error.status_code, json.loads(target_error.body)
        )
    client = UpdaterClient()
    if target is None:
        return await client.check()
    envelope = await client.preflight(
        target=target,
        confirm_channel_switch=False,
    )
    return _annotate_channel_readiness(envelope, target)


@router.get("/info")
async def info(request: Request, user: dict = Depends(require_api_auth)):
    return await get_version_info(user=user, request=request)


async def get_version_info(user: dict, request: Request | None = None):
    """当前版本 + 部署模式 + 更新可用性 + updater 连接状态（所有登录用户，驱动 navbar badge）。"""
    mode = get_settings().sakura_deploy_mode or "unknown"
    update_info = await read_cache()
    updater_info = await UpdaterClient().get_status()
    info = build_version_info(mode, update_info, updater_info)
    checker = getattr(request.app.state, "update_checker", None) if request else None
    info.update(
        checker.status()
        if checker
        else {
            "scheduler_running": False,
            "checking": False,
            "next_check_at": None,
        }
    )
    return JSONResponse(
        info,
        headers={"Cache-Control": "no-store, max-age=0"},
    )


@router.get("/releases")
async def releases(user: dict = Depends(require_api_super_admin)):
    return await get_version_releases(user=user)


async def get_version_releases(user: dict):
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


@router.get("/images")
async def images(user: dict = Depends(require_api_super_admin)):
    return await get_version_images(user=user)


async def get_version_images(user: dict):
    """Return the read-only, digest-grouped official GHCR catalog."""

    try:
        payload = await _get_registry_client().list_images()
    except ContainerRegistryError, ValueError:
        return JSONResponse(
            {"error": "registry_unavailable"},
            status_code=503,
            headers={"Cache-Control": "no-store, max-age=0"},
        )
    return JSONResponse(payload, headers={"Cache-Control": "no-store, max-age=0"})


@router.post("/check")
async def check(
    request: Request,
    user: dict = Depends(require_api_super_admin),
    csrf: str = Depends(require_csrf_header),
):
    return await trigger_check(request, user=user, _csrf=csrf)


async def trigger_check(
    request: Request,
    user: dict,
    _csrf: str,
):
    """手动触发一次更新检查（super_admin + CSRF）。

    镜像部署按当前 build channel 强制刷新 GHCR head，并用结构化 digest
    目标调用 updater；源码/旧镜像回退到 Release checker。
    """
    checker = getattr(request.app.state, "update_checker", None)
    if checker is not None:
        data = await checker.check_once()
        if data.get("channel_check_error"):
            return JSONResponse(
                {"error": "update_check_failed"},
                status_code=503,
                headers={"Cache-Control": "no-store, max-age=0"},
            )
        return JSONResponse(
            data.get("channel_result") or data,
            headers={"Cache-Control": "no-store, max-age=0"},
        )

    build = get_build_info()
    channel = build.get("channel") if isinstance(build, dict) else None
    mode = get_settings().sakura_deploy_mode or "unknown"
    if mode == "image" and channel in {"stable", "development"}:
        try:
            data = await _check_current_channel(force_refresh=True)
        except Exception as exc:
            return _updater_error(exc)
        return JSONResponse(data, headers={"Cache-Control": "no-store, max-age=0"})

    checker = getattr(request.app.state, "update_checker", None)
    if checker is None:
        return JSONResponse(
            {"error": "update_checker_unavailable"},
            status_code=503,
            headers={"Cache-Control": "no-store, max-age=0"},
        )
    data = await checker.check_once()
    return JSONResponse(data, headers={"Cache-Control": "no-store, max-age=0"})


@router.post("/readiness")
async def readiness(
    user: dict = Depends(require_api_super_admin),
    csrf: str = Depends(require_csrf_header),
):
    return await updater_readiness(user=user, _csrf=csrf)


async def updater_readiness(
    user: dict,
    _csrf: str,
):
    """Read-only readiness for the running build's current update channel."""

    try:
        return JSONResponse(
            await _check_current_channel(),
            headers={"Cache-Control": "no-store"},
        )
    except Exception as exc:
        return _updater_error(exc)


@router.post("/preflight")
async def preflight(
    request: Request,
    body: VersionAction,
    user: dict = Depends(require_api_super_admin),
    csrf: str = Depends(require_csrf_header),
):
    return await updater_preflight(request, user=user, _csrf=csrf)


async def updater_preflight(
    request: Request,
    user: dict,
    _csrf: str,
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


@router.post("/update")
async def update(
    request: Request,
    body: VersionAction,
    user: dict = Depends(require_api_super_admin),
    db: AsyncSession = Depends(get_db),
    csrf: str = Depends(require_csrf_header),
):
    return await updater_update(request, db=db, user=user, _csrf=csrf)


async def updater_update(
    request: Request,
    db: AsyncSession,
    user: dict,
    _csrf: str,
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
                    "target_version": (
                        target_object.get("version") if target_object else target
                    ),
                    "target_channel": target_object.get("channel")
                    if target_object
                    else None,
                    "target_revision": target_object.get("revision")
                    if target_object
                    else None,
                    "target_digest": target_object.get("digest")
                    if target_object
                    else None,
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


@router.get("/jobs/{job_id}")
async def job(job_id: str, user: dict = Depends(require_api_super_admin)):
    return await updater_job(job_id, user=user)


async def updater_job(
    job_id: str,
    user: dict,
):
    try:
        return JSONResponse(
            await UpdaterClient().get_job(job_id),
            headers={"Cache-Control": "no-store"},
        )
    except Exception as exc:
        return _updater_error(exc)


@router.get("/jobs/{job_id}/logs")
async def logs(job_id: str, user: dict = Depends(require_api_super_admin)):
    return await updater_job_logs(job_id, user=user)


async def updater_job_logs(
    job_id: str,
    user: dict,
):
    try:
        return JSONResponse(
            await UpdaterClient().get_job_logs(job_id),
            headers={"Cache-Control": "no-store"},
        )
    except Exception as exc:
        return _updater_error(exc)
