"""SetupService.test_database_connection 的单元测试。

锁定初始配置向导"测试连接"对连接串的处理：
- 接受 mysql+asyncmy / mysql+aiomysql / mysql / postgresql+asyncpg / postgresql
- 拒绝不支持的格式
- aiomysql URL 会被规范化为 asyncmy 后再传给引擎（回归保护：项目驱动已
  迁移到 asyncmy，原样传 aiomysql 会触发 ModuleNotFoundError）
- 连接失败时错误消息不得泄露连接串
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from backend.core.setup_service import SetupService


def _fake_engine_ok():
    """构造一个连接成功的假引擎，避免真实数据库连接。"""
    conn = MagicMock()
    conn.execute = AsyncMock(return_value=None)
    connect_cm = MagicMock()
    connect_cm.__aenter__ = AsyncMock(return_value=conn)
    connect_cm.__aexit__ = AsyncMock(return_value=None)
    engine = MagicMock()
    engine.connect = MagicMock(return_value=connect_cm)
    engine.dispose = AsyncMock(return_value=None)
    return engine


@pytest.mark.parametrize(
    "url",
    [
        "mysql+asyncmy://u:p@localhost:3306/sakura_ai",
        "mysql+aiomysql://u:p@localhost:3306/sakura_ai",
        "mysql://u:p@localhost:3306/sakura_ai",
        "postgresql+asyncpg://u:p@localhost:5432/sakura_ai",
        "postgresql://u:p@localhost:5432/sakura_ai",
    ],
)
@pytest.mark.asyncio
async def test_accepts_supported_database_urls(url):
    with patch(
        "backend.core.setup_service.create_async_engine",
        return_value=_fake_engine_ok(),
    ):
        result = await SetupService().test_database_connection(url)
    assert result["success"] is True, result["message"]


@pytest.mark.asyncio
async def test_rejects_unsupported_database_url():
    result = await SetupService().test_database_connection("sqlite://x")
    assert result["success"] is False
    assert "必须以" in result["message"]


@pytest.mark.asyncio
async def test_aiomysql_url_is_normalized_before_engine():
    """回归保护：aiomysql URL 必须规范化为 asyncmy 再传给 create_async_engine。

    这是本次修复的核心：项目驱动已从 aiomysql 迁移到 asyncmy，若把 aiomysql
    URL 原样传给 create_async_engine，SQLAlchemy 会因 aiomysql 未安装而报
    ModuleNotFoundError（初始配置向导"测试连接"曾出现的 bug）。
    """
    captured = {}

    def fake_create_async_engine(url, **kwargs):
        captured["url"] = url
        return _fake_engine_ok()

    with patch(
        "backend.core.setup_service.create_async_engine",
        side_effect=fake_create_async_engine,
    ):
        result = await SetupService().test_database_connection(
            "mysql+aiomysql://u:p@localhost:3306/sakura_ai"
        )
    assert result["success"] is True
    assert captured["url"].startswith("mysql+asyncmy://")
    assert "aiomysql" not in captured["url"]


@pytest.mark.asyncio
async def test_error_message_redacts_connection_string():
    """连接失败时，错误消息不得泄露原始连接串（含密码）。"""
    secret = "mysql+aiomysql://u:SECRET_PASS@localhost:3306/sakura_ai"
    with patch(
        "backend.core.setup_service.create_async_engine",
        side_effect=RuntimeError(f"boom {secret}"),
    ):
        result = await SetupService().test_database_connection(secret)
    assert result["success"] is False
    assert "SECRET_PASS" not in result["message"]
    assert "***" in result["message"]
