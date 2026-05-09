"""WebUI 个人设置路由"""

from datetime import datetime

from fastapi import APIRouter, Request, Depends, Form, Body, HTTPException
from fastapi.responses import JSONResponse
from loguru import logger
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.core.config import (
    get_user_dynamic_config_state,
    invalidate_user_dynamic_config_cache,
    validate_user_dynamic_config_value,
)
from backend.models.database import UserConfig, WebUIConfig
from backend.models.telegram_models import TelegramUser, UserWebAuthnCredential
from backend.services.two_factor_service import (
    TwoFactorError,
    TwoFactorReplayError,
    count_unused_recovery_codes,
    create_totp_setup,
    disable_totp,
    encrypt_totp_secret,
    replace_recovery_codes,
    verify_totp_secret,
    verify_user_totp,
)
from backend.services.webauthn_service import (
    WebAuthnError,
    begin_registration,
    finish_registration,
)
from backend.webui.deps import (
    require_auth,
    get_db,
    get_templates,
    get_csrf_serializer,
    require_csrf,
    get_user_preferences,
    toast_redirect,
    invalidate_user_prefs_cache,
    render_template,
    user_requires_mfa_enrollment,
)
from backend.webui.i18n import detect_language

router = APIRouter(prefix="/settings", tags=["WebUI Settings"])
templates = get_templates()


def _request_origin(request: Request) -> str:
    """获取当前请求 Origin，用于 WebAuthn RP 配置推导。"""
    return request.headers.get("origin") or f"{request.url.scheme}://{request.url.netloc}"


@router.get("/")
async def settings_page(
    request: Request,
    db: AsyncSession = Depends(get_db),
    user: dict = Depends(require_auth),
    user_prefs: dict = Depends(get_user_preferences),
):
    """渲染个人设置页面"""
    user_id = int(user["user_id"])
    output_language_config = await get_user_dynamic_config_state(
        "output_language", user_id
    )
    result = await db.execute(select(TelegramUser).where(TelegramUser.id == user_id))
    db_user = result.scalar_one_or_none()
    recovery_code_count = await count_unused_recovery_codes(db, user_id) if db_user else 0
    passkeys = await _get_user_passkeys(db, user_id)
    mfa_enrollment_required = (
        await user_requires_mfa_enrollment(user_id, db) if db_user else False
    )
    return render_template(
        "settings.html",
        request,
        user_prefs=user_prefs,
        current_user=user,
        csrf_token=get_csrf_serializer().dumps({}),
        active_page="settings",
        items_per_page=user_prefs["items_per_page"],
        language=user_prefs["language"],
        output_language_config=output_language_config,
        output_language=output_language_config["user_value"]
        if output_language_config["user_value"] is not None
        else "",
        two_factor_enabled=bool(db_user and db_user.totp_enabled),
        two_factor_allowed=True,
        recovery_code_count=recovery_code_count,
        totp_setup=None,
        recovery_codes=None,
        passkeys=passkeys,
        mfa_enrollment_required=mfa_enrollment_required,
    )


async def _get_user_passkeys(
    db: AsyncSession, user_id: int
) -> list[UserWebAuthnCredential]:
    result = await db.execute(
        select(UserWebAuthnCredential)
        .where(UserWebAuthnCredential.user_id == user_id)
        .order_by(UserWebAuthnCredential.created_at.desc())
    )
    return list(result.scalars().all())


@router.post("/")
async def save_settings(
    request: Request,
    db: AsyncSession = Depends(get_db),
    user: dict = Depends(require_auth),
    csrf_token: str = Depends(require_csrf),
    items_per_page: int = Form(...),
    language: str = Form(default="zh-CN"),
    output_language: str = Form(default=""),
):
    """保存个人设置"""
    user_id = int(user["user_id"])

    # 验证参数范围
    if items_per_page not in (10, 20, 50, 100):
        return toast_redirect(
            "/webui/settings/",
            "toast.invalid_param",
            "error",
            lang=detect_language({"language": language}),
        )

    # 验证语言参数
    if language not in ("zh-CN", "en"):
        language = "zh-CN"

    try:
        normalized_output_language = validate_user_dynamic_config_value(
            "output_language", output_language
        )
    except ValueError:
        return toast_redirect(
            "/webui/settings/",
            "toast.invalid_param",
            "error",
            lang=detect_language({"language": language}),
        )

    # Upsert 配置
    result = await db.execute(
        select(WebUIConfig).where(WebUIConfig.user_id == user_id)
    )
    config = result.scalar_one_or_none()
    if config:
        config.items_per_page = items_per_page
        config.language = language
    else:
        config = WebUIConfig(
            user_id=user_id,
            items_per_page=items_per_page,
            language=language,
        )
        db.add(config)

    result = await db.execute(
        select(UserConfig).where(
            UserConfig.user_id == user_id,
            UserConfig.config_key == "output_language",
        )
    )
    user_config = result.scalar_one_or_none()
    if user_config:
        user_config.config_value = normalized_output_language
        user_config.description = "AI 输出语言"
    else:
        db.add(
            UserConfig(
                user_id=user_id,
                config_key="output_language",
                config_value=normalized_output_language,
                description="AI 输出语言",
            )
        )
    await db.commit()

    invalidate_user_prefs_cache(user_id)
    invalidate_user_dynamic_config_cache(user_id, ["output_language"])

    logger.info(
        f"WebUI 设置已更新: user={user['sub']}, items_per_page={items_per_page}, "
        f"language={language}, output_language={normalized_output_language or 'inherit'}"
    )
    return toast_redirect(
        "/webui/settings/",
        "toast.settings_saved",
        lang=detect_language({"language": language}),
    )


@router.post("/2fa/setup")
async def start_two_factor_setup(
    request: Request,
    db: AsyncSession = Depends(get_db),
    user: dict = Depends(require_auth),
    csrf_token: str = Depends(require_csrf),
    user_prefs: dict = Depends(get_user_preferences),
):
    """开始 TOTP 设置，展示二维码。"""
    user_id = int(user["user_id"])
    result = await db.execute(select(TelegramUser).where(TelegramUser.id == user_id))
    db_user = result.scalar_one_or_none()
    if not db_user:
        return toast_redirect("/webui/settings/", "toast.login_required", "error")

    setup = create_totp_setup(db_user)
    return render_template(
        "settings.html",
        request,
        user_prefs=user_prefs,
        current_user=user,
        csrf_token=get_csrf_serializer().dumps({}),
        active_page="settings",
        items_per_page=user_prefs["items_per_page"],
        language=user_prefs["language"],
        output_language_config=await get_user_dynamic_config_state(
            "output_language", user_id
        ),
        output_language="",
        two_factor_enabled=bool(db_user.totp_enabled),
        two_factor_allowed=True,
        recovery_code_count=await count_unused_recovery_codes(db, user_id),
        totp_setup=setup,
        recovery_codes=None,
        passkeys=await _get_user_passkeys(db, user_id),
        mfa_enrollment_required=await user_requires_mfa_enrollment(user_id, db),
    )


@router.post("/2fa/enable")
async def enable_two_factor(
    request: Request,
    db: AsyncSession = Depends(get_db),
    user: dict = Depends(require_auth),
    csrf_token: str = Depends(require_csrf),
    secret: str = Form(...),
    code: str = Form(...),
    user_prefs: dict = Depends(get_user_preferences),
):
    """确认验证码并启用 TOTP。"""
    user_id = int(user["user_id"])
    used_step = verify_totp_secret(secret, code)
    if used_step is None:
        return toast_redirect("/webui/settings/", "toast.two_factor_invalid", "error")

    result = await db.execute(select(TelegramUser).where(TelegramUser.id == user_id))
    db_user = result.scalar_one_or_none()
    if not db_user:
        return toast_redirect("/webui/settings/", "toast.login_required", "error")

    db_user.totp_enabled = True
    db_user.totp_secret_encrypted = encrypt_totp_secret(secret)
    db_user.totp_enabled_at = datetime.utcnow()
    db_user.totp_last_used_step = used_step
    if db_user.mfa_required:
        db_user.mfa_required = False
    recovery_codes = await replace_recovery_codes(db, user_id)
    await db.commit()

    logger.info("TOTP 已启用: user={}", user["sub"])
    return render_template(
        "settings.html",
        request,
        user_prefs=user_prefs,
        current_user=user,
        csrf_token=get_csrf_serializer().dumps({}),
        active_page="settings",
        items_per_page=user_prefs["items_per_page"],
        language=user_prefs["language"],
        output_language_config=await get_user_dynamic_config_state(
            "output_language", user_id
        ),
        output_language="",
        two_factor_enabled=True,
        two_factor_allowed=True,
        recovery_code_count=len(recovery_codes),
        totp_setup=None,
        recovery_codes=recovery_codes,
        passkeys=await _get_user_passkeys(db, user_id),
        mfa_enrollment_required=False,
    )


@router.post("/2fa/disable")
async def disable_two_factor_route(
    db: AsyncSession = Depends(get_db),
    user: dict = Depends(require_auth),
    csrf_token: str = Depends(require_csrf),
    code: str = Form(...),
):
    """使用当前验证码或恢复码禁用 TOTP。"""
    user_id = int(user["user_id"])
    result = await db.execute(select(TelegramUser).where(TelegramUser.id == user_id))
    db_user = result.scalar_one_or_none()
    if not db_user or not db_user.totp_enabled:
        return toast_redirect("/webui/settings/", "toast.two_factor_not_enabled", "error")

    verified = False
    try:
        used_step = verify_user_totp(db_user, code)
        if used_step is not None:
            db_user.totp_last_used_step = used_step
            verified = True
    except (TwoFactorError, TwoFactorReplayError):
        verified = False

    if not verified:
        verified = await replace_or_consume_disable_recovery_code(db, user_id, code)
    if not verified:
        await db.rollback()
        return toast_redirect("/webui/settings/", "toast.two_factor_invalid", "error")

    await disable_totp(db, db_user)
    await db.commit()
    return toast_redirect("/webui/settings/", "toast.two_factor_disabled")


async def replace_or_consume_disable_recovery_code(
    db: AsyncSession, user_id: int, code: str
) -> bool:
    """延迟导入以避免设置路由暴露恢复码实现细节。"""
    from backend.services.two_factor_service import consume_recovery_code

    return await consume_recovery_code(db, user_id, code)


@router.post("/2fa/recovery-codes/regenerate")
async def regenerate_recovery_codes(
    request: Request,
    db: AsyncSession = Depends(get_db),
    user: dict = Depends(require_auth),
    csrf_token: str = Depends(require_csrf),
    code: str = Form(...),
    user_prefs: dict = Depends(get_user_preferences),
):
    """验证当前 TOTP 后重新生成恢复码。"""
    user_id = int(user["user_id"])
    result = await db.execute(select(TelegramUser).where(TelegramUser.id == user_id))
    db_user = result.scalar_one_or_none()
    if not db_user or not db_user.totp_enabled:
        return toast_redirect("/webui/settings/", "toast.two_factor_not_enabled", "error")

    try:
        used_step = verify_user_totp(db_user, code)
    except TwoFactorError:
        used_step = None
    if used_step is None:
        return toast_redirect("/webui/settings/", "toast.two_factor_invalid", "error")

    db_user.totp_last_used_step = used_step
    recovery_codes = await replace_recovery_codes(db, user_id)
    await db.commit()

    return render_template(
        "settings.html",
        request,
        user_prefs=user_prefs,
        current_user=user,
        csrf_token=get_csrf_serializer().dumps({}),
        active_page="settings",
        items_per_page=user_prefs["items_per_page"],
        language=user_prefs["language"],
        output_language_config=await get_user_dynamic_config_state(
            "output_language", user_id
        ),
        output_language="",
        two_factor_enabled=True,
        two_factor_allowed=True,
        recovery_code_count=len(recovery_codes),
        totp_setup=None,
        recovery_codes=recovery_codes,
        passkeys=await _get_user_passkeys(db, user_id),
        mfa_enrollment_required=False,
    )


@router.post("/passkeys/register/options")
async def passkey_register_options(
    request: Request,
    db: AsyncSession = Depends(get_db),
    user: dict = Depends(require_auth),
):
    """创建 Passkey 注册 options。"""
    user_id = int(user["user_id"])
    result = await db.execute(select(TelegramUser).where(TelegramUser.id == user_id))
    db_user = result.scalar_one_or_none()
    if not db_user:
        raise HTTPException(status_code=401, detail="未登录")
    try:
        data = await begin_registration(db, db_user, _request_origin(request))
    except WebAuthnError as exc:
        return JSONResponse(
            status_code=400,
            content={"success": False, "message": str(exc), "data": None},
        )
    return {"success": True, "message": "ok", "data": data}


@router.post("/passkeys/register/verify")
async def passkey_register_verify(
    db: AsyncSession = Depends(get_db),
    user: dict = Depends(require_auth),
    body: dict = Body(...),
):
    """验证并保存 Passkey 注册结果。"""
    user_id = int(user["user_id"])
    result = await db.execute(select(TelegramUser).where(TelegramUser.id == user_id))
    db_user = result.scalar_one_or_none()
    if not db_user:
        raise HTTPException(status_code=401, detail="未登录")
    try:
        credential = await finish_registration(
            db,
            db_user,
            body.get("challenge_id", ""),
            body.get("credential", {}),
            body.get("device_name") or "Passkey",
        )
        if db_user.mfa_required:
            db_user.mfa_required = False
        await db.commit()
    except Exception as exc:
        await db.rollback()
        logger.warning("Passkey 注册失败: user_id={}, error={}", user_id, exc)
        return JSONResponse(
            status_code=400,
            content={"success": False, "message": "Passkey 注册失败", "data": None},
        )
    return {
        "success": True,
        "message": "ok",
        "data": {"id": credential.id, "device_name": credential.device_name},
    }


@router.post("/passkeys/{credential_id}/delete")
async def passkey_delete(
    credential_id: int,
    db: AsyncSession = Depends(get_db),
    user: dict = Depends(require_auth),
    csrf_token: str = Depends(require_csrf),
):
    """删除当前用户的 Passkey。"""
    await db.execute(
        delete(UserWebAuthnCredential).where(
            UserWebAuthnCredential.id == credential_id,
            UserWebAuthnCredential.user_id == int(user["user_id"]),
        )
    )
    await db.commit()
    return toast_redirect("/webui/settings/", "toast.passkey_deleted")


@router.get("/about")
async def about_page(
    request: Request,
    user: dict = Depends(require_auth),
    user_prefs: dict = Depends(get_user_preferences),
):
    """关于页面"""
    from datetime import datetime
    from backend.webui.routes.auth import APP_VERSION

    return render_template(
        "about.html",
        request,
        user_prefs=user_prefs,
        current_user=user,
        csrf_token=get_csrf_serializer().dumps({}),
        active_page="about",
        app_version=APP_VERSION,
        build_date=datetime.utcnow().strftime("%Y-%m-%d"),
    )
