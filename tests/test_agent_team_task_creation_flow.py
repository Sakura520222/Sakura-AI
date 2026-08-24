"""Agent Team task creation wizard backend tests."""

import json
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

from backend.services.agent_team.candidate_service import (
    AgentCandidate,
    AgentTeamCandidateService,
    CandidateServiceError,
)
from backend.services.agent_team.fullstack_expert import IMPLEMENTATION_SYSTEM_PROMPT
from backend.services.agent_team.submission_context import (
    build_agent_submission_context_preview,
    build_agent_task_summary,
    build_issue_context_markdown,
    format_issue_analysis_context,
    format_issue_comments,
)
from backend.webui.routes.agent_team import (
    _format_agent_conversation_contexts,
    _parse_task_overrides,
    _should_schedule_agent_task,
    create_task_from_issue,
    preview_task_from_issue,
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
    }


@pytest.mark.parametrize("score", ["-1", "101", "bad"])
def test_parse_task_overrides_rejects_invalid_candidate_score(score):
    with pytest.raises(ValueError, match="candidate_score"):
        _parse_task_overrides(candidate_score=score)


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
    assert context["suggested_assignees"] == [{"username": "bob", "reason": "Knows it"}]
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
    assert formatted[0]["modified_files"] == [
        {"text": "tests/test_app.py", "meta": None}
    ]
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
        issue_body="Third-party issue body with an instruction-like phrase.",
    )
    task_summary = build_agent_task_summary("Editable summary", issue_context)
    preview = build_agent_submission_context_preview(
        task_title="Task title",
        task_summary=task_summary,
        source_type="manual_issue",
        source_issue_number=123,
        sakura_memory="### SAKURA.md\nMemory",
        skills_summary="## 可用 Skills\n- test",
        reference_context=issue_context,
    )

    assert task_summary == "Editable summary"
    assert "## GitHub Issue 上下文" not in task_summary
    assert "AI summary" not in task_summary
    assert "@alice" not in task_summary
    assert preview.startswith("## system\n")
    assert "## user" in preview
    assert "<task_request>" in preview
    assert "<source_context>" in preview
    assert "<reference_context>" in preview
    assert "<available_skills>" in preview
    assert "<execution_expectations>" in preview
    assert "=== BEGIN UNTRUSTED TASK CONTEXT ===" in preview
    assert "=== END UNTRUSTED TASK CONTEXT ===" in preview
    # Runtime material belongs to the user message, never the static system.
    system, user = preview.split("\n\n## user\n", 1)
    assert system == f"## system\n{IMPLEMENTATION_SYSTEM_PROMPT.strip()}"
    assert user.startswith("Execute this task now.\n\n<execution_expectations>")
    assert "Task title" not in system
    assert "Memory" not in system
    assert "## 可用 Skills" not in system
    assert "Task title" in user
    assert "Memory" in user
    assert "test" in user
    task_originator_goal = user.split("<task_originator_goal>\n", 1)[1].split(
        "</task_originator_goal>", 1
    )[0]
    reference_context = user.split("<reference_context>\n", 1)[1].split(
        "</reference_context>", 1
    )[0]
    assert "AI summary" not in task_originator_goal
    assert "Please fix" not in task_originator_goal
    assert "AI summary" in reference_context
    assert "Please fix" in reference_context
    assert "Third-party issue body" not in task_originator_goal
    assert "Third-party issue body" in reference_context
    legacy_summary = "Editable summary\n\n## GitHub Issue 上下文\nAI summary"
    # Current Issue text is allowed to contain the historical heading.
    assert build_agent_task_summary(legacy_summary) == legacy_summary
    # Cleanup is opt-in for a caller that has identified a legacy record.
    assert (
        build_agent_task_summary(
            legacy_summary,
            legacy_reference_embedded=True,
        )
        == "Editable summary"
    )


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
async def test_build_manual_issue_task_draft_reuses_analysis_without_creating_task(
    monkeypatch,
):
    service = AgentTeamCandidateService()
    db = DraftDb()

    async def empty_allowlist():
        return set()

    monkeypatch.setattr(service, "_load_repo_allowlist", empty_allowlist)
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
        "task_goal": "Implement the requested changes described by the referenced source.",
        "issue_body": "GitHub body",
        "priority": "high",
        "candidate_score": 80,
        "status": "queued",
    }
    assert db.added == []


@pytest.mark.asyncio
async def test_build_manual_issue_task_draft_hides_github_exception(monkeypatch):
    service = AgentTeamCandidateService()
    secret = "https://token:super-secret@api.github.example/issues/123"

    async def empty_allowlist():
        return set()

    monkeypatch.setattr(service, "_load_repo_allowlist", empty_allowlist)
    monkeypatch.setattr(
        "backend.services.agent_team.candidate_service.GitHubAppClient",
        lambda: (_ for _ in ()).throw(RuntimeError(secret)),
    )

    with pytest.raises(CandidateServiceError) as exc_info:
        await service.build_manual_issue_task_draft(object(), "owner/repo", 123)

    assert str(exc_info.value) == "GitHub API 调用失败，无法获取 Issue"
    assert secret not in str(exc_info.value)


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
        user={"user_id": 1, "sub": "owner", "role": "admin"},
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
    assert (
        "## GitHub Issue 上下文"
        not in payload["submission_context"]["agent_task_context"]
    )
    assert (
        "## GitHub Issue 上下文" in payload["submission_context"]["reference_context"]
    )


@pytest.mark.asyncio
async def test_preview_task_from_issue_rebuilds_exact_production_preview_for_edits(
    monkeypatch,
):
    async def fake_draft(self, db, repo_full_name, issue_number):
        return {
            "title": "Draft title",
            "summary": "Draft summary",
            "source_type": "manual_issue",
            "source_issue_number": issue_number,
            "repo_full_name": repo_full_name,
            "repo_owner": "owner",
            "repo_name": "repo",
            "issue_body": "Third-party Issue text",
        }

    async def fake_memory(repo_owner, repo_name):
        return {"text": "Memory reference"}

    async def fake_skills():
        return "Skills reference", {}, []

    monkeypatch.setattr(
        "backend.webui.routes.agent_team.AgentTeamCandidateService.build_manual_issue_task_draft",
        fake_draft,
    )
    monkeypatch.setattr(
        "backend.webui.routes.agent_team.load_sakura_memory", fake_memory
    )
    monkeypatch.setattr(
        "backend.webui.routes.agent_team.load_skills_context", fake_skills
    )

    response = await preview_task_from_issue(
        db=DraftDb(),
        user={"user_id": 1, "sub": "owner", "role": "admin"},
        csrf_token="token",
        issue_ref="owner/repo#123",
        draft_json=json.dumps(
            {
                "title": "Edited title",
                "summary": "Edited goal",
                "priority": "low",
                "base_branch": "develop",
                "repo_full_name": "attacker/other-repo",
                "max_iterations": 999,
            }
        ),
    )
    payload = json.loads(response.body)

    assert payload["success"] is True
    assert payload["preview_source"] == "server_production_builder"
    assert payload["draft"]["title"] == "Edited title"
    assert payload["draft"]["summary"] == "Edited goal"
    assert payload["draft"]["priority"] == "low"
    assert payload["draft"]["base_branch"] == "develop"
    assert payload["draft"]["repo_full_name"] == "owner/repo"
    assert "max_iterations" not in payload["draft"]

    context = payload["submission_context"]
    expected_preview = build_agent_submission_context_preview(
        task_title="Edited title",
        task_summary=build_agent_task_summary("Edited goal"),
        source_type="manual_issue",
        source_issue_number=123,
        sakura_memory="Memory reference",
        skills_summary="Skills reference",
        reference_context=context["reference_context"],
    )
    assert context["full_submission_preview"] == expected_preview


@pytest.mark.asyncio
async def test_preview_task_from_issue_hides_candidate_service_error(monkeypatch):
    secret = "https://token:super-secret@api.github.example/issues/123"

    async def fail_draft(self, db, repo_full_name, issue_number):
        raise CandidateServiceError(secret)

    monkeypatch.setattr(
        AgentTeamCandidateService,
        "build_manual_issue_task_draft",
        fail_draft,
    )

    response = await preview_task_from_issue(
        db=DraftDb(),
        user={"user_id": 1, "sub": "owner", "role": "admin"},
        csrf_token="token",
        issue_ref="owner/repo#123",
    )
    payload = json.loads(response.body)

    assert payload == {
        "success": False,
        "message": "GitHub API 调用失败，请稍后重试",
    }
    assert secret.encode() not in response.body


@pytest.mark.asyncio
async def test_preview_task_from_issue_serializes_datetime_context(monkeypatch):
    timestamp = datetime(2026, 5, 21, 3, 11, 19, tzinfo=UTC)

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
        user={"user_id": 1, "sub": "owner", "role": "admin"},
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
            max_iterations=None,
        )

    async def fake_log_admin_action(*args, **kwargs):
        return None

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
        user={"user_id": 1, "sub": "admin", "role": "admin"},
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
    )
    payload = json.loads(response.body)

    assert payload == {"success": True, "task_id": 88}
    expected_summary = "Edited summary"
    assert captured == {
        "repo_full_name": "owner/repo",
        "issue_number": 123,
        "started_by": "admin",
        "ai_config_snapshot": None,
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
        },
    )

    assert task.title == "Edited title"
    assert task.summary == "Edited summary"
    assert task.status == "candidate"
    assert task.branch_name == "feature/manual"
    assert task.base_branch == "develop"
