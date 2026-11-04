"""Versioned JSON adapters for existing backup and maintenance services."""

from typing import Literal

from fastapi import APIRouter, Depends, File, Form, Request, Response, UploadFile
from fastapi.responses import JSONResponse
from loguru import logger
from pydantic import BaseModel, ConfigDict, Field, JsonValue, ValidationError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.api.v1.deps import require_api_super_admin
from backend.core.config import get_settings
from backend.core.log_sanitizer import sensitive_log_scope
from backend.core.time_service import filename_timestamp
from backend.models.database import AppConfig
from backend.services import (
    config_backup_service,
    user_backup_service,
)
from backend.services import (
    system_maintenance as maintenance,
)
from backend.services.agent_team.capability_policy import PermissionProfile
from backend.services.agent_team.plugin_config import (
    HarnessPluginConfig,
    PluginConfigError,
    public_plugin_config,
    read_plugin_config,
    save_plugin_config,
)
from backend.services.config_backup_service import (
    ConfigBackupError,
    export_config_backup,
    serialize_config_backup,
)
from backend.services.database_reset_service import DATABASE_RESET_CONFIRMATION
from backend.services.user_backup_service import (
    UserBackupError,
    export_user_backup,
    serialize_user_backup,
)
from backend.webui.deps import (
    get_db,
    get_user_preferences,
    invalidate_user_prefs_cache,
    require_csrf,
    require_csrf_header,
    require_super_admin,
    toast_redirect,
)
from backend.webui.helpers.admin_log import log_admin_action
from backend.webui.i18n import detect_language, i18n

router = APIRouter(prefix="/webui", tags=["WebUI settings"])
PRIVATE_HEADERS = {"Cache-Control": "no-store, max-age=0"}


class BackupMetadata(BaseModel):
    scopes: list[str]
    config_max_bytes: int
    users_max_bytes: int


class ImportResult(BaseModel):
    persisted: bool = True
    kind: Literal["config", "users"]
    sections: list[str]
    created: int
    updated: int
    deleted: int
    unchanged: int
    requires_restart: bool = False
    runtime_refresh_ok: bool = True
    audit_recorded: bool = True
    recovery_codes_portable: bool | None = None


class SystemMetadata(BaseModel):
    database_reset_confirmation: str


class ConnectionTest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    type: Literal["database", "redis"]
    use_saved: Literal[True]


class PluginMutation(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    plugins: dict[str, JsonValue]
    profile: str


class AppSlugRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    client_id: str = Field(alias="clientId", min_length=1, max_length=512)


class ResetRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    confirmation: str = Field(max_length=200)
    language: Literal["zh-CN", "en"] = "zh-CN"


def _failure(code: str, status: int = 422) -> JSONResponse:
    return JSONResponse(
        {"error": {"code": code}}, status_code=status, headers=PRIVATE_HEADERS
    )


def _config_language(request, user_prefs=None):
    carried = getattr(request, "config_language", None)
    if carried in {"zh-CN", "en"}:
        return carried
    return (
        detect_language(user_prefs)
        if isinstance(user_prefs, dict)
        else detect_language()
    )


async def download_user_backup(
    db: AsyncSession = Depends(get_db),
    user: dict = Depends(require_super_admin),
    csrf_token: str = Depends(require_csrf),
    user_prefs: dict = Depends(get_user_preferences),
):
    """下载全部用户及个人配置、两步验证和通行密钥的 JSON 备份。"""
    lang = _config_language(None, user_prefs)
    try:
        document = await export_user_backup(db)
        content = serialize_user_backup(document)
        counts = {
            "users": document.get("user_count", len(document.get("users", []))),
            "personal_configs": sum(
                len(item.get("personal_config", {}).get("dynamic_overrides", []))
                for item in document.get("users", [])
            ),
            "recovery_codes": sum(
                len(item.get("two_factor", {}).get("recovery_codes", []))
                for item in document.get("users", [])
            ),
            "passkeys": sum(
                len(item.get("passkeys", [])) for item in document.get("users", [])
            ),
        }
        await log_admin_action(
            db,
            user["user_id"],
            "user_export",
            "users",
            "all",
            {"scope": "users", "counts": counts},
        )

        timestamp = filename_timestamp()
        filename = f"sakura-ai-users-{timestamp}.json"
        logger.info(
            "用户信息备份已导出, by={}, counts={}",
            user["sub"],
            counts,
        )
        return Response(
            content=content,
            media_type="application/json",
            headers={
                "Content-Disposition": f'attachment; filename="{filename}"',
                "Cache-Control": "no-store, max-age=0",
                "Pragma": "no-cache",
                "X-Content-Type-Options": "nosniff",
            },
        )
    except UserBackupError as exc:
        return toast_redirect(
            "/config/backup",
            "toast.user_backup_export_failed",
            "error",
            lang=lang,
            reason=str(exc),
        )
    except Exception as exc:
        logger.opt(exception=True).error("用户信息备份导出失败: {}", exc)
        return toast_redirect(
            "/config/backup",
            "toast.user_backup_export_failed",
            "error",
            lang=lang,
            reason="internal error",
        )


async def download_config_backup(
    scope: str,
    db: AsyncSession = Depends(get_db),
    user: dict = Depends(require_super_admin),
    csrf_token: str = Depends(require_csrf),
    user_prefs: dict = Depends(get_user_preferences),
):
    """下载全局、AI、系统配置或完整的版本化 JSON 备份。"""
    lang = _config_language(None, user_prefs)
    try:
        document = await export_config_backup(db, scope)
        content = serialize_config_backup(document)
        counts = {
            section: data["count"] for section, data in document["sections"].items()
        }
        await log_admin_action(
            db,
            user["user_id"],
            "config_export",
            "config",
            scope,
            {"scope": scope, "counts": counts},
        )

        timestamp = filename_timestamp()
        filename = f"sakura-ai-config-{scope}-{timestamp}.json"
        logger.info(
            "配置备份已导出, by={}, scope={}, counts={}",
            user["sub"],
            scope,
            counts,
        )
        return Response(
            content=content,
            media_type="application/json",
            headers={
                "Content-Disposition": f'attachment; filename="{filename}"',
                "Cache-Control": "no-store, max-age=0",
                "Pragma": "no-cache",
                "X-Content-Type-Options": "nosniff",
            },
        )
    except ConfigBackupError as exc:
        return toast_redirect(
            "/config/backup",
            "toast.config_backup_export_failed",
            "error",
            lang=lang,
            reason=str(exc),
        )
    except Exception as exc:
        logger.opt(exception=True).error("配置备份导出失败: {}", exc)
        return toast_redirect(
            "/config/backup",
            "toast.config_backup_export_failed",
            "error",
            lang=lang,
            reason="internal error",
        )


@router.get("/backup", response_model=BackupMetadata)
async def backup_metadata(
    response: Response, _user: dict = Depends(require_api_super_admin)
):
    response.headers.update(PRIVATE_HEADERS)
    return BackupMetadata(
        scopes=["all", "global", "ai", "system", "users"],
        config_max_bytes=config_backup_service.BACKUP_MAX_BYTES,
        users_max_bytes=user_backup_service.USER_BACKUP_MAX_BYTES,
    )


@router.post("/backup/export/{scope}")
async def export_backup(
    scope: Literal["all", "global", "ai", "system", "users"],
    language: Literal["zh-CN", "en"] = "zh-CN",
    db: AsyncSession = Depends(get_db),
    user: dict = Depends(require_api_super_admin),
    csrf: str = Depends(require_csrf_header),
):
    """Reuse the established export format, authorization context and audit."""
    kwargs = {
        "db": db,
        "user": user,
        "csrf_token": csrf,
        "user_prefs": {"language": language},
    }
    result = (
        await download_user_backup(**kwargs)
        if scope == "users"
        else await download_config_backup(scope=scope, **kwargs)
    )
    # Legacy failure responses redirect to a template; API consumers need a safe error.
    if 300 <= result.status_code < 400:
        return _failure("backup_export_failed", 503)
    return result


@router.post("/backup/import", response_model=ImportResult)
async def import_backup(
    response: Response,
    backup_file: UploadFile = File(...),
    kind: Literal["config", "users"] = Form("config"),
    db: AsyncSession = Depends(get_db),
    user: dict = Depends(require_api_super_admin),
    _csrf: str = Depends(require_csrf_header),
):
    """Keep restore semantics in existing services; return only committed counters."""
    response.headers.update(PRIVATE_HEADERS)
    restored = None
    result = None
    try:
        limit = (
            user_backup_service.USER_BACKUP_MAX_BYTES
            if kind == "users"
            else config_backup_service.BACKUP_MAX_BYTES
        )
        content = await backup_file.read(limit + 1)
        if len(content) > limit:
            return _failure("backup_too_large", 413)
        if kind == "users":
            document = user_backup_service.parse_user_backup(content)
            restored = await user_backup_service.restore_user_backup(db, document)
            result = ImportResult(
                kind="users",
                sections=["users"],
                created=restored.created,
                updated=restored.updated,
                deleted=restored.deleted,
                unchanged=restored.unchanged,
                recovery_codes_portable=restored.recovery_codes_portable,
            )
            for user_id in restored.affected_user_ids:
                invalidate_user_prefs_cache(user_id)
        else:
            sections = config_backup_service.parse_config_backup(content)
            restored = await config_backup_service.restore_config_backup(
                db, sections, allow_database_url=False
            )
            result = ImportResult(
                kind="config",
                sections=list(restored.sections),
                created=restored.created,
                updated=restored.updated,
                deleted=restored.deleted,
                unchanged=restored.unchanged,
                requires_restart=restored.requires_restart,
            )
            try:
                config_backup_service.refresh_imported_runtime_config(restored)
            except Exception:
                logger.exception("Backup committed; runtime refresh requires restart")
                result.runtime_refresh_ok = False
                result.requires_restart = True
        try:
            await log_admin_action(
                db,
                user["user_id"],
                "user_import" if kind == "users" else "config_import",
                "users" if kind == "users" else "config",
                ",".join(result.sections),
                result.model_dump(exclude_none=True),
            )
        except Exception:
            await db.rollback()
            logger.exception("Backup committed; import audit could not be recorded")
            result.audit_recorded = False
        return result
    except config_backup_service.ConfigBackupError, user_backup_service.UserBackupError:
        await db.rollback()
        return _failure("invalid_backup")
    except Exception:
        logger.exception("Backup import failed")
        if result is not None:
            result.runtime_refresh_ok = False
            result.requires_restart = True
            result.audit_recorded = False
            return result
        await db.rollback()
        return _failure("backup_import_failed", 503)
    finally:
        await backup_file.close()


@router.get("/system", response_model=SystemMetadata)
async def system_metadata(
    response: Response, _user: dict = Depends(require_api_super_admin)
):
    response.headers.update(PRIVATE_HEADERS)
    return SystemMetadata(database_reset_confirmation=DATABASE_RESET_CONFIRMATION)


@router.post("/system/test-connection")
async def test_connection(
    request: Request,
    _body: ConnectionTest,
    user: dict = Depends(require_api_super_admin),
    _csrf: str = Depends(require_csrf_header),
):
    return JSONResponse(
        await maintenance.test_connection(request, user, _csrf), headers=PRIVATE_HEADERS
    )


@router.post("/system/github-app-slug")
async def github_app_slug(
    request: Request,
    _body: AppSlugRequest,
    language: Literal["zh-CN", "en"] = "zh-CN",
    user: dict = Depends(require_api_super_admin),
    _csrf: str = Depends(require_csrf_header),
):
    result = await maintenance.fetch_github_app_slug(
        request, user, {"language": language}, _csrf
    )
    return JSONResponse(result, headers=PRIVATE_HEADERS)


@router.post("/system/restart")
async def restart(
    db: AsyncSession = Depends(get_db),
    user: dict = Depends(require_api_super_admin),
    csrf: str = Depends(require_csrf_header),
):
    return await maintenance.restart_application(db, user, csrf)


@router.post("/system/reset-database")
async def reset_database(
    request: Request,
    _body: ResetRequest,
    user: dict = Depends(require_api_super_admin),
    csrf: str = Depends(require_csrf_header),
):
    return await maintenance.reset_database(request, user, csrf)


async def _public_state(db):
    config = await read_plugin_config(db)
    result = await db.execute(
        select(AppConfig).where(AppConfig.key_name == "agent_team_permission_profile")
    )
    row = result.scalar_one_or_none()
    profile = row.key_value if row else get_settings().agent_team_permission_profile
    try:
        PermissionProfile(profile)
    except ValueError:
        raise PluginConfigError("invalid_permission_profile") from None
    return {"plugins": public_plugin_config(config), "profile": profile}


@router.get("/plugins")
async def plugin_settings(
    response: Response,
    language: Literal["zh-CN", "en"] = "zh-CN",
    db: AsyncSession = Depends(get_db),
    _user: dict = Depends(require_api_super_admin),
):
    response.headers.update(PRIVATE_HEADERS)
    try:
        state = await _public_state(db)
    except PluginConfigError:
        return _failure("plugin_configuration_unavailable", 503)
    schema = HarnessPluginConfig.model_json_schema()
    fields = schema["$defs"]["MCPServerConfig"]["properties"]
    fields["timeout_seconds"]["default"] = (
        get_settings().agent_team_mcp_io_timeout_seconds
    )
    fields["headers"]["additionalProperties"]["format"] = "password"
    for key in ("id", "url"):
        fields[key]["minLength"] = 1
    schema["$defs"]["HookConfig"]["properties"]["id"]["minLength"] = 1
    schema["properties"]["mcp_repository_scopes"]["x-keyLabel"] = "editor_server_id"
    return {
        **state,
        "schema": schema,
        "profiles": [profile.value for profile in PermissionProfile],
        "messages": {
            **i18n.namespace("config_center", language),
            **i18n.namespace("agent_plugins.form", language),
        },
    }


@router.put("/plugins")
async def update_plugins(
    request: Request,
    db: AsyncSession = Depends(get_db),
    user: dict = Depends(require_api_super_admin),
    _csrf: str = Depends(require_csrf_header),
):
    # Parse manually so validation errors never echo a credential-bearing request.
    chunks, size = [], 0
    async for chunk in request.stream():
        size += len(chunk)
        if size > 1_048_576:
            return _failure("payload_too_large", 413)
        chunks.append(chunk)
    try:
        body = PluginMutation.model_validate_json(b"".join(chunks))
    except ValidationError:
        return _failure("invalid_plugin_configuration")
    secrets = []
    mcp = body.plugins.get("mcp")
    if isinstance(mcp, dict) and isinstance(mcp.get("servers"), list):
        for server in mcp["servers"]:
            if isinstance(server, dict) and isinstance(server.get("headers"), dict):
                secrets.extend(
                    value
                    for value in server["headers"].values()
                    if isinstance(value, str)
                )
    with sensitive_log_scope(*secrets):
        try:
            config = await save_plugin_config(db, body.plugins, body.profile)
        except PluginConfigError:
            await db.rollback()
            return _failure("invalid_plugin_configuration")
        audit_recorded = True
        try:
            await log_admin_action(
                db,
                user["user_id"],
                "config_save",
                "agent_plugins",
                None,
                {
                    "mcp_count": len(config.mcp.servers),
                    "hook_count": len(config.hooks),
                    "profile": body.profile,
                },
            )
        except Exception:
            await db.rollback()
            logger.exception(
                "Plugin configuration committed; audit could not be recorded"
            )
            audit_recorded = False
    return JSONResponse(
        {
            "plugins": public_plugin_config(config),
            "profile": body.profile,
            "persisted": True,
            "audit_recorded": audit_recorded,
        },
        headers=PRIVATE_HEADERS,
    )
