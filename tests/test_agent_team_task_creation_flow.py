"""Agent Team task creation wizard backend tests."""

import json
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from backend.services.agent_team.candidate_service import (
    AgentCandidate,
    AgentTeamCandidateService,
)
from backend.services.agent_team.submission_context import (
    build_agent_submission_context_preview,
    build_agent_task_summary,
    build_issue_context_markdown,
)
from backend.webui.routes.agent_team import (
    _format_agent_conversation_contexts,
    _parse_task_overrides,
    _should_schedule_agent_task,
    create_task_from_issue,
    preview_task_from_issue,
)
from backend.services.agent_team.submission_context import (
    format_issue_analysis_context,
    format_issue_comments,
)


async def _fake_sakura_memory(repo_owner, repo_name):
    return {"text": ""}


async def _fake_skills_context():
    return "", {}, []


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


def test_format_issue_analysis_context_parses_json_fields():
    analysis = SimpleNamespace(
        id=1,
        issue_number=123,
        repo_owner="owner",
        repo_name="repo",
        author="alice",
        title="Issue title",
        category="bug",
        priority="high",
        summary="Stored summary",
        feasibility="Stored feasibility",
        suggested_title=None,
        suggested_labels=json.dumps([{"name": "bug"}]),
        suggested_assignees=json.dumps([{"username": "bob", "reason": "Knows it"}]),
        related_prs=json.dumps([{"number": 7, "title": "Fix", "state": "open"}]),
        duplicate_of=None,
        status="completed",
        error_message=None,
        prompt_tokens=10,
        completion_tokens=5,
        estimated_cost=3,
        comment_posted=True,
        comment_url="https://example.test/comment",
        analysis_detail=json.dumps({"summary": "Detail summary"}),
        created_at=None,
        completed_at=None,
    )

    context = format_issue_analysis_context(analysis)

    assert context["repo_full_name"] == "owner/repo"
    assert context["summary"] == "Stored summary"
    assert context["suggested_labels"] == [{"name": "bug"}]
    assert context["suggested_assignees"] == [
        {"username": "bob", "reason": "Knows it"}
    ]
    assert context["related_prs"] == [{"number": 7, "title": "Fix", "state": "open"}]
    assert '"Detail summary"' in context["analysis_detail_json"]


def test_format_issue_comments_detects_bot_and_skips_empty_body():
    comments = [
        SimpleNamespace(
            id=1,
            user=SimpleNamespace(login="alice", type="User"),
            body="Need more context",
            created_at=None,
            updated_at=None,
            html_url="https://example.test/1",
            author_association="MEMBER",
        ),
        SimpleNamespace(
            id=2,
            user=SimpleNamespace(login="sakura-ai[bot]", type="Bot"),
            body="AI analysis",
            created_at=None,
            updated_at=None,
            html_url="https://example.test/2",
            author_association="NONE",
        ),
        SimpleNamespace(
            id=3,
            user=SimpleNamespace(login="empty", type="User"),
            body="  ",
            created_at=None,
            updated_at=None,
            html_url="https://example.test/3",
            author_association="NONE",
        ),
    ]

    formatted = format_issue_comments(comments, bot_username="sakura-ai[bot]")

    assert [item["author"] for item in formatted] == ["alice", "sakura-ai[bot]"]
    assert formatted[0]["is_bot"] is False
    assert formatted[1]["is_bot"] is True


def test_format_agent_conversation_contexts_parses_items():
    contexts = [
        SimpleNamespace(
            id=2,
            iteration_number=2,
            source_role="reviewer",
            target_role="fullstack",
            summary="Review summary",
            unresolved_items_json=json.dumps(
                [{"title": "Handle edge case", "severity": "medium"}]
            ),
            modified_files_json=json.dumps(["backend/main.py"]),
            token_estimate=32,
            created_at=None,
        ),
        SimpleNamespace(
            id=1,
            iteration_number=1,
            source_role="fullstack",
            target_role="reviewer",
            summary="Build summary",
            unresolved_items_json="bad json",
            modified_files_json=json.dumps([{"file_path": "tests/test_app.py"}]),
            token_estimate=20,
            created_at=None,
        ),
    ]

    formatted = _format_agent_conversation_contexts(contexts)

    assert [item["iteration_number"] for item in formatted] == [1, 2]
    assert formatted[0]["unresolved_items"] == []
    assert formatted[0]["modified_files"] == [{"text": "tests/test_app.py", "meta": None}]
    assert formatted[1]["unresolved_items"] == [
        {"text": "Handle edge case", "meta": "medium"}
    ]


def test_build_issue_submission_context_markdown_includes_analysis_and_comments():
    issue_context = build_issue_context_markdown(
        repo_full_name="owner/repo",
        issue_number=123,
        issue_analysis_context={
            "title": "Issue title",
            "summary": "AI summary",
            "priority": "high",
            "analysis_detail_json": '{"root_cause": "bug"}',
        },
        issue_comments=[{"author": "alice", "body": "Please fix", "is_bot": False}],
    )
    task_summary = build_agent_task_summary("Editable summary", issue_context)
    preview = build_agent_submission_context_preview(
        task_title="Task title",
        task_summary=task_summary,
        source_type="manual_issue",
        source_issue_number=123,
        sakura_memory="### SAKURA.md\nMemory",
        skills_summary="## 可用 Skills\n- test",
    )

    assert "## GitHub Issue 上下文" in task_summary
    assert "AI summary" in task_summary
    assert "@alice" in task_summary
    assert preview.startswith("## system\n")
    assert "## user" in preview
    assert "## 项目记忆" in preview
    assert "## 可用 Skills" in preview


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

    async def get(self, *args, **kwargs):
        return None

    async def execute(self, *args, **kwargs):
        return SimpleNamespace(scalar_one_or_none=lambda: None)


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
        return {
            "title": "Draft title",
            "summary": "Draft summary",
            "source_type": "manual_issue",
            "source_issue_number": 123,
            "repo_full_name": repo_full_name,
            "repo_owner": "owner",
            "repo_name": "repo",
        }

    monkeypatch.setattr(
        "backend.webui.routes.agent_team.AgentTeamCandidateService.build_manual_issue_task_draft",
        fake_draft,
    )
    monkeypatch.setattr(
        "backend.webui.routes.agent_team.load_sakura_memory",
        _fake_sakura_memory,
    )
    monkeypatch.setattr(
        "backend.webui.routes.agent_team.load_skills_context",
        _fake_skills_context,
    )

    response = await preview_task_from_issue(
        db=DraftDb(),
        user={"user_id": 1},
        csrf_token="token",
        issue_ref="owner/repo#123",
    )
    payload = json.loads(response.body)

    assert payload["success"] is True
    assert payload["draft"]["title"] == "Draft title"
    assert payload["submission_context"]["agent_task_context"].startswith(
        "Draft summary"
    )
    assert "## system" in payload["submission_context"]["full_submission_preview"]
    assert "## user" in payload["submission_context"]["full_submission_preview"]
    assert "## GitHub Issue 上下文" in payload["submission_context"]["agent_task_context"]


@pytest.mark.asyncio
async def test_preview_task_from_issue_serializes_datetime_context(monkeypatch):
    timestamp = datetime(2026, 5, 21, 3, 11, 19, tzinfo=timezone.utc)

    async def fake_draft(self, db, repo_full_name, issue_number):
        return {
            "title": "Draft title",
            "summary": "Draft summary",
            "source_type": "manual_issue",
            "source_issue_number": issue_number,
            "repo_full_name": repo_full_name,
            "repo_owner": "owner",
            "repo_name": "repo",
        }

    async def fake_submission_context(db, draft):
        return {
            "issue_analysis": {"created_at": timestamp, "completed_at": timestamp},
            "issue_comments": [{"author": "alice", "created_at": timestamp}],
            "issue_context_markdown": "## GitHub Issue 上下文",
            "agent_task_context": "Draft summary",
            "fullstack_user_message": "user message",
            "full_submission_preview": "## system\nsystem\n\n## user\nuser",
            "runtime_context": {"sakura_memory": "", "skills_summary": ""},
        }

    monkeypatch.setattr(
        "backend.webui.routes.agent_team.AgentTeamCandidateService.build_manual_issue_task_draft",
        fake_draft,
    )
    monkeypatch.setattr(
        "backend.webui.routes.agent_team._build_manual_issue_submission_context",
        fake_submission_context,
    )

    response = await preview_task_from_issue(
        db=DraftDb(),
        user={"user_id": 1},
        csrf_token="token",
        issue_ref="owner/repo#123",
    )
    payload = json.loads(response.body)

    assert payload["success"] is True
    assert payload["submission_context"]["issue_analysis"]["created_at"].startswith(
        "2026-05-21T03:11:19"
    )
    assert payload["submission_context"]["issue_comments"][0]["created_at"].startswith(
        "2026-05-21T03:11:19"
    )


@pytest.mark.asyncio
async def test_create_task_from_issue_passes_edited_overrides(monkeypatch):
    captured = {}

    class FakeConfig:
        def validate(self):
            pass

        def safe_snapshot(self):
            return {"model": "safe"}

    async def fake_config():
        return FakeConfig()

    async def fake_create(
        self,
        db,
        repo_full_name,
        issue_number,
        started_by,
        ai_config_snapshot=None,
        base_branch=None,
        overrides=None,
    ):
        captured.update(
            {
                "repo_full_name": repo_full_name,
                "issue_number": issue_number,
                "started_by": started_by,
                "ai_config_snapshot": ai_config_snapshot,
                "base_branch": base_branch,
                "overrides": overrides,
            }
        )
        return SimpleNamespace(
            id=88,
            source_type=overrides["source_type"],
            source_id=overrides["source_id"],
            repo_full_name=overrides["repo_full_name"],
            source_issue_number=overrides["source_issue_number"],
            status=overrides["status"],
            base_branch=overrides["base_branch"],
            branch_name=overrides["branch_name"],
            max_iterations=overrides["max_iterations"],
        )

    async def fake_log_admin_action(*args, **kwargs):
        return None

    monkeypatch.setattr("backend.webui.routes.agent_team.load_agent_team_ai_config", fake_config)
    monkeypatch.setattr(
        "backend.webui.routes.agent_team.AgentTeamCandidateService.create_task_from_manual_issue",
        fake_create,
    )
    monkeypatch.setattr(
        "backend.webui.routes.agent_team.log_admin_action",
        fake_log_admin_action,
    )
    monkeypatch.setattr(
        "backend.webui.routes.agent_team.load_sakura_memory",
        _fake_sakura_memory,
    )
    monkeypatch.setattr(
        "backend.webui.routes.agent_team.load_skills_context",
        _fake_skills_context,
    )

    background_tasks = SimpleNamespace(add_task=lambda *args, **kwargs: None)
    response = await create_task_from_issue(
        background_tasks=background_tasks,
        db=DraftDb(),
        user={"user_id": 1, "sub": "admin"},
        csrf_token="token",
        issue_ref="owner/repo#123",
        title="Edited title",
        summary="Edited summary",
        priority="high",
        candidate_score="91",
        source_type="manual_issue",
        source_id="77",
        source_issue_number="123",
        repo_full_name="owner/repo",
        repo_owner="owner",
        repo_name="repo",
        status="candidate",
        branch_name="feature/manual-edit",
        base_branch="develop",
        max_iterations="5",
    )
    payload = json.loads(response.body)

    assert payload == {"success": True, "task_id": 88}
    expected_summary = (
        "Edited summary\n\n"
        "## GitHub Issue 上下文\n"
        "仓库: owner/repo\n"
        "Issue: #123\n\n"
        "### Issue AI 分析\n暂无已完成的 Issue AI 分析。\n\n"
        "### Issue 评论讨论\n暂无 Issue 评论。"
    )
    assert captured == {
        "repo_full_name": "owner/repo",
        "issue_number": 123,
        "started_by": "admin",
        "ai_config_snapshot": {"model": "safe"},
        "base_branch": "develop",
        "overrides": {
            "title": "Edited title",
            "summary": expected_summary,
            "source_type": "manual_issue",
            "repo_full_name": "owner/repo",
            "repo_owner": "owner",
            "repo_name": "repo",
            "branch_name": "feature/manual-edit",
            "base_branch": "develop",
            "priority": "high",
            "status": "candidate",
            "candidate_score": 91,
            "max_iterations": 5,
            "source_id": 77,
            "source_issue_number": 123,
        },
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
