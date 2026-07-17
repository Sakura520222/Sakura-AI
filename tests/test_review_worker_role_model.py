"""ReviewWorker 角色模型解析契约测试。"""

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from backend.workers import review_worker
from backend.workers.review_worker import ReviewWorker


@pytest.mark.asyncio
async def test_resolve_role_model_uses_reviewer_api_client_context():
    resolver = AsyncMock(return_value=("provider-main-model", 128000))
    worker = ReviewWorker.__new__(ReviewWorker)
    worker.ai_reviewer = SimpleNamespace(
        api_client=SimpleNamespace(resolve_role_model_context=resolver)
    )

    assert await worker._resolve_role_model("main") == "provider-main-model"
    resolver.assert_awaited_once_with("main")


@pytest.mark.asyncio
async def test_resolve_role_model_is_empty_safe_when_role_context_unavailable():
    resolver = AsyncMock(side_effect=RuntimeError("role binding unavailable"))
    worker = ReviewWorker.__new__(ReviewWorker)
    worker.ai_reviewer = SimpleNamespace(
        api_client=SimpleNamespace(resolve_role_model_context=resolver)
    )

    assert await worker._resolve_role_model("summary") is None


def test_review_worker_does_not_read_legacy_model_or_endpoint_settings():
    source = Path(review_worker.__file__).read_text(encoding="utf-8")

    for legacy_access in (
        "settings.openai_model",
        "settings.summary_model",
        "settings.openai_api_base",
        "settings.openai_api_key",
        "settings.summary_api_base",
        "settings.summary_api_key",
    ):
        assert legacy_access not in source


@pytest.mark.asyncio
async def test_resolve_role_model_is_empty_safe_without_reviewer_client():
    worker = ReviewWorker.__new__(ReviewWorker)
    worker.ai_reviewer = SimpleNamespace()

    assert await worker._resolve_role_model("main") is None
