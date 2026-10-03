"""Legacy NULL state requires authoritative admission after DB authorization."""

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from github import GithubException, UnknownObjectException

from backend.core.ai_protocol.errors import ReviewCancelledError
from backend.services.issues import reanalysis_admission
from tests import test_issue_reanalysis_admission as admission_tests

analysis_db = admission_tests.analysis_db


@pytest.fixture
def source(monkeypatch):
    raw = {
        "number": 7,
        "state": "open",
        "title": "Live edited title",
        "body": "Live edited body",
        "user": {"login": "live-author"},
        "labels": [{"name": "bug"}],
        "state_reason": None,
        "updated_at": "2026-10-03T00:00:00Z",
        "comments": 0,
    }
    reads = []
    outcome = SimpleNamespace(
        raw=raw, error=None, unavailable=False, repo_full_name="owner/repo"
    )

    def client(_self, owner, name):
        reads.append((owner, name))
        if outcome.error:
            raise outcome.error
        if outcome.unavailable:
            return None

        def get_repo(full_name):
            assert full_name == "owner/repo"

            def get_issue(number):
                assert number == 7
                return SimpleNamespace(raw_data=outcome.raw)

            return SimpleNamespace(
                full_name=outcome.repo_full_name, get_issue=get_issue
            )

        return SimpleNamespace(get_repo=get_repo)

    # Earlier suites restore patched bound methods onto the shared singleton.
    # Patch this consumer's provider factory so those instance attributes cannot
    # shadow the fixture. The actual admission reader and validation still run.
    app = SimpleNamespace(get_repo_client=lambda owner, name: client(None, owner, name))
    monkeypatch.setattr(reanalysis_admission, "GitHubAppClient", lambda: app)
    outcome.reads = reads
    outcome.submit = AsyncMock(return_value="task-7")
    monkeypatch.setattr(
        "backend.workers.issue_worker.submit_issue_analysis_task", outcome.submit
    )
    return outcome


@pytest.mark.asyncio
@pytest.mark.parametrize("surface", ["api", "webui"])
@pytest.mark.parametrize("repo_name", ["repo", "owner/repo"])
async def test_legacy_open_resolves_live_state_without_replacing_user_content(
    analysis_db, source, surface, repo_name
):
    analysis_db.record.issue_state = None
    analysis_db.record.repo_name = repo_name
    analysis_db.session.commit()

    response = await admission_tests._reanalyze(
        surface, analysis_db.db, {"sub": "owner"}
    )

    assert response.status_code == 200
    assert source.reads == [("owner", "repo")]
    payload = source.submit.call_args.args[0]
    assert payload["state"] == "open"
    assert payload["repo_name"] == "repo"
    assert payload["repo_owner"] == "owner"
    assert payload["title"] == "Current issue"
    assert payload["body"] == "Raw body"
    assert payload["author"] == "alice"
    assert payload["analysis_version"] == 3
    if "/" in repo_name:
        assert payload["repo_full_name"] == "owner/repo"
    assert analysis_db.record.issue_state is None
    assert len(analysis_db.statements) == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("surface", ["api", "webui"])
async def test_legacy_live_closed_rejected_before_version_query(
    analysis_db, source, surface
):
    analysis_db.record.issue_state = None
    analysis_db.session.commit()
    source.raw["state"] = "closed"

    response = await admission_tests._reanalyze(
        surface, analysis_db.db, {"sub": "owner"}
    )

    assert response.status_code == 409
    assert len(analysis_db.statements) == 1
    source.submit.assert_not_awaited()
    assert source.reads == [("owner", "repo")]


@pytest.mark.asyncio
@pytest.mark.parametrize("surface", ["api", "webui"])
@pytest.mark.parametrize(
    "failure",
    [
        "unavailable",
        "read",
        "auth",
        "not_found",
        "state",
        "number",
        "pr",
        "non_dict",
        "repository",
    ],
)
async def test_legacy_source_failures_are_safe_and_do_not_enqueue(
    analysis_db, source, surface, failure, capfd
):
    analysis_db.record.issue_state = None
    analysis_db.session.commit()
    if failure == "unavailable":
        source.unavailable = True
    elif failure == "read":
        source.error = RuntimeError("secret-token-private-host")
    elif failure == "auth":
        source.error = GithubException(403, {"message": "secret-token-private-host"})
    elif failure == "not_found":
        source.error = UnknownObjectException(
            404, {"message": "secret-token-private-host"}
        )
    elif failure == "state":
        source.raw["state"] = None
    elif failure == "number":
        source.raw["number"] = 8
    elif failure == "repository":
        source.repo_full_name = "another/repo"
    elif failure == "pr":
        source.raw["pull_request"] = {"url": "private-url"}
    else:
        source.raw = None

    response = await admission_tests._reanalyze(
        surface, analysis_db.db, {"sub": "owner"}
    )

    assert response.status_code == 503
    assert json.loads(response.body)["success"] is False
    assert "secret-token" not in response.body.decode()
    assert "private-url" not in response.body.decode()
    output = capfd.readouterr()
    assert "secret-token-private-host" not in output.out + output.err
    assert len(analysis_db.statements) == 1
    source.submit.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("surface", ["api", "webui"])
@pytest.mark.parametrize("state", ["open", "closed"])
async def test_known_state_does_not_read_github(analysis_db, source, surface, state):
    analysis_db.record.issue_state = state
    analysis_db.session.commit()
    source.error = AssertionError("known states do not need source reads")

    response = await admission_tests._reanalyze(
        surface, analysis_db.db, {"sub": "owner"}
    )

    assert response.status_code == (200 if state == "open" else 409)
    assert source.reads == []
    if state == "closed":
        source.submit.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("surface", ["api", "webui"])
async def test_null_state_out_of_scope_does_not_read_github(
    analysis_db, source, surface
):
    analysis_db.record.issue_state = None
    analysis_db.session.commit()

    response = await admission_tests._reanalyze(
        surface, analysis_db.db, {"sub": "outsider"}
    )

    assert response.status_code == 404
    assert source.reads == []
    assert len(analysis_db.statements) == 1
    source.submit.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("surface", ["api", "webui"])
@pytest.mark.parametrize("repo_name", ["another/repo", "owner/repo/extra", "", " repo"])
async def test_legacy_repository_identity_must_match_authorized_row(
    analysis_db, source, surface, repo_name
):
    analysis_db.record.issue_state = None
    analysis_db.record.repo_name = repo_name
    analysis_db.session.commit()

    response = await admission_tests._reanalyze(
        surface, analysis_db.db, {"sub": "owner"}
    )

    assert response.status_code == 503
    assert source.reads == []
    assert len(analysis_db.statements) == 1
    source.submit.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("surface", ["api", "webui"])
@pytest.mark.parametrize(
    "error", [asyncio.CancelledError(), ReviewCancelledError("stop")]
)
async def test_legacy_resolution_propagates_cancellation(
    analysis_db, source, surface, error
):
    analysis_db.record.issue_state = None
    analysis_db.session.commit()
    source.error = error

    with pytest.raises(type(error)):
        await admission_tests._reanalyze(surface, analysis_db.db, {"sub": "owner"})
    assert len(analysis_db.statements) == 1
    source.submit.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("surface", ["api", "webui"])
@pytest.mark.parametrize("repo_name", ["repo", "owner/repo"])
async def test_legacy_open_payload_reaches_real_worker_relation_and_persists_open(
    monkeypatch, analysis_db, source, surface, repo_name
):
    """Use the worker's actual row creation and guarded save, plus real freshness."""
    from backend.models.database import IssueAnalysis
    from backend.services.issue_service import IssueService
    from backend.services.issues.corpus_service import snapshot_issue
    from backend.services.issues.issue_source_freshness import IssueSourceReader
    from backend.services.issues.relation_analyzer import IssueRelationAnalyzer
    from backend.workers import issue_worker as worker_module
    from tests.test_pr_issue_budget import summary_candidate

    analysis_db.record.issue_state = None
    analysis_db.record.repo_name = repo_name
    analysis_db.session.add(
        IssueAnalysis(
            issue_number=7,
            repo_owner="other",
            repo_name="repo",
            author="bob",
            title="Other owner's Issue",
            body="Body",
            issue_state="open",
            status="completed",
            analysis_version=99,
        )
    )
    analysis_db.session.commit()
    current = dict(source.raw)
    candidate_raw = dict(current, number=8)
    raws = {7: current, 8: candidate_raw}
    repo = SimpleNamespace(
        full_name="owner/repo",
        get_issue=lambda number: SimpleNamespace(raw_data=raws[number]),
    )
    app = SimpleNamespace(
        get_repo_client=lambda owner, name: SimpleNamespace(
            get_repo=lambda full_name: repo
        )
    )
    monkeypatch.setattr(reanalysis_admission, "GitHubAppClient", lambda: app)
    response = await admission_tests._reanalyze(
        surface, analysis_db.db, {"sub": "owner"}
    )
    assert response.status_code == 200
    payload = source.submit.call_args.args[0]

    config_values = {
        "issue_include_comments": False,
        "issue_relation_max_candidates": 5,
        "issue_relation_similarity_threshold": 0.75,
        "issue_relation_confidence_threshold": 0.85,
        "issue_duplicate_confidence_threshold": 0.95,
        "issue_relation_max_input_tokens": 64000,
    }

    async def config(key, **kwargs):
        return config_values[key]

    monkeypatch.setattr(
        "backend.services.issues.relation_analyzer.get_dynamic_config", config
    )
    monkeypatch.setattr(
        "backend.services.issues.issue_budget.get_dynamic_config", config
    )
    relation_client = SimpleNamespace(
        resolve_role_candidates=AsyncMock(return_value=[summary_candidate()]),
        call_with_retry=AsyncMock(
            return_value=SimpleNamespace(
                usage=SimpleNamespace(prompt_tokens=3, completion_tokens=5),
                choices=[
                    SimpleNamespace(
                        message=SimpleNamespace(
                            content=json.dumps(
                                {
                                    "relations": [
                                        {
                                            "number": 8,
                                            "relation": "duplicate",
                                            "confidence": 0.99,
                                            "reason": "Same failure and affected input",
                                            "similarities": ["Same failure"],
                                            "differences": [],
                                            "evidence": [
                                                {
                                                    "current_quote": "Live edited title",
                                                    "candidate_quote": "Live edited title",
                                                }
                                            ],
                                        }
                                    ],
                                }
                            )
                        )
                    )
                ],
            )
        ),
    )
    relation_analyzer = IssueRelationAnalyzer(
        retriever=SimpleNamespace(
            retrieve=AsyncMock(return_value=[snapshot_issue(candidate_raw)])
        ),
        client=relation_client,
        source_reader=IssueSourceReader(repo=repo),
    )
    relation_results = []

    async def analyze_issue(*, issue_info, repo_owner, repo_name, **kwargs):
        result = await relation_analyzer.analyze(
            repo_owner,
            repo_name,
            issue_info,
            cancel_event=kwargs["cancel_event"],
            deadline=kwargs["deadline"],
        )
        relation_results.append(result)
        return {
            "issue_relations": result.to_dict(),
            "duplicate_of": result.duplicate_of,
        }

    class WorkerDb:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_):
            return False

        def add(self, record):
            analysis_db.session.add(record)

        async def scalar(self, statement):
            from backend.models.scan_models import RepoScan

            if statement.column_descriptions[0].get("entity") is RepoScan:
                return None  # Scanning is an unrelated external worker integration.
            return analysis_db.session.scalar(statement)

        async def execute(self, statement):
            return analysis_db.session.execute(statement)

        async def commit(self):
            analysis_db.session.commit()

        async def refresh(self, record):
            analysis_db.session.refresh(record)

    execution = SimpleNamespace(
        merged=False,
        finish=AsyncMock(),
        publication_coordinator=None,
        invocation_context=None,
        observer=None,
    )
    worker = worker_module.IssueWorker.__new__(worker_module.IssueWorker)
    worker.activity_integration = SimpleNamespace(
        admit_issue=AsyncMock(return_value=SimpleNamespace(session_id=1, trigger_id=2)),
        start_execution=AsyncMock(return_value=execution),
    )
    worker.github_app = app
    worker.analyzer = SimpleNamespace(api_client=None, analyze_issue=analyze_issue)
    worker._background_tasks = set()
    worker._log_activity = AsyncMock()
    monkeypatch.setattr(worker_module, "async_session", WorkerDb)
    monkeypatch.setattr(
        worker_module,
        "get_settings",
        lambda: SimpleNamespace(review_timeout_seconds=60),
    )
    monkeypatch.setattr(
        worker_module,
        "_get_issue_semaphore",
        AsyncMock(return_value=asyncio.Semaphore(1)),
    )
    monkeypatch.setattr(
        worker_module, "get_dynamic_config", AsyncMock(return_value=False)
    )
    monkeypatch.setattr(
        worker_module, "get_sakura_memory_config", lambda: {"enabled": False}
    )
    monkeypatch.setattr("backend.webui.sse.publish_event", AsyncMock())
    monkeypatch.setattr(
        worker_module,
        "issue_service",
        SimpleNamespace(
            save_analysis_result=IssueService().save_analysis_result,
            find_related_prs=AsyncMock(return_value=[]),
            post_analysis_comment=AsyncMock(return_value=False),
        ),
    )

    await worker.process_issue_analysis(dict(payload, task_id="legacy-live-open"))

    assert len(relation_results) == 1
    assert relation_results[0].status == "verified"
    assert relation_results[0].duplicate_of == 8
    sent = json.loads(
        relation_client.call_with_retry.call_args.kwargs["messages"][1]["content"]
    )
    assert sent["current"]["title"] == "Live edited title"
    assert sent["current"]["state"] == "open"
    from sqlalchemy import select

    saved = analysis_db.session.scalars(
        select(IssueAnalysis).where(
            IssueAnalysis.id != 1, IssueAnalysis.repo_owner == "owner"
        )
    ).one()
    assert saved.status == "completed"
    assert saved.analysis_version == 3
    assert saved.issue_state == "open"
    assert saved.repo_name == "repo"
    assert saved.title == "Current issue"
    assert saved.duplicate_of == 8
    assert json.loads(saved.issue_relations)["status"] == "verified"
    assert analysis_db.record.issue_state is None

    # The historical NULL row remains a valid entrypoint after the normalized
    # worker record has been completed. Both names must share one version series.
    response = await admission_tests._reanalyze(
        surface, analysis_db.db, {"sub": "owner"}
    )
    assert response.status_code == 200
    second_payload = source.submit.call_args.args[0]
    assert second_payload["analysis_version"] == 4
    await worker.process_issue_analysis(
        dict(second_payload, task_id="legacy-live-open-again")
    )
    versions = analysis_db.session.scalars(
        select(IssueAnalysis.analysis_version)
        .where(IssueAnalysis.id != 1, IssueAnalysis.repo_owner == "owner")
        .order_by(IssueAnalysis.analysis_version)
    ).all()
    assert versions == [3, 4]
    assert len(relation_results) == 2
    assert all(result.status == "verified" for result in relation_results)


@pytest.mark.asyncio
@pytest.mark.parametrize("surface", ["api", "webui"])
@pytest.mark.parametrize("repo_name", ["repo", "owner/repo"])
async def test_next_version_covers_short_and_full_names_with_same_owner_only(
    analysis_db, source, surface, repo_name
):
    from backend.models.database import IssueAnalysis

    analysis_db.record.issue_state = None
    analysis_db.record.repo_name = repo_name
    analysis_db.session.add_all(
        [
            IssueAnalysis(
                issue_number=7,
                repo_owner="owner",
                repo_name="owner/repo",
                author="alice",
                title="Prior completed",
                body="Body",
                issue_state="open",
                status="completed",
                analysis_version=5,
            ),
            IssueAnalysis(
                issue_number=7,
                repo_owner="other",
                repo_name="repo",
                author="bob",
                title="Other owner",
                body="Body",
                issue_state="open",
                status="completed",
                analysis_version=99,
            ),
            IssueAnalysis(
                issue_number=8,
                repo_owner="owner",
                repo_name="repo",
                author="alice",
                title="Other Issue",
                body="Body",
                issue_state="open",
                status="completed",
                analysis_version=99,
            ),
        ]
    )
    analysis_db.session.commit()

    response = await admission_tests._reanalyze(
        surface, analysis_db.db, {"sub": "owner"}
    )

    assert response.status_code == 200
    assert source.submit.call_args.args[0]["analysis_version"] == 6
