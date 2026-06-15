"""Runtime AI configuration refresh coverage."""

from types import SimpleNamespace

import pytest

from backend.services.ai_reviewer import reviewer as reviewer_module
from backend.services.ai_reviewer.tools import ToolHandler
from backend.services import embedding_service as embedding_module
from backend.services.issue_analyzer import IssueAnalyzer
from backend.services import issue_analyzer as issue_analyzer_module
from backend.services import sakura_memory_service as sakura_memory_module


class _FakeAIApiClient:
    def __init__(self, base_url, api_key):
        self.base_url = base_url
        self.api_key = api_key


def test_reviewer_refreshes_clients_after_dynamic_config_change(monkeypatch):
    settings = SimpleNamespace(
        openai_api_base="https://old.example/v1",
        openai_api_key="old-key",
        openai_model="old-model",
        summary_api_base="",
        summary_api_key="",
        summary_model="",
    )
    monkeypatch.setattr(reviewer_module, "get_settings", lambda: settings)
    monkeypatch.setattr(reviewer_module, "AIApiClient", _FakeAIApiClient)

    reviewer = reviewer_module.AIReviewer.__new__(reviewer_module.AIReviewer)
    reviewer._ai_client_config = None
    reviewer._summary_client_config = None
    reviewer.context_compressor = SimpleNamespace(api_client=None, model=None)
    reviewer.label_recommender = SimpleNamespace(api_client=None, model=None)

    reviewer._refresh_ai_clients()
    old_client = reviewer.api_client

    settings.openai_api_base = "https://new.example/v1"
    settings.openai_api_key = "new-key"
    settings.openai_model = "new-model"
    reviewer._refresh_ai_clients()

    assert reviewer.api_client is not old_client
    assert reviewer.api_client.base_url == "https://new.example/v1"
    assert reviewer.api_client.api_key == "new-key"
    assert reviewer.summary_api_client is reviewer.api_client
    assert reviewer.context_compressor.api_client is reviewer.api_client
    assert reviewer.context_compressor.model == "new-model"
    assert reviewer.label_recommender.api_client is reviewer.api_client
    assert reviewer.label_recommender.model == "new-model"


def test_reviewer_refreshes_summary_client_when_auxiliary_config_changes(monkeypatch):
    """辅助模型独立配置变化后，_refresh_ai_clients 重建 summary_api_client。

    覆盖 summary_api_base/key 非空（独立 provider）场景：修改辅助模型凭据后，
    summary_api_client 应重建为新凭据并始终与主 AI api_client 解耦；
    summary_model 跟随最新 settings；主 AI client 不受影响。
    对应 WebUI 即时修改辅助 AI 配置后辅助任务（PR 总结/依赖图/历史上下文）
    必须使用新 provider 的回归保护。
    """
    settings = SimpleNamespace(
        openai_api_base="https://main.example/v1",
        openai_api_key="main-key",
        openai_model="main-model",
        summary_api_base="https://aux-old.example/v1",
        summary_api_key="aux-old-key",
        summary_model="aux-old-model",
    )
    monkeypatch.setattr(reviewer_module, "get_settings", lambda: settings)
    monkeypatch.setattr(reviewer_module, "AIApiClient", _FakeAIApiClient)

    reviewer = reviewer_module.AIReviewer.__new__(reviewer_module.AIReviewer)
    reviewer._ai_client_config = None
    reviewer._summary_client_config = None
    reviewer.context_compressor = SimpleNamespace(api_client=None, model=None)
    reviewer.label_recommender = SimpleNamespace(api_client=None, model=None)

    reviewer._refresh_ai_clients()
    # 独立 provider：summary_api_client 与主 api_client 解耦
    assert reviewer.summary_api_client is not reviewer.api_client
    assert reviewer.summary_api_client.base_url == "https://aux-old.example/v1"
    assert reviewer.summary_api_client.api_key == "aux-old-key"
    assert reviewer.summary_model == "aux-old-model"
    old_summary_client = reviewer.summary_api_client

    # 仅修改辅助模型凭据（主 AI 不变）
    settings.summary_api_base = "https://aux-new.example/v1"
    settings.summary_api_key = "aux-new-key"
    settings.summary_model = "aux-new-model"
    reviewer._refresh_ai_clients()

    # summary_api_client 重建为新凭据，主 api_client 保持不变
    assert reviewer.summary_api_client is not old_summary_client
    assert reviewer.summary_api_client.base_url == "https://aux-new.example/v1"
    assert reviewer.summary_api_client.api_key == "aux-new-key"
    assert reviewer.summary_model == "aux-new-model"
    assert reviewer.api_client.base_url == "https://main.example/v1"
    assert reviewer.api_client.api_key == "main-key"
    # context_compressor / label_recommender 跟随最新辅助凭据
    assert reviewer.context_compressor.api_client is reviewer.summary_api_client
    assert reviewer.context_compressor.model == "aux-new-model"
    assert reviewer.label_recommender.api_client is reviewer.summary_api_client
    assert reviewer.label_recommender.model == "aux-new-model"


def test_issue_analyzer_refreshes_client_after_dynamic_config_change(monkeypatch):
    settings = SimpleNamespace(
        openai_api_base="https://old.example/v1",
        openai_api_key="old-key",
    )
    monkeypatch.setattr(issue_analyzer_module, "get_settings", lambda: settings)
    monkeypatch.setattr(issue_analyzer_module, "AIApiClient", _FakeAIApiClient)

    analyzer = IssueAnalyzer.__new__(IssueAnalyzer)
    analyzer._ai_client_config = None
    analyzer._refresh_ai_client()
    old_client = analyzer.api_client

    settings.openai_api_base = "https://new.example/v1"
    settings.openai_api_key = "new-key"
    analyzer._refresh_ai_client()

    assert analyzer.api_client is not old_client
    assert analyzer.api_client.base_url == "https://new.example/v1"
    assert analyzer.api_client.api_key == "new-key"


def test_reviewer_refreshes_runtime_tool_and_compression_config(monkeypatch):
    settings = SimpleNamespace(
        enable_context_compression=False,
        context_compression_threshold=0.7,
        context_compression_keep_rounds=4,
        web_search_enabled=False,
        fetch_url_enabled=False,
    )
    monkeypatch.setattr(reviewer_module, "get_settings", lambda: settings)

    reviewer = reviewer_module.AIReviewer.__new__(reviewer_module.AIReviewer)
    reviewer.context_compressor = SimpleNamespace(keep_rounds=1)
    reviewer.tool_handler = ToolHandler(
        file_tool=None,
        search_tool=None,
        web_search_tool=object(),
        git_tool=None,
        search_files_tool=None,
        sakura_tool=None,
        fetch_url_tool=object(),
    )

    reviewer._refresh_runtime_config()

    assert reviewer.enable_compression is False
    assert reviewer.compression_threshold == 0.7
    assert reviewer.keep_rounds == 4
    assert reviewer.context_compressor.keep_rounds == 4
    assert reviewer.tool_handler.web_search_tool is None
    assert reviewer.tool_handler.fetch_url_tool is None


class _FakeEmbeddingClient:
    def __init__(self, base_url, api_key):
        self.base_url = base_url
        self.api_key = api_key


class _FakeRerankerClient:
    def __init__(self, base_url, headers, **_kwargs):
        self.base_url = base_url
        self.headers = headers


def test_embedding_and_reranker_services_refresh_dynamic_clients(monkeypatch):
    settings = SimpleNamespace(
        embedding_provider="openai",
        embedding_base_url="https://old-embedding.example/v1",
        embedding_api_key="old-embedding-key",
        embedding_model="old-embedding-model",
        rerank_provider="siliconflow",
        rerank_base_url="https://old-rerank.example",
        rerank_api_key="old-rerank-key",
        rerank_model="old-rerank-model",
    )
    monkeypatch.setattr(embedding_module, "get_settings", lambda: settings)
    monkeypatch.setattr(embedding_module, "AsyncOpenAI", _FakeEmbeddingClient)
    monkeypatch.setattr(embedding_module.httpx, "AsyncClient", _FakeRerankerClient)

    embedding = embedding_module.EmbeddingService()
    reranker = embedding_module.RerankerService()
    old_embedding_client = embedding.client
    old_reranker_client = reranker.client

    settings.embedding_base_url = "https://new-embedding.example/v1"
    settings.embedding_api_key = "new-embedding-key"
    settings.rerank_base_url = "https://new-rerank.example"
    settings.rerank_api_key = "new-rerank-key"
    embedding._refresh_client()
    reranker._refresh_client()

    assert embedding.client is not old_embedding_client
    assert embedding.client.base_url == "https://new-embedding.example/v1"
    assert embedding.client.api_key == "new-embedding-key"
    assert old_embedding_client in embedding._retired_clients
    assert reranker.client is not old_reranker_client
    assert reranker.client.base_url == "https://new-rerank.example"
    assert reranker.client.headers["Authorization"] == "Bearer new-rerank-key"
    assert old_reranker_client in reranker._retired_clients


def test_sakura_memory_refreshes_main_and_summary_credentials(monkeypatch):
    settings = SimpleNamespace(
        sakura_use_summary_model=False,
        openai_api_base="https://main.example/v1",
        openai_api_key="main-key",
        openai_model="main-model",
        summary_api_base="https://summary.example/v1",
        summary_api_key="summary-key",
        summary_model="summary-model",
    )
    monkeypatch.setattr(sakura_memory_module, "get_settings", lambda: settings)
    monkeypatch.setattr(sakura_memory_module, "AIApiClient", _FakeAIApiClient)

    service = sakura_memory_module.SakuraMemoryService.__new__(
        sakura_memory_module.SakuraMemoryService
    )
    service._ai_client_config = None
    service._refresh_ai_client()

    settings.sakura_use_summary_model = True
    service._refresh_ai_client()

    assert service.api_client.base_url == "https://summary.example/v1"
    assert service.api_client.api_key == "summary-key"
    assert service._default_model == "summary-model"


def test_reviewer_runtime_config_creates_web_tools_when_enabled(monkeypatch):
    """web_search / fetch_url 启用时按需创建工具，且刷新幂等不重建已有实例。"""
    settings = SimpleNamespace(
        enable_context_compression=True,
        context_compression_threshold=0.85,
        context_compression_keep_rounds=2,
        web_search_enabled=True,
        fetch_url_enabled=True,
    )
    monkeypatch.setattr(reviewer_module, "get_settings", lambda: settings)

    reviewer = reviewer_module.AIReviewer.__new__(reviewer_module.AIReviewer)
    reviewer.context_compressor = SimpleNamespace(keep_rounds=1)
    reviewer.tool_handler = ToolHandler(
        file_tool=None,
        search_tool=None,
        web_search_tool=None,
        git_tool=None,
        search_files_tool=None,
        sakura_tool=None,
        fetch_url_tool=None,
    )

    reviewer._refresh_runtime_config()
    assert reviewer.tool_handler.web_search_tool is not None
    assert reviewer.tool_handler.fetch_url_tool is not None

    # 配置不变再次刷新，已有实例应保持同一对象（幂等）
    web_before = reviewer.tool_handler.web_search_tool
    fetch_before = reviewer.tool_handler.fetch_url_tool
    reviewer._refresh_runtime_config()
    assert reviewer.tool_handler.web_search_tool is web_before
    assert reviewer.tool_handler.fetch_url_tool is fetch_before


def test_embedding_service_skips_rebuild_when_config_unchanged(monkeypatch):
    """配置未变化时 _refresh_client 命中缓存，不重建客户端（cache-hit 路径）。"""
    settings = SimpleNamespace(
        embedding_provider="openai",
        embedding_base_url="https://emb.example/v1",
        embedding_api_key="emb-key",
        embedding_model="emb-model",
    )
    monkeypatch.setattr(embedding_module, "get_settings", lambda: settings)
    monkeypatch.setattr(embedding_module, "AsyncOpenAI", _FakeEmbeddingClient)

    embedding = embedding_module.EmbeddingService()
    client_before = embedding.client
    embedding._refresh_client()  # 配置不变
    assert embedding.client is client_before
    assert embedding._retired_clients == []


@pytest.mark.asyncio
async def test_embedding_service_close_releases_retired_clients(monkeypatch):
    """close() 关闭当前与退役客户端，并清空 _retired_clients。"""
    settings = SimpleNamespace(
        embedding_provider="openai",
        embedding_base_url="https://emb.example/v1",
        embedding_api_key="emb-key",
        embedding_model="emb-model",
    )
    monkeypatch.setattr(embedding_module, "get_settings", lambda: settings)

    closed = []

    class _ClosableEmbeddingClient(_FakeEmbeddingClient):
        async def close(self):
            closed.append(self)

    monkeypatch.setattr(embedding_module, "AsyncOpenAI", _ClosableEmbeddingClient)

    embedding = embedding_module.EmbeddingService()
    first = embedding.client
    settings.embedding_api_key = "new-key"
    embedding._refresh_client()
    assert first in embedding._retired_clients

    await embedding.close()
    assert embedding._retired_clients == []
    assert embedding.client in closed
    assert first in closed


@pytest.mark.asyncio
async def test_reranker_service_close_releases_retired_clients(monkeypatch):
    """close() 关闭当前与退役 httpx 客户端，并清空 _retired_clients。"""
    settings = SimpleNamespace(
        rerank_provider="siliconflow",
        rerank_base_url="https://rerank.example",
        rerank_api_key="rerank-key",
        rerank_model="rerank-model",
    )
    monkeypatch.setattr(embedding_module, "get_settings", lambda: settings)

    closed = []

    class _ClosableRerankerClient(_FakeRerankerClient):
        async def aclose(self):
            closed.append(self)

    monkeypatch.setattr(embedding_module.httpx, "AsyncClient", _ClosableRerankerClient)

    reranker = embedding_module.RerankerService()
    first = reranker.client
    settings.rerank_api_key = "new-key"
    reranker._refresh_client()
    assert first in reranker._retired_clients

    await reranker.close()
    assert reranker._retired_clients == []
    assert reranker.client in closed
    assert first in closed
