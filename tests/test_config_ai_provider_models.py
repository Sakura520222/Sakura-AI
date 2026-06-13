"""配置页"获取模型"凭证解析：脱敏/空值回退数据库真实值。

回归场景：配置页表单回显的 API Key 是脱敏占位值（含 ****），若直接用于请求
会触发 AI 服务返回 401 → "API Key 无效"。验证 _resolve_provider_credentials
在脱敏/空值时回退数据库真实值，真实值原样使用。
"""

from unittest.mock import AsyncMock, MagicMock

import pytest

from backend.api.v1.config import _is_masked_value, _resolve_provider_credentials


def _key_name_from_stmt(stmt) -> str | None:
    """从 select(...).where(AppConfig.key_name == X) 语句中提取配置项名 X。

    通过公开的 ``compile().params`` 读取 WHERE 条件绑定的字面量值，避免反射
    whereclause / clauses / right.value 等 SQLAlchemy 内部属性（其结构在版本
    升级时可能变化）。
    """
    for value in stmt.compile().params.values():
        if isinstance(value, str) and value:
            return value
    return None


def _make_db(values: dict[str, str]) -> AsyncMock:
    """构造 AsyncSession mock：execute(stmt) 按 key_name 返回对应真实值。"""

    db = AsyncMock()

    async def execute(stmt):
        result = MagicMock()
        result.scalar_one_or_none.return_value = values.get(_key_name_from_stmt(stmt))
        return result

    db.execute.side_effect = execute
    return db


class TestIsMaskedValue:
    def test_long_masked(self):
        assert _is_masked_value("sk-1****5678") is True

    def test_short_masked(self):
        assert _is_masked_value("****") is True

    def test_real_key_not_masked(self):
        assert _is_masked_value("sk-realkey-no-mask") is False

    def test_empty_not_masked(self):
        assert _is_masked_value("") is False


@pytest.mark.asyncio
async def test_masked_api_key_falls_back_to_db_real_key():
    db = _make_db(
        {"openai_api_key": "sk-realkey", "openai_api_base": "https://api.example.com"}
    )
    api_key, api_base = await _resolve_provider_credentials(
        "sk-1****5678", "", "openai_api_key", db
    )
    assert api_key == "sk-realkey"
    assert api_base == "https://api.example.com"


@pytest.mark.asyncio
async def test_empty_api_key_falls_back_to_db():
    db = _make_db({"openai_api_key": "sk-realkey"})
    api_key, _ = await _resolve_provider_credentials("", "", "openai_api_key", db)
    assert api_key == "sk-realkey"


@pytest.mark.asyncio
async def test_real_api_key_used_as_is_without_db():
    db = _make_db({"openai_api_key": "sk-realkey"})
    api_key, api_base = await _resolve_provider_credentials(
        "sk-user-input", "https://base.example.com", "openai_api_key", db
    )
    assert api_key == "sk-user-input"
    assert api_base == "https://base.example.com"
    db.execute.assert_not_called()


@pytest.mark.asyncio
async def test_summary_key_name_reads_summary_config():
    db = _make_db(
        {"summary_api_key": "sk-summary", "summary_api_base": "https://s.example.com"}
    )
    api_key, api_base = await _resolve_provider_credentials(
        "su****ary", "", "summary_api_key", db
    )
    assert api_key == "sk-summary"
    assert api_base == "https://s.example.com"


@pytest.mark.asyncio
async def test_unsafe_key_name_defaults_to_openai():
    db = _make_db({"openai_api_key": "sk-realkey"})
    api_key, _ = await _resolve_provider_credentials("", "", "database_url", db)
    assert api_key == "sk-realkey"


@pytest.mark.asyncio
async def test_default_key_name_when_none():
    db = _make_db({"openai_api_key": "sk-realkey"})
    api_key, _ = await _resolve_provider_credentials("", "", None, db)
    assert api_key == "sk-realkey"
