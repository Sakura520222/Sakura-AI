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
