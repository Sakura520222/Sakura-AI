"""AIReviewer 标签推荐可观测性回调透传契约测试。"""

import pytest

from backend.services.ai_reviewer.reviewer import AIReviewer


@pytest.mark.asyncio
async def test_reviewer_forwards_label_event_callback_to_recommender():
    """AIReviewer 必须把标签推荐事件回调透传到 LabelRecommender。

    The wrapper preserves the callback so label-recommendation request and
    response events can reach the auxiliary summary thread.
    """
    captured = {}

    class _Recommender:
        async def recommend_labels(self, *_args, **kwargs):
            captured.update(kwargs)
            return [{"name": "bug"}]

    reviewer = AIReviewer.__new__(AIReviewer)
    reviewer.label_recommender = _Recommender()
    reviewer._refresh_ai_clients = lambda: None
    reviewer._refresh_runtime_config = lambda: None
    event_callback = object()
    invocation_context = object()
    observer = object()

    result = await reviewer.recommend_labels(
        {},
        {"bug": {}},
        {},
        event_callback=event_callback,
        invocation_context=invocation_context,
        observer=observer,
        propagate_errors=True,
    )

    assert result == [{"name": "bug"}]
    assert captured["event_callback"] is event_callback
    assert captured["invocation_context"] is invocation_context
    assert captured["observer"] is observer
    assert captured["propagate_errors"] is True
