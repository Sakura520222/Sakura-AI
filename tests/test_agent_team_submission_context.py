"""Agent Team submission reference context regression tests."""

import pytest

from backend.models.agent_team_models import AgentTeamSourceType
from backend.models.scan_models import RepoScan, ScanFinding
from backend.services.agent_team.submission_context import (
    load_agent_task_reference_context,
)


class CollisionDb:
    def __init__(self, finding: ScanFinding | None, scan: RepoScan | None):
        self.finding = finding
        self.scan = scan
        self.get_calls: list[tuple[type, int]] = []

    async def get(self, model: type, source_id: int):
        self.get_calls.append((model, source_id))
        if model is ScanFinding:
            return self.finding
        if model is RepoScan:
            return self.scan
        raise AssertionError(f"unexpected model lookup: {model!r}")


@pytest.mark.asyncio
async def test_scan_reference_context_uses_source_type_for_same_id_collision():
    finding = ScanFinding(
        id=42,
        scan_id=9001,
        severity="critical",
        category="security",
        title="Finding-only title",
        description="Finding-only description",
        suggestion="Finding-only suggestion",
        code_snippet="finding-only snippet",
    )
    scan = RepoScan(
        id=42,
        repo_name="report-owner/report-repository",
        repo_owner="report-owner",
        trigger_type="manual",
        summary="Report-only summary",
    )
    db = CollisionDb(finding, scan)

    report_context = await load_agent_task_reference_context(
        db,
        source_type=AgentTeamSourceType.SCAN_REPORT_ISSUE.value,
        source_id=42,
        repo_owner="report-owner",
        repo_name="report-repository",
        repo_full_name="report-owner/report-repository",
        issue_number=123,
    )
    finding_context = await load_agent_task_reference_context(
        db,
        source_type=AgentTeamSourceType.SCAN_FINDING.value,
        source_id=42,
        repo_owner="finding-owner",
        repo_name="finding-repository",
        repo_full_name="finding-owner/finding-repository",
        issue_number=456,
    )

    assert "## Repository scan report reference" in report_context
    assert "Repository: report-owner/report-repository" in report_context
    assert "Report-only summary" in report_context
    assert "Finding-only title" not in report_context

    assert "## Repository scan finding reference" in finding_context
    assert "Repository: finding-owner/finding-repository" in finding_context
    assert "Finding-only title" in finding_context
    assert "Finding-only description" in finding_context
    assert "Report-only summary" not in finding_context

    assert db.get_calls == [
        (RepoScan, 42),
        (ScanFinding, 42),
    ]


@pytest.mark.asyncio
async def test_scan_reference_context_missing_record_is_non_fatal():
    db = CollisionDb(None, None)

    for source_type in (
        AgentTeamSourceType.SCAN_REPORT_ISSUE.value,
        AgentTeamSourceType.SCAN_FINDING.value,
    ):
        context = await load_agent_task_reference_context(
            db,
            source_type=source_type,
            source_id=404,
            repo_owner="owner",
            repo_name="repository",
            repo_full_name="owner/repository",
            issue_number=123,
        )
        assert context == ""

    assert db.get_calls == [
        (RepoScan, 404),
        (ScanFinding, 404),
    ]
