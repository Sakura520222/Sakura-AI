"""Focused cancellation propagation tests for Issue analysis protocol paths."""

from types import SimpleNamespace

import pytest

import backend.services.issue_analyzer as issue_analyzer_module
import backend.services.protocol_repair as protocol_repair_module
from backend.core.ai_protocol.errors import ReviewCancelledError
from backend.services.ai_task_deadline import AITaskDeadline
from backend.services.issue_analyzer import IssueAnalyzer
from backend.services.issue_protocol import IssueProtocolError
from backend.services.protocol_repair import run_protocol_repair_loop


async def _async_result(value):
    return value


def _configure_analyzer(monkeypatch, analyzer, client):
    class _Settings:
        review_timeout_seconds = 120
        ai_temperature = 0.2
        issue_price_per_1k_prompt = 1
        issue_price_per_1k_completion = 1

    analyzer.api_client = client
    analyzer.tool_manager = SimpleNamespace(
        get_enabled_tools=lambda _repo: _async_result([])
    )
    analyzer._refresh_ai_client = lambda: None
    analyzer._refresh_runtime_config = lambda: None
    analyzer._build_system_prompt = lambda *_args, **_kwargs: "system"
    analyzer._build_user_message = lambda *_args, **_kwargs: "user"

    async def get_repo_labels(*_args):
        return {}

    async def get_sakura_context(*_args, **_kwargs):
        return {}

    monkeypatch.setattr(issue_analyzer_module, "get_settings", lambda: _Settings())
    monkeypatch.setattr(
        issue_analyzer_module,
        "get_user_dynamic_config",
        lambda *_args: _async_result("en"),
    )
    monkeypatch.setattr(
        issue_analyzer_module,
        "get_dynamic_config",
        lambda _key: _async_result(False),
    )
    monkeypatch.setattr(
        issue_analyzer_module,
        "get_model_context_manager",
        lambda: SimpleNamespace(calculate_safe_context=lambda *_args: 80_000),
    )
    monkeypatch.setattr(
        "backend.services.label_service.label_service.get_repo_labels",
        get_repo_labels,
    )
    monkeypatch.setattr(
        "backend.core.github_app.GitHubAppClient",
        lambda: SimpleNamespace(get_repo_collaborators=lambda *_args: []),
    )
    monkeypatch.setattr(
        "backend.services.sakura_memory_service.get_sakura_memory_service",
        lambda: SimpleNamespace(get_sakura_context=get_sakura_context),
    )


def _issue_info():
    return {
        "issue_number": 1,
        "title": "title",
        "body": "body",
        "author": "author",
        "state": "open",
    }


@pytest.mark.asyncio
async def test_issue_analyzer_reraises_provider_cancellation(monkeypatch):
    cancellation = ReviewCancelledError("provider cancelled")

    class _Client:
        async def resolve_role_model_context(self, _role):
            return "model-x", 100_000

        async def call_with_retry(self, **_kwargs):
            raise cancellation

    analyzer = IssueAnalyzer.__new__(IssueAnalyzer)
    _configure_analyzer(monkeypatch, analyzer, _Client())

    with pytest.raises(ReviewCancelledError) as raised:
        await analyzer.analyze_issue(
            _issue_info(),
            "owner",
            "repo",
            cancel_event=SimpleNamespace(is_set=lambda: False),
            deadline=AITaskDeadline.from_timeout(120),
        )

    assert raised.value is cancellation


@pytest.mark.asyncio
async def test_issue_analyzer_rechecks_cancellation_at_repair_return(monkeypatch):
    analyzer = IssueAnalyzer.__new__(IssueAnalyzer)
    analyzer.api_client = SimpleNamespace()
    cancelled = SimpleNamespace(value=False)

    async def fake_repair_loop(**_kwargs):
        cancelled.value = True
        return {"parse_source": "test"}

    monkeypatch.setattr(
        issue_analyzer_module,
        "run_protocol_repair_loop",
        fake_repair_loop,
    )
    monkeypatch.setattr(
        issue_analyzer_module,
        "get_dynamic_config",
        lambda _key: _async_result(1),
    )

    with pytest.raises(ReviewCancelledError):
        await analyzer._parse_or_repair_analysis(
            "response",
            [],
            SimpleNamespace(),
            cancel_event=SimpleNamespace(is_set=lambda: cancelled.value),
        )


@pytest.mark.asyncio
async def test_protocol_repair_reraises_provider_cancellation(monkeypatch):
    cancellation = ReviewCancelledError("repair cancelled")
    fallback_calls = []

    class _Client:
        async def call_with_retry(self, **_kwargs):
            raise cancellation

    def parse_fn(_text):
        raise IssueProtocolError("invalid envelope")

    async def no_publish(*_args, **_kwargs):
        return None

    monkeypatch.setattr(protocol_repair_module, "publish_event", no_publish)

    with pytest.raises(ReviewCancelledError) as raised:
        await run_protocol_repair_loop(
            parse_fn=parse_fn,
            error_type=IssueProtocolError,
            base_messages=[],
            final_text="invalid",
            repair_instruction="repair",
            api_client=_Client(),
            tracker=SimpleNamespace(accumulate=lambda _response: None),
            max_attempts=1,
            fallback_result_fn=lambda error: fallback_calls.append(error) or {},
            log_label="Issue 分析",
            sse_channel="issue:protocol_repair",
        )

    assert raised.value is cancellation
    assert fallback_calls == []


@pytest.mark.asyncio
async def test_protocol_repair_preserves_fallback_for_regular_error(monkeypatch):
    class _Client:
        async def call_with_retry(self, **_kwargs):
            raise RuntimeError("network down")

    def parse_fn(_text):
        raise IssueProtocolError("invalid envelope")

    fallback_errors = []

    async def no_publish(*_args, **_kwargs):
        return None

    monkeypatch.setattr(protocol_repair_module, "publish_event", no_publish)

    result = await run_protocol_repair_loop(
        parse_fn=parse_fn,
        error_type=IssueProtocolError,
        base_messages=[],
        final_text="invalid",
        repair_instruction="repair",
        api_client=_Client(),
        tracker=SimpleNamespace(accumulate=lambda _response: None),
        max_attempts=1,
        fallback_result_fn=lambda error: fallback_errors.append(error)
        or {"parse_source": "fallback"},
        log_label="Issue 分析",
        sse_channel="issue:protocol_repair",
    )

    assert result == {"parse_source": "fallback"}
    assert len(fallback_errors) == 1
    assert isinstance(fallback_errors[0], RuntimeError)


@pytest.mark.asyncio
async def test_protocol_repair_rechecks_cancellation_before_success_return():
    cancelled = SimpleNamespace(value=False)

    async def on_repaired(*_args):
        cancelled.value = True

    with pytest.raises(ReviewCancelledError):
        await run_protocol_repair_loop(
            parse_fn=lambda _text: {"ok": True},
            error_type=IssueProtocolError,
            base_messages=[],
            final_text="valid",
            repair_instruction="repair",
            api_client=SimpleNamespace(),
            tracker=SimpleNamespace(),
            max_attempts=1,
            fallback_result_fn=lambda _error: {"fallback": True},
            log_label="Issue 分析",
            sse_channel="issue:protocol_repair",
            on_repaired=on_repaired,
            cancel_event=SimpleNamespace(is_set=lambda: cancelled.value),
        )
