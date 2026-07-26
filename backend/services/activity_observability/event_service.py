"""Small same-transaction lifecycle event helper."""

from __future__ import annotations

from typing import Any

from backend.services.activity_observability.legacy_scope_authorizer import (
    LegacyRepositoryScopeAuthorizer,
)
from backend.services.activity_observability.outbox_service import (
    append_event_and_outbox,
)


async def append_lifecycle_event(
    db,
    *,
    session_id: int,
    event_type: str,
    payload: dict[str, Any],
    invocation_id: int | None = None,
    work_unit_id: int | None = None,
    visibility: str = "public",
):
    """Append an event and user-scoped outbox notifications in one transaction."""
    return await append_event_and_outbox(
        db,
        session_id=session_id,
        invocation_id=invocation_id,
        work_unit_id=work_unit_id,
        event_type=event_type,
        visibility=visibility,
        payload=payload,
        recipient_resolver=LegacyRepositoryScopeAuthorizer(),
    )


__all__ = ["append_lifecycle_event"]
