"""A late visible operation cannot invert the wallet/operation lock order."""

import pytest
from sqlalchemy import update

from backend.models.billing_models import BillingOperation
from backend.services.billing_service import BillingConflict
from tests.test_billing_settlement import prepare
from tests.test_billing_wallet import db as wallet_db

db = wallet_db


@pytest.mark.asyncio
async def test_old_identity_snapshot_requires_fresh_transaction_without_debit(
    db, monkeypatch
):
    service = await prepare(db)
    original_execute = db.execute
    probes = []

    class OldIdentitySnapshot:
        def one_or_none(self):
            return None

    async def execute(statement, *args, **kwargs):
        result = await original_execute(statement, *args, **kwargs)
        columns = list(getattr(statement, "selected_columns", ()))
        if len(columns) == 1 and columns[0].shares_lineage(
            BillingOperation.__table__.c.user_id
        ):
            probes.append(statement)
            return OldIdentitySnapshot()
        return result

    monkeypatch.setattr(db, "execute", execute)
    # SQLite cannot reproduce native RR; only the identity read is intercepted.
    # The current operation, wallet and reservation still come from real SQL.
    with pytest.raises(BillingConflict, match="retry the transaction"):
        await service.finish_operation("op-1", "failed")
    assert probes
    operation = await db.get(BillingOperation, "op-1")
    assert operation.outcome is None and operation.reserve_units == 1_000_000
    monkeypatch.setattr(db, "execute", original_execute)
    await service.finish_operation("op-1", "failed")
    wallet = await service.get_wallet(1)
    assert wallet.reserved_units == 0 and wallet.balance_units == 10_000_000
    assert (await service.reconcile_wallet(1))["consistent"]


@pytest.mark.asyncio
async def test_optional_current_probe_distinguishes_missing_operation(db):
    service = await prepare(db)
    assert await service._operation("not-registered", allow_missing=True) is None
    assert (await service._operation("op-1", allow_missing=True)).user_id == 1


@pytest.mark.asyncio
async def test_registration_refreshes_a_previously_loaded_terminal_identity(db):
    service = await prepare(db)
    loaded = await db.get(BillingOperation, "op-1")
    assert loaded.outcome is None
    await db.execute(
        update(BillingOperation)
        .where(BillingOperation.operation_id == "op-1")
        .values(outcome="completed")
        .execution_options(synchronize_session=False)
    )
    assert loaded.outcome is None
    registered = await service.register_operation(1, "op-1", "pr_review")
    assert registered.outcome == "completed"
