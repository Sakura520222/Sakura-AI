"""Versioned browser adapters for the existing updater control plane."""

from typing import Literal

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.ext.asyncio import AsyncSession

from backend.api.v1.deps import require_api_auth, require_api_super_admin
from backend.api.v1.private_routes import PrivateAPIRoute
from backend.webui.deps import get_db, require_csrf_header
from backend.webui.routes import version as legacy

router = APIRouter(prefix="/version", tags=["Version Management"], route_class=PrivateAPIRoute)


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


@router.get("/info")
async def info(user: dict = Depends(require_api_auth)):
    return await legacy.get_version_info(user=user)


@router.get("/releases")
async def releases(user: dict = Depends(require_api_super_admin)):
    return await legacy.get_version_releases(user=user)


@router.get("/images")
async def images(user: dict = Depends(require_api_super_admin)):
    return await legacy.get_version_images(user=user)


@router.post("/check")
async def check(request: Request, user: dict = Depends(require_api_super_admin), csrf: str = Depends(require_csrf_header)):
    return await legacy.trigger_check(request, user=user, _csrf=csrf)


@router.post("/readiness")
async def readiness(user: dict = Depends(require_api_super_admin), csrf: str = Depends(require_csrf_header)):
    return await legacy.updater_readiness(user=user, _csrf=csrf)


@router.post("/preflight")
async def preflight(request: Request, body: VersionAction, user: dict = Depends(require_api_super_admin), csrf: str = Depends(require_csrf_header)):
    return await legacy.updater_preflight(request, user=user, _csrf=csrf)


@router.post("/update")
async def update(request: Request, body: VersionAction, user: dict = Depends(require_api_super_admin), db: AsyncSession = Depends(get_db), csrf: str = Depends(require_csrf_header)):
    return await legacy.updater_update(request, db=db, user=user, _csrf=csrf)


@router.get("/jobs/{job_id}")
async def job(job_id: str, user: dict = Depends(require_api_super_admin)):
    return await legacy.updater_job(job_id, user=user)


@router.get("/jobs/{job_id}/logs")
async def logs(job_id: str, user: dict = Depends(require_api_super_admin)):
    return await legacy.updater_job_logs(job_id, user=user)
