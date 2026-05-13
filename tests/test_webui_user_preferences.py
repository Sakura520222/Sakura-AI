"""WebUI 用户偏好缓存测试。"""

from types import SimpleNamespace

import pytest

from backend.webui import deps


class RequestStub:
    def __init__(self, token: str | None = "token"):
        self.cookies = {"webui_token": token} if token else {}


class ResultStub:
    def __init__(self, value):
        self.value = value

    def scalar_one_or_none(self):
        return self.value


class DbStub:
    def __init__(self, config):
        self.config = config
        self.execute_count = 0

    async def execute(self, statement):
        self.execute_count += 1
        return ResultStub(self.config)


@pytest.mark.asyncio
async def test_user_preferences_cache_key_uses_integer_user_id(monkeypatch):
    deps._USER_PREFS_CACHE.clear()
    monkeypatch.setattr(
        deps,
        "decode_access_token",
        lambda token: {"user_id": "42", "token_type": "access"},
    )
    db = DbStub(SimpleNamespace(language="en", items_per_page=50))

    prefs = await deps.get_user_preferences(RequestStub(), db)

    assert prefs == {"language": "en", "items_per_page": 50}
    assert list(deps._USER_PREFS_CACHE.keys()) == [42]
    assert "42" not in deps._USER_PREFS_CACHE
    assert db.execute_count == 1

    cached_prefs = await deps.get_user_preferences(RequestStub(), db)

    assert cached_prefs == prefs
    assert db.execute_count == 1

    deps.invalidate_user_prefs_cache(42)

    assert not deps._USER_PREFS_CACHE
