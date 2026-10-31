"""Versioned entry points for the single notification delivery pipeline."""

from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from backend.api.v1.deps import require_api_auth, require_api_super_admin
from backend.api.v1.private_routes import PrivateAPIRoute
from backend.webui.deps import get_db, require_csrf_header
from backend.webui.routes import notifications as notification

router = APIRouter(
    prefix="/notifications", tags=["Notifications"], route_class=PrivateAPIRoute
)


@router.get("/settings")
async def settings(
    user: dict = Depends(require_api_auth), db: AsyncSession = Depends(get_db)
):
    return await notification.settings(user=user, db=db)


@router.post("/endpoints", dependencies=[Depends(require_csrf_header)])
async def add_endpoint(
    body: notification.EndpointCreate,
    user: dict = Depends(require_api_auth),
    db: AsyncSession = Depends(get_db),
):
    return await notification.add_endpoint(body, user=user, db=db)


@router.patch("/endpoints/{endpoint_id}", dependencies=[Depends(require_csrf_header)])
async def update_endpoint(
    endpoint_id: int,
    body: notification.EndpointUpdate,
    user: dict = Depends(require_api_auth),
    db: AsyncSession = Depends(get_db),
):
    return await notification.update_endpoint(endpoint_id, body, user=user, db=db)


@router.delete("/endpoints/{endpoint_id}", dependencies=[Depends(require_csrf_header)])
async def remove_endpoint(
    endpoint_id: int,
    user: dict = Depends(require_api_auth),
    db: AsyncSession = Depends(get_db),
):
    return await notification.remove_endpoint(endpoint_id, user=user, db=db)


@router.post(
    "/endpoints/{endpoint_id}/verify", dependencies=[Depends(require_csrf_header)]
)
async def verify(
    endpoint_id: int,
    body: notification.Verification,
    user: dict = Depends(require_api_auth),
    db: AsyncSession = Depends(get_db),
):
    return await notification.verify(endpoint_id, body, user=user, db=db)


@router.post(
    "/endpoints/{endpoint_id}/test", dependencies=[Depends(require_csrf_header)]
)
async def test_endpoint(
    endpoint_id: int,
    user: dict = Depends(require_api_auth),
    db: AsyncSession = Depends(get_db),
):
    return await notification.test_endpoint(endpoint_id, user=user, db=db)


@router.get("/endpoints/{endpoint_id}/status")
async def endpoint_status(
    endpoint_id: int,
    user: dict = Depends(require_api_auth),
    db: AsyncSession = Depends(get_db),
):
    return await notification.endpoint_status(endpoint_id, user=user, db=db)


@router.post("/subscriptions", dependencies=[Depends(require_csrf_header)])
async def add_subscription(
    body: notification.Preference,
    user: dict = Depends(require_api_auth),
    db: AsyncSession = Depends(get_db),
):
    return await notification.add_subscription(body, user=user, db=db)


@router.delete(
    "/subscriptions/{subscription_id}", dependencies=[Depends(require_csrf_header)]
)
async def remove_subscription(
    subscription_id: int,
    user: dict = Depends(require_api_auth),
    db: AsyncSession = Depends(get_db),
):
    return await notification.remove_subscription(subscription_id, user=user, db=db)


@router.get("/history")
async def history(
    page: int = Query(1, ge=1),
    user: dict = Depends(require_api_auth),
    db: AsyncSession = Depends(get_db),
):
    return await notification.history(user=user, db=db, page=page)


@router.post("/read-all", dependencies=[Depends(require_csrf_header)])
async def read_all(
    user: dict = Depends(require_api_auth), db: AsyncSession = Depends(get_db)
):
    return await notification.mark_all_read(user=user, db=db)


@router.post("/{notification_id}/read", dependencies=[Depends(require_csrf_header)])
async def read_notification(
    notification_id: int,
    user: dict = Depends(require_api_auth),
    db: AsyncSession = Depends(get_db),
):
    return await notification.mark_read(notification_id, user=user, db=db)


@router.get("/admin/diagnostics")
async def diagnostics(
    user: dict = Depends(require_api_super_admin), db: AsyncSession = Depends(get_db)
):
    return await notification.diagnostics(user=user, db=db)


@router.post(
    "/admin/deliveries/{delivery_id}/retry", dependencies=[Depends(require_csrf_header)]
)
async def retry(
    delivery_id: int,
    user: dict = Depends(require_api_super_admin),
    db: AsyncSession = Depends(get_db),
):
    return await notification.retry(delivery_id, user=user, db=db)
