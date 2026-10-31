"""One React WebUI; only protocol callbacks remain outside the versioned API."""

from fastapi import APIRouter, Depends

from backend.webui.deps import mark_webui_request
from backend.webui.routes import assetlinks, auth, entrypoints, spa, star_aid

webui_router = APIRouter(dependencies=[Depends(mark_webui_request)])
webui_router.include_router(entrypoints.router)
webui_router.include_router(spa.router)
webui_router.include_router(assetlinks.router)

# Keep OAuth callbacks and already-issued authorization states valid.
# Page renderers and HTML fragment routes are no longer mounted.
for source, names in (
    (
        auth.router,
        {
            "github_login",
            "github_callback",
            "logout",
            "passkey_discover_options",
            "passkey_verify_discover",
            "two_factor_passkey_options",
            "two_factor_passkey_verify",
        },
    ),
    (star_aid.router, {"auth_start", "auth_callback"}),
):
    callbacks = APIRouter()
    callbacks.routes.extend(route for route in source.routes if route.name in names)
    webui_router.include_router(callbacks)
