"""Repository community actions over the existing membership and GitHub services."""

from typing import Literal
from urllib.parse import urlencode

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.encoders import jsonable_encoder
from fastapi.responses import RedirectResponse
from loguru import logger
from pydantic import BaseModel, ConfigDict
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.api.v1.console_data import action_result
from backend.api.v1.deps import (
    require_api_admin,
    require_api_auth,
    require_api_super_admin,
)
from backend.api.v1.private_routes import PrivateAPIRoute
from backend.services import star_aid_service, star_aid_summary_service
from backend.webui.deps import get_db, require_csrf_header, toast_redirect
from backend.webui.i18n import detect_language

router = APIRouter(
    prefix="/community", tags=["Repository Community"], route_class=PrivateAPIRoute
)


async def sync_repositories(*, user: dict, db: AsyncSession):
    """Refresh the repositories available for display from GitHub."""
    lang = detect_language()
    if not await star_aid_service.is_feature_enabled():
        return toast_redirect(
            "/star-aid/", "star_aid.feature_disabled", toast_type="error", lang=lang
        )
    result = await star_aid_service.refresh_available_repositories(
        db, int(user["user_id"])
    )

    await db.commit()
    if result["success"]:
        await _trigger_displayed_summaries(db, int(user["user_id"]))
        return toast_redirect("/star-aid/", "star_aid.sync_success", lang=lang)
    return toast_redirect(
        "/star-aid/", "star_aid.sync_failed", toast_type="error", lang=lang
    )


async def _trigger_displayed_summaries(db: AsyncSession, user_id: int) -> None:
    """Trigger summary refreshes for the user's displayed repositories."""
    from backend.models.star_aid_models import StarAidRepository

    result = await db.execute(
        select(StarAidRepository.id).where(
            StarAidRepository.owner_user_id == user_id,
            StarAidRepository.is_displayed.is_(True),
        )
    )
    for (repo_id,) in result.all():
        star_aid_summary_service.trigger_summary_refresh(int(repo_id))


async def refresh_summary_route(repo_id: int, *, user: dict, db: AsyncSession):
    """Manually refresh a repository summary after checking ownership."""
    from backend.models.star_aid_models import StarAidRepository

    lang = detect_language()
    repo_result = await db.execute(
        select(StarAidRepository).where(StarAidRepository.id == int(repo_id))
    )
    repo = repo_result.scalar_one_or_none()
    if repo is None:
        return toast_redirect(
            "/star-aid/", "star_aid.summary_not_found", toast_type="error", lang=lang
        )
    is_admin = user.get("role") in ("admin", "super_admin")
    if repo.owner_user_id != int(user["user_id"]) and not is_admin:
        logger.warning(
            "star_aid summary refresh forbidden: repo_id={}, requester={}",
            repo_id,
            user.get("user_id"),
        )
        return toast_redirect(
            "/star-aid/", "star_aid.summary_forbidden", toast_type="error", lang=lang
        )

    result = await star_aid_summary_service.refresh_repository_summary(
        db, int(repo_id), force=True
    )
    await db.commit()
    status = result.get("status")
    if status == "ready":
        return toast_redirect(
            "/star-aid/", "star_aid.summary_refresh_success", lang=lang
        )
    logger.warning(
        "star_aid summary refresh not ready: repo_id={}, status={}, error={}",
        repo_id,
        status,
        result.get("error"),
    )
    return toast_redirect(
        "/star-aid/", "star_aid.summary_refresh_failed", toast_type="error", lang=lang
    )


async def join_plan_route(
    *, user: dict, db: AsyncSession, repo_full_names: list[str] | None = None
):
    """Join the plan and select repositories to display."""
    github_username = user.get("sub") or ""
    result = await star_aid_service.join_plan(
        db, int(user["user_id"]), github_username, repo_full_names or []
    )
    await db.commit()
    lang = detect_language()
    if result["success"]:
        await _trigger_displayed_summaries(db, int(user["user_id"]))
        return toast_redirect("/star-aid/", "star_aid.join_success", lang=lang)
    message_key = {
        "feature_disabled": "star_aid.feature_disabled",
        "banned": "star_aid.banned",
    }.get(result.get("message"), "star_aid.join_failed")
    return toast_redirect("/star-aid/", message_key, toast_type="error", lang=lang)


async def leave_plan_route(
    *, user: dict, db: AsyncSession, unstar_created: bool = False
):
    """Leave the plan and optionally unstar repositories created by this feature."""
    result = await star_aid_service.leave_plan(
        db, int(user["user_id"]), unstar_created=unstar_created
    )
    await db.commit()
    lang = detect_language()
    if not result.get("success"):
        message_key = {
            "banned": "star_aid.leave_banned",
            "not_joined": "star_aid.leave_not_joined",
        }.get(result.get("message"), "star_aid.leave_failed")
        return toast_redirect("/star-aid/", message_key, toast_type="error", lang=lang)
    if unstar_created and result.get("unstar"):
        unstar = result["unstar"]
        if unstar.get("failed", 0) > 0:
            return toast_redirect(
                "/star-aid/",
                "star_aid.exit_unstar_failed",
                toast_type="error",
                lang=lang,
            )
        return toast_redirect("/star-aid/", "star_aid.exit_unstar_done", lang=lang)
    return toast_redirect("/star-aid/", "star_aid.leave_success", lang=lang)


async def update_repositories_route(
    *, user: dict, db: AsyncSession, repo_full_names: list[str] | None = None
):
    """Update the current user's displayed repositories."""
    await star_aid_service.select_repositories(
        db, int(user["user_id"]), repo_full_names or []
    )
    await db.commit()
    await _trigger_displayed_summaries(db, int(user["user_id"]))
    lang = detect_language()
    return toast_redirect("/star-aid/", "star_aid.selection_updated", lang=lang)


async def manual_star_route(repo_id: int, *, user: dict, db: AsyncSession):
    """Manually star a repository without changing membership state."""
    lang = detect_language()
    if not await star_aid_service.is_feature_enabled():
        return toast_redirect(
            "/star-aid/", "star_aid.feature_disabled", toast_type="error", lang=lang
        )

    result = await star_aid_service.perform_star(
        db,
        actor_user_id=int(user["user_id"]),
        repository_id=int(repo_id),
        trigger="manual",
        enforce_daily_limit=False,
    )
    await db.commit()
    if result.get("reauth_required"):
        # Build the redirect from a constant path plus urlencode so the target
        # origin is fixed by construction; safe="/" keeps "/" literal, matching
        # the previous literal query string (RFC 3986 allows "/" in queries).
        # 由常量路径 + urlencode 构造重定向，目标源在构造上即固定；safe="/"
        # 保持 "/" 原样输出，与原先的字面查询串一致（RFC 3986 允许 query 含 "/"）。
        return RedirectResponse(
            "/star-aid/auth/start?"
            + urlencode(
                {
                    "intent": "manual_star",
                    "repo_id": int(repo_id),
                    "return_to": "/star-aid/",
                },
                safe="/",
            ),
            status_code=302,
        )

    status = result.get("status")
    message, toast_type = {
        "success": ("star_aid.manual_star_success", "success"),
        "already_done": ("star_aid.manual_star_already", "success"),
    }.get(status, ("star_aid.manual_star_failed", "error"))
    return toast_redirect("/star-aid/", message, toast_type=toast_type, lang=lang)


async def ban_member_route(
    member_user_id: int, *, user: dict, db: AsyncSession, reason: str = ""
):
    """Ban a community member."""
    result = await star_aid_service.ban_member(
        db, int(user["user_id"]), int(member_user_id), reason
    )
    await db.commit()
    lang = detect_language()
    if result.get("success"):
        return toast_redirect("/star-aid/", "star_aid.ban_success", lang=lang)
    return toast_redirect(
        "/star-aid/", "star_aid.ban_failed", toast_type="error", lang=lang
    )


async def unban_member_route(member_user_id: int, *, db: AsyncSession):
    """Unban a community member."""
    result = await star_aid_service.unban_member(db, int(member_user_id))
    await db.commit()
    lang = detect_language()
    if result.get("success"):
        return toast_redirect("/star-aid/", "star_aid.unban_success", lang=lang)
    return toast_redirect(
        "/star-aid/", "star_aid.unban_failed", toast_type="error", lang=lang
    )


async def disable_repository_route(repo_id: int, *, db: AsyncSession, reason: str = ""):
    """Disable a repository from community display."""
    result = await star_aid_service.set_repository_disabled(
        db, int(repo_id), disabled=True, reason=reason
    )
    await db.commit()
    lang = detect_language()
    if result.get("success"):
        return toast_redirect("/star-aid/", "star_aid.repo_disable_success", lang=lang)
    return toast_redirect(
        "/star-aid/", "star_aid.repo_disable_failed", toast_type="error", lang=lang
    )


async def enable_repository_route(repo_id: int, *, db: AsyncSession):
    """Re-enable a community repository."""
    result = await star_aid_service.set_repository_disabled(
        db, int(repo_id), disabled=False
    )
    await db.commit()
    lang = detect_language()
    if result.get("success"):
        return toast_redirect("/star-aid/", "star_aid.repo_enable_success", lang=lang)
    return toast_redirect(
        "/star-aid/", "star_aid.repo_enable_failed", toast_type="error", lang=lang
    )


async def toggle_feature_route(*, db: AsyncSession, enabled: bool = False):
    """Toggle and apply the repository community feature flag."""
    from backend.services.system_config_service import system_config_service

    value = "true" if enabled else "false"
    changed, _ = await system_config_service.save_configs(
        db, {"star_aid_enabled": value}
    )
    await system_config_service.apply_live_settings(changed)
    lang = detect_language()
    message = "star_aid.feature_now_on" if enabled else "star_aid.feature_now_off"
    return toast_redirect("/star-aid/", message, lang=lang)


@router.get("")
async def state(
    q: str = "",
    status: str = "all",
    sort: str = "stars",
    order: str = "desc",
    page: int = Query(1, ge=1),
    member_q: str = "",
    member_status: str = "all",
    member_page: int = Query(1, ge=1),
    user=Depends(require_api_auth),
    db: AsyncSession = Depends(get_db),
):
    value = await star_aid_service.get_page_state(
        db,
        user,
        admin_repository_query={
            "q": q,
            "status": status,
            "sort": sort,
            "order": order,
            "page": page,
            "page_size": 20,
        },
        admin_member_query={
            "member_q": member_q,
            "member_status": member_status,
            "member_page": member_page,
            "member_page_size": 20,
        },
    )
    for key in ("admin_member_page", "admin_repository_page"):
        if value.get(key) is not None:
            value[key]["total_pages"] = value[key]["pages"]
    return jsonable_encoder(value)


class Membership(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")
    repositories: list[str] = []
    unstar_created: bool = False


@router.post("/membership/{action}", dependencies=[Depends(require_csrf_header)])
async def membership(
    action: Literal["join", "leave", "repositories", "sync"],
    body: Membership,
    user=Depends(require_api_auth),
    db: AsyncSession = Depends(get_db),
):
    if action == "sync":
        result = await sync_repositories(user=user, db=db)
    elif action == "leave":
        result = await leave_plan_route(
            user=user, db=db, unstar_created=body.unstar_created
        )
    elif action == "join":
        result = await join_plan_route(
            user=user, db=db, repo_full_names=body.repositories
        )
    else:
        result = await update_repositories_route(
            user=user, db=db, repo_full_names=body.repositories
        )
    return action_result(result)


@router.post(
    "/repositories/{repo_id}/{action}", dependencies=[Depends(require_csrf_header)]
)
async def repository_action(
    repo_id: int,
    action: Literal["star", "summary"],
    user=Depends(require_api_auth),
    db: AsyncSession = Depends(get_db),
):
    result = (
        await manual_star_route(repo_id, user=user, db=db)
        if action == "star"
        else await refresh_summary_route(repo_id, user=user, db=db)
    )
    if isinstance(result, RedirectResponse) and result.headers.get(
        "location", ""
    ).startswith("/star-aid/auth/start?"):
        return {
            "redirect": result.headers["location"].replace(
                "return_to=/star-aid/", "return_to=/app/community"
            )
        }
    return action_result(result)


class Moderation(BaseModel):
    reason: str = ""


@router.post(
    "/admin/{entity}/{target_id}/{action}", dependencies=[Depends(require_csrf_header)]
)
async def moderation(
    entity: Literal["members", "repositories"],
    target_id: int,
    action: Literal["ban", "unban", "disable", "enable"],
    body: Moderation,
    user=Depends(require_api_admin),
    db: AsyncSession = Depends(get_db),
):
    if entity == "members" and action == "ban":
        result = await ban_member_route(target_id, user=user, db=db, reason=body.reason)
    elif entity == "members" and action == "unban":
        result = await unban_member_route(target_id, db=db)
    elif entity == "repositories" and action == "disable":
        result = await disable_repository_route(target_id, db=db, reason=body.reason)
    elif entity == "repositories" and action == "enable":
        result = await enable_repository_route(target_id, db=db)
    else:
        raise HTTPException(404)
    return action_result(result)


class FeatureState(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")
    enabled: bool


@router.put("/feature", dependencies=[Depends(require_csrf_header)])
async def feature(
    body: FeatureState,
    user=Depends(require_api_super_admin),
    db: AsyncSession = Depends(get_db),
):
    return action_result(await toggle_feature_route(db=db, enabled=body.enabled))
