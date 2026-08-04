"""用户自注册开关行为测试。"""

import pytest

from backend.core.config import get_settings
from backend.models.telegram_models import UserRole
from backend.services.telegram_service import TelegramService


class _NoDatabaseSession:
    async def execute(self, _statement):
        raise AssertionError("自注册关闭时不应访问数据库")


class _RecordingSession:
    def __init__(self):
        self.added = []
        self.committed = False

    def add(self, value):
        self.added.append(value)

    async def commit(self):
        self.committed = True


class _AdminTelegramService(TelegramService):
    async def get_user_by_telegram_id(self, _telegram_id):
        return None

    async def is_super_admin(self, _telegram_id):
        return False


@pytest.mark.asyncio
async def test_register_user_is_rejected_when_self_registration_disabled():
    settings = get_settings()
    old_value = settings.allow_user_registration
    settings.allow_user_registration = False
    try:
        service = TelegramService(_NoDatabaseSession())

        success, message = await service.register_user(12345, "octocat")

        assert success is False
        assert message == "用户自注册已关闭，请联系管理员创建账号"
    finally:
        settings.allow_user_registration = old_value


@pytest.mark.asyncio
async def test_admin_can_add_user_when_self_registration_disabled():
    settings = get_settings()
    old_value = settings.allow_user_registration
    settings.allow_user_registration = False
    try:
        session = _RecordingSession()
        service = _AdminTelegramService(session)

        success, message = await service.add_user(
            12345,
            "octocat",
            role=UserRole.USER,
        )

        assert success is True
        assert message == "用户添加成功"
        assert session.committed is True
        assert len(session.added) == 1
        assert session.added[0].github_username == "octocat"
    finally:
        settings.allow_user_registration = old_value
