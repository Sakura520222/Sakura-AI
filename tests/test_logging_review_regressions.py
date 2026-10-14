"""Reproduce credential and Activity gaps at their real execution boundaries."""

import asyncio
import io
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock
from urllib.parse import quote

import httpx
import pytest
from loguru import logger

from backend.core import logging_bridge
from backend.core.log_context import (
    get_log_context,
    log_context,
    provider_log_fields,
    traced_operation,
    update_log_context,
)
from backend.core.logging_bridge import _format_record, _patch_record
from backend.models.scan_models import RepoScan, ScanStatus
from backend.services.ai_reviewer.compression.unified_compressor import (
    UnifiedContextCompressor,
)
from backend.services.ai_reviewer.unified_client import FallbackConfig, UnifiedAIClient
from backend.services.operational_log_service import session_logs
from backend.workers import scan_worker as scan_module
from backend.workers.scan_worker import ScanWorker
from tests.test_scan_worker_timeout import _AsyncSession as _ScanSession
from tests.test_unified_client_fallback import _candidate


class _AsyncSession(_ScanSession):
    async def get(self, model, value, **kwargs):
        return self.scan if model is RepoScan else None

    """Support the composed billing entrypoint without performing provider calls."""

    def __init__(self, scan):
        super().__init__(scan)
        scan.billing_operation_id = None
        scan.trigger_type = (
            "manual"
            if scan.triggered_by.startswith(("webui:", "api:"))
            else "scheduled"
        )

    async def execute(self, statement):
        expression = statement.column_descriptions[0]["expr"]
        if expression is RepoScan:
            self.scan.id = statement.compile().params["id_1"]
            return SimpleNamespace(scalar_one=lambda: self.scan)
        if statement.column_descriptions[0]["name"] == "status":
            # These fixtures model terminal worker failures. Financial effects
            # themselves are covered by the real-SQL billing worker suites.
            return SimpleNamespace(scalar_one=lambda: ScanStatus.FAILED.value)
        return SimpleNamespace(scalar_one_or_none=lambda: None)

    async def commit(self):
        pass


@pytest.fixture
def output(monkeypatch, tmp_path):
    monkeypatch.setattr(logger._core, "patcher", _patch_record)
    monkeypatch.setattr(logging_bridge, "APP_LOG_DIRECTORY", tmp_path)
    console = io.StringIO()
    path = tmp_path / "app_review.log"
    records = []
    handlers = [
        logger.add(console, format=_format_record, level=0, catch=False),
        logger.add(path, format=_format_record, serialize=True, level=0, catch=False),
        logger.add(
            lambda message: records.append(message.record), level=0, catch=False
        ),
    ]
    try:
        yield console, path, records
    finally:
        for handler in handlers:
            logger.remove(handler)


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["runtime", "cancelled", "group"])
@pytest.mark.parametrize("other_task", [False, True])
async def test_explicit_stream_close_keeps_error_identity_and_secret_metadata(
    output, failure, other_task
):
    console, path, records = output
    credential = "opaque-close-credential-713"
    cause = RuntimeError("transport cleanup echoed " + credential)
    error = {
        "runtime": cause,
        "cancelled": asyncio.CancelledError("cleanup cancelled with " + credential),
        "group": ExceptionGroup("cleanup failed", [cause]),
    }[failure]

    @traced_operation(provider_log_fields, events=True)
    async def provider(*, credential):
        update_log_context(attempt_id=73, logical_call_id="close-call")
        try:
            yield "partial"
        finally:
            raise error

    async def close(iterator):
        with log_context(clear=True, request_id="closing-task"):
            try:
                await iterator.aclose()
            finally:
                assert get_log_context() == {"request_id": "closing-task"}

    with log_context(clear=True, request_id="producer", task_id="task-73"):
        iterator = provider(credential=credential)
        assert await anext(iterator) == "partial"
        with pytest.raises(type(error)) as raised:
            if other_task:
                await asyncio.create_task(close(iterator))
            else:
                await iterator.aclose()
        assert raised.value is error
        assert get_log_context() == {"request_id": "producer", "task_id": "task-73"}
        logger.opt(exception=error).error(
            "Worker observed stream close failure: {}", error
        )

    assert credential not in console.getvalue() + path.read_text(encoding="utf-8")
    assert records[-1]["extra"]["attempt_id"] == 73
    assert records[-1]["extra"]["logical_call_id"] == "close-call"
    assert "cleanup" in records[-1]["message"]


@pytest.mark.asyncio
async def test_normal_stream_close_preserves_cleanup_without_failure_log(output):
    _, _, records = output
    cleaned = []

    @traced_operation(provider_log_fields, events=True)
    async def provider(*, credential):
        try:
            yield "partial"
        finally:
            cleaned.append(get_log_context()["attempt_id"])

    with log_context(request_id="normal-close"):
        iterator = provider(credential="opaque-normal-key")
        assert await anext(iterator) == "partial"
        await asyncio.create_task(iterator.aclose())
        assert get_log_context() == {"request_id": "normal-close"}
    assert len(cleaned) == 1
    assert not any(
        record["message"].startswith("Operation failed") for record in records
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("stage", ["clone", "index", "report"])
async def test_scan_failure_is_retrievable_and_parallel_sessions_are_isolated(
    monkeypatch, tmp_path, output, stage
):
    import backend.models.database as database_module
    import backend.services.scan_prompt_builder as prompt_builder

    _, _, records = output
    barrier = asyncio.Barrier(2)
    monkeypatch.setattr(
        scan_module, "_get_scan_semaphore", AsyncMock(return_value=asyncio.Semaphore(2))
    )
    monkeypatch.setattr(
        database_module,
        "async_session",
        lambda: _AsyncSession(
            SimpleNamespace(
                status=ScanStatus.PENDING.value,
                repo_name="owner/repo",
                triggered_by="webui:1",
            )
        ),
    )
    monkeypatch.setattr(
        prompt_builder, "collect_code_files", lambda _: [{"path": "app.py"}]
    )
    monkeypatch.setattr(
        scan_module, "get_user_dynamic_config", AsyncMock(return_value="zh-CN")
    )

    async def run(scan_id):
        workspace = tmp_path / f"repo-{scan_id}"
        workspace.mkdir()
        context = SimpleNamespace(
            session_id=scan_id + 100,
            invocation_id=scan_id + 200,
            work_unit_id=scan_id + 300,
            thread_id=scan_id + 400,
        )
        execution = SimpleNamespace(invocation_context=context, finish=AsyncMock())
        worker = ScanWorker.__new__(ScanWorker)
        worker._start_threaded_execution = AsyncMock(return_value=execution)
        worker._update_scan = AsyncMock()
        worker._clone_repo = AsyncMock(return_value=str(workspace))
        worker._get_commit_sha = AsyncMock(return_value="abc")
        worker._index_repository = AsyncMock(
            return_value={"indexed": 1, "total_chunks": 1}
        )
        worker._full_scan_with_tools = AsyncMock(
            return_value=({"findings": [], "overall_score": 90, "summary": "ok"}, 1)
        )
        worker._save_findings = AsyncMock()
        worker._generate_reports = AsyncMock(return_value={})

        async def fail(*args, **kwargs):
            await barrier.wait()
            logger.info("scan reached failure stage {}", stage)
            raise RuntimeError(f"{stage} stage failed for scan {scan_id}")

        setattr(
            worker,
            {
                "clone": "_clone_repo",
                "index": "_index_repository",
                "report": "_generate_reports",
            }[stage],
            fail,
        )
        parent = {"request_id": f"request-{scan_id}", "session_id": 999}
        with log_context(clear=True, **parent):
            await worker.process_scan(scan_id)
            assert get_log_context() == parent
        execution.finish.assert_awaited_once_with(
            "failed", error_message=f"{stage} stage failed for scan {scan_id}"
        )

    await asyncio.gather(run(31), run(32))
    for scan_id in (31, 32):
        matches = [
            record
            for record in records
            if f"扫描 {scan_id} 执行失败" in record["message"]
        ]
        assert len(matches) == 1
        extra = matches[0]["extra"]
        assert extra["session_id"] == scan_id + 100
        assert extra["invocation_id"] == scan_id + 200
        assert extra["task_id"] == scan_id
        assert extra["request_id"] == f"request-{scan_id}"
        text = session_logs(scan_id + 100)
        assert f"扫描 {scan_id} 执行失败" in text and "Traceback" in text
        assert f"扫描 {63 - scan_id} 执行失败" not in text


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "encoding",
    [
        "plain",
        "quoted",
        "quoted_lowercase",
        "quoted_mixedcase",
        "plus",
        "double_quoted",
        "escaped_unreserved",
        "double_quoted_plus",
    ],
)
@pytest.mark.parametrize("long_key", [False, True])
async def test_real_chat_retry_fallback_logs_and_diagnostic_copies_are_safe(
    output, encoding, long_key
):
    from backend.core.ai_protocol.models import ProtocolFamily, UnifiedMessage

    console, path, records = output
    key = "opaque-fallback-secret-" + ("q" * 220 if long_key else "q" * 5) + "+/="
    echoed = quote(key, safe="") if encoding != "plain" else key
    if encoding == "quoted_lowercase":
        echoed = (
            echoed.replace("%2B", "%2b").replace("%2F", "%2f").replace("%3D", "%3d")
        )
    elif encoding == "quoted_mixedcase":
        echoed = echoed.replace("%2B", "%2b").replace("%3D", "%3d")
    elif encoding == "double_quoted":
        echoed = quote(echoed, safe="")
    elif encoding == "escaped_unreserved":
        echoed = echoed.replace("opaque", "%6fpaque")
    elif encoding in {"plus", "double_quoted_plus"}:
        from urllib.parse import quote_plus

        key += " key suffix"
        echoed = quote_plus(key)
        if encoding == "double_quoted_plus":
            echoed = quote(echoed, safe="")
    calls = []

    def upstream(request):
        body = json.loads(request.content)
        calls.append(body["model"])
        if body["model"] == "primary":
            return httpx.Response(
                429, json={"error": {"message": "HTTP 429 provider echoed " + echoed}}
            )
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {"role": "assistant", "content": "fallback-ok"},
                        "finish_reason": "stop",
                    }
                ]
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(upstream)) as transport:
        client = UnifiedAIClient(
            http_client=transport,
            fallback_config=FallbackConfig(
                enabled=True, max_candidates=2, max_retries=1, initial_retry_delay=0
            ),
        )
        primary = _candidate(
            ProtocolFamily.OPENAI_COMPATIBLE, "primary", credential=key
        )
        fallback = _candidate(
            ProtocolFamily.OPENAI_COMPATIBLE,
            "fallback",
            credential="opaque-fallback-good-key",
        )
        response = await client.call_with_retry(
            [primary, fallback],
            [UnifiedMessage(role="user", content="hello")],
            model="primary",
        )
    assert response.content == "fallback-ok"
    assert calls == ["primary", "primary", "fallback"]
    text = (
        console.getvalue()
        + path.read_text(encoding="utf-8")
        + json.dumps(response.meta.attempt_chain)
    )
    assert key not in text and echoed[:40] not in text
    assert any("AI 候选失败" in record["message"] for record in records)
    assert "429" in text


@pytest.mark.asyncio
async def test_direct_compression_failure_redacts_credentials_and_remains_best_effort(
    output,
):
    from backend.core.ai_protocol.models import ProtocolFamily, UnifiedMessage

    console, path, _ = output
    key = "opaque-compression-secret-714"

    def upstream(request):
        return httpx.Response(
            401, json={"error": {"message": "provider echoed " + key}}
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(upstream)) as transport:
        compressor = UnifiedContextCompressor(http_client=transport)
        result = await compressor.compress_for_candidate(
            candidate=_candidate(
                ProtocolFamily.OPENAI_COMPATIBLE, "compressor", credential=key
            ),
            messages=[
                UnifiedMessage(role="user", content="a conversation to summarize")
            ],
        )
    assert result is None
    text = console.getvalue() + path.read_text(encoding="utf-8")
    assert key not in text
    assert "压缩摘要调用失败，放弃压缩" in text


@pytest.mark.asyncio
async def test_scan_without_activity_keeps_degraded_failure_behavior_and_resets_context(
    monkeypatch, output
):
    import backend.models.database as database_module

    _, _, records = output
    monkeypatch.setattr(
        scan_module, "_get_scan_semaphore", AsyncMock(return_value=asyncio.Semaphore(1))
    )
    monkeypatch.setattr(
        database_module,
        "async_session",
        lambda: _AsyncSession(
            SimpleNamespace(
                status=ScanStatus.PENDING.value,
                repo_name="owner/repo",
                triggered_by="scheduled",
            )
        ),
    )
    worker = ScanWorker.__new__(ScanWorker)
    worker._start_threaded_execution = AsyncMock(return_value=None)
    worker._update_scan = AsyncMock()
    worker._clone_repo = AsyncMock(
        side_effect=RuntimeError("clone failed before model call")
    )

    with log_context(
        clear=True, request_id="scheduler", session_id=999, invocation_id=888
    ):
        await worker.process_scan(34)
        assert get_log_context() == {
            "request_id": "scheduler",
            "session_id": 999,
            "invocation_id": 888,
        }
    failure = next(
        record for record in records if "扫描 34 执行失败" in record["message"]
    )
    assert failure["extra"]["task_id"] == 34
    assert "session_id" not in failure["extra"]
    assert "invocation_id" not in failure["extra"]
    assert worker._update_scan.await_args.kwargs["status"] == ScanStatus.FAILED.value


@pytest.mark.asyncio
async def test_real_stream_retry_and_fallback_preserve_outputs_without_credentials(
    output,
):
    from backend.core.ai_protocol.models import ProtocolFamily, UnifiedMessage

    console, path, _ = output
    key = "opaque-stream-key-" + "m" * 220 + "+/="
    encoded = quote(key, safe="").replace("%2B", "%2b")
    calls = []

    def upstream(request):
        model = json.loads(request.content)["model"]
        calls.append(model)
        if model == "primary":
            return httpx.Response(
                429, json={"error": {"message": "provider echoed " + encoded}}
            )
        chunks = [
            {
                "choices": [
                    {
                        "index": 0,
                        "delta": {"content": "fallback-ok"},
                        "finish_reason": None,
                    }
                ]
            },
            {"choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]},
        ]
        body = (
            "".join("data: " + json.dumps(chunk) + "\n\n" for chunk in chunks)
            + "data: [DONE]\n\n"
        )
        return httpx.Response(
            200, headers={"content-type": "text/event-stream"}, text=body
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(upstream)) as transport:
        client = UnifiedAIClient(
            http_client=transport,
            fallback_config=FallbackConfig(
                enabled=True, max_candidates=2, max_retries=1, initial_retry_delay=0
            ),
        )
        candidates = [
            _candidate(ProtocolFamily.OPENAI_COMPATIBLE, "primary", credential=key),
            _candidate(
                ProtocolFamily.OPENAI_COMPATIBLE,
                "fallback",
                credential="fallback-good-key",
            ),
        ]
        events = [
            event
            async for event in client.stream_with_retry(
                candidates,
                [UnifiedMessage(role="user", content="hello")],
                model="primary",
            )
        ]
    assert calls == ["primary", "primary", "fallback"]
    assert "".join(event.text or "" for event in events) == "fallback-ok"
    assert any(event.type == "done" for event in events)
    text = console.getvalue() + path.read_text(encoding="utf-8")
    assert key[:40] not in text and encoded[:40] not in text


@pytest.mark.asyncio
async def test_real_stream_failure_after_delta_is_not_replayed(output):
    from backend.core.ai_protocol.errors import AIError
    from backend.core.ai_protocol.models import (
        AIErrorCategory,
        ProtocolFamily,
        UnifiedMessage,
    )

    console, path, _ = output
    key = "opaque-emitted-stream-key-715"
    error = AIError(AIErrorCategory.SERVER_ERROR, "stream failed with " + key)
    calls = []

    class FailingStream(httpx.AsyncByteStream):
        async def __aiter__(self):
            chunk = {
                "choices": [
                    {"index": 0, "delta": {"content": "partial"}, "finish_reason": None}
                ]
            }
            yield ("data: " + json.dumps(chunk) + "\n\n").encode()
            raise error

    def upstream(request):
        calls.append(json.loads(request.content)["model"])
        return httpx.Response(
            200, headers={"content-type": "text/event-stream"}, stream=FailingStream()
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(upstream)) as transport:
        client = UnifiedAIClient(
            http_client=transport,
            fallback_config=FallbackConfig(
                enabled=True, max_candidates=2, max_retries=1, initial_retry_delay=0
            ),
        )
        candidates = [
            _candidate(ProtocolFamily.OPENAI_COMPATIBLE, "primary", credential=key),
            _candidate(ProtocolFamily.OPENAI_COMPATIBLE, "fallback"),
        ]
        delivered = []
        with pytest.raises(AIError) as raised:
            async for event in client.stream_with_retry(
                candidates,
                [UnifiedMessage(role="user", content="hello")],
                model="primary",
            ):
                delivered.append(event.text or "")
    assert raised.value is error
    assert "".join(delivered) == "partial"
    assert calls == ["primary"]
    assert key not in console.getvalue() + path.read_text(encoding="utf-8")


def test_exception_message_redaction_handles_nested_causes_without_mutation():
    from backend.core.log_sanitizer import sanitize_exception_text, sensitive_log_scope

    key = "opaque-nested-key-716"
    cause = RuntimeError("provider failure")
    cause._log_redaction_secrets = (key,)
    outer = RuntimeError("HTTP 503 copied " + key)
    outer.__cause__ = ExceptionGroup("nested failure", [cause])
    assert sanitize_exception_text(outer) == "HTTP 503 copied ***"
    assert str(outer) == "HTTP 503 copied " + key
    control = "HTTP 503 path=/repo | route=%2fdocs | value=alpha%2b%2f%3d"
    with sensitive_log_scope("Alpha+/="):
        assert sanitize_exception_text(RuntimeError(control)) == control


@pytest.mark.parametrize("depth", [1, 2, 8, 64])
def test_deep_percent_encoding_preserves_unmatched_diagnostic_text(depth):
    from urllib.parse import quote_plus

    from backend.core.log_sanitizer import sanitize_text

    key = "opaque-literal%2F+/= key"
    encoded = quote_plus(key)
    for _ in range(depth):
        encoded = quote(encoded, safe="")
    prefix = "HTTP 503 路径=repo/目录 | unrelated=%2fdocs | malformed=%GG | echoed="
    assert sanitize_text(prefix + encoded, secrets=(key,)) == prefix + "***"


def test_unicode_percent_encoded_key_and_malformed_unicode_diagnostic_are_safe():
    from backend.core.log_sanitizer import sanitize_text

    key = "opaque-密钥+/="
    encoded = quote(quote(key, safe=""), safe="")
    prefix = "provider detail \ud800 | HTTP 503 | "
    assert sanitize_text(prefix + encoded, secrets=(key,)) == prefix + "***"
