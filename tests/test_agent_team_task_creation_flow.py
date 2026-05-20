"""Agent Team task creation wizard backend tests."""

import json
from types import SimpleNamespace

import pytest

from backend.services.agent_team.candidate_service import (
    AgentCandidate,
    AgentTeamCandidateService,
)
from backend.webui.routes.agent_team import (
    _parse_task_overrides,
    _should_schedule_agent_task,
    preview_task_from_issue,
)


def test_parse_task_overrides_cleans_empty_values():
    overrides = _parse_task_overrides(
        title="  Improve reviewer  ",
        summary="   ",
        priority="medium",
        candidate_score="42",
        source_type="manual_issue",
        source_id="",
        source_issue_number="123",
        repo_full_name="owner/repo",
        repo_owner="owner",
        repo_name="repo",
        status="queued",
        branch_name="  ",
        base_branch=" develop ",
        max_iterations="3",
    )

    assert overrides == {
        "title": "Improve reviewer",
        "priority": "medium",
        "candidate_score": 42,
        "source_type": "manual_issue",
        "source_issue_number": 123,
        "repo_full_name": "owner/repo",
        "repo_owner": "owner",
        "repo_name": "repo",
        "status": "queued",
        "base_branch": "develop",
        "max_iterations": 3,
    }


@pytest.mark.parametrize("score", ["-1", "101", "bad"])
def test_parse_task_overrides_rejects_invalid_candidate_score(score):
    with pytest.raises(ValueError, match="candidate_score"):
        _parse_task_overrides(candidate_score=score)


@pytest.mark.parametrize("max_iterations", ["0", "-1", "bad"])
def test_parse_task_overrides_rejects_invalid_max_iterations(max_iterations):
    with pytest.raises(ValueError, match="max_iterations"):
        _parse_task_overrides(max_iterations=max_iterations)


def test_parse_task_overrides_rejects_invalid_status():
    with pytest.raises(ValueError, match="status"):
        _parse_task_overrides(status="not-a-status")


def test_parse_task_overrides_rejects_inconsistent_repo_fields():
    with pytest.raises(ValueError, match="repo_full_name"):
        _parse_task_overrides(
            repo_full_name="owner/repo",
            repo_owner="other",
            repo_name="repo",
        )


def test_should_schedule_agent_task_only_for_queued_status():
    assert _should_schedule_agent_task("queued") is True
    assert _should_schedule_agent_task("candidate") is False
    assert _should_schedule_agent_task("completed") is False


class DraftDb:
    def __init__(self):
        self.scalar_calls = 0
        self.added = []

    async def scalar(self, _stmt):
        self.scalar_calls += 1
        if self.scalar_calls == 1:
            return 0
        return SimpleNamespace(
            id=77,
            suggested_title="Use analyzed title",
            summary="Use analyzed summary",
            priority="high",
            completed_at=None,
        )

    def add(self, item):
        self.added.append(item)


@pytest.mark.asyncio
async def test_build_manual_issue_task_draft_reuses_analysis_without_creating_task(monkeypatch):
    service = AgentTeamCandidateService()
    db = DraftDb()

    async def empty_allowlist():
        return set()

    async def max_iterations():
        return 5

    monkeypatch.setattr(service, "_load_repo_allowlist", empty_allowlist)
    monkeypatch.setattr(service, "_load_max_iterations_per_task", max_iterations)
    monkeypatch.setattr(
        "backend.services.agent_team.candidate_service.GitHubAppClient",
        lambda: SimpleNamespace(
            get_issue=lambda owner, repo, issue_number: SimpleNamespace(
                title="GitHub title",
                body="GitHub body",
                state="open",
            )
        ),
    )

    draft = await service.build_manual_issue_task_draft(db, "owner/repo", 123)

    assert draft == {
        "source_type": "issue_analysis",
        "source_id": 77,
        "source_issue_number": 123,
        "repo_full_name": "owner/repo",
        "repo_owner": "owner",
        "repo_name": "repo",
        "title": "Use analyzed title",
        "summary": "Use analyzed summary",
        "priority": "high",
        "candidate_score": 80,
        "status": "queued",
        "max_iterations": 5,
    }
    assert db.added == []


@pytest.mark.asyncio
async def test_preview_task_from_issue_returns_draft(monkeypatch):
    async def fake_draft(self, db, repo_full_name, issue_number):
        assert repo_full_name == "owner/repo"
        assert issue_number == 123
        return {"title": "Draft title", "repo_full_name": repo_full_name}

    monkeypatch.setattr(
        "backend.webui.routes.agent_team.AgentTeamCandidateService.build_manual_issue_task_draft",
        fake_draft,
    )

    response = await preview_task_from_issue(
        db=object(),
        user={"user_id": 1},
        csrf_token="token",
        issue_ref="owner/repo#123",
    )
    payload = json.loads(response.body)

    assert payload == {
        "success": True,
        "draft": {"title": "Draft title", "repo_full_name": "owner/repo"},
    }


class PersistDb:
    def __init__(self):
        self.added = []

    def add(self, item):
        self.added.append(item)

    async def commit(self):
        pass

    async def refresh(self, item):
        item.id = 1


@pytest.mark.asyncio
async def test_create_task_from_candidate_applies_overrides(monkeypatch):
    service = AgentTeamCandidateService()
    db = PersistDb()
    candidate = AgentCandidate(
        source_type="issue_analysis",
        source_id=10,
        source_issue_number=20,
        repo_full_name="owner/repo",
        repo_owner="owner",
        repo_name="repo",
        title="Original title",
        summary="Original summary",
        priority="medium",
        candidate_score=50,
    )

    async def max_iterations():
        return 3

    monkeypatch.setattr(service, "_load_max_iterations_per_task", max_iterations)

    task = await service.create_task_from_candidate(
        db,
        candidate,
        started_by="admin",
        ai_config_snapshot={"model": "safe"},
        base_branch="main",
        overrides={
            "source_type": "manual_issue",
            "source_id": 99,
            "source_issue_number": 123,
            "repo_full_name": "edited/repo",
            "repo_owner": "edited",
            "repo_name": "repo",
            "title": "Edited title",
            "summary": "Edited summary",
            "priority": "high",
            "candidate_score": 88,
            "status": "candidate",
            "branch_name": "feature/custom",
            "base_branch": "develop",
            "max_iterations": 7,
        },
    )

    assert task in db.added
    assert task.source_type == "manual_issue"
    assert task.source_id == 99
    assert task.source_issue_number == 123
    assert task.repo_full_name == "edited/repo"
    assert task.repo_owner == "edited"
    assert task.title == "Edited title"
    assert task.summary == "Edited summary"
    assert task.priority == "high"
    assert task.candidate_score == 88
    assert task.status == "candidate"
    assert task.branch_name == "feature/custom"
    assert task.base_branch == "develop"
    assert task.max_iterations == 7


@pytest.mark.asyncio
async def test_create_task_from_manual_issue_applies_overrides(monkeypatch):
    service = AgentTeamCandidateService()
    db = PersistDb()

    async def fake_draft(db, repo_full_name, issue_number):
        return {
            "source_type": "manual_issue",
            "source_id": None,
            "source_issue_number": issue_number,
            "repo_full_name": repo_full_name,
            "repo_owner": "owner",
            "repo_name": "repo",
            "title": "Original title",
            "summary": "Original summary",
            "priority": "medium",
            "candidate_score": 0,
            "status": "queued",
            "max_iterations": 3,
        }

    monkeypatch.setattr(service, "build_manual_issue_task_draft", fake_draft)

    task = await service.create_task_from_manual_issue(
        db,
        repo_full_name="owner/repo",
        issue_number=123,
        started_by="admin",
        ai_config_snapshot={},
        base_branch="main",
        overrides={
            "title": "Edited title",
            "summary": "Edited summary",
            "status": "candidate",
            "branch_name": "feature/manual",
            "base_branch": "develop",
            "max_iterations": 4,
        },
    )

    assert task.title == "Edited title"
    assert task.summary == "Edited summary"
    assert task.status == "candidate"
    assert task.branch_name == "feature/manual"
    assert task.base_branch == "develop"
    assert task.max_iterations == 4
