"""标签辅助 AI 调用的软 deadline 行为测试。"""

import pytest

from backend.services.ai_reviewer.label_recommender import LabelRecommender
from backend.services.ai_task_deadline import AITaskDeadline


class _Client:
    def __init__(self):
        self.calls = 0

    async def call_with_retry(self, **_kwargs):
        self.calls += 1
        raise AssertionError("expired auxiliary label call must be skipped")


class _PromptBuilder:
    def build_label_recommendation_message(self, *_args):
        return "labels"


class _Parser:
    def parse_label_recommendation(self, _text):
        return []


@pytest.mark.asyncio
async def test_expired_deadline_skips_label_auxiliary_provider_call():
    client = _Client()
    recommender = LabelRecommender(
        api_client=client,
        prompt_builder=_PromptBuilder(),
        result_parser=_Parser(),
    )

    result = await recommender.recommend_labels(
        {},
        {"bug": {}},
        {},
        deadline=AITaskDeadline.from_timeout(0),
    )

    assert result == []
    assert client.calls == 0
