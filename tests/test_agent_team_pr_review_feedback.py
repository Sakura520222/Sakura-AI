from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from backend.core.github_app import extract_pr_info_from_webhook
from backend.models.agent_team_models import (
    AgentTeamFeedback,
    AgentTeamTask,
    AgentTeamTaskStatus,
)
from backend.models.database import PRReview, ReviewComment
from backend.services.agent_team import pr_review_feedback as feedback_module
from backend.services.agent_team.pr_review_feedback import (
    AgentPRReviewOutcome,
    AgentTeamPRReviewFeedbackService,
    classify_agent_pr_review_outcome,
)


def _review(score=9, decision="comment"):
    return SimpleNamespace(overall_score=score, decision=decision)


def _comment(severity="minor"):
    return SimpleNamespace(
        severity=severity, file_path="a.py", line_number=1, content="note"
    )


def test_comment_only_high_score_without_blockers_completes():
    outcome = classify_agent_pr_review_outcome(
        _review(score=9, decision="comment"),
        [_comment("minor"), _comment("suggestion")],
        pass_score=8,
        blocking_severities={"critical", "major"},
    )

    assert outcome == AgentPRReviewOutcome.PASSED


def test_comment_only_major_finding_requires_iteration():
    outcome = classify_agent_pr_review_outcome(
        _review(score=9, decision="comment"),
        [_comment("major")],
        pass_score=8,
        blocking_severities={"critical", "major"},
    )

    assert outcome == AgentPRReviewOutcome.NEEDS_ITERATION


def test_low_score_requires_iteration_even_when_github_comment_only():
    outcome = classify_agent_pr_review_outcome(
        _review(score=6, decision="comment"),
        [_comment("minor")],
        pass_score=8,
        blocking_severities={"critical", "major"},
    )

    assert outcome == AgentPRReviewOutcome.NEEDS_ITERATION


def test_agent_team_task_has_pr_head_sha_column():
    assert "pr_head_sha" in AgentTeamTask.__table__.columns
    assert AgentTeamTask.__table__.columns["pr_head_sha"].type.length == 64


def test_pr_review_has_head_sha_column():
    assert "head_sha" in PRReview.__table__.columns
    assert PRReview.__table__.columns["head_sha"].type.length == 64


def test_agent_team_feedback_has_review_external_id_uniqueness():
    constraints = {
        tuple(constraint.columns.keys())
        for constraint in AgentTeamFeedback.__table__.constraints
        if getattr(constraint, "name", "") == "uq_agent_feedback_external"
    }
    assert ("task_id", "source", "external_id") in constraints


def test_extract_pr_info_includes_head_sha():
    payload = {
        "action": "ready_for_review",
        "repository": {
            "name": "repo",
            "full_name": "owner/repo",
            "owner": {"login": "owner"},
        },
        "installation": {"id": 123},
        "sender": {"login": "sakura-bot"},
        "pull_request": {
            "id": 999,
            "number": 7,
            "user": {"login": "sakura-bot"},
            "title": "Fix bug",
            "body": "body",
            "head": {"ref": "sakura-agent/issue-7", "sha": "abc123"},
            "base": {"ref": "develop"},
            "diff_url": "https://example/diff",
            "patch_url": "https://example/patch",
            "html_url": "https://example/pr/7",
            "state": "open",
            "draft": False,
            "merged": False,
        },
    }

    assert extract_pr_info_from_webhook(payload)["head_sha"] == "abc123"


class _FakeScalarResult:
    def __init__(self, values):
        self._values = list(values)

    def first(self):
        return self._values[0] if self._values else None

    def all(self):
        return list(self._values)


class _FakeResult:
    def __init__(self, values):
        self._values = list(values)

    def scalars(self):
        return _FakeScalarResult(self._values)

    def scalar_one_or_none(self):
        return self._values[0] if self._values else None


class _FakeAsyncSession:
    def __init__(self, state):
        self.state = state
        self.commits = 0

    async def __aenter__(self):
        self.state.sessions.append(self)
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    async def get(self, model, obj_id):
        if model is PRReview:
            review = self.state.reviews.get(obj_id)
            self.state.current_review = review
            self.state.current_review_id = obj_id
            self.state.current_external_id = f"pr_review:{obj_id}"
            return review
        return None

    async def execute(self, statement):
        entity = statement.column_descriptions[0].get("entity")
        if entity is AgentTeamTask:
            # 与生产 _find_task() 一致：仅按 repo_owner + repo_name + branch_name 匹配
            tasks = [
                task
                for task in self.state.tasks
                if task.repo_owner == self.state.current_review.repo_owner
                and task.repo_name == self.state.current_review.repo_name
                and task.branch_name == self.state.current_review.branch
            ]
            tasks.sort(key=lambda task: task.updated_at, reverse=True)
            self.state.current_task_id = tasks[0].id if tasks else None
            return _FakeResult(tasks)
        if entity is AgentTeamFeedback:
            values = [
                feedback
                for feedback in self.state.feedback
                if feedback.task_id == self.state.current_task_id
                and feedback.external_id == self.state.current_external_id
            ]
            return _FakeResult(values)
        if entity is ReviewComment:
            return _FakeResult(
                [
                    comment
                    for comment in self.state.comments
                    if comment.review_id == self.state.current_review_id
                ]
            )
        return _FakeResult([])

    def add(self, obj):
        if isinstance(obj, AgentTeamFeedback):
            self.state.feedback.append(obj)

    async def commit(self):
        self.commits += 1
        self.state.commit_count += 1


class _FakeSessionFactory:
    def __init__(self, state):
        self.state = state

    def __call__(self):
        return _FakeAsyncSession(self.state)


@pytest.fixture
def memory_bridge(monkeypatch):
    state = SimpleNamespace(
        reviews={},
        tasks=[],
        comments=[],
        feedback=[],
        sessions=[],
        commit_count=0,
        scheduled=[],
        config={
            "agent_team_pr_review_pass_score": 8,
            "agent_team_pr_review_blocking_severities": "critical,major",
        },
        current_review=None,
        current_review_id=None,
        current_task_id=None,
        current_external_id=None,
    )

    async def fake_get_dynamic_config(key):
        return state.config.get(key)

    async def fake_schedule_iteration(task_id, review_id):
        state.scheduled.append((task_id, review_id))

    monkeypatch.setattr(
        feedback_module.db_module,
        "async_session",
        _FakeSessionFactory(state),
    )
    monkeypatch.setattr(feedback_module, "get_dynamic_config", fake_get_dynamic_config)
    monkeypatch.setattr(
        feedback_module,
        "schedule_agent_pr_review_iteration",
        fake_schedule_iteration,
    )
    return state


def _task(
    task_id=101,
    status=AgentTeamTaskStatus.EXTERNAL_REVIEWING.value,
    iteration_count=0,
    max_iterations=3,
    head_sha="head-1",
):
    return AgentTeamTask(
        id=task_id,
        source_type="manual_issue",
        repo_full_name="owner/repo",
        repo_owner="owner",
        repo_name="repo",
        title="Fix issue",
        status=status,
        current_phase=status,
        branch_name="feature/agent",
        pr_number=7,
        pr_head_sha=head_sha,
        iteration_count=iteration_count,
        max_iterations=max_iterations,
        updated_at=datetime.now(timezone.utc).replace(tzinfo=None),
    )


def _completed_review(review_id=201, score=9, head_sha="head-1"):
    return PRReview(
        id=review_id,
        pr_id=7,
        repo_owner="owner",
        repo_name="repo",
        title="Review",
        branch="feature/agent",
        head_sha=head_sha,
        strategy="standard",
        status="completed",
        overall_score=score,
        decision="comment",
        review_summary="Sakura reviewed the PR.",
    )


def _review_comment(
    review_id=201,
    severity="minor",
    content="Looks good",
    file_path="app.py",
    line_number=12,
):
    return ReviewComment(
        review_id=review_id,
        severity=severity,
        file_path=file_path,
        line_number=line_number,
        content=content,
    )


@pytest.mark.asyncio
async def test_comment_only_passed_review_completes_external_reviewing_task(
    memory_bridge,
):
    task = _task()
    review = _completed_review(score=9)
    comments = [_review_comment(content="Keep the helpful detail intact")]
    memory_bridge.tasks.append(task)
    memory_bridge.reviews[review.id] = review
    memory_bridge.comments.extend(comments)

    result = (
        await AgentTeamPRReviewFeedbackService().handle_review_completed_with_result(
            review.id
        )
    )

    assert result.handled is True
    assert result.task_id == task.id
    assert result.action == "completed"
    assert task.status == AgentTeamTaskStatus.COMPLETED.value
    assert task.current_phase == AgentTeamTaskStatus.COMPLETED.value
    assert task.completed_at is not None
    assert task.error_message is None
    assert memory_bridge.commit_count == 1
    assert memory_bridge.scheduled == []
    assert len(memory_bridge.feedback) == 1
    assert memory_bridge.feedback[0].source == "sakura_pr_review"
    assert memory_bridge.feedback[0].external_id == f"pr_review:{review.id}"
    assert "Keep the helpful detail intact" in memory_bridge.feedback[0].content


@pytest.mark.asyncio
async def test_comment_only_blocking_review_creates_feedback_and_schedules_iteration(
    memory_bridge,
):
    task = _task()
    review = _completed_review(score=9)
    blocking_comment = _review_comment(
        severity="major",
        content="Must fix the security regression before merge.",
    )
    memory_bridge.tasks.append(task)
    memory_bridge.reviews[review.id] = review
    memory_bridge.comments.append(blocking_comment)

    result = (
        await AgentTeamPRReviewFeedbackService().handle_review_completed_with_result(
            review.id
        )
    )

    assert result.handled is True
    assert result.action == "scheduled_iteration"
    assert task.status == AgentTeamTaskStatus.ITERATING.value
    assert task.current_phase == AgentTeamTaskStatus.ITERATING.value
    assert task.error_message is None
    assert memory_bridge.commit_count == 1
    assert memory_bridge.scheduled == [(task.id, review.id)]
    assert len(memory_bridge.feedback) == 1
    assert memory_bridge.feedback[0].author == "Sakura PR Review"
    assert memory_bridge.feedback[0].resolved == 0
    assert (
        "Must fix the security regression before merge."
        in memory_bridge.feedback[0].content
    )


@pytest.mark.asyncio
async def test_duplicate_review_completion_is_idempotent(memory_bridge):
    task = _task()
    review = _completed_review()
    existing = AgentTeamFeedback(
        task_id=task.id,
        source="sakura_pr_review",
        external_id=f"pr_review:{review.id}",
        author="Sakura PR Review",
        content="already handled",
        resolved=0,
    )
    memory_bridge.tasks.append(task)
    memory_bridge.reviews[review.id] = review
    memory_bridge.feedback.append(existing)

    result = (
        await AgentTeamPRReviewFeedbackService().handle_review_completed_with_result(
            review.id
        )
    )

    assert result.handled is False
    assert result.task_id == task.id
    assert result.action == "ignored"
    assert result.reason == "duplicate_feedback"
    assert task.status == AgentTeamTaskStatus.EXTERNAL_REVIEWING.value
    assert memory_bridge.commit_count == 0
    assert memory_bridge.scheduled == []
    assert memory_bridge.feedback == [existing]


@pytest.mark.asyncio
async def test_stale_review_head_sha_is_ignored(memory_bridge):
    task = _task(head_sha="new-head")
    review = _completed_review(head_sha="old-head")
    memory_bridge.tasks.append(task)
    memory_bridge.reviews[review.id] = review

    result = (
        await AgentTeamPRReviewFeedbackService().handle_review_completed_with_result(
            review.id
        )
    )

    assert result.handled is False
    assert result.task_id == task.id
    assert result.action == "ignored"
    assert result.reason == "stale_head_sha"
    assert task.status == AgentTeamTaskStatus.EXTERNAL_REVIEWING.value
    assert memory_bridge.feedback == []
    assert memory_bridge.commit_count == 0
    assert memory_bridge.scheduled == []


@pytest.mark.asyncio
async def test_blocking_review_at_iteration_limit_moves_task_to_waiting_human(
    memory_bridge,
):
    task = _task(iteration_count=3, max_iterations=3)
    task.updated_at = datetime.now(timezone.utc).replace(tzinfo=None) + timedelta(
        seconds=1
    )
    review = _completed_review(score=9)
    memory_bridge.tasks.append(task)
    memory_bridge.reviews[review.id] = review
    memory_bridge.comments.append(
        _review_comment(
            severity="critical",
            content="Critical issue still blocks the PR.",
        )
    )

    result = (
        await AgentTeamPRReviewFeedbackService().handle_review_completed_with_result(
            review.id
        )
    )

    assert result.handled is True
    assert result.task_id == task.id
    assert result.action == "waiting_human"
    assert task.status == AgentTeamTaskStatus.WAITING_HUMAN.value
    assert task.current_phase == AgentTeamTaskStatus.WAITING_HUMAN.value
    assert "达到 Agent 最大迭代轮数" in task.error_message
    assert memory_bridge.commit_count == 1
    assert memory_bridge.scheduled == []
    assert len(memory_bridge.feedback) == 1
    assert "Critical issue still blocks the PR." in memory_bridge.feedback[0].content
