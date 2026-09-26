"""Compatibility redirects for the former review logs page.

Review records are now listed and inspected in the PR domain. Keep the old
addresses so bookmarks continue to work during the route migration.
"""

from urllib.parse import parse_qsl, urlencode, urlsplit

from fastapi import APIRouter, Depends, Request
from fastapi.responses import RedirectResponse, Response

from backend.webui.deps import require_auth

router = APIRouter(prefix="/logs", tags=["WebUI Logs"])

_LEGACY_QUERY_FIELDS = frozenset(
    {
        "search",
        "repo",
        "status",
        "decision",
        "date_from",
        "date_to",
        "page",
        "per_page",
    }
)


def _legacy_redirect_url(destination: str, raw_query: str) -> str:
    """Build an internal redirect while forwarding only legacy list filters."""
    parsed = urlsplit(destination)
    if (
        parsed.scheme
        or parsed.netloc
        or parsed.query
        or parsed.fragment
        or not parsed.path.startswith("/")
        or parsed.path.startswith("//")
    ):
        raise ValueError("legacy redirect destination must be an internal path")

    forwarded_query = urlencode(
        [
            (key, value)
            for key, value in parse_qsl(raw_query, keep_blank_values=True)
            if key in _LEGACY_QUERY_FIELDS
        ],
        doseq=True,
    )
    suffix = f"?{forwarded_query}" if forwarded_query else ""
    return parsed.path + suffix


def _redirect(
    request: Request, destination: str, *, browser_destination: str | None = None
) -> Response:
    redirect_url = _legacy_redirect_url(destination, request.url.query)
    if request.headers.get("HX-Request") == "true":
        # HTMX follows HTTP redirects inside the fragment request. Navigate the
        # top-level page instead, including clients still on the old /logs/ UI.
        browser_url = _legacy_redirect_url(
            browser_destination or destination,
            request.url.query,
        )
        return Response(
            status_code=200,
            headers={"HX-Redirect": browser_url},
        )
    return RedirectResponse(redirect_url, status_code=307)


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
    return _redirect(request, "/pr/" + str(review_id))
