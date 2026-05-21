"""语言切换端到端流程测试。"""

from types import SimpleNamespace
from unittest.mock import patch, MagicMock

import pytest

from backend.webui import deps
from backend.webui.deps import (
    get_user_preferences,
    invalidate_user_prefs_cache,
    _USER_PREFS_CACHE,
)
from backend.webui.i18n import detect_language, make_translation_func
from tests.stubs import RequestStub, DbStub


@pytest.fixture(autouse=True)
def _clear_prefs_cache():
    """确保每个测试前后全局缓存干净。"""
    _USER_PREFS_CACHE.clear()
    yield
    _USER_PREFS_CACHE.clear()


# ========== 测试用例 ==========


@pytest.mark.asyncio
async def test_language_switch_full_flow():
    """模拟完整的语言切换流程：读取 → 保存（缓存失效） → 再读取。"""

    # 1. 初始状态：用户语言为 zh-CN
    initial_config = SimpleNamespace(language="zh-CN", items_per_page=20)
    db = DbStub(initial_config)

    with (
        patch.object(
            deps,
            "decode_access_token",
            return_value={"user_id": 42, "token_type": "access"},
        ),
        patch.object(deps, "is_access_token_payload", return_value=True),
    ):
        prefs_before = await get_user_preferences(RequestStub(), db)

    assert prefs_before == {"language": "zh-CN", "items_per_page": 20}
    assert db.execute_count == 1
    # 缓存已填充
    assert 42 in _USER_PREFS_CACHE

    # 2. 模拟 save_settings：更新 DB 配置并失效缓存
    initial_config.language = "en"
    initial_config.items_per_page = 50
    invalidate_user_prefs_cache(42)
    assert 42 not in _USER_PREFS_CACHE, "缓存应已失效"

    # 3. 再次读取偏好：应从 DB 获取新值 en
    db2 = DbStub(initial_config)
    with (
        patch.object(
            deps,
            "decode_access_token",
            return_value={"user_id": 42, "token_type": "access"},
        ),
        patch.object(deps, "is_access_token_payload", return_value=True),
    ):
        prefs_after = await get_user_preferences(RequestStub(), db2)

    assert prefs_after == {"language": "en", "items_per_page": 50}
    assert db2.execute_count == 1, "缓存失效后应重新查询 DB"

    # 4. detect_language 应返回 en
    lang = detect_language(prefs_after)
    assert lang == "en", f"预期 en，实际 {lang}"

    # 翻译函数应绑定 en
    translate = make_translation_func(lang)
    assert translate("settings.title") == "Settings"


@pytest.mark.asyncio
async def test_cache_hit_returns_old_value_after_save_without_invalidation():
    """验证：如果不调用 invalidate，缓存会返回旧值。"""

    config = SimpleNamespace(language="zh-CN", items_per_page=20)
    db = DbStub(config)

    with (
        patch.object(
            deps,
            "decode_access_token",
            return_value={"user_id": 99, "token_type": "access"},
        ),
        patch.object(deps, "is_access_token_payload", return_value=True),
    ):
        prefs1 = await get_user_preferences(RequestStub(), db)

    assert prefs1 == {"language": "zh-CN", "items_per_page": 20}

    # 模拟 DB 被更新但缓存未失效
    config.language = "en"

    with (
        patch.object(
            deps,
            "decode_access_token",
            return_value={"user_id": 99, "token_type": "access"},
        ),
        patch.object(deps, "is_access_token_payload", return_value=True),
    ):
        prefs2 = await get_user_preferences(RequestStub(), db)

    # 缓存命中，仍然返回旧值
    assert prefs2 == {"language": "zh-CN", "items_per_page": 20}
    assert db.execute_count == 1, "缓存命中，不应再查 DB"


@pytest.mark.asyncio
async def test_cache_invalidation_then_read_returns_new_value():
    """验证：失效缓存后再次读取返回新值。"""

    config = SimpleNamespace(language="zh-CN", items_per_page=20)
    db = DbStub(config)

    with (
        patch.object(
            deps,
            "decode_access_token",
            return_value={"user_id": 88, "token_type": "access"},
        ),
        patch.object(deps, "is_access_token_payload", return_value=True),
    ):
        await get_user_preferences(RequestStub(), db)

    # 更新并失效
    config.language = "en"
    invalidate_user_prefs_cache(88)

    with (
        patch.object(
            deps,
            "decode_access_token",
            return_value={"user_id": 88, "token_type": "access"},
        ),
        patch.object(deps, "is_access_token_payload", return_value=True),
    ):
        prefs = await get_user_preferences(RequestStub(), db)

    assert prefs["language"] == "en"


def test_detect_language_with_user_prefs():
    """detect_language 优先使用 user_prefs。"""
    assert detect_language({"language": "en"}) == "en"
    assert detect_language({"language": "zh-CN"}) == "zh-CN"
    assert detect_language({"language": "fr"}) == "zh-CN"  # 不在支持列表中，回退默认
    assert detect_language({"language": None}) == "zh-CN"  # None → 回退
    assert detect_language({}) == "zh-CN"  # 无 language 键
    assert detect_language(None) == "zh-CN"  # None → 回退


def test_make_translation_func_en():
    """en 翻译函数应返回英文文本。"""
    t = make_translation_func("en")
    assert t("settings.title") == "Settings"
    assert t("nav.dashboard") == "Dashboard"
    assert t("nonexistent.key") == "nonexistent.key"  # 回退到 key 本身


def test_render_template_injects_correct_lang():
    """render_template 应注入正确的 lang 和 _ 翻译函数。"""
    from backend.webui.deps import render_template, get_templates

    # 用 MagicMock 替换 TemplateResponse 以拦截调用
    captured = {}

    def mock_template_response(template_name, context):
        captured.update(context)
        return MagicMock()

    with patch.object(
        get_templates(), "TemplateResponse", side_effect=mock_template_response
    ):
        request = RequestStub()
        user_prefs = {"language": "en", "items_per_page": 20}
        render_template("test.html", request, user_prefs=user_prefs)

    assert captured["lang"] == "en"
    assert captured["user_prefs"] == {"language": "en", "items_per_page": 20}
    # 翻译函数应绑定 en
    assert captured["_"]("settings.title") == "Settings"
    assert "supported_languages" in captured


def test_render_template_with_user_prefs_in_context():
    """当 user_prefs 通过 **context 传入时，应正确绑定到 user_prefs 参数。"""
    from backend.webui.deps import render_template, get_templates

    captured = {}

    def mock_template_response(template_name, context):
        captured.update(context)
        return MagicMock()

    with patch.object(
        get_templates(), "TemplateResponse", side_effect=mock_template_response
    ):
        request = RequestStub()
        # 模拟 _render_settings_page 的调用方式：user_prefs 放在 **context 中
        context = {
            "user_prefs": {"language": "en", "items_per_page": 50},
            "language": "en",
            "items_per_page": 50,
        }
        render_template("settings.html", request, **context)

    assert captured["lang"] == "en"
    assert captured["user_prefs"] == {"language": "en", "items_per_page": 50}
    assert captured["language"] == "en"
    assert captured["items_per_page"] == 50
    assert captured["_"]("settings.title") == "Settings"
