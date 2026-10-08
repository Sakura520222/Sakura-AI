"""Closed Issues must be rejected before reanalysis work is admitted."""

import json
from html.parser import HTMLParser
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from starlette.requests import Request

from backend.api.v1 import issues as api_routes
from backend.models.database import IssueAnalysis
from backend.webui.routes import issues as webui_routes


@pytest.fixture
def analysis_db():
    engine = create_engine("sqlite:///:memory:")
    IssueAnalysis.__table__.create(engine)
    with Session(engine) as session:
        record = IssueAnalysis(
            issue_number=7,
            repo_owner="owner",
            repo_name="repo",
            author="alice",
            title="Current issue",
            body="Raw body",
            issue_state="open",
            status="completed",
            analysis_version=2,
        )
        session.add(record)
        session.commit()
        statements = []

        class AsyncFacade:
            async def execute(self, statement):
                statements.append(statement)
                return session.execute(statement)

        yield SimpleNamespace(
            record=record,
            session=session,
            db=AsyncFacade(),
            statements=statements,
        )
    engine.dispose()


def _request():
    return Request(
        {
            "type": "http",
            "method": "GET",
            "path": "/issues/1",
            "headers": [],
            "query_string": b"",
            "scheme": "http",
            "server": ("testserver", 80),
        }
    )


async def _reanalyze(surface, db, user):
    if surface == "api":
        return await api_routes.reanalyze_issue(1, db=db, user=user)
    return await webui_routes.reanalyze_issue(_request(), 1, db=db, user=user)


@pytest.mark.asyncio
@pytest.mark.parametrize("surface", ["api", "webui"])
@pytest.mark.parametrize("status", ["completed", "cancelled", "pending", "analyzing"])
async def test_closed_reanalysis_conflicts_before_version_lookup_or_enqueue(
    monkeypatch, analysis_db, surface, status
):
    analysis_db.record.issue_state = "closed"
    analysis_db.record.status = status
    analysis_db.session.commit()
    submit = AsyncMock(return_value="unexpected-task")
    monkeypatch.setattr(
        "backend.workers.issue_worker.submit_issue_analysis_task", submit
    )

    response = await _reanalyze(surface, analysis_db.db, {"sub": "owner"})

    assert response.status_code == 409
    field = "error" if surface == "api" else "message"
    assert json.loads(response.body) == {
        "success": False,
        field: "已关闭的 Issue 不支持重新分析，请先在 GitHub 上重新打开",
    }
    assert len(analysis_db.statements) == 1
    submit.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("surface", ["api", "webui"])
async def test_open_reanalysis_submit_with_next_version(
    monkeypatch, analysis_db, surface
):
    analysis_db.record.issue_state = "open"
    analysis_db.session.commit()
    submit = AsyncMock(return_value="task-7")
    monkeypatch.setattr(
        "backend.workers.issue_worker.submit_issue_analysis_task", submit
    )

    response = await _reanalyze(
        surface, analysis_db.db, {"sub": "alice", "user_id": 42}
    )

    assert response.status_code == 200
    payload = json.loads(response.body)
    assert payload["success"] is True
    assert (payload["data"] if surface == "api" else payload)["task_id"] == "task-7"
    assert len(analysis_db.statements) == 2
    submit.assert_awaited_once_with(
        {
            "issue_number": 7,
            "repo_name": "repo",
            "repo_owner": "owner",
            "author": "alice",
            "title": "Current issue",
            "body": "Raw body",
            "state": "open",
            "analysis_version": 3,
            "user_id": 42,
        }
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("surface", ["api", "webui"])
async def test_out_of_scope_closed_issue_still_returns_not_found(
    monkeypatch, analysis_db, surface
):
    analysis_db.record.issue_state = "closed"
    analysis_db.session.commit()
    submit = AsyncMock(return_value="unexpected-task")
    monkeypatch.setattr(
        "backend.workers.issue_worker.submit_issue_analysis_task", submit
    )

    response = await _reanalyze(surface, analysis_db.db, {"sub": "outsider"})

    assert response.status_code == 404
    field = "error" if surface == "api" else "message"
    assert json.loads(response.body) == {
        "success": False,
        field: "记录不存在或无权访问",
    }
    assert len(analysis_db.statements) == 1
    submit.assert_not_awaited()


class _Buttons(HTMLParser):
    def __init__(self):
        super().__init__()
        self.buttons = []
        self.current = None

    def handle_starttag(self, tag, attrs):
        if tag == "button":
            self.current = {"attrs": dict(attrs), "text": ""}

    def handle_data(self, data):
        if self.current is not None:
            self.current["text"] += data

    def handle_endtag(self, tag):
        if tag == "button" and self.current is not None:
            self.buttons.append(self.current)
            self.current = None


@pytest.mark.asyncio
@pytest.mark.parametrize("state", ["closed", "open", None])
@pytest.mark.parametrize(
    "language,label,unavailable",
    [
        (
            "en",
            "Re-analyze",
            "Closed Issues cannot be re-analyzed. Reopen the Issue on GitHub first.",
        ),
        (
            "zh-CN",
            "重新分析",
            "已关闭的 Issue 不支持重新分析，请先在 GitHub 上重新打开",
        ),
    ],
)
async def test_detail_renders_closed_action_unavailable_in_both_languages(
    analysis_db, state, language, label, unavailable
):
    analysis_db.record.issue_state = state
    analysis_db.session.commit()

    response = await webui_routes.issue_detail_page(
        _request(),
        1,
        db=analysis_db.db,
        user={"role": "super_admin", "sub": "owner", "username": "owner"},
        user_prefs={"language": language},
    )

    assert response.status_code == 200
    html = response.body.decode()
    parser = _Buttons()
    parser.feed(html)
    action = next(
        button for button in parser.buttons if button["text"].strip() == label
    )
    if state == "closed":
        assert "disabled" in action["attrs"]
        assert "onclick" not in action["attrs"]
        assert unavailable in html
    else:
        assert "disabled" not in action["attrs"]
        assert action["attrs"]["onclick"] == "reanalyze(1)"
        assert unavailable not in html
