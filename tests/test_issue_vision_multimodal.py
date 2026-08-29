"""Issue 图片多模态测试 / Tests for Issue vision multimodal support (Issue #538).

覆盖：
- UnifiedImagePart / UnifiedMessage.images 的协议渲染（4 个适配器）
- 非 vision 候选的请求级剥离与 legacy 转换透传
- 上下文压缩器的图片预算与保留
- 观测性捕获边界的 base64 折叠
- Issue 图片提取、白名单下载与推送脱敏
- IssueAnalyzer 的图片注入路径
"""

from types import SimpleNamespace

import httpx
import pytest

import backend.services.issue_analyzer as issue_analyzer_module
import backend.services.issue_image_service as issue_image_service_module
from backend.core.ai_protocol.adapters.anthropic_native import AnthropicNativeAdapter
from backend.core.ai_protocol.adapters.gemini_native import GeminiNativeAdapter
from backend.core.ai_protocol.adapters.openai_compatible import (
    OpenAICompatibleAdapter,
)
from backend.core.ai_protocol.adapters.openai_responses import (
    OpenAIResponsesAdapter,
)
from backend.core.ai_protocol.models import (
    AuthScheme,
    ModelCapabilitySet,
    ModelMetadata,
    ProtocolFamily,
    ProviderDeclaration,
    ReasoningParams,
    ResolvedModel,
    StopReason,
    UnifiedImagePart,
    UnifiedMessage,
    UnifiedRequest,
    UnifiedResponse,
    UnifiedUsage,
    images_from_mapping,
    strip_message_images,
)
from backend.core.ai_protocol.registry import resolve_endpoint
from backend.services.activity_observability.observer import fold_image_payloads
from backend.services.ai_reviewer.compression.unified_compressor import (
    UnifiedContextCompressor,
)
from backend.services.ai_reviewer.unified_client import (
    FallbackConfig,
    UnifiedAIClient,
)
from backend.services.issue_analyzer import IssueAnalyzer
from backend.services.issue_image_service import (
    collect_issue_images,
    extract_image_references,
    strip_image_payloads_for_display,
)

# ---------------------------------------------------------------------------
# 适配器渲染 / Adapter rendering
# ---------------------------------------------------------------------------


def _vision_request() -> UnifiedRequest:
    return UnifiedRequest(
        model="test-model",
        messages=[
            UnifiedMessage(
                role="user",
                content="look at this",
                images=[
                    UnifiedImagePart(data="YWJj", media_type="image/png"),
                    UnifiedImagePart(url="https://img.example.test/a.png"),
                ],
            )
        ],
        max_tokens=1024,
    )


def test_openai_compatible_renders_image_parts():
    body = OpenAICompatibleAdapter().serialize_request(_vision_request())
    content = body["messages"][0]["content"]
    assert isinstance(content, list)
    assert content[0] == {"type": "text", "text": "look at this"}
    assert content[1] == {
        "type": "image_url",
        "image_url": {"url": "data:image/png;base64,YWJj"},
    }
    assert content[2] == {
        "type": "image_url",
        "image_url": {"url": "https://img.example.test/a.png"},
    }


def test_openai_compatible_text_message_stays_string():
    request = UnifiedRequest(
        model="m",
        messages=[UnifiedMessage(role="user", content="plain")],
        max_tokens=16,
    )
    body = OpenAICompatibleAdapter().serialize_request(request)
    assert body["messages"][0]["content"] == "plain"


def test_openai_responses_renders_input_image():
    body = OpenAIResponsesAdapter().serialize_request(_vision_request())
    user_item = body["input"][0]
    assert user_item["role"] == "user"
    assert user_item["content"] == [
        {"type": "input_text", "text": "look at this"},
        {"type": "input_image", "image_url": "data:image/png;base64,YWJj"},
        {"type": "input_image", "image_url": "https://img.example.test/a.png"},
    ]


def test_anthropic_renders_image_blocks():
    body = AnthropicNativeAdapter().serialize_request(_vision_request())
    content = body["messages"][0]["content"]
    assert content[0] == {"type": "text", "text": "look at this"}
    assert content[1] == {
        "type": "image",
        "source": {
            "type": "base64",
            "media_type": "image/png",
            "data": "YWJj",
        },
    }
    assert content[2] == {
        "type": "image",
        "source": {"type": "url", "url": "https://img.example.test/a.png"},
    }


def test_gemini_renders_inline_data_parts():
    body = GeminiNativeAdapter().serialize_request(_vision_request())
    parts = body["contents"][0]["parts"]
    assert parts[0] == {"text": "look at this"}
    assert parts[1] == {
        "inlineData": {"mimeType": "image/png", "data": "YWJj"}
    }
    assert parts[2] == {"fileData": {"fileUri": "https://img.example.test/a.png"}}


# ---------------------------------------------------------------------------
# 模型 helpers / Model helpers
# ---------------------------------------------------------------------------


def test_images_from_mapping_parses_and_filters():
    parsed = images_from_mapping(
        [
            {"url": "https://a.example.test/1.png"},
            {"media_type": "image/png", "data": "YWJj"},
            {"no_url_no_data": True},
            "junk",
            UnifiedImagePart(url="https://b.example.test/2.png"),
        ]
    )
    assert parsed is not None
    assert parsed[0].url == "https://a.example.test/1.png"
    assert parsed[1].data == "YWJj"
    assert parsed[2].url == "https://b.example.test/2.png"
    assert images_from_mapping("not-a-list") is None
    assert images_from_mapping([]) is None


def test_strip_message_images_returns_same_list_without_images():
    plain = UnifiedMessage(role="user", content="a")
    stripped = strip_message_images([plain])
    assert stripped is not None and stripped[0] is plain

    with_image = UnifiedMessage(
        role="user", content="a", images=[UnifiedImagePart(url="https://x/y.png")]
    )
    result = strip_message_images([plain, with_image])
    assert result[0] is plain
    assert result[1].images is None
    assert result[1].content == "a"
    assert with_image.images is not None  # 原消息不被修改


# ---------------------------------------------------------------------------
# UnifiedAIClient 门控 / Capability gating
# ---------------------------------------------------------------------------


def _candidate(vision: bool) -> ResolvedModel:
    decl = ProviderDeclaration(
        id=f"prov-{'vision' if vision else 'text'}",
        label="prov",
        family=ProtocolFamily.OPENAI_COMPATIBLE,
        base_url="https://example.test/v1/",
        auth_scheme=AuthScheme.BEARER,
    )
    metadata = ModelMetadata(
        model_id="model-x",
        provider_id=decl.id,
        display_name="model-x",
        context_window_tokens=128000,
        max_output_tokens=4096,
        capabilities=ModelCapabilitySet(vision=vision),
        reasoning_params=ReasoningParams(),
    )
    return ResolvedModel(
        provider=decl,
        model=metadata,
        credential="key",
        endpoint=resolve_endpoint(decl, None),
    )


class _CapturingAdapter:
    family = ProtocolFamily.OPENAI_COMPATIBLE

    def __init__(self):
        self.requests = []

    async def list_models(self, *args, **kwargs):
        return []

    async def fetch_model_metadata(self, *args, **kwargs):
        return None

    async def chat(self, client, endpoint, credential, request, *, timeout=None):
        self.requests.append(request)
        return UnifiedResponse(
            content="ok",
            tool_calls=[],
            stop_reason=StopReason.END_TURN,
            usage=UnifiedUsage(),
        )

    async def stream(self, *args, **kwargs):  # pragma: no cover - unused here
        raise NotImplementedError

    def build_headers(self, credential, endpoint):
        return {}

    def translate_error(self, status_code, body):
        raise NotImplementedError


@pytest.mark.asyncio
async def test_call_with_retry_strips_images_for_non_vision_candidate(monkeypatch):
    adapter = _CapturingAdapter()
    monkeypatch.setattr(
        "backend.services.ai_reviewer.unified_client._get_adapter",
        lambda _family: adapter,
    )
    client = UnifiedAIClient(fallback_config=FallbackConfig(enabled=False))
    legacy = [
        {
            "role": "user",
            "content": "see the screenshot",
            "images": [{"media_type": "image/png", "data": "YWJj"}],
        }
    ]

    await client.call_with_retry(
        [_candidate(vision=False)], legacy, model="model-x", max_tokens=64
    )
    assert adapter.requests[0].messages[0].images is None
    assert adapter.requests[0].messages[0].content == "see the screenshot"

    await client.call_with_retry(
        [_candidate(vision=True)], legacy, model="model-x", max_tokens=64
    )
    assert adapter.requests[1].messages[0].images is not None
    assert adapter.requests[1].messages[0].images[0].data == "YWJj"


# ---------------------------------------------------------------------------
# 压缩器 / Compressor
# ---------------------------------------------------------------------------


def _compressor() -> UnifiedContextCompressor:
    compressor = UnifiedContextCompressor.__new__(UnifiedContextCompressor)
    compressor._model_ctx = SimpleNamespace(
        estimate_tokens=lambda text: max(1, len(text) // 4)
    )
    return compressor


def test_compressor_last_user_message_and_render_keep_images():
    compressor = _compressor()
    body = [
        UnifiedMessage(role="user", content="task", images=[UnifiedImagePart(url="https://x/1.png")]),
    ]
    last = compressor._last_user_message(body)
    assert last is not None and last.images is not None

    rendered = compressor._render_history(body)
    assert "[image attachment: https://x/1.png]" in rendered


def test_compressor_truncate_message_keeps_images():
    compressor = _compressor()
    message = UnifiedMessage(
        role="user",
        content="a" * 400,
        images=[UnifiedImagePart(data="YWJj", media_type="image/png")],
    )
    truncated = compressor._truncate_message(message, 10)
    assert truncated is not None
    assert truncated.images is not None and truncated.images[0].data == "YWJj"


# ---------------------------------------------------------------------------
# 观测性折叠 / Observability folding
# ---------------------------------------------------------------------------


def test_fold_image_payloads_folds_all_protocol_shapes():
    payload = {
        "messages": [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": "hi"},
                    {
                        "type": "image_url",
                        "image_url": {"url": "data:image/png;base64," + "A" * 500},
                    },
                ],
            },
            {
                "role": "user",
                "content": [
                    {
                        "type": "image",
                        "source": {
                            "type": "base64",
                            "media_type": "image/png",
                            "data": "B" * 500,
                        },
                    }
                ],
            },
            {
                "role": "model",
                "parts": [
                    {"inlineData": {"mimeType": "image/png", "data": "C" * 500}}
                ],
            },
        ]
    }
    folded = fold_image_payloads(payload)
    image_url = folded["messages"][0]["content"][1]["image_url"]["url"]
    assert image_url.startswith("<folded image data-url: 5")
    source = folded["messages"][1]["content"][0]["source"]
    assert source["data"].startswith("<folded image data: 5")
    inline = folded["messages"][2]["parts"][0]["inlineData"]
    assert inline["data"].startswith("<folded image data: 5")
    # 非图片文本不受影响
    assert folded["messages"][0]["content"][0]["text"] == "hi"


# ---------------------------------------------------------------------------
# 图片提取与下载 / Extraction & download
# ---------------------------------------------------------------------------


def test_extract_image_references_mixed_and_deduped():
    text = (
        "![a](https://user-images.githubusercontent.com/1.png)\n"
        '<img src="https://github.com/user-attachments/assets/abc.png">\n'
        "![a again](https://user-images.githubusercontent.com/1.png)\n"
        "plain link https://example.test/no-image.png stays out\n"
    )
    assert extract_image_references(text) == [
        "https://user-images.githubusercontent.com/1.png",
        "https://github.com/user-attachments/assets/abc.png",
    ]
    assert extract_image_references(None) == []
    assert extract_image_references("") == []


def test_validate_image_url_domain_allowlist():
    entries = ["user-images.githubusercontent.com", "github.com/user-attachments"]
    validate = issue_image_service_module._validate_image_url
    assert (
        validate("https://user-images.githubusercontent.com/a/b.png", entries)
        is not None
    )
    assert (
        validate(
            "https://github.com/user-attachments/assets/abc.png", entries
        )
        is not None
    )
    # host 匹配但路径不在白名单前缀内
    assert validate("https://github.com/owner/repo", entries) is None
    assert validate("https://evil.example.test/a.png", entries) is None
    assert validate("ftp://user-images.githubusercontent.com/a.png", entries) is None
    assert validate("http://127.0.0.1/a.png", entries) is None


def test_validate_image_url_s3_wildcard_pattern():
    """GitHub 用户资产 302 跳转的 S3 签名桶须按段级通配匹配。"""
    entries = ["github-production-user-asset-*.s3.amazonaws.com"]
    validate = issue_image_service_module._validate_image_url
    s3_url = (
        "https://github-production-user-asset-6210df.s3.amazonaws.com"
        "/x.png?X-Amz-Signature=abc"
    )
    assert validate(s3_url, entries) is not None
    # 段数不匹配 / 段内容不匹配均拒绝
    assert validate(
        "https://github-production-user-asset-6210df.s3.us-east-1.amazonaws.com/x.png",
        entries,
    ) is None
    assert validate(
        "https://evil-production-user-asset-6210df.s3.amazonaws.com/x.png",
        entries,
    ) is None


def _mock_transport_settings(monkeypatch, *, max_size=1000):
    class _Settings:
        issue_vision_max_image_size_bytes = max_size
        issue_vision_allowed_image_domains = (
            "user-images.githubusercontent.com,github.com/user-attachments,"
            "private-user-images.githubusercontent.com"
        )

    monkeypatch.setattr(
        issue_image_service_module, "get_settings", lambda: _Settings()
    )


@pytest.mark.asyncio
async def test_collect_issue_images_downloads_and_encodes(monkeypatch):
    _mock_transport_settings(monkeypatch)
    payload = b"fakepng"

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "user-images.githubusercontent.com":
            return httpx.Response(
                200, headers={"content-type": "image/png"}, content=payload
            )
        return httpx.Response(404)

    real_client = httpx.AsyncClient
    monkeypatch.setattr(
        issue_image_service_module.httpx,
        "AsyncClient",
        lambda **kwargs: real_client(transport=httpx.MockTransport(handler), **kwargs),
    )
    github_app = SimpleNamespace(
        integration=SimpleNamespace(
            get_access_token=lambda installation_id: SimpleNamespace(token="tok")
        )
    )

    images = await collect_issue_images(
        [
            "https://user-images.githubusercontent.com/a/b.png",
            "https://evil.example.test/x.png",
        ],
        github_app=github_app,
        installation_id=123,
    )

    assert len(images) == 1
    assert images[0]["media_type"] == "image/png"
    import base64

    assert base64.b64decode(images[0]["data"]) == payload


@pytest.mark.asyncio
async def test_collect_issue_images_follows_redirect_to_s3_without_auth(monkeypatch):
    """真实链路：user-assets 302 → S3 签名桶，重定向后不得携带 Authorization。"""
    class _Settings:
        issue_vision_max_image_size_bytes = 1000
        issue_vision_allowed_image_domains = (
            "user-images.githubusercontent.com,"
            "github-production-user-asset-*.s3.amazonaws.com"
        )

    monkeypatch.setattr(
        issue_image_service_module, "get_settings", lambda: _Settings()
    )
    seen_headers = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen_headers.append(dict(request.headers))
        if request.url.host == "user-images.githubusercontent.com":
            return httpx.Response(
                302,
                headers={
                    "location": (
                        "https://github-production-user-asset-6210df.s3.amazonaws.com"
                        "/obj.png?X-Amz-Signature=abc"
                    )
                },
            )
        if request.url.host.startswith("github-production-user-asset-"):
            return httpx.Response(
                200,
                headers={"content-type": "image/png"},
                content=b"pngdata",
            )
        return httpx.Response(404)

    real_client = httpx.AsyncClient
    monkeypatch.setattr(
        issue_image_service_module.httpx,
        "AsyncClient",
        lambda **kwargs: real_client(transport=httpx.MockTransport(handler), **kwargs),
    )
    github_app = SimpleNamespace(
        integration=SimpleNamespace(
            get_access_token=lambda installation_id: SimpleNamespace(token="tok")
        )
    )

    images = await collect_issue_images(
        ["https://user-images.githubusercontent.com/a/b.png"],
        github_app=github_app,
        installation_id=1,
    )

    assert len(images) == 1
    assert images[0]["media_type"] == "image/png"
    # 首跳带 installation 凭据；S3 签名跳不带 Authorization
    assert seen_headers[0].get("authorization") == "Bearer tok"
    assert seen_headers[1].get("authorization") is None


@pytest.mark.asyncio
async def test_collect_issue_images_rejects_bad_redirect_and_content_type(
    monkeypatch,
):
    _mock_transport_settings(monkeypatch)

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "user-images.githubusercontent.com":
            # 重定向到白名单外域名
            return httpx.Response(
                302, headers={"location": "https://evil.example.test/x.png"}
            )
        if request.url.host == "github.com":
            return httpx.Response(
                200, headers={"content-type": "text/html"}, content=b"<html/>"
            )
        return httpx.Response(404)

    real_client = httpx.AsyncClient
    monkeypatch.setattr(
        issue_image_service_module.httpx,
        "AsyncClient",
        lambda **kwargs: real_client(transport=httpx.MockTransport(handler), **kwargs),
    )

    images = await collect_issue_images(
        [
            "https://user-images.githubusercontent.com/a/b.png",
            "https://github.com/user-attachments/assets/abc.png",
        ]
    )
    assert images == []


@pytest.mark.asyncio
async def test_collect_issue_images_respects_size_limit(monkeypatch):
    _mock_transport_settings(monkeypatch, max_size=10)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers={"content-type": "image/png", "content-length": "500"},
            content=b"x" * 500,
        )

    real_client = httpx.AsyncClient
    monkeypatch.setattr(
        issue_image_service_module.httpx,
        "AsyncClient",
        lambda **kwargs: real_client(transport=httpx.MockTransport(handler), **kwargs),
    )

    images = await collect_issue_images(
        [
            "https://user-images.githubusercontent.com/a/b.png",
            "https://user-images.githubusercontent.com/c/d.png",
        ]
    )
    # 两张均因 Content-Length 超过防内存耗尽上限被跳过，无数量截取
    assert images == []


def test_strip_image_payloads_for_display_removes_base64():
    messages = [
        {"role": "system", "content": "sys"},
        {
            "role": "user",
            "content": "body",
            "images": [
                {
                    "url": "https://user-images.githubusercontent.com/a/b.png",
                    "media_type": "image/png",
                    "data": "YWJj",
                }
            ],
        },
    ]
    sanitized = strip_image_payloads_for_display(messages)
    assert sanitized[0] is messages[0]
    assert sanitized[1]["images"] == [
        {
            "url": "https://user-images.githubusercontent.com/a/b.png",
            "media_type": "image/png",
        }
    ]
    # 原消息保留 base64 供 AI 请求使用
    assert messages[1]["images"][0]["data"] == "YWJj"


# ---------------------------------------------------------------------------
# IssueAnalyzer 集成 / Analyzer integration
# ---------------------------------------------------------------------------


def test_build_user_message_mentions_attached_images(monkeypatch):
    class _StrategyConfig:
        def get_issue_analysis_config(self):
            return {}

    monkeypatch.setattr(
        "backend.services.issue_analyzer.get_strategy_config",
        lambda: _StrategyConfig(),
    )
    analyzer = IssueAnalyzer.__new__(IssueAnalyzer)
    message = analyzer._build_user_message(
        {"issue_number": 1, "title": "t", "author": "a", "state": "open"},
        ["bug"],
        [],
        image_count=2,
    )
    assert "2 张" in message
    assert "多模态附件" in message
    plain = analyzer._build_user_message(
        {"issue_number": 1, "title": "t", "author": "a", "state": "open"},
        ["bug"],
        [],
    )
    assert "多模态附件" not in plain


async def _result(value):
    return value


@pytest.mark.asyncio
async def test_analyze_issue_attaches_images_and_sanitizes_callback(monkeypatch):
    class _Settings:
        review_timeout_seconds = 120
        ai_temperature = 0.2
        issue_price_per_1k_prompt = 1
        issue_price_per_1k_completion = 1
        issue_vision_enabled = True

    class _FakeClient:
        def __init__(self):
            self.calls = []

        async def resolve_role_model_context(self, _role):
            return "model-x", 100_000

        async def resolve_role_primary_candidate(self, _role):
            return SimpleNamespace(
                model=SimpleNamespace(
                    model_id="model-x",
                    context_window_tokens=100_000,
                    capabilities=SimpleNamespace(vision=True),
                )
            )

        async def call_with_retry(self, **kwargs):
            self.calls.append(kwargs)
            return SimpleNamespace(
                usage=SimpleNamespace(prompt_tokens=3, completion_tokens=5),
                choices=[
                    SimpleNamespace(
                        message=SimpleNamespace(content="final", tool_calls=None)
                    )
                ],
            )

    downloaded = [
        {
            "url": "https://user-images.githubusercontent.com/a/b.png",
            "media_type": "image/png",
            "data": "YWJj",
        }
    ]

    async def fake_collect(urls, **_kwargs):
        assert urls == ["https://user-images.githubusercontent.com/a/b.png"]
        return list(downloaded)

    monkeypatch.setattr(
        issue_analyzer_module, "collect_issue_images", fake_collect
    )
    monkeypatch.setattr(
        issue_analyzer_module, "get_settings", lambda: _Settings()
    )
    monkeypatch.setattr(
        issue_analyzer_module,
        "get_user_dynamic_config",
        lambda *_args: _result("en"),
    )
    monkeypatch.setattr(
        issue_analyzer_module,
        "get_dynamic_config",
        lambda _key: _result(False),
    )
    monkeypatch.setattr(
        issue_analyzer_module,
        "get_model_context_manager",
        lambda: SimpleNamespace(calculate_safe_context=lambda *_a: 80_000),
    )
    monkeypatch.setattr(
        "backend.services.label_service.label_service.get_repo_labels",
        lambda *_args: _result({"bug": {}}),
    )
    monkeypatch.setattr(
        "backend.core.github_app.GitHubAppClient",
        lambda: SimpleNamespace(get_repo_collaborators=lambda *_a: []),
    )
    monkeypatch.setattr(
        "backend.services.sakura_memory_service.get_sakura_memory_service",
        lambda: SimpleNamespace(
            get_sakura_context=lambda *a, **k: _result({})
        ),
    )

    async def fake_parse(_text, _messages, _tracker, **_kwargs):
        return {"category": "bug"}

    client = _FakeClient()
    analyzer = IssueAnalyzer.__new__(IssueAnalyzer)
    analyzer.api_client = client
    analyzer.tool_manager = SimpleNamespace(
        get_enabled_tools=lambda _repo: _result([])
    )
    analyzer.tool_handler = SimpleNamespace()
    analyzer._refresh_ai_client = lambda: None
    analyzer._refresh_runtime_config = lambda: None
    analyzer._parse_or_repair_analysis = fake_parse

    pushed = []

    async def event_callback(event_type, data):
        pushed.append((event_type, data))

    result = await analyzer.analyze_issue(
        {
            "issue_number": 1,
            "title": "title",
            "body": "![shot](https://user-images.githubusercontent.com/a/b.png)",
            "author": "author",
            "state": "open",
            "installation_id": 42,
        },
        "owner",
        "repo",
        event_callback=event_callback,
    )

    assert result["category"] == "bug"
    # AI 请求收到完整 base64 附件
    sent_user = client.calls[0]["messages"][1]
    assert sent_user["images"] == downloaded
    assert "多模态附件" in sent_user["content"]
    # 前端推送边界不含 base64
    pushed_user = next(data for _t, data in pushed if data.get("role") == "user")
    assert "data" not in pushed_user["images"][0]
    assert (
        pushed_user["images"][0]["url"]
        == "https://user-images.githubusercontent.com/a/b.png"
    )


@pytest.mark.asyncio
async def test_analyze_issue_skips_images_when_disabled(monkeypatch):
    class _Settings:
        review_timeout_seconds = 120
        ai_temperature = 0.2
        issue_price_per_1k_prompt = 1
        issue_price_per_1k_completion = 1
        issue_vision_enabled = False

    class _FakeClient:
        def __init__(self):
            self.calls = []

        async def resolve_role_model_context(self, _role):
            return "model-x", 100_000

        async def resolve_role_primary_candidate(self, _role):
            return SimpleNamespace(
                model=SimpleNamespace(
                    model_id="model-x",
                    context_window_tokens=100_000,
                    capabilities=SimpleNamespace(vision=True),
                )
            )

        async def call_with_retry(self, **kwargs):
            self.calls.append(kwargs)
            return SimpleNamespace(
                usage=SimpleNamespace(prompt_tokens=3, completion_tokens=5),
                choices=[
                    SimpleNamespace(
                        message=SimpleNamespace(content="final", tool_calls=None)
                    )
                ],
            )

    collect_calls = []

    async def fake_collect(urls, **kwargs):
        collect_calls.append(urls)
        return []

    monkeypatch.setattr(
        issue_analyzer_module, "collect_issue_images", fake_collect
    )
    monkeypatch.setattr(
        issue_analyzer_module, "get_settings", lambda: _Settings()
    )
    monkeypatch.setattr(
        issue_analyzer_module,
        "get_user_dynamic_config",
        lambda *_args: _result("en"),
    )
    monkeypatch.setattr(
        issue_analyzer_module,
        "get_dynamic_config",
        lambda _key: _result(False),
    )
    monkeypatch.setattr(
        issue_analyzer_module,
        "get_model_context_manager",
        lambda: SimpleNamespace(calculate_safe_context=lambda *_a: 80_000),
    )
    monkeypatch.setattr(
        "backend.services.label_service.label_service.get_repo_labels",
        lambda *_args: _result({"bug": {}}),
    )
    monkeypatch.setattr(
        "backend.core.github_app.GitHubAppClient",
        lambda: SimpleNamespace(get_repo_collaborators=lambda *_a: []),
    )
    monkeypatch.setattr(
        "backend.services.sakura_memory_service.get_sakura_memory_service",
        lambda: SimpleNamespace(
            get_sakura_context=lambda *a, **k: _result({})
        ),
    )

    async def fake_parse(_text, _messages, _tracker, **_kwargs):
        return {"category": "bug"}

    client = _FakeClient()
    analyzer = IssueAnalyzer.__new__(IssueAnalyzer)
    analyzer.api_client = client
    analyzer.tool_manager = SimpleNamespace(
        get_enabled_tools=lambda _repo: _result([])
    )
    analyzer.tool_handler = SimpleNamespace()
    analyzer._refresh_ai_client = lambda: None
    analyzer._refresh_runtime_config = lambda: None
    analyzer._parse_or_repair_analysis = fake_parse

    await analyzer.analyze_issue(
        {
            "issue_number": 1,
            "title": "title",
            "body": "![shot](https://user-images.githubusercontent.com/a/b.png)",
            "author": "author",
            "state": "open",
        },
        "owner",
        "repo",
    )

    assert collect_calls == []
    assert "images" not in client.calls[0]["messages"][1]
