"""SetupService.test_database_connection 的单元测试。

锁定初始配置向导"测试连接"对连接串的处理：
- 接受 mysql+asyncmy / mysql+aiomysql / mysql / postgresql+asyncpg / postgresql
- 拒绝不支持的格式
- aiomysql URL 会被规范化为 asyncmy 后再传给引擎（回归保护：项目驱动已
  迁移到 asyncmy，原样传 aiomysql 会触发 ModuleNotFoundError）
- 连接失败时错误消息不得泄露连接串
"""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlalchemy import create_engine, inspect, text

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
async def test_setup_init_database_runs_schema_migration_after_create_tables():
    service = SetupService()
    with patch("backend.models.database.async_engine", object()), patch(
        "backend.models.database.init_async_db"
    ) as init_async_db, patch(
        "backend.models.database.create_tables_async", new_callable=AsyncMock
    ) as create_tables, patch(
        "backend.models.database.migrate_schema_async", new_callable=AsyncMock
    ) as migrate, patch(
        "backend.models.database.insert_default_configs_async", new_callable=AsyncMock
    ):
        await service.init_database("mysql+asyncmy://u:p@host/db")
    init_async_db.assert_not_called()
    create_tables.assert_not_called()
    migrate.assert_awaited_once()


@pytest.mark.asyncio
async def test_setup_create_admin_user_runs_migration_when_engine_exists():
    service = SetupService()
    fake_session = MagicMock()
    fake_session.__aenter__ = AsyncMock(return_value=fake_session)
    fake_session.__aexit__ = AsyncMock(return_value=None)
    result = SimpleNamespace(
        scalar=lambda: 0,
        scalar_one_or_none=lambda: None,
        scalars=lambda: SimpleNamespace(first=lambda: None),
    )
    fake_session.execute = AsyncMock(return_value=result)
    fake_session.commit = AsyncMock()
    with patch("backend.models.database.async_engine", object()), patch(
        "backend.models.database.async_session", MagicMock(return_value=fake_session)
    ), patch(
        "backend.models.database.insert_default_configs_async", new_callable=AsyncMock
    ), patch(
        "backend.models.database.migrate_schema_async", new_callable=AsyncMock
    ) as migrate:
        await service.create_admin_user("admin", 1, "mysql+asyncmy://u:p@host/db")
    migrate.assert_awaited_once()




@pytest.mark.asyncio
async def test_sqlite_auto_migration_skips_mysql_longtext_alter():
    from backend.models.database import _ensure_agent_message_longtext_columns

    conn = MagicMock()
    conn.dialect.name = "sqlite"
    logger = MagicMock()
    await _ensure_agent_message_longtext_columns(conn, logger)
    conn.run_sync.assert_not_called()
    conn.execute.assert_not_called()


def test_mysql_marker_and_agent_migration_sql_remain_mysql_specific():
    from backend.models.database import _activity_publication_marker_upgrade_sql

    sql = _activity_publication_marker_upgrade_sql("mysql", 64)
    assert sql is not None
    assert "MODIFY COLUMN `marker` VARCHAR(128)" in sql
    assert _activity_publication_marker_upgrade_sql("sqlite", 64) is None


def test_legacy_activity_cleanup_drops_only_retired_tables():
    from backend.models.database import _drop_legacy_activity_tables

    engine = create_engine("sqlite://")
    with engine.begin() as conn:
        for table_name in (
            "activity_sessions",
            "activity_messages",
            "activity_tool_calls",
            "activity_events",
            "activity_observability_sessions",
            "unrelated_table",
        ):
            conn.execute(text(f'CREATE TABLE "{table_name}" (id INTEGER PRIMARY KEY)'))

        assert _drop_legacy_activity_tables(conn) == (
            "activity_tool_calls",
            "activity_messages",
            "activity_events",
            "activity_sessions",
        )
        assert _drop_legacy_activity_tables(conn) == ()

    remaining = set(inspect(engine).get_table_names())
    assert "activity_observability_sessions" in remaining
    assert "unrelated_table" in remaining
    assert not remaining.intersection(
        {
            "activity_sessions",
            "activity_messages",
            "activity_tool_calls",
            "activity_events",
        }
    )


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
