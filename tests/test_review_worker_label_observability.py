"""标签推荐 worker 可观测回调契约测试。

Verify the worker-side callback that connects LabelRecommender events to the
auxiliary summary thread so label-recommendation request/response cards become
distinguishable in the live activity monitor.
"""

from types import SimpleNamespace

import pytest

from backend.workers.review_worker import _make_label_event_callback


class _RecordingToolService:
    def __init__(self):
        self.calls = []

    async def append_conversation_message(self, **kwargs):
        self.calls.append(kwargs)


class _FailingToolService:
    async def append_conversation_message(self, **_kwargs):
        raise RuntimeError("db down")


def _bundle(tool_service):
    return SimpleNamespace(
        thread=SimpleNamespace(id=42),
        work_unit=SimpleNamespace(id=7),
        tool_service=tool_service,
        lease=object(),
    )


@pytest.mark.asyncio
async def test_label_event_callback_persists_message_events_on_summary_thread():
    """回调把 message 事件转为 append_conversation_message 调用，写入辅助
    summary Thread，保留 message_kind 与 lease。The callback forwards each
    message event to the auxiliary summary thread with kind and lease intact.
    """
    tool_service = _RecordingToolService()
    callback = _make_label_event_callback(_bundle(tool_service), task_id="task-1")

    await callback(
        "message",
        {
            "role": "system",
            "content": "sys",
            "message_kind": "label_recommendation_request",
        },
    )
    await callback(
        "message",
        {
            "role": "user",
            "content": "u",
            "message_kind": "label_recommendation_request",
        },
    )
    # 非 message 事件必须忽略 / non-message events are ignored
    await callback("tool_running", {"name": "x"})
    await callback(
        "message",
        {
            "role": "assistant",
            "content": "{}",
            "message_kind": "label_recommendation_response",
        },
    )

    assert len(tool_service.calls) == 3
    assert {call["thread_id"] for call in tool_service.calls} == {42}
    assert {call["work_unit_id"] for call in tool_service.calls} == {7}
    assert all(call["lease"] is not None for call in tool_service.calls)
    assert tool_service.calls[0]["message"]["message_kind"] == (
        "label_recommendation_request"
    )
    assert tool_service.calls[-1]["message"]["message_kind"] == (
        "label_recommendation_response"
    )


@pytest.mark.asyncio
async def test_label_event_callback_swallows_observability_failures():
    """可观测写入失败不向上抛，避免阻断标签推荐业务。Observability write
    failures are swallowed so they never break the label recommendation flow.
    """
    callback = _make_label_event_callback(
        _bundle(_FailingToolService()), task_id="task-2"
    )
    # 不应抛出异常 / must not raise
    await callback("message", {"role": "user", "content": "x"})
