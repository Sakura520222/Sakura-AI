"""Rate limit and secondary limit tests for Star Aid."""

from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from backend.services import star_aid_github_service as gh
from backend.workers.star_aid_worker import StarAidWorker


def test_403_with_remaining_zero_is_rate_limited():
    headers = httpx.Headers({
        "x-ratelimit-remaining": "0",
        "x-ratelimit-reset": "1800000000",
    })
    resp = httpx.Response(
        status_code=403,
        headers=headers,
        json={"message": "API rate limit exceeded"},
        request=httpx.Request("GET", "https://api.github.com/user"),
    )

    result = gh._result_from_response(resp)

    assert result.error_code == "rate_limited"
    assert result.rate_limit_kind == "primary"
    assert result.rate_limit_remaining == 0
    assert result.rate_limit_reset_at == datetime.fromtimestamp(1800000000, tz=UTC)


def test_429_is_rate_limited_with_retry_after():
    headers = httpx.Headers({
        "retry-after": "120",
    })
    resp = httpx.Response(
        status_code=429,
        headers=headers,
        json={"message": "Too many requests"},
        request=httpx.Request("GET", "https://api.github.com/user"),
    )

    result = gh._result_from_response(resp)

    assert result.error_code == "rate_limited"
    assert result.retry_after_seconds == 120
    assert result.rate_limit_reset_at is not None
    # 应大致为 now + 120s
    diff = (result.rate_limit_reset_at - datetime.now(UTC)).total_seconds()
    assert 115 <= diff <= 125


def test_403_secondary_rate_limit_message_is_detected():
    headers = httpx.Headers({
        "x-ratelimit-remaining": "50",
    })
    resp = httpx.Response(
        status_code=403,
        headers=headers,
        json={"message": "You have exceeded a secondary rate limit. Please wait a few minutes before you try again."},
        request=httpx.Request("POST", "https://api.github.com/user"),
    )

    result = gh._result_from_response(resp)

    assert result.error_code == "rate_limited"
    assert result.rate_limit_kind == "secondary"
    assert result.rate_limit_reset_at is not None


def test_403_ordinary_forbidden_is_not_rate_limited():
    headers = httpx.Headers({
        "x-ratelimit-remaining": "50",
    })
    resp = httpx.Response(
        status_code=403,
        headers=headers,
        json={"message": "Resource not accessible by integration"},
        request=httpx.Request("GET", "https://api.github.com/repos/foo/bar"),
    )

    result = gh._result_from_response(resp)

    assert result.error_code == "forbidden"
    assert result.rate_limit_kind is None


@pytest.mark.asyncio
async def test_worker_aborts_batch_on_rate_limit(monkeypatch):
    worker = StarAidWorker()

    # 清理冷却状态
    await worker.set_cooldown_until(datetime.now(UTC) - timedelta(seconds=10))

    # Mock 动态配置
    async def fake_cfg(k):
        if k == "star_aid_batch_size":
            return 5
        return None

    monkeypatch.setattr("backend.workers.star_aid_worker.get_dynamic_config", fake_cfg)
    monkeypatch.setattr("backend.services.star_aid_service.is_feature_enabled", AsyncMock(return_value=True))
    monkeypatch.setattr("backend.services.star_aid_service.is_auto_star_enabled", AsyncMock(return_value=True))

    processed_members = []

    async def fake_process_member(member_id):
        processed_members.append(member_id)
        return member_id == 1

    monkeypatch.setattr(worker, "_process_member", fake_process_member)

    # Mock 数据库会话返回 3 个待执行成员 [1, 2, 3]
    fake_session = AsyncMock()
    fake_result = MagicMock()
    fake_result.all.return_value = [(1,), (2,), (3,)]
    fake_session.execute.return_value = fake_result

    class FakeSessionContext:
        async def __aenter__(self):
            return fake_session

        async def __aexit__(self, *args):
            pass

    with patch("backend.workers.star_aid_worker.async_session", return_value=FakeSessionContext()):
        await worker.run_tick()

    # 成员 1 限流后，应短路中断，不再执行成员 2 和 3
    assert processed_members == [1]
