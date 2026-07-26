"""活动 cursor signing secret 自动生成与注册的测试。

覆盖三件事：
1. 配置键注册到各配置集合（CORE_CONFIG_KEYS / DB 加载集合 / system-config 页面分组 / 敏感脱敏 / 需重启 / Setup 环境变量映射）；
2. Setup Wizard ``complete_setup`` 自动生成该 secret；
3. ``ensure_activity_cursor_signing_secret`` 启动自愈：空则生成落库、非空则不动、并发幂等。
"""

from __future__ import annotations

import pytest
from unittest.mock import AsyncMock, patch

from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from backend.core.config import CORE_CONFIG_KEYS, get_all_db_config_keys, get_settings
from backend.models.database import AppConfig, Base


# ---------------------------------------------------------------------------
# 契约测试：配置键注册位置
# ---------------------------------------------------------------------------


def test_activity_cursor_signing_secret_in_core_config_keys():
    """注册到 CORE_CONFIG_KEYS，使 get_all_db_config_keys 能加载它。"""
    assert "activity_cursor_signing_secret" in CORE_CONFIG_KEYS


def test_activity_cursor_signing_secret_in_db_config_keys():
    assert "activity_cursor_signing_secret" in get_all_db_config_keys()


def test_activity_cursor_signing_secret_is_sensitive():
    from backend.services.system_config_service import SYSTEM_SENSITIVE_KEYS

    assert "activity_cursor_signing_secret" in SYSTEM_SENSITIVE_KEYS


def test_activity_cursor_signing_secret_requires_restart():
    from backend.services.system_config_service import RESTART_REQUIRED_KEYS

    assert "activity_cursor_signing_secret" in RESTART_REQUIRED_KEYS


def test_activity_cursor_signing_secret_in_application_group():
    from backend.services.system_config_service import SYSTEM_CONFIG_GROUPS

    app_group = next(g for g in SYSTEM_CONFIG_GROUPS if g["id"] == "application")
    assert "activity_cursor_signing_secret" in app_group["keys"]


def test_activity_cursor_signing_secret_env_mapping_in_setup_service():
    from backend.core.setup_service import _ENV_TO_SETTINGS_KEY

    assert (
        _ENV_TO_SETTINGS_KEY["ACTIVITY_CURSOR_SIGNING_SECRET"]
        == "activity_cursor_signing_secret"
    )


# ---------------------------------------------------------------------------
# complete_setup 自动生成
# ---------------------------------------------------------------------------

_BASE_COMPLETE_SETUP_CONFIG = {
    "DATABASE_URL": "mysql+asyncmy://u:p@host/db",
    "ADMIN_GITHUB_USERNAME": "admin",
    "ADMIN_TELEGRAM_ID": "123",
}


@pytest.mark.asyncio
async def test_complete_setup_autogenerates_activity_cursor_secret():
    """complete_setup 在未提供 secret 时自动生成 64 位十六进制串并落库。"""
    from backend.core.setup_service import SetupService

    service = SetupService()
    captured: dict = {}

    async def fake_save(inner_self, values):
        captured["values"] = dict(values)
        return 1

    with (
        patch.object(SetupService, "init_database", new_callable=AsyncMock),
        patch.object(
            SetupService, "save_configs_to_db", autospec=True, side_effect=fake_save
        ),
        patch.object(SetupService, "create_admin_user", new_callable=AsyncMock),
        patch("backend.core.setup_service.mark_setup_completed"),
    ):
        result = await service.complete_setup(dict(_BASE_COMPLETE_SETUP_CONFIG))

    assert result["success"] is True, result["message"]
    secret = captured["values"]["ACTIVITY_CURSOR_SIGNING_SECRET"]
    assert isinstance(secret, str)
    assert len(secret) == 64  # secrets.token_hex(32) → 64 chars
    int(secret, 16)  # 是合法十六进制


@pytest.mark.asyncio
async def test_complete_setup_respects_user_provided_activity_cursor_secret():
    """调用方显式提供 secret 时，setdefault 不覆盖。"""
    from backend.core.setup_service import SetupService

    service = SetupService()
    captured: dict = {}

    async def fake_save(inner_self, values):
        captured["values"] = dict(values)
        return 1

    config = dict(_BASE_COMPLETE_SETUP_CONFIG)
    config["ACTIVITY_CURSOR_SIGNING_SECRET"] = "user-provided-secret"

    with (
        patch.object(SetupService, "init_database", new_callable=AsyncMock),
        patch.object(
            SetupService, "save_configs_to_db", autospec=True, side_effect=fake_save
        ),
        patch.object(SetupService, "create_admin_user", new_callable=AsyncMock),
        patch("backend.core.setup_service.mark_setup_completed"),
    ):
        result = await service.complete_setup(config)

    assert result["success"] is True
    assert (
        captured["values"]["ACTIVITY_CURSOR_SIGNING_SECRET"] == "user-provided-secret"
    )


# ---------------------------------------------------------------------------
# ensure_activity_cursor_signing_secret 启动自愈
# ---------------------------------------------------------------------------


class _AsyncSessionShim:
    """把同步 SQLAlchemy Session 包装成 ``async with async_session()`` 形态。

    仿照 tests/test_activity_observability_outbox.py 中的 AsyncSqliteAdapter，
    仅实现自愈函数用到的方法子集，使测试在真实 sqlite 上验证 SQL 幂等行为。
    """

    def __init__(self, sync_session):
        self._sync = sync_session

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        if exc_type is not None:
            self._sync.rollback()
        return False

    def add(self, obj):
        self._sync.add(obj)

    async def execute(self, stmt):
        return self._sync.execute(stmt)

    async def commit(self):
        self._sync.commit()

    async def rollback(self):
        self._sync.rollback()


@pytest.fixture
def sqlite_session_factory():
    """构建一个仅含 AppConfig 表的内存 sqlite async session 工厂。"""
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine, tables=[AppConfig.__table__])
    sync_factory = sessionmaker(bind=engine, expire_on_commit=False)

    def _factory():
        return _AsyncSessionShim(sync_factory())

    yield _factory
    engine.dispose()


@pytest.mark.asyncio
async def test_self_heal_generates_when_empty(sqlite_session_factory, monkeypatch):
    """Settings 为空、DB 无 key → 生成 64 位十六进制密钥，落库并回填 Settings。"""
    from backend.core.setup_service import ensure_activity_cursor_signing_secret

    settings = get_settings()
    monkeypatch.setattr(settings, "activity_cursor_signing_secret", "")

    with patch("backend.models.database.async_session", sqlite_session_factory):
        result = await ensure_activity_cursor_signing_secret()

    assert result == settings.activity_cursor_signing_secret
    assert isinstance(result, str)
    assert len(result) == 64
    int(result, 16)  # 合法十六进制

    async with sqlite_session_factory() as session:
        row = (
            await session.execute(
                select(AppConfig).where(
                    AppConfig.key_name == "activity_cursor_signing_secret"
                )
            )
        ).scalar_one()
    assert row.key_value == result


@pytest.mark.asyncio
async def test_self_heal_skips_when_nonempty(sqlite_session_factory, monkeypatch):
    """Settings 已有值 → 直接返回，不碰 DB。"""
    from backend.core.setup_service import ensure_activity_cursor_signing_secret

    settings = get_settings()
    monkeypatch.setattr(settings, "activity_cursor_signing_secret", "preset-value")

    with patch("backend.models.database.async_session", sqlite_session_factory):
        result = await ensure_activity_cursor_signing_secret()

    assert result == "preset-value"
    async with sqlite_session_factory() as session:
        rows = (
            (
                await session.execute(
                    select(AppConfig).where(
                        AppConfig.key_name == "activity_cursor_signing_secret"
                    )
                )
            )
            .scalars()
            .all()
        )
    assert rows == []


@pytest.mark.asyncio
async def test_self_heal_uses_existing_db_value_without_overwriting(
    sqlite_session_factory, monkeypatch
):
    """Settings 为空但 DB 已有值（并发对手先写）→ 用 DB 值回填，不生成新值覆盖。"""
    from backend.core.setup_service import ensure_activity_cursor_signing_secret

    async with sqlite_session_factory() as session:
        session.add(
            AppConfig(
                key_name="activity_cursor_signing_secret", key_value="db-existing"
            )
        )
        await session.commit()

    settings = get_settings()
    monkeypatch.setattr(settings, "activity_cursor_signing_secret", "")

    with patch("backend.models.database.async_session", sqlite_session_factory):
        result = await ensure_activity_cursor_signing_secret()

    assert result == "db-existing"
    async with sqlite_session_factory() as session:
        row = (
            await session.execute(
                select(AppConfig).where(
                    AppConfig.key_name == "activity_cursor_signing_secret"
                )
            )
        ).scalar_one()
    assert row.key_value == "db-existing"
