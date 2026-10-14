"""Dashboard aggregates count selected Usage once across original and late evidence."""

from datetime import UTC, datetime

import pytest

from backend.core.time_service import TimeService
from backend.services.ai_usage_service import fetch_global_token_totals
from backend.services.billing_reconciliation_service import resolve_billing_call
from backend.services.billing_service import BillingService
from backend.services.dashboard_stats_service import fetch_token_trend
from tests.test_billing_usage_attribution import complete_usage
from tests.test_billing_usage_attribution import sql_runtime as runtime_fixture
from tests.test_review_usage_reconciliation_late_results import finish_result
from tests.test_review_usage_reconciliation_liveness import evidence, pending_call

sql_runtime = runtime_fixture


@pytest.mark.asyncio
@pytest.mark.parametrize("original", [{}, {"input_tokens": 7}])
async def test_dashboards_remove_original_and_late_observations_across_local_days(
    sql_runtime, monkeypatch, original
):
    factory, _, _ = sql_runtime
    await pending_call(factory)
    # The DST boundary also verifies that evidence subtraction uses the same
    # application-day intervals as the SQL aggregate.
    zone = TimeService("America/New_York")
    monkeypatch.setattr(
        "backend.services.dashboard_stats_service.get_time_service", lambda: zone
    )
    first = datetime(2026, 3, 8, 6, 30, tzinfo=UTC)
    reviewed_at = datetime(2026, 3, 9, 4, 30, tzinfo=UTC)
    late_at = datetime(2026, 3, 10, 4, 30, tzinfo=UTC)
    monkeypatch.setattr("backend.services.ai_usage_service.now_utc", lambda: first)
    monkeypatch.setattr("backend.models.database.now_utc", lambda: first)
    await finish_result(factory, usage=original, usage_complete=False)
    monkeypatch.setattr("backend.models.database.now_utc", lambda: reviewed_at)
    async with factory() as db:
        await BillingService(db).finish_operation("review-operation", "completed")
        await db.commit()
        await resolve_billing_call(db, **evidence())
        await db.commit()
    monkeypatch.setattr("backend.services.ai_usage_service.now_utc", lambda: late_at)
    monkeypatch.setattr("backend.models.database.now_utc", lambda: late_at)
    await finish_result(factory, usage=complete_usage(input_tokens=2000))
    async with factory() as db:
        totals = await fetch_global_token_totals(db)
        assert totals.recorded_calls == 1 and totals.unreported_usage_calls == 0
        assert (totals.input_tokens, totals.output_tokens) == (1000, 100)
        start = datetime(2026, 3, 8, 5, tzinfo=UTC)
        assert await fetch_token_trend(db, start, ["03-08", "03-09", "03-10"]) == [
            0,
            1100,
            0,
        ]
        # The canonical record may lie outside the requested chart window.
        assert await fetch_token_trend(db, start, ["03-08"]) == [0]
        assert await fetch_token_trend(db, start, ["03-08"], scope_user="alice") == [0]
