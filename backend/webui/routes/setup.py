"""Setup Wizard 路由

首次部署时的配置引导界面，免认证访问。
完成后自动关闭，重定向到正常 WebUI。
"""

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, RedirectResponse
from loguru import logger

from backend.core.bootstrap import (
    clear_bootstrap_cache,
    get_current_step,
    get_missing_fields,
    is_bootstrap_mode,
    write_connection_config,
)
from backend.core.setup_service import setup_service
from backend.services.config_backup_service import (
    ConfigBackupError,
    parse_config_backup,
)
from backend.webui.deps import get_templates, render_template

router = APIRouter(prefix="/setup", tags=["Setup Wizard"])
templates = get_templates()

_AI_CONFIG_MIGRATION = {
    "success": False,
    "message": "Setup 已移除旧的 LLM supplier 配置流程，请使用 AI 账号与角色绑定配置。",
    "migration": {
        "accounts": "ai_account.*",
        "role_bindings": "ai_role_bindings",
    },
}


def _legacy_ai_migration_response() -> JSONResponse:
    """明确告知旧 Setup AI API 的迁移入口，不触发旧 supplier 流程。"""
    return JSONResponse(_AI_CONFIG_MIGRATION, status_code=410)


def _check_bootstrap():
    """检查是否处于 bootstrap 模式，已完成后拒绝访问"""
    if not is_bootstrap_mode():
        # 直接跳转登录页，避免与根路径路由产生重定向循环
        return RedirectResponse(url="/auth/login", status_code=302)
    return None


@router.get("")
@router.get("/")
async def setup_page(request: Request):
    """Setup Wizard 主页面"""
    redirect = _check_bootstrap()
    if redirect:
        return redirect

    current_step = await get_current_step()
    missing = await get_missing_fields()

    return render_template(
        "setup_wizard.html",
        request,
        current_step=current_step,
        missing_fields=missing,
    )


@router.get("/api/state")
async def get_setup_state(request: Request):
    """返回当前 Setup 状态"""
    if not is_bootstrap_mode():
        return JSONResponse({"state": "completed", "current_step": -1})

    return JSONResponse(
        {
            "state": "in_progress",
            "current_step": await get_current_step(),
            "missing_fields": await get_missing_fields(),
        }
    )


@router.post("/api/test-connection")
async def test_connection(request: Request):
    """测试各类连接"""
    if not is_bootstrap_mode():
        return JSONResponse(
            {"success": False, "message": "Setup 已完成"}, status_code=403
        )

    body = await request.json()
    test_type = body.get("type", "")

    if test_type == "database":
        return await setup_service.test_database_connection(body.get("url", ""))
    elif test_type == "redis":
        return await setup_service.test_redis_connection(body.get("url", ""))
    elif test_type == "github":
        return await setup_service.test_github_app(
            body.get("app_id", ""), body.get("private_key", "")
        )
    elif test_type == "openai":
        return _legacy_ai_migration_response()
    elif test_type == "telegram":
        return await setup_service.test_telegram_bot(body.get("token", ""))
    else:
        return JSONResponse(
            {"success": False, "message": f"未知的测试类型: {test_type}"}
        )


@router.get("/api/ai-providers")
async def get_ai_providers(request: Request):
    """返回内置 AI 厂商列表。"""
    if not is_bootstrap_mode():
        return JSONResponse(
            {"success": False, "message": "Setup 已完成"}, status_code=403
        )
    return _legacy_ai_migration_response()


@router.post("/api/ai-models")
async def get_ai_models(request: Request):
    """旧供应商模型 API 的迁移响应。"""
    if not is_bootstrap_mode():
        return JSONResponse(
            {"success": False, "message": "Setup 已完成"}, status_code=403
        )
    return _legacy_ai_migration_response()


@router.post("/api/backup/inspect")
async def inspect_config_backup(request: Request):
    """校验配置备份，并返回 Setup 表单可预填的字段与分类摘要。"""
    if not is_bootstrap_mode():
        return JSONResponse(
            {"success": False, "message": "Setup 已完成"}, status_code=403
        )

    try:
        body = await request.json()
        content = body.get("content") if isinstance(body, dict) else None
        if not isinstance(content, str):
            raise ConfigBackupError("缺少备份文件内容")

        sections = parse_config_backup(content.encode("utf-8"))
        counts = {section: len(records) for section, records in sections.items()}
        setup_values = setup_service.get_backup_setup_values(sections)
        return JSONResponse(
            {
                "success": True,
                "sections": list(sections),
                "counts": counts,
                "total_count": sum(counts.values()),
                "setup_values": setup_values,
                "requires_database_url": not bool(
                    setup_values.get("database_url", "").strip()
                ),
            }
        )
    except ConfigBackupError as exc:
        return JSONResponse(
            {"success": False, "message": f"备份文件无效: {exc}"},
            status_code=400,
        )
    except Exception as exc:
        logger.error("Setup 备份校验失败: {}", exc, exc_info=True)
        return JSONResponse(
            {"success": False, "message": "备份文件校验失败"},
            status_code=400,
        )


@router.post("/api/save-step")
async def save_step(request: Request):
    """保存单步配置

    Step 1（含 DATABASE_URL）：写入 connection.json + 初始化 DB + 存入 DB
    其他步骤：直写 DB
    """
    if not is_bootstrap_mode():
        return JSONResponse(
            {"success": False, "message": "Setup 已完成"}, status_code=403
        )

    body = await request.json()
    values = body.get("values", {})

    if not values:
        return JSONResponse({"success": False, "message": "没有配置需要保存"})

    try:
        database_url = values.get("DATABASE_URL", "").strip()

        if database_url:
            # Step 1: 数据库配置 — 先验证连接，再初始化 DB，最后写 connection.json
            # 先验证数据库连接可用
            test_result = await setup_service.test_database_connection(database_url)
            if not test_result["success"]:
                return JSONResponse(test_result)

            # 初始化 DB 引擎并创建表
            await setup_service.init_database(database_url)

            # 将当前步的所有配置写入 DB
            await setup_service.save_configs_to_db(values)

            # 全部成功后才写入 connection.json
            write_connection_config(database_url)
        else:
            # 其他步骤：直写 DB（DB 已在 Step 1 初始化）
            from backend.models import database as db_module

            if db_module.async_engine is None:
                return JSONResponse(
                    {"success": False, "message": "数据库尚未配置，请先完成数据库配置"}
                )
            await setup_service.save_configs_to_db(values)

        clear_bootstrap_cache()
        return JSONResponse({"success": True, "message": "配置已保存"})
    except Exception as e:
        logger.error(f"保存配置失败: {e}")
        return JSONResponse({"success": False, "message": f"保存失败: {e}"})


@router.post("/api/complete")
async def complete_setup(request: Request):
    """完成 Setup 全流程"""
    if not is_bootstrap_mode():
        return JSONResponse(
            {"success": False, "message": "Setup 已完成"}, status_code=403
        )

    body = await request.json()
    if not isinstance(body, dict):
        return JSONResponse(
            {"success": False, "message": "请求内容必须是对象"}, status_code=400
        )

    backup_sections = None
    backup_content = body.pop("CONFIG_BACKUP", None)
    if backup_content is not None:
        if not isinstance(backup_content, str):
            return JSONResponse(
                {"success": False, "message": "备份文件内容无效"}, status_code=400
            )
        try:
            # 完成时重新校验浏览器保留的原始内容，不能依赖预检结果。
            backup_sections = parse_config_backup(backup_content.encode("utf-8"))
        except ConfigBackupError as exc:
            return JSONResponse(
                {"success": False, "message": f"备份文件无效: {exc}"},
                status_code=400,
            )

    result = await setup_service.complete_setup(
        body,
        backup_sections=backup_sections,
    )

    if result["success"]:
        # 异步触发重启（给前端时间接收响应）
        import asyncio

        async def _delayed_restart():
            await asyncio.sleep(2)
            setup_service.trigger_restart()

        asyncio.create_task(_delayed_restart())

    return JSONResponse(result)
