"""扫描报告 GitHub Issue 生命周期测试。"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from backend.services import scan_report_service


@pytest.fixture
def issue_context(monkeypatch):
    """构造最小的扫描、历史扫描及 GitHub 仓库替身。"""

    monkeypatch.setattr(
        scan_report_service.settings, "scan_min_severity_for_issue", "critical"
    )

    scan = SimpleNamespace(
        id=101,
        repo_name="owner/repository",
        overall_health_score=80,
        report_issue_url=None,
    )
    previous_scan = SimpleNamespace(report_issue_number=42)
    findings = [SimpleNamespace(severity="critical")]

    previous_issue = MagicMock()
    previous_issue.state = "open"
    repo = MagicMock()
    repo.raw_data = {"has_issues": True}
    repo.get_issue.return_value = previous_issue

    repo_client = MagicMock()
    repo_client.get_repo.return_value = repo
    github_app = MagicMock()
    github_app.get_repo_client.return_value = repo_client

    return SimpleNamespace(
        scan=scan,
        previous_scan=previous_scan,
        findings=findings,
        previous_issue=previous_issue,
        repo=repo,
        github_app=github_app,
    )


async def _create_issue(context, *, embedding_service_cls=None):
    service = scan_report_service.ScanReportService()
    with (
        patch(
            "backend.core.github_app.GitHubAppClient",
            return_value=context.github_app,
        ),
        patch.object(service, "generate_issue_body", return_value="report body"),
    ):
        if embedding_service_cls is None:
            return await service._create_github_issue(
                context.scan,
                context.findings,
                previous_scan=context.previous_scan,
                previous_findings=[],
            )
        with patch(
            "backend.services.issue_embedding_service.IssueEmbeddingService",
            return_value=embedding_service_cls,
        ):
            return await service._create_github_issue(
                context.scan,
                context.findings,
                previous_scan=context.previous_scan,
                previous_findings=[],
            )


def test_generate_telegram_message_escapes_untrusted_summary(monkeypatch):
    monkeypatch.setattr(scan_report_service, "get_webui_url", lambda _path: "")
    scan = SimpleNamespace(
        id=101,
        repo_name="owner/repository",
        summary="identifier_with_underscore *literal* [text",
        overall_health_score=80,
        commit_sha=None,
        code_file_count=1,
        started_at=None,
        completed_at=None,
        total_findings=0,
        critical_count=0,
        major_count=0,
        minor_count=0,
        suggestion_count=0,
        report_issue_url=None,
        prompt_tokens=0,
        completion_tokens=0,
    )

    message = scan_report_service.ScanReportService().generate_telegram_message(scan)

    assert "identifier\\_with\\_underscore" in message
    assert "\\*literal\\*" in message
    assert "\\[text" in message
    assert "identifier_with_underscore *literal* [text" not in message


@pytest.mark.asyncio
async def test_create_failure_keeps_previous_issue_open(issue_context):
    issue_context.repo.create_issue.side_effect = RuntimeError("GitHub unavailable")

    result = await _create_issue(issue_context)

    assert result is None
    assert issue_context.repo.create_issue.call_count == 2
    issue_context.repo.get_issue.assert_not_called()
    issue_context.previous_issue.create_comment.assert_not_called()
    issue_context.previous_issue.edit.assert_not_called()


@pytest.mark.asyncio
async def test_successful_create_survives_previous_issue_close_failure(issue_context):
    new_issue = SimpleNamespace(
        number=77,
        html_url="https://github.com/owner/repository/issues/77",
        title="scan report",
    )
    issue_context.repo.create_issue.return_value = new_issue
    issue_context.previous_issue.edit.side_effect = RuntimeError("close failed")
    embedding_service = MagicMock()
    embedding_service.upsert_issue = AsyncMock()

    result = await _create_issue(issue_context, embedding_service_cls=embedding_service)

    assert result == {
        "issue_number": 77,
        "issue_url": "https://github.com/owner/repository/issues/77",
    }
    issue_context.repo.get_issue.assert_called_once_with(42)
    issue_context.previous_issue.create_comment.assert_called_once()
    issue_context.previous_issue.edit.assert_called_once_with(state="closed")


@pytest.mark.asyncio
async def test_successful_create_closes_previous_issue(issue_context):
    new_issue = SimpleNamespace(
        number=78,
        html_url="https://github.com/owner/repository/issues/78",
        title="scan report",
    )
    issue_context.repo.create_issue.return_value = new_issue
    embedding_service = MagicMock()
    embedding_service.upsert_issue = AsyncMock()

    result = await _create_issue(issue_context, embedding_service_cls=embedding_service)

    assert result == {
        "issue_number": 78,
        "issue_url": "https://github.com/owner/repository/issues/78",
    }
    issue_context.repo.get_issue.assert_called_once_with(42)
    issue_context.previous_issue.create_comment.assert_called_once()
    issue_context.previous_issue.edit.assert_called_once_with(state="closed")
