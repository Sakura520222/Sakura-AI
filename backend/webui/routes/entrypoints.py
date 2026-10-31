"""Canonical React entry points and redirects for saved links."""

from urllib.parse import quote, urlencode

from fastapi import APIRouter, Request
from fastapi.responses import RedirectResponse

router = APIRouter(include_in_schema=False)


def redirect_page(target: str):
    async def endpoint(request: Request):
        values = {
            key: quote(str(value), safe="")
            for key, value in request.path_params.items()
        }
        destination = target.format(**values)
        query = [
            (key, value)
            for key, value in request.query_params.multi_items()
            if key not in {"token", "access_token", "csrf_token"}
        ]
        if (
            request.url.path.rstrip("/") == "/agent-team"
            and request.query_params.get("task_id", "").isdigit()
        ):
            destination = f"/app/agent/{request.query_params['task_id']}"
            query = [(key, value) for key, value in query if key != "task_id"]
        if query:
            destination += ("&" if "?" in destination else "?") + urlencode(query)
        return RedirectResponse(
            destination, status_code=302, headers={"Cache-Control": "no-store"}
        )

    return endpoint


PAGES = {
    "/": "/app/",
    "/about": "/app/settings/about",
    "/setup": "/app/setup",
    "/setup/verify": "/app/setup",
    "/auth/login": "/app/login",
    "/auth/2fa": "/app/login",
    "/pr/{record_id}/files": "/app/reviews/{record_id}/files",
    "/pr/{record_id}": "/app/reviews/{record_id}",
    "/pr": "/app/reviews",
    "/issues/{record_id}": "/app/issues/{record_id}",
    "/issues": "/app/issues",
    "/repos": "/app/repositories",
    "/github-app": "/app/github-app",
    "/queue": "/app/queue",
    "/logs/actions": "/app/admin/logs",
    "/logs": "/app/reviews",
    "/scans/{record_id}": "/app/scans/{record_id}",
    "/scans": "/app/scans",
    "/users/{record_id}": "/app/admin/users/{record_id}",
    "/users": "/app/admin/users",
    "/settings": "/app/settings/personal",
    "/settings-center": "/app/settings/personal",
    "/settings-center/{category}": "/app/settings/{category}",
    "/config/ai": "/app/settings/ai",
    "/config/backup": "/app/settings/backup",
    "/config": "/app/settings/review",
    "/system-config": "/app/settings/system",
    "/announcements/admin": "/app/admin/announcements",
    "/announcements/{record_id}": "/app/notifications/announcements/{record_id}",
    "/announcements": "/app/notifications/announcements",
    "/agent-team": "/app/agent",
    "/agent-skills": "/app/agent/skills",
    "/agent-plugins": "/app/settings/agent?panel=plugins",
    "/activity/observability": "/app/activity",
    "/activity": "/app/activity",
    "/security/users/{record_id}": "/app/admin/security/{record_id}",
    "/security": "/app/admin/security",
    "/billing/admin/plans": "/app/admin/plans",
    "/billing/admin/codes": "/app/admin/codes",
    "/billing/admin/pricing": "/app/admin/pricing",
    "/billing/admin/refund-requests": "/app/admin/refunds",
    "/billing/credits": "/app/billing/credits",
    "/billing/payment/result": "/app/billing/result",
    "/billing/crypto-payment/{order_no}": "/app/billing/invoice/{order_no}",
    "/billing": "/app/billing",
    "/version/manager": "/app/admin/version",
    "/star-aid": "/app/community",
    "/sakura-memory/{owner}/{repo}/file/{file_path:path}": "/app/memory?repo={owner}%2F{repo}&file={file_path}",
    "/sakura-memory/{owner}/{repo}": "/app/memory?repo={owner}%2F{repo}",
    "/sakura-memory": "/app/memory",
    "/vector-db": "/app/admin/storage",
    "/vector-db/collection/{name}": "/app/admin/storage/{name}",
    "/terms": "/app/legal/terms",
    "/privacy": "/app/legal/privacy",
    "/refund": "/app/legal/refund",
    "/pricing": "/app/legal/pricing",
}
for path, target in PAGES.items():
    router.add_api_route(path, redirect_page(target), methods=["GET"])
    if path != "/" and not path.endswith("{file_path:path}"):
        router.add_api_route(path + "/", redirect_page(target), methods=["GET"])
