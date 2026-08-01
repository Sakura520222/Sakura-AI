from types import SimpleNamespace

import pytest

from backend.services.ai_reviewer.label_recommender import LabelRecommender


class _PromptBuilder:
    def build_label_recommendation_message(self, *_args):
        return "recommend labels"


class _ResultParser:
    def parse_label_recommendation(self, _text):
        return [{"name": "bug", "confidence": 0.9, "reason": "test"}]


@pytest.mark.asyncio
async def test_label_recommender_forwards_its_own_observation_lane():
    captured = {}

    class _Client:
        async def call_with_retry(self, **kwargs):
            captured.update(kwargs)
            return SimpleNamespace(
                choices=[
                    SimpleNamespace(
                        message=SimpleNamespace(
                            content='{"labels":[{"name":"bug"}]}'
                        )
                    )
                ]
            )

    invocation_context = object()
    observer = object()
    recommender = LabelRecommender(
        api_client=_Client(),
        prompt_builder=_PromptBuilder(),
        result_parser=_ResultParser(),
    )

    result = await recommender.recommend_labels(
        {},
        {"bug": {}},
        {},
        invocation_context=invocation_context,
        observer=observer,
        propagate_errors=True,
    )

    assert result[0]["name"] == "bug"
    assert captured["role"] == "summary"
    assert captured["context"] is invocation_context
    assert captured["observer"] is observer


@pytest.mark.asyncio
async def test_label_recommender_can_propagate_failure_for_work_unit_status():
    class _Client:
        async def call_with_retry(self, **_kwargs):
            raise RuntimeError("provider failed")

    recommender = LabelRecommender(
        api_client=_Client(),
        prompt_builder=_PromptBuilder(),
        result_parser=_ResultParser(),
    )

    with pytest.raises(RuntimeError, match="provider failed"):
        await recommender.recommend_labels(
            {},
            {"bug": {}},
            {},
            propagate_errors=True,
        )


@pytest.mark.asyncio
async def test_label_recommender_emits_request_and_response_events():
    """标签推荐经 event_callback 把请求/响应写入辅助可观测通道。

    Emit label-recommendation request/response messages through event_callback
    so the auxiliary summary thread can surface distinguishable cards in the
    live activity monitor instead of a bare summary-role attempt.
    """
    events: list[tuple[str, dict]] = []

    async def capture(event_type, data):
        events.append((event_type, dict(data)))

    class _Client:
        async def call_with_retry(self, **_kwargs):
            # 请求事件必须在 provider 调用之前发出
            # request events must be emitted before the provider call
            assert any(
                et == "message"
                and d.get("message_kind") == "label_recommendation_request"
                for et, d in events
            )
            return SimpleNamespace(
                choices=[
                    SimpleNamespace(
                        message=SimpleNamespace(
                            content='{"labels":[{"name":"bug"}]}'
                        )
                    )
                ]
            )

    recommender = LabelRecommender(
        api_client=_Client(),
        prompt_builder=_PromptBuilder(),
        result_parser=_ResultParser(),
    )

    result = await recommender.recommend_labels(
        {},
        {"bug": {}},
        {},
        event_callback=capture,
        propagate_errors=True,
    )

    assert result[0]["name"] == "bug"

    request_events = [
        d
        for et, d in events
        if et == "message"
        and d.get("message_kind") == "label_recommendation_request"
    ]
    response_events = [
        d
        for et, d in events
        if et == "message"
        and d.get("message_kind") == "label_recommendation_response"
    ]

    # 请求覆盖 system + user 两条消息，且携带实际 prompt 内容
    # request covers both system and user turns with real prompt text
    assert {d["role"] for d in request_events} == {"system", "user"}
    assert all(d.get("content") for d in request_events)

    # 响应恰好一条 assistant 消息，携带模型返回内容
    # response is a single assistant turn carrying the model output
    assert len(response_events) == 1
    assert response_events[0]["role"] == "assistant"
    assert "bug" in response_events[0]["content"]
