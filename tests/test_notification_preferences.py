"""Tests for Telegram notification preferences and is_event_enabled logic."""

import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from backend.telegram.notifications import NotificationSender


# ========== is_event_enabled tests ==========


class TestIsEventEnabled:
    """Boundary matrix for NotificationSender.is_event_enabled."""

    def test_none_preferences_returns_true(self):
        """NULL/未设置偏好 → 默认启用"""
        assert NotificationSender.is_event_enabled(None, "review_start") is True

    def test_empty_string_returns_true(self):
        """空字符串偏好 → 默认启用"""
        assert NotificationSender.is_event_enabled("", "review_start") is True

    def test_illegal_json_returns_true(self):
        """非法 JSON → 默认启用（容错）"""
        assert NotificationSender.is_event_enabled("not-json{{", "review_start") is True

    def test_json_array_returns_true(self):
        """JSON 数组（非 dict）→ 默认启用"""
        assert NotificationSender.is_event_enabled("[1, 2, 3]", "review_start") is True

    def test_json_true_returns_true(self):
        """JSON true（非 dict）→ 默认启用"""
        assert NotificationSender.is_event_enabled("true", "review_start") is True

    def test_json_number_returns_true(self):
        """JSON 数字（非 dict）→ 默认启用"""
        assert NotificationSender.is_event_enabled("42", "review_start") is True

    def test_normal_dict_event_enabled(self):
        """正常 dict 且事件类型显式启用 → True"""
        prefs = json.dumps({"review_start": True, "review_complete": False})
        assert NotificationSender.is_event_enabled(prefs, "review_start") is True

    def test_normal_dict_event_disabled(self):
        """正常 dict 且事件类型显式禁用 → False"""
        prefs = json.dumps({"review_start": True, "review_complete": False})
        assert NotificationSender.is_event_enabled(prefs, "review_complete") is False

    def test_normal_dict_event_not_set_defaults_true(self):
        """正常 dict 中未包含的事件类型 → 默认 True"""
        prefs = json.dumps({"review_start": False})
        assert NotificationSender.is_event_enabled(prefs, "agent_task_started") is True

    def test_empty_dict_all_enabled(self):
        """空 dict → 所有事件默认启用"""
        prefs = json.dumps({})
        assert NotificationSender.is_event_enabled(prefs, "review_start") is True
        assert NotificationSender.is_event_enabled(prefs, "agent_task_failed") is True

    def test_all_disabled(self):
        """所有事件禁用 → 返回 False"""
        prefs = json.dumps({et: False for et in NotificationSender.EVENT_TYPES})
        for et in NotificationSender.EVENT_TYPES:
            assert NotificationSender.is_event_enabled(prefs, et) is False

    def test_all_enabled(self):
        """所有事件启用 → 返回 True"""
        prefs = json.dumps({et: True for et in NotificationSender.EVENT_TYPES})
        for et in NotificationSender.EVENT_TYPES:
            assert NotificationSender.is_event_enabled(prefs, et) is True

    def test_unknown_event_type_with_empty_prefs(self):
        """未知事件类型 + 空 dict → 默认 True"""
        prefs = json.dumps({})
        assert NotificationSender.is_event_enabled(prefs, "nonexistent_event") is True

    def test_unknown_event_type_disabled(self):
        """未知事件类型被显式禁用 → False"""
        prefs = json.dumps({"nonexistent_event": False})
        assert NotificationSender.is_event_enabled(prefs, "nonexistent_event") is False


# ========== get_notification_preferences tests ==========


@pytest.mark.anyio
async def test_get_notification_preferences_user_not_found():
    """用户不存在时返回全 True 默认偏好"""
    from backend.services.telegram_service import TelegramService

    session = AsyncMock()
    service = TelegramService(session)

    with patch.object(service, "get_user_by_telegram_id", return_value=None):
        result = await service.get_notification_preferences(12345)

    assert result == {et: True for et in NotificationSender.EVENT_TYPES}


@pytest.mark.anyio
async def test_get_notification_preferences_null_column():
    """用户 notification_preferences 列为 NULL → 返回全 True"""
    from backend.services.telegram_service import TelegramService

    session = AsyncMock()
    service = TelegramService(session)

    user = MagicMock()
    user.notification_preferences = None

    with patch.object(service, "get_user_by_telegram_id", return_value=user):
        result = await service.get_notification_preferences(12345)

    assert result == {et: True for et in NotificationSender.EVENT_TYPES}


@pytest.mark.anyio
async def test_get_notification_preferences_valid_json():
    """用户有有效偏好 JSON → 合并默认值"""
    from backend.services.telegram_service import TelegramService

    session = AsyncMock()
    service = TelegramService(session)

    user = MagicMock()
    user.notification_preferences = json.dumps(
        {"review_start": False, "agent_task_started": True}
    )

    with patch.object(service, "get_user_by_telegram_id", return_value=user):
        result = await service.get_notification_preferences(12345)

    assert result["review_start"] is False
    assert result["agent_task_started"] is True
    # 未设置的事件类型默认为 True
    assert result["review_complete"] is True
    assert result["agent_task_failed"] is True


@pytest.mark.anyio
async def test_get_notification_preferences_corrupt_json():
    """损坏的 JSON → 返回全 True"""
    from backend.services.telegram_service import TelegramService

    session = AsyncMock()
    service = TelegramService(session)

    user = MagicMock()
    user.notification_preferences = "corrupt-json"

    with patch.object(service, "get_user_by_telegram_id", return_value=user):
        result = await service.get_notification_preferences(12345)

    assert result == {et: True for et in NotificationSender.EVENT_TYPES}


# ========== set_notification_preference tests ==========


@pytest.mark.anyio
async def test_set_notification_preference_invalid_event_type():
    """无效事件类型 → 返回错误"""
    from backend.services.telegram_service import TelegramService

    session = AsyncMock()
    service = TelegramService(session)

    success, msg = await service.set_notification_preference(
        12345, "invalid_event_type", True
    )

    assert success is False
    assert "无效的事件类型" in msg


@pytest.mark.anyio
async def test_set_notification_preference_user_not_found():
    """用户不存在 → 返回错误"""
    from backend.services.telegram_service import TelegramService

    session = AsyncMock()
    service = TelegramService(session)

    with patch.object(service, "get_user_by_telegram_id", return_value=None):
        success, msg = await service.set_notification_preference(
            12345, "review_start", False
        )

    assert success is False
    assert "用户未注册" in msg


@pytest.mark.anyio
async def test_set_notification_preference_disable_event():
    """禁用事件类型 → 保存到数据库"""
    from backend.services.telegram_service import TelegramService

    session = AsyncMock()
    service = TelegramService(session)

    user = MagicMock()
    user.notification_preferences = None

    with patch.object(service, "get_user_by_telegram_id", return_value=user):
        success, msg = await service.set_notification_preference(
            12345, "agent_task_started", False
        )

    assert success is True
    assert "禁用" in msg
    # 验证写入了正确的 JSON
    saved = json.loads(user.notification_preferences)
    assert saved["agent_task_started"] is False
    session.commit.assert_awaited_once()


@pytest.mark.anyio
async def test_set_notification_preference_enable_event():
    """启用事件类型 → 保存到数据库"""
    from backend.services.telegram_service import TelegramService

    session = AsyncMock()
    service = TelegramService(session)

    user = MagicMock()
    user.notification_preferences = json.dumps({"agent_task_started": False})

    with patch.object(service, "get_user_by_telegram_id", return_value=user):
        success, msg = await service.set_notification_preference(
            12345, "agent_task_started", True
        )

    assert success is True
    assert "启用" in msg
    saved = json.loads(user.notification_preferences)
    assert saved["agent_task_started"] is True


@pytest.mark.anyio
async def test_set_notification_preference_preserves_existing():
    """修改单个事件 → 不影响其他已保存的偏好"""
    from backend.services.telegram_service import TelegramService

    session = AsyncMock()
    service = TelegramService(session)

    user = MagicMock()
    user.notification_preferences = json.dumps(
        {"review_start": False, "review_complete": True}
    )

    with patch.object(service, "get_user_by_telegram_id", return_value=user):
        await service.set_notification_preference(12345, "agent_task_failed", False)

    saved = json.loads(user.notification_preferences)
    assert saved["review_start"] is False  # preserved
    assert saved["review_complete"] is True  # preserved
    assert saved["agent_task_failed"] is False  # new


# ========== get_notification_targets_with_preference tests ==========


@pytest.mark.anyio
async def test_get_notification_targets_with_preference_filters_disabled():
    """禁用事件的用户应被过滤"""
    from backend.services.telegram_service import TelegramService

    session = AsyncMock()
    service = TelegramService(session)

    # Mock get_notification_targets to return two users
    with patch.object(
        service, "get_notification_targets", new_callable=AsyncMock
    ) as mock_targets:
        mock_targets.return_value = [111, 222]

        # Mock session.execute for preference query
        mock_result = MagicMock()
        # user 111 has event enabled, user 222 has it disabled
        mock_result.all.return_value = [
            (111, json.dumps({"agent_task_started": True})),
            (222, json.dumps({"agent_task_started": False})),
        ]
        session.execute = AsyncMock(return_value=mock_result)

        result = await service.get_notification_targets_with_preference(
            "owner/repo", "author", "agent_task_started"
        )

    assert 111 in result
    assert 222 not in result


@pytest.mark.anyio
async def test_get_notification_targets_with_preference_all_enabled():
    """所有用户都启用事件 → 全部返回"""
    from backend.services.telegram_service import TelegramService

    session = AsyncMock()
    service = TelegramService(session)

    with patch.object(
        service, "get_notification_targets", new_callable=AsyncMock
    ) as mock_targets:
        mock_targets.return_value = [111, 222]

        mock_result = MagicMock()
        mock_result.all.return_value = [
            (111, None),  # NULL → default enabled
            (222, json.dumps({"agent_task_started": True})),
        ]
        session.execute = AsyncMock(return_value=mock_result)

        result = await service.get_notification_targets_with_preference(
            "owner/repo", "author", "agent_task_started"
        )

    assert set(result) == {111, 222}


@pytest.mark.anyio
async def test_get_notification_targets_with_preference_empty_targets():
    """无通知目标 → 返回空列表"""
    from backend.services.telegram_service import TelegramService

    session = AsyncMock()
    service = TelegramService(session)

    with patch.object(
        service, "get_notification_targets", new_callable=AsyncMock
    ) as mock_targets:
        mock_targets.return_value = []

        result = await service.get_notification_targets_with_preference(
            "owner/repo", "", "agent_task_started"
        )

    assert result == []
