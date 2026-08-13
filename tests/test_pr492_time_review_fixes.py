"""Regression coverage for actionable PR #492 time review feedback."""

from __future__ import annotations

from datetime import UTC, date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from backend.core import logging_bridge, time_service
from backend.core.time_service import (
    TimeService,
    parse_local_date_boundary,
    start_of_local_day,
)
from backend.models.ai_usage_models import AIUsageRecord
from backend.models.database import PRReview
from backend.models.telegram_models import TelegramUser
from backend.services import dashboard_stats_service, star_aid_service
from backend.services.dashboard_stats_service import (
    fetch_review_trend,
    fetch_token_trend,
)
from backend.services.quota_service import QuotaService


class _AsyncSessionAdapter:
    def __init__(self, session: Session):
        self.session = session
        self.statements = []

    async def execute(self, statement):
        self.statements.append(statement)
        return self.session.execute(statement)

    async def commit(self):
        self.session.commit()


def test_local_day_boundary_uses_first_valid_instant_when_midnight_is_skipped():
    boundary = start_of_local_day(date(2023, 4, 28), "Africa/Cairo")

    assert boundary == datetime(2023, 4, 27, 22, 0, tzinfo=UTC)
    assert boundary.astimezone(ZoneInfo("Africa/Cairo")) == datetime(
        2023,
        4,
        28,
        1,
        0,
        tzinfo=ZoneInfo("Africa/Cairo"),
    )


def test_skipped_local_date_forms_an_empty_calendar_bucket():
    skipped = start_of_local_day(date(2011, 12, 30), "Pacific/Apia")
    following = start_of_local_day(date(2011, 12, 31), "Pacific/Apia")

    assert skipped == following


def test_date_filter_boundaries_are_application_calendar_aware_utc():
    start = parse_local_date_boundary("2023-04-28", "Africa/Cairo")
    end = parse_local_date_boundary(
        "2023-04-28",
        "Africa/Cairo",
        exclusive_end=True,
    )

    assert start == datetime(2023, 4, 27, 22, 0, tzinfo=UTC)
    assert end == datetime(2023, 4, 28, 21, 0, tzinfo=UTC)
    assert start.tzinfo is UTC
    assert end.tzinfo is UTC


def test_all_reviewed_date_filter_routes_use_the_aware_boundary_helper():
    root = Path(__file__).parents[1]
    for relative in (
        "backend/api/v1/logs.py",
        "backend/webui/routes/logs.py",
        "backend/webui/routes/action_logs.py",
        "backend/webui/routes/queue.py",
    ):
        source = (root / relative).read_text(encoding="utf-8")
        assert "parse_local_date_boundary" in source
        assert "datetime.strptime" not in source


def test_github_timestamp_parser_returns_aware_utc():
    parsed = star_aid_service._parse_github_timestamp("2026-08-13T03:04:05Z")

    assert parsed == datetime(2026, 8, 13, 3, 4, 5, tzinfo=UTC)
    assert parsed.tzinfo is UTC


@pytest.mark.asyncio
async def test_quota_bulk_reset_binds_only_aware_utc_cutoffs(monkeypatch):
    engine = create_engine("sqlite:///:memory:")
    TelegramUser.__table__.create(engine)
    frozen = datetime(2026, 8, 13, 12, 34, 56, tzinfo=UTC)
    monkeypatch.setattr(QuotaService, "_utcnow", staticmethod(lambda: frozen))

    with Session(engine) as session:
        adapter = _AsyncSessionAdapter(session)
        result = await QuotaService(adapter).reset_all_expired_quotas_atomic()

    assert result.affected_users == 0
    assert result.affected_fields == 0


@pytest.mark.asyncio
async def test_token_trend_is_aggregated_in_sql_by_application_day(monkeypatch):
    engine = create_engine("sqlite:///:memory:")
    AIUsageRecord.__table__.create(engine)
    service = TimeService("America/New_York")
    monkeypatch.setattr(
        dashboard_stats_service,
        "get_time_service",
        lambda: service,
    )

    records = (
        ("chat:before", datetime(2026, 3, 8, 4, 30, tzinfo=UTC), 1, 2),
        ("chat:early", datetime(2026, 3, 8, 6, 30, tzinfo=UTC), 3, 4),
        ("chat:late", datetime(2026, 3, 8, 7, 30, tzinfo=UTC), 5, 6),
        ("chat:next", datetime(2026, 3, 9, 4, 30, tzinfo=UTC), 7, 8),
    )
    with Session(engine) as session:
        session.add_all(
            [
                AIUsageRecord(
                    record_key=key,
                    call_kind="chat",
                    role="main",
                    provider_id="provider",
                    model_id="model",
                    protocol_family="openai-compatible",
                    input_tokens=input_tokens,
                    output_tokens=output_tokens,
                    usage_reported=True,
                    occurred_at=occurred_at,
                )
                for key, occurred_at, input_tokens, output_tokens in records
            ]
        )
        session.commit()
        adapter = _AsyncSessionAdapter(session)
        trend = await fetch_token_trend(
            adapter,
            start_of_local_day(date(2026, 3, 7), service.zone),
            ["03-07", "03-08", "03-09"],
        )

    assert trend == [3, 18, 15]
    sql = str(adapter.statements[-1]).lower()
    assert "sum(" in sql
    assert "case when" in sql


@pytest.mark.asyncio
async def test_review_trend_is_aggregated_in_sql_by_application_day(monkeypatch):
    engine = create_engine("sqlite:///:memory:")
    PRReview.__table__.create(engine)
    service = TimeService("America/New_York")
    monkeypatch.setattr(
        dashboard_stats_service,
        "get_time_service",
        lambda: service,
    )

    records = (
        (1, datetime(2026, 3, 8, 4, 30, tzinfo=UTC), "completed"),
        (2, datetime(2026, 3, 8, 6, 30, tzinfo=UTC), "completed"),
        (3, datetime(2026, 3, 8, 7, 30, tzinfo=UTC), "failed"),
        (4, datetime(2026, 3, 9, 4, 30, tzinfo=UTC), "failed"),
    )
    with Session(engine) as session:
        session.add_all(
            [
                PRReview(
                    pr_id=pr_id,
                    repo_name="owner/repo",
                    repo_owner="owner",
                    strategy="balanced",
                    status=status,
                    created_at=created_at,
                    updated_at=created_at,
                )
                for pr_id, created_at, status in records
            ]
        )
        session.commit()
        adapter = _AsyncSessionAdapter(session)
        completed, failed = await fetch_review_trend(
            adapter,
            start_of_local_day(date(2026, 3, 7), service.zone),
            ["03-07", "03-08", "03-09"],
        )

    assert completed == [1, 1, 0]
    assert failed == [0, 1, 1]
    sql = str(adapter.statements[-1]).lower()
    assert "sum(" in sql
    assert "case when" in sql


def test_bootstrap_log_wall_clock_does_not_resolve_application_timezone(
    monkeypatch,
    tmp_path,
):
    monkeypatch.setattr(
        time_service,
        "get_localzone_name",
        lambda: (_ for _ in ()).throw(RuntimeError("no system zone")),
    )
    monkeypatch.setattr(logging_bridge, "APP_LOG_DIRECTORY", tmp_path)

    logging_bridge._cleanup_expired_app_logs()
    path = logging_bridge._create_startup_log_file(tmp_path, process_id=123)

    assert path.name.endswith("Z_pid123.log")


def test_browser_time_formatter_includes_numeric_offset_label():
    source = (
        Path(__file__).parents[1] / "backend/webui/templates/base.html"
    ).read_text(encoding="utf-8")

    assert "timeZoneName: 'longOffset'" in source
