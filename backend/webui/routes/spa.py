"""Serve the sole WebUI: the compiled React application."""

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import FileResponse, PlainTextResponse, RedirectResponse

from backend.core.config import get_settings
from backend.core.webui_distribution import WebUIDistributionError, get_distribution
from backend.webui.deps import get_current_user, require_auth

router = APIRouter(tags=["WebUI App"])


def unavailable_frontend():
    return PlainTextResponse(
        "暂时无法获取当前版本的前端资源，请稍后刷新。\n"
        "The matching frontend bundle is temporarily unavailable. Please retry shortly.",
        status_code=503,
        headers={
            "Cache-Control": "no-store",
            "Retry-After": "30",
            "X-Content-Type-Options": "nosniff",
        },
    )


async def require_app_session(request: Request):
    if request.url.path in {
        "/app/login",
        "/app/setup",
        "/app/error",
    } or request.url.path.startswith("/app/legal/"):
        return None
    try:
        if request.url.path == "/app/security":
            # Only the enrollment page may be rendered before required enrollment.
            # get_current_user still requires a full, live, active account session.
            return await get_current_user(request)
        return await require_auth(request)
    except HTTPException as exc:
        if exc.status_code in {401, 428}:
            return RedirectResponse(
                "/app/login" if exc.status_code == 401 else "/app/security",
                status_code=302,
                headers={"Cache-Control": "no-store"},
            )
        raise


@router.get("/app/assets/{asset_path:path}", include_in_schema=False)
async def app_asset(asset_path: str):
    """Serve build assets only; missing assets must never receive the SPA HTML."""
    if not asset_path or any(
        part.startswith(".") for part in asset_path.replace("\\", "/").split("/")
    ):
        raise HTTPException(status_code=404)
    try:
        dist = await get_distribution(get_settings().sakura_update_repo)
    except WebUIDistributionError:
        return unavailable_frontend()
    root = (dist / "assets").resolve()
    path = (root / asset_path).resolve()
    if not path.is_relative_to(root) or not path.is_file():
        raise HTTPException(status_code=404)
    return FileResponse(
        path,
        headers={
            "Cache-Control": "public, max-age=31536000, immutable",
            "X-Content-Type-Options": "nosniff",
        },
    )


@router.get("/app", include_in_schema=False)
@router.get("/app/{path:path}", include_in_schema=False)
async def app_index(path: str = "", user=Depends(require_app_session)):
    """Authenticate deep links using the same live account/MFA policy as WebUI."""
    if isinstance(user, RedirectResponse):
        return user
    if any(part.startswith(".") for part in path.replace("\\", "/").split("/")):
        raise HTTPException(status_code=404)
    try:
        dist = await get_distribution(get_settings().sakura_update_repo)
    except WebUIDistributionError:
        return unavailable_frontend()
    index = dist / "index.html"
    if not index.is_file():
        return unavailable_frontend()
    return FileResponse(
        index,
        media_type="text/html",
        headers={"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"},
    )
