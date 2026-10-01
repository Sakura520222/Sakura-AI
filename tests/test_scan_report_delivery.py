"""Scan reports publish GitHub results without selecting Telegram recipients."""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from telegram import Bot

from backend.services import scan_report_service


@pytest.mark.asyncio
@pytest.mark.parametrize("finding_count", [0, 1])
async def test_scan_delivery_returns_github_result_without_other_channel_selection(
    monkeypatch, finding_count
):
    class ReportSettings:
        def __getattr__(self, name):
            raise AssertionError(
                f"Report delivery consulted notification setting: {name}"
            )

    monkeypatch.setattr(scan_report_service, "settings", ReportSettings())
    external_sends = []

    async def unexpected_send(_bot, **kwargs):
        external_sends.append(kwargs)
        raise AssertionError("Scan delivery attempted an unsolicited Telegram send")

    monkeypatch.setattr(Bot, "send_message", unexpected_send)
    scan = SimpleNamespace(
        id=101,
        repo_name="owner/repository",
        total_findings=0,
        overall_health_score=0,
    )
    finding = SimpleNamespace(severity="critical")
    results = [
        MagicMock(scalars=lambda: SimpleNamespace(all=lambda: [finding])),
        MagicMock(scalar_one_or_none=lambda: None),
    ]
    session = AsyncMock()
    session.get.return_value = scan
    session.execute.side_effect = results
    session.__aenter__.return_value = session
    monkeypatch.setattr("backend.models.database.async_session", lambda: session)
    service = scan_report_service.ScanReportService()
    published = []

    async def publish(current_scan, findings, **kwargs):
        published.append((current_scan.id, findings, kwargs["language"]))
        return {
            "issue_number": 77,
            "issue_url": "https://github.com/owner/repository/issues/77",
        }

    monkeypatch.setattr(service, "_create_github_issue", publish)
    result = await service.generate_and_deliver(
        101,
        {
            "total_findings": finding_count,
            "overall_health_score": 82,
            "output_language": "en",
        },
    )

    assert scan.overall_health_score == 82
    assert session.execute.await_count == 2
    assert external_sends == []
    if finding_count:
        assert published == [(101, [finding], "en")]
        assert result == {
            "issue_number": 77,
            "issue_url": "https://github.com/owner/repository/issues/77",
        }
    else:
        assert published == []
        assert result == {}
