"""Stable configuration-centre API; JSON errors never echo submitted values."""

from typing import Any

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse
from loguru import logger
from pydantic import BaseModel, ConfigDict, Field, ValidationError
from sqlalchemy.ext.asyncio import AsyncSession

from backend.api.v1.deps import limiter, require_api_auth
from backend.core.config import get_settings
from backend.services.config_center_service import ConfigError, config_center_service
from backend.webui.deps import get_csrf_serializer, get_db, require_csrf_header

router = APIRouter(prefix="/config-center", tags=["Configuration Centre"])


class FieldMutation(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    field: str = Field(max_length=512)
    version: str = Field(min_length=64, max_length=64)
    value: Any = None
    confirmed: bool = False


def config_error(error: ConfigError):
    return JSONResponse(
        {
            "error": {
                "code": error.code,
                "field": error.field_id,
                "detail": error.detail,
            }
        },
        status_code=error.status,
        headers={"Cache-Control": "no-store"},
    )


@router.get("")
async def read_config_center(
    category: str | None = None,
    language: str = "zh-CN",
    search: bool = False,
    provider: str | None = None,
    model: str | None = None,
    user: dict = Depends(require_api_auth),
    db: AsyncSession = Depends(get_db),
):
    try:
        fields = await config_center_service.describe(
            db,
            user,
            category=category,
            lang=language if language in {"en", "zh-CN"} else "zh-CN",
            search=search,
            model=(provider, model) if provider and model else None,
        )
        from backend.webui.i18n import i18n

        messages = i18n.namespace("config_center", language)
        groups = {}
        for field in fields:
            group = field["group"]
            for key in (
                f"config_center.group.{group}",
                f"config.group.{group}",
                f"system_config.group_{group}",
                f"config.{group.removeprefix('strategy.').removeprefix('label.')}",
            ):
                text = i18n.t(key, lang=language)
                if text != key:
                    groups[group] = text
                    break
        ui_messages = (
            {
                f"{prefix}.{key}": value
                for prefix in ("settings", "auth", "common")
                for key, value in i18n.namespace(prefix, language).items()
            }
            if category == "personal"
            else {}
        )
        return JSONResponse(
            {
                "fields": fields,
                "messages": messages,
                "groups": groups,
                "uiMessages": ui_messages,
                "syncIntervalMs": get_settings().config_sync_interval_seconds * 1000,
                "csrf": get_csrf_serializer().dumps({}),
            },
            headers={"Cache-Control": "no-store"},
        )
    except ConfigError as error:
        return config_error(error)


async def _mutate(request, db, user, *, reset=False):
    try:
        if int(request.headers.get("content-length", "0")) > 1_048_576:
            raise ConfigError("payload_too_large", status=413)
        chunks, size = [], 0
        async for chunk in request.stream():
            size += len(chunk)
            if size > 1_048_576:
                raise ConfigError("payload_too_large", status=413)
            chunks.append(chunk)
        raw = b"".join(chunks)
        body = FieldMutation.model_validate_json(raw)
        if not reset and "value" not in body.model_fields_set:
            raise ConfigError("invalid_request")
        value = await config_center_service.mutate(
            db,
            user,
            body.field,
            value=body.value,
            version=body.version,
            confirmed=body.confirmed,
            reset=reset,
        )
        return JSONResponse({"field": value}, headers={"Cache-Control": "no-store"})
    except ValidationError:
        return config_error(ConfigError("invalid_request"))
    except ConfigError as error:
        return config_error(error)
    except Exception:
        logger.exception("Configuration request failed")
        return config_error(ConfigError("storage_unavailable", status=503))


@router.patch(
    "",
    openapi_extra={
        "requestBody": {
            "required": True,
            "content": {
                "application/json": {"schema": FieldMutation.model_json_schema()}
            },
        }
    },
)
@limiter.limit("120/minute")
async def patch_config_center(
    request: Request,
    user: dict = Depends(require_api_auth),
    db: AsyncSession = Depends(get_db),
    _csrf: str = Depends(require_csrf_header),
):
    return await _mutate(request, db, user)


@router.post(
    "/reset",
    openapi_extra={
        "requestBody": {
            "required": True,
            "content": {
                "application/json": {"schema": FieldMutation.model_json_schema()}
            },
        }
    },
)
@limiter.limit("120/minute")
async def reset_config_field(
    request: Request,
    user: dict = Depends(require_api_auth),
    db: AsyncSession = Depends(get_db),
    _csrf: str = Depends(require_csrf_header),
):
    return await _mutate(request, db, user, reset=True)
