"""业务 AI 调用必须通过角色协议层的回归测试。"""

import inspect
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from backend.core.ai_protocol.errors import AllCandidatesFailedError
from backend.services import (
    history_context_service,
    issue_embedding_service,
    sakura_consolidation_agent,
    sakura_knowledge_extractor,
    sakura_memory_service,
    star_aid_summary_service,
)
from backend.workers import scan_worker


class _Response:
    choices = [SimpleNamespace(message=SimpleNamespace(content="summary"))]


@pytest.mark.asyncio
async def test_sakura_memory_uses_summary_role_and_ignores_legacy_model(monkeypatch):
    service = sakura_memory_service.SakuraMemoryService.__new__(
        sakura_memory_service.SakuraMemoryService
    )
    client = MagicMock()
    client.call_with_retry = AsyncMock(return_value=_Response())
    service.api_client = client
    service._default_model = "legacy-model-must-not-be-used"
    monkeypatch.setattr(service, "_refresh_ai_client", lambda: None)

    result = await service._call_llm("prompt", model="legacy-override")

    assert result == "summary"
    kwargs = client.call_with_retry.await_args.kwargs
    assert kwargs["role"] == "summary"
    assert kwargs["model"] == ""


def test_sakura_agents_create_unconfigured_clients_and_no_model_override(monkeypatch):
    calls = []

    class FakeClient:
        def __init__(self, *args, **kwargs):
            calls.append((args, kwargs))

    monkeypatch.setattr(sakura_knowledge_extractor, "AIApiClient", FakeClient)
    monkeypatch.setattr(sakura_consolidation_agent, "AIApiClient", FakeClient)
    monkeypatch.setattr(
        sakura_knowledge_extractor, "get_strategy_config", lambda: object()
    )
    monkeypatch.setattr(sakura_consolidation_agent, "AIApiClient", FakeClient)

    extractor = sakura_knowledge_extractor.SakuraKnowledgeExtractor()
    extractor._ensure_client()
    consolidator = sakura_consolidation_agent.SakuraConsolidationAgent()
    consolidator._ensure_client()

    assert calls == [((), {}), ((), {})]
    assert extractor._default_model == ""
    assert consolidator._default_model == ""


def test_scan_worker_does_not_keep_legacy_direct_sdk_path():
    source = inspect.getsource(scan_worker.ScanWorker)
    assert "AsyncOpenAI" not in source
    assert not hasattr(scan_worker.ScanWorker, "_call_ai")


@pytest.mark.asyncio
async def test_history_context_ignores_injected_legacy_client_and_model(monkeypatch):
    class FakeClient:
        def __init__(self):
            self.calls = []

        async def call_with_retry(self, **kwargs):
            self.calls.append(kwargs)
            return _Response()

    clients = []

    def new_client():
        client = FakeClient()
        clients.append(client)
        return client

    monkeypatch.setattr(history_context_service, "AIApiClient", new_client)
    legacy_client = MagicMock()
    service = history_context_service.HistoryContextService(
        legacy_client, model="legacy-model"
    )
    result = await service._generate_ai_summary("history")

    assert result == "summary"
    assert not legacy_client.call_with_retry.called
    kwargs = clients[0].calls[0]
    assert kwargs["role"] == "summary"
    assert kwargs["model"] == ""
    assert "temperature" not in kwargs
    assert "max_tokens" not in kwargs


@pytest.mark.asyncio
async def test_issue_verification_uses_summary_role_without_flat_model(monkeypatch):
    calls = []

    class FakeClient:
        async def resolve_role_candidates(self, role):
            from tests.test_pr_issue_budget import summary_candidate

            assert role == "summary"
            return [summary_candidate()]

        async def call_with_retry(self, **kwargs):
            calls.append(kwargs)
            return SimpleNamespace(
                choices=[
                    SimpleNamespace(
                        message=SimpleNamespace(
                            content='{"relations": [{"number": 1, "relation": "related", "confidence": 0.99, "reason": "partial fix", "evidence": [{"path": "legacy", "change": "added", "code_quote": "details", "issue_quote": "details"}]}]}'
                        )
                    )
                ]
            )

    from backend.services.issues import pr_budget

    monkeypatch.setattr(
        pr_budget,
        "get_dynamic_config",
        AsyncMock(
            side_effect=lambda key, **_: {
                "pr_issue_max_files": 128,
                "pr_issue_max_input_tokens": 64000,
            }[key]
        ),
    )

    monkeypatch.setattr(issue_embedding_service, "AIApiClient", lambda: FakeClient())
    service = issue_embedding_service.IssueEmbeddingService.__new__(
        issue_embedding_service.IssueEmbeddingService
    )
    candidates = [{"number": 1, "title": "issue", "body": "details", "state": "open"}]

    result = await service.verify_related_issues(
        "PR", "body", candidates, pr_files="@@ -0,0 +1 @@\n+details"
    )

    assert result[0]["number"] == 1
    assert result[0]["relation"] == "related"
    assert calls[0]["role"] == "summary"
    assert calls[0]["model"] == ""


@pytest.mark.asyncio
async def test_star_aid_summary_uses_summary_role_client(monkeypatch):
    calls = []

    class FakeClient:
        async def call_with_retry(self, **kwargs):
            calls.append(kwargs)
            return _Response()

    monkeypatch.setattr(star_aid_summary_service, "AIApiClient", lambda: FakeClient())
    monkeypatch.setattr(
        star_aid_summary_service,
        "get_settings",
        lambda: SimpleNamespace(summary_model="legacy", openai_model="legacy"),
    )

    result = await star_aid_summary_service.generate_summary(
        full_name="owner/repo",
        description="description",
        topics=[],
        primary_language="Python",
        readme_excerpt="README",
        lang="zh-CN",
    )

    assert result == "summary"
    assert calls[0]["role"] == "summary"
    assert calls[0]["model"] == ""


@pytest.mark.asyncio
async def test_issue_verification_propagates_missing_summary_role(monkeypatch):
    """summary 角色配置错误不能伪装成所有候选均已验证。"""

    class FailingClient:
        async def resolve_role_candidates(self, role):
            from tests.test_pr_issue_budget import summary_candidate

            assert role == "summary"
            return [summary_candidate()]

        async def call_with_retry(self, **_kwargs):
            raise AllCandidatesFailedError("角色 summary 无可用 AI 候选模型")

    from backend.services.issues import pr_budget

    monkeypatch.setattr(
        pr_budget,
        "get_dynamic_config",
        AsyncMock(
            side_effect=lambda key, **_: {
                "pr_issue_max_files": 128,
                "pr_issue_max_input_tokens": 64000,
            }[key]
        ),
    )

    monkeypatch.setattr(issue_embedding_service, "AIApiClient", lambda: FailingClient())
    service = issue_embedding_service.IssueEmbeddingService.__new__(
        issue_embedding_service.IssueEmbeddingService
    )

    with pytest.raises(AllCandidatesFailedError, match="summary"):
        await service.verify_related_issues(
            "PR",
            "body",
            [{"number": 1, "title": "issue", "body": "details", "state": "open"}],
        )


def test_scan_role_failure_is_not_converted_to_empty_findings():
    """main 角色调用失败必须交给扫描外层标记失败，不能伪装为空扫描。"""
    source = inspect.getsource(scan_worker.ScanWorker._full_scan_with_tools)

    assert 'logger.error(f"全仓扫描 AI 调用失败: {e}")\n                raise' in source


def test_agent_team_candidate_filter_does_not_depend_on_openai_sdk_errors():
    source = inspect.getsource(
        __import__(
            "backend.services.agent_team.candidate_service",
            fromlist=["AgentTeamCandidateService"],
        )
    )

    assert "BadRequestError" not in source


@pytest.mark.asyncio
async def test_issue_verification_propagates_missing_summary_role_at_budget_preflight(
    monkeypatch,
):
    from backend.services.ai_reviewer.api_client import AIApiClient
    from backend.services.issues import pr_budget

    client = AIApiClient()
    client.resolve_role_candidates = AsyncMock(return_value=[])
    client.call_with_retry = AsyncMock()
    monkeypatch.setattr(issue_embedding_service, "AIApiClient", lambda: client)
    monkeypatch.setattr(
        pr_budget,
        "get_dynamic_config",
        AsyncMock(
            side_effect=lambda key, **_: {
                "pr_issue_max_files": 128,
                "pr_issue_max_input_tokens": 64000,
            }[key]
        ),
    )
    service = issue_embedding_service.IssueEmbeddingService.__new__(
        issue_embedding_service.IssueEmbeddingService
    )
    with pytest.raises(AllCandidatesFailedError, match="summary"):
        await service.verify_related_issues(
            "PR",
            "Human",
            [{"number": 1, "title": "Issue", "body": "details", "state": "open"}],
            pr_files="@@ -0,0 +1 @@\n+details",
        )
    client.call_with_retry.assert_not_awaited()
