"""Compatibility redirects for the former review logs page.

Review records are now listed and inspected in the PR domain. Keep the old
addresses so bookmarks continue to work during the route migration.
"""

from fastapi import APIRouter, Depends, Request
from fastapi.responses import RedirectResponse, Response

from backend.webui.deps import require_auth

router = APIRouter(prefix="/logs", tags=["WebUI Logs"])


def _redirect(
    request: Request, destination: str, *, browser_destination: str | None = None
) -> Response:
    query = request.url.query
    suffix = f"?{query}" if query else ""
    if request.headers.get("HX-Request") == "true":
        # HTMX follows HTTP redirects inside the fragment request. Navigate the
        # top-level page instead, including clients still on the old /logs/ UI.
        return Response(
            status_code=200,
            headers={"HX-Redirect": (browser_destination or destination) + suffix},
        )
    return RedirectResponse(destination + suffix, status_code=307)


@router.get("/")
async def logs_page(request: Request, user: dict = Depends(require_auth)):
    return _redirect(request, "/pr/")


@router.get("/list-fragment")
async def logs_list_fragment(request: Request, user: dict = Depends(require_auth)):
    return _redirect(request, "/pr/list-fragment", browser_destination="/pr/")


@router.get("/{review_id}/detail-fragment")
async def log_detail_fragment(
    request: Request, review_id: int, user: dict = Depends(require_auth)
):
    return _redirect(request, f"/pr/{review_id}")
