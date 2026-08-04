"""WebUI 用户偏好缓存测试。"""

from types import SimpleNamespace

import pytest

from backend.webui import deps
from backend.webui.deps import _USER_PREFS_CACHE
from tests.stubs import DbStub, RequestStub


@pytest.fixture(autouse=True)
def _clear_prefs_cache():
    """确保每个测试前后全局缓存干净。"""
    _USER_PREFS_CACHE.clear()
    yield
    _USER_PREFS_CACHE.clear()


@pytest.mark.asyncio
async def test_user_preferences_cache_key_uses_integer_user_id(monkeypatch):
    monkeypatch.setattr(
        deps,
        "decode_access_token",
        lambda token: {"user_id": "42", "token_type": "access"},
    )
    monkeypatch.setattr(
        deps,
        "is_access_token_payload",
        lambda payload: True,
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
