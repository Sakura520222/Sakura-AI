"""Repository community actions over the existing membership and GitHub services."""

from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.encoders import jsonable_encoder
from fastapi.responses import RedirectResponse
from pydantic import BaseModel, ConfigDict
from sqlalchemy.ext.asyncio import AsyncSession

from backend.api.v1.console_data import action_result
from backend.api.v1.deps import (
    require_api_admin,
    require_api_auth,
    require_api_super_admin,
)
from backend.api.v1.private_routes import PrivateAPIRoute
from backend.services import star_aid_service as service
from backend.webui.deps import get_db, require_csrf_header
from backend.webui.routes import star_aid as actions

router = APIRouter(
    prefix="/community", tags=["Repository Community"], route_class=PrivateAPIRoute
)


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
    value = await service.get_page_state(
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
        result = await actions.sync_repositories(user=user, db=db)
    elif action == "leave":
        result = await actions.leave_plan_route(
            user=user, db=db, unstar_created=body.unstar_created
        )
    elif action == "join":
        result = await actions.join_plan_route(
            user=user, db=db, repo_full_names=body.repositories
        )
    else:
        result = await actions.update_repositories_route(
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
        await actions.manual_star_route(repo_id, user=user, db=db)
        if action == "star"
        else await actions.refresh_summary_route(repo_id, user=user, db=db)
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
        result = await actions.ban_member_route(
            target_id, user=user, db=db, reason=body.reason
        )
    elif entity == "members" and action == "unban":
        result = await actions.unban_member_route(target_id, db=db)
    elif entity == "repositories" and action == "disable":
        result = await actions.disable_repository_route(
            target_id, db=db, reason=body.reason
        )
    elif entity == "repositories" and action == "enable":
        result = await actions.enable_repository_route(target_id, db=db)
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
    return action_result(
        await actions.toggle_feature_route(db=db, enabled=body.enabled)
    )
