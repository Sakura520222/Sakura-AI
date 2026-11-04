"""Database transaction primitives shared by every configuration writer."""

from __future__ import annotations

import hashlib
import json

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.models.database import ConfigRevision


async def lock_config(db: AsyncSession, scope_key: str) -> ConfigRevision:
    """Lock even a missing configuration row, across processes and replicas.

    Lock creation and the mutation commit together. A retained revision prevents
    an absent -> overridden -> reset ABA from accepting an old browser request.
    All writers lock sorted scope keys to avoid batch deadlocks.
    """
    dialect = db.get_bind().dialect.name
    if dialect == "mysql":
        from sqlalchemy.dialects.mysql import insert

        statement = insert(ConfigRevision).values(scope_key=scope_key, revision=0)
        statement = statement.on_duplicate_key_update(
            scope_key=statement.inserted.scope_key
        )
    elif dialect in {"postgresql", "sqlite"}:
        if dialect == "postgresql":
            from sqlalchemy.dialects.postgresql import insert
        else:
            from sqlalchemy.dialects.sqlite import insert

        statement = insert(ConfigRevision).values(scope_key=scope_key, revision=0)
        statement = statement.on_conflict_do_nothing(index_elements=["scope_key"])
    else:
        raise RuntimeError("Unsupported configuration database dialect")
    await db.execute(statement)
    result = await db.execute(
        select(ConfigRevision)
        .where(ConfigRevision.scope_key == scope_key)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    return result.scalar_one()


def config_version(revision: int, value, source: str) -> str:
    """Opaque state token also detects writes by older application versions.

    Sensitive fields use presence only here; their durable revision carries
    change identity without exposing a password hash to the browser.
    """
    encoded = json.dumps(
        [revision, value, source], sort_keys=True, ensure_ascii=False, default=str
    )
    return hashlib.sha256(encoded.encode()).hexdigest()
