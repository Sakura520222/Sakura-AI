"""Setup Wizard 路由

首次部署时的配置引导界面，免认证访问。
完成后自动关闭，重定向到正常 WebUI。
"""

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, RedirectResponse
from loguru import logger

from backend.core.bootstrap import (
    is_bootstrap_mode,
    get_current_step,
    get_missing_fields,
    clear_bootstrap_cache,
)
from backend.core.setup_service import setup_service
from backend.webui.deps import get_templates

router = APIRouter(prefix="/setup", tags=["Setup Wizard"])
templates = get_templates()


def _check_bootstrap():
    """检查是否处于 bootstrap 模式，已完成后拒绝访问"""
    if not is_bootstrap_mode():
        return RedirectResponse(url="/webui/", status_code=302)
    return None


@router.get("/")
async def setup_page(request: Request):
    """Setup Wizard 主页面"""
    redirect = _check_bootstrap()
    if redirect:
        return redirect

    current_step = get_current_step()
    missing = get_missing_fields()

    return templates.TemplateResponse(
        "setup_wizard.html",
        {
            "request": request,
            "current_step": current_step,
            "missing_fields": missing,
        },
    )


@router.get("/api/state")
async def get_setup_state(request: Request):
    """返回当前 Setup 状态"""
    if not is_bootstrap_mode():
        return JSONResponse({"state": "completed", "current_step": -1})

    return JSONResponse(
        {
            "state": "in_progress",
            "current_step": get_current_step(),
            "missing_fields": get_missing_fields(),
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
        return await setup_service.test_openai_api(
            body.get("api_key", ""), body.get("api_base", "")
        )
    elif test_type == "telegram":
        return await setup_service.test_telegram_bot(body.get("token", ""))
    else:
        return JSONResponse(
            {"success": False, "message": f"未知的测试类型: {test_type}"}
        )


@router.post("/api/save-step")
async def save_step(request: Request):
    """保存单步配置到 .env"""
    if not is_bootstrap_mode():
        return JSONResponse(
            {"success": False, "message": "Setup 已完成"}, status_code=403
        )

    body = await request.json()
    values = body.get("values", {})

    if not values:
        return JSONResponse({"success": False, "message": "没有配置需要保存"})

    try:
        setup_service.write_env_config(values)
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
    result = await setup_service.complete_setup(body)

    if result["success"]:
        # 异步触发重启（给前端时间接收响应）
        import asyncio

        async def _delayed_restart():
            await asyncio.sleep(2)
            setup_service.trigger_restart()

        asyncio.create_task(_delayed_restart())

    return JSONResponse(result)
