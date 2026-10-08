"""Localized configuration feedback shared by AJAX and legacy form saves."""

import json
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from fastapi.responses import JSONResponse

from backend.webui.deps import toast_redirect
from backend.webui.i18n import detect_language, i18n


async def config_api_language(db, user):
    """Bearer and cookie API clients share the trusted user's saved preference."""
    from sqlalchemy import select

    from backend.models.database import WebUIConfig

    row = (
        await db.execute(
            select(WebUIConfig).where(WebUIConfig.user_id == user["user_id"])
        )
    ).scalar_one_or_none()
    language = getattr(row, "language", None)
    return (
        detect_language({"language": language})
        if isinstance(language, str)
        else detect_language()
    )


def config_issue(
    field,
    code,
    message_key,
    *,
    lang,
    params=None,
    help_url=None,
    help_label_key=None,
    details=None,
    field_label_key=None,
    anchor=None,
):
    label_key = field_label_key or f"config.label.{field}"
    label = i18n.t(label_key, lang=lang)
    if label == label_key:
        label = field
    context = {**(params or {}), "field_key": label}
    issue = {
        "field": field,
        "code": code,
        "message": i18n.t(message_key, lang=lang, **context),
        "params": params or {},
    }
    if details:
        issue["details"] = list(details)
    if anchor:
        issue["anchor"] = anchor
    if help_url and help_url.startswith("/") and not help_url.startswith("//"):
        issue["help_url"] = help_url
        issue["help_label"] = (
            i18n.t(help_label_key, lang=lang)
            if help_label_key
            else i18n.t("config.save_resolve", lang=lang)
        )
    return issue


def config_save_response(request, url, toast_key, *, lang, errors=None, **fmt):
    issues = errors or []
    if issues and "error" not in fmt:
        fmt["error"] = issues[0]["message"]
    message = i18n.t(toast_key, lang=lang, **fmt)
    headers = getattr(request, "headers", {})
    if getattr(request, "config_ajax", False) or "application/json" in headers.get(
        "accept", ""
    ):
        return JSONResponse(
            {"ok": not issues, "toast": message, "errors": issues},
            status_code=400 if issues else 200,
        )
    response = toast_redirect(url, message, "error" if issues else "success")
    if issues:
        parsed = urlsplit(response.headers["location"])
        query = parse_qsl(parsed.query, keep_blank_values=True)
        query.append(
            ("_errors", json.dumps(issues, ensure_ascii=False, separators=(",", ":")))
        )
        response.headers["location"] = urlunsplit(
            parsed._replace(query=urlencode(query))
        )
    return response
