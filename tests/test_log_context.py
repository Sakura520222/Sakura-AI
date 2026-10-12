import asyncio
import logging
from types import SimpleNamespace

import pytest
from loguru import logger

from backend.core.log_context import (
    RequestLogContextMiddleware,
    get_log_context,
    log_context,
    model_log_fields,
    task_logging,
    traced_operation,
    update_log_context,
)
from backend.core.logging_bridge import (
    InterceptHandler,
    _patch_record,
    install_standard_logging_bridge,
)


@pytest.fixture
def captured(monkeypatch):
    monkeypatch.setattr(logger._core, "patcher", _patch_record)
    records = []
    handler = logger.add(lambda message: records.append(message.record), catch=False)
    try:
        yield records
    finally:
        logger.remove(handler)


@pytest.mark.asyncio
async def test_request_worker_thread_and_concurrent_lane_isolation(captured):
    barrier = asyncio.Barrier(2)
    ids = {}

    @task_logging("test")
    async def worker(task_id):
        assert "session_id" not in get_log_context()
        update_log_context(session_id=task_id, invocation_id=task_id + 10)
        await barrier.wait()
        logger.info("worker")
        await asyncio.to_thread(logger.info, "thread")
        await asyncio.sleep(0)

    async def app(scope, receive, send):
        task_id = scope["task_id"]
        assert set(get_log_context()) == {"request_id"}
        ids[task_id] = get_log_context()["request_id"]
        await worker(task_id)
        assert get_log_context() == {"request_id": ids[task_id]}
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"ok"})

    async def request(number):
        messages = []

        async def send(message):
            messages.append(message)

        with log_context(request_id="old", session_id="must-not-leak"):
            await RequestLogContextMiddleware(app)(
                {
                    "type": "http",
                    "task_id": number,
                    "headers": [(b"x-request-id", b"credential-value")],
                },
                None,
                send,
            )
            assert get_log_context() == {
                "request_id": "old",
                "session_id": "must-not-leak",
            }
        assert dict(messages[0]["headers"])[b"x-request-id"].decode() == ids[number]

    await asyncio.gather(request(1), request(2))
    assert ids[1] != ids[2]
    for record in captured:
        if record["message"] not in {"worker", "thread"}:
            continue
        extra = record["extra"]
        assert extra["request_id"] == ids[extra["task_id"]]
        assert extra["session_id"] == extra["task_id"]
        assert extra["invocation_id"] == extra["task_id"] + 10
    assert get_log_context() == {}


@pytest.mark.asyncio
async def test_stream_context_does_not_leak_to_consumer_or_close_task(captured):
    closed = []

    @traced_operation(model_log_fields)
    async def stream(*, context, logical_call_id):
        update_log_context(attempt_id=77)
        try:
            logger.info("inside-stream")
            yield 1
            assert get_log_context()["attempt_id"] == 77
            yield 2
        finally:
            closed.append(get_log_context())

    lane = SimpleNamespace(session_id=1, invocation_id=2, work_unit_id=3, thread_id=4)
    with log_context(request_id="outer"):
        iterator = stream(context=lane, logical_call_id="logical-1")
        assert await anext(iterator) == 1
        assert get_log_context() == {"request_id": "outer"}
        assert await anext(iterator) == 2
        await asyncio.create_task(iterator.aclose())
        assert get_log_context() == {"request_id": "outer"}
    assert closed[0]["attempt_id"] == 77
    assert closed[0]["logical_call_id"] == "logical-1"
    assert captured[-1]["extra"]["session_id"] == 1


@pytest.mark.asyncio
async def test_cancellation_resets_worker_context():
    @task_logging("cancelled")
    async def worker(task_id):
        update_log_context(invocation_id=999)
        raise asyncio.CancelledError

    with log_context(request_id="caller"):
        with pytest.raises(asyncio.CancelledError):
            await worker(1)
        assert get_log_context() == {"request_id": "caller"}


@pytest.mark.asyncio
async def test_escaping_provider_error_keeps_redaction_and_original_type(captured):
    @traced_operation()
    async def send(*, credential):
        update_log_context(attempt_id=91, logical_call_id="failed-call")
        raise RuntimeError("provider echoed " + credential)

    try:
        await send(credential="opaque-credential")
    except RuntimeError as exc:
        assert type(exc) is RuntimeError
        logger.opt(exception=exc).error("worker failed: {}", exc)
    assert "opaque-credential" not in str(captured)
    assert "RuntimeError" in captured[-1]["message"]
    assert captured[-1]["extra"]["attempt_id"] == 91
    assert captured[-1]["extra"]["logical_call_id"] == "failed-call"


def test_delayed_standard_record_preserves_producer_context(monkeypatch, captured):
    previous = logging.getLogRecordFactory()
    root_handlers = logging.getLogger().handlers[:]
    root_level = logging.getLogger().level
    try:
        install_standard_logging_bridge()
        factory = logging.getLogRecordFactory()
        install_standard_logging_bridge()
        assert logging.getLogRecordFactory() is factory
        with log_context(request_id="producer", task_id="task-a"):
            record = logging.getLogger("queue.source").makeRecord(
                "queue.source",
                logging.ERROR,
                "/producer.py",
                15,
                "queued",
                (),
                None,
                func="produce",
            )
        with log_context(request_id="consumer", task_id="task-b"):
            InterceptHandler().emit(record)
        assert captured[-1]["extra"]["request_id"] == "producer"
        assert captured[-1]["extra"]["task_id"] == "task-a"
    finally:
        logging.setLogRecordFactory(previous)
        logging.getLogger().handlers = root_handlers
        logging.getLogger().setLevel(root_level)
