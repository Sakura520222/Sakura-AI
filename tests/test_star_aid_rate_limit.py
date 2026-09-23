"""Rate limit and secondary limit tests for Star Aid."""

from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from backend.core.time_service import format_rfc3339
from backend.services import star_aid_github_service as gh
from backend.workers import star_aid_worker
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
async def test_cooldown_only_extends_shared_deadline(monkeypatch):
    """A shorter concurrent rate limit cannot replace the Redis value or TTL."""
    state = {"value": None, "ttl": None}

    class FakeRedis:
        async def eval(self, script, key_count, key, value, ttl):
            assert key_count == 1
            assert key == star_aid_worker._REDIS_COOLDOWN_KEY
            assert "GET" in script and "SET" in script
            if state["value"] is None or state["value"] < value:
                state.update(value=value, ttl=ttl)
            return state["value"]

    monkeypatch.setattr(star_aid_worker, "_in_process_cooldown_until", None)
    monkeypatch.setattr(
        "backend.core.redis.get_async_redis", AsyncMock(return_value=FakeRedis())
    )
    now = datetime.now(UTC)
    long_until = now + timedelta(seconds=180)
    short_until = now + timedelta(seconds=60)
    await StarAidWorker.set_cooldown_until(long_until)
    original_ttl = state["ttl"]

    await StarAidWorker.set_cooldown_until(short_until)

    assert state == {"value": format_rfc3339(long_until), "ttl": original_ttl}
    assert star_aid_worker._in_process_cooldown_until == long_until
    assert await StarAidWorker.get_cooldown_until() == long_until
    state["value"] = format_rfc3339(short_until)
    assert await StarAidWorker.get_cooldown_until() == long_until

    later_until = now + timedelta(seconds=300)
    await StarAidWorker.set_cooldown_until(later_until)
    assert state["value"] == format_rfc3339(later_until)
    assert state["ttl"] >= original_ttl


@pytest.mark.asyncio
async def test_worker_aborts_batch_on_rate_limit(monkeypatch):
    worker = StarAidWorker()

    # Keep the unit test isolated from the production Redis cooldown key.
    monkeypatch.setattr(worker, "get_cooldown_until", AsyncMock(return_value=None))
    monkeypatch.setattr(worker, "set_cooldown_until", AsyncMock())

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
