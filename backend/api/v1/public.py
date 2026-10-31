"""Public information used by the React policy and pricing pages."""

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from backend.api.v1.billing_webui import _plan_view
from backend.services.payment_service import PaymentService
from backend.webui.deps import get_db

router = APIRouter(prefix="/public", tags=["Public"])


@router.get("/plans")
async def public_plans(db: AsyncSession = Depends(get_db)):
    return [
        _plan_view(plan)
        for plan in await PaymentService(db).list_plans(active_only=True)
    ]
