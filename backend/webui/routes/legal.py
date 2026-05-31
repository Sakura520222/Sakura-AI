"""法律与公开信息页面路由（无需登录）

提供 Terms of Service、Privacy Policy、Refund Policy、Pricing 页面。
这些页面使用独立模板（不继承 base.html），不依赖登录态。
"""

from fastapi import APIRouter, Request, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from backend.services.payment_service import PaymentService
from backend.webui.deps import get_db, get_templates
from backend.webui.i18n import detect_language, make_translation_func

router = APIRouter(tags=["Legal & Public"])
templates = get_templates()


def _render_public(template_name: str, request: Request, **context):
    """渲染公开页面（无需登录态，使用简单模板）"""
    lang = detect_language()
    context["_"] = make_translation_func(lang)
    context["lang"] = lang
    context["request"] = request
    return templates.TemplateResponse(request, template_name, context)


@router.get("/terms")
async def terms_of_service(request: Request):
    """服务条款页面"""
    return _render_public("legal/terms.html", request, page_title="Terms of Service")


@router.get("/privacy")
async def privacy_policy(request: Request):
    """隐私政策页面"""
    return _render_public("legal/privacy.html", request, page_title="Privacy Policy")


@router.get("/refund")
async def refund_policy(request: Request):
    """退款政策页面"""
    return _render_public("legal/refund.html", request, page_title="Refund Policy")


@router.get("/pricing")
async def pricing(request: Request, db: AsyncSession = Depends(get_db)):
    """公开套餐价格页面（无需登录）"""
    svc = PaymentService(db)
    plans = await svc.list_plans(active_only=True)
    return _render_public("legal/pricing.html", request, plans=plans, page_title="Pricing")
