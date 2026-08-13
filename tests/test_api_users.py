"""API 用户端点辅助逻辑测试。"""

from datetime import UTC, datetime

from backend.api.v1.users import _serialize_quota_usage_log
from backend.models.telegram_models import QuotaUsageLog


def test_serialize_quota_usage_log_uses_model_fields():
    created_at = datetime(2026, 5, 22, 12, 30, 45, tzinfo=UTC)
    log = QuotaUsageLog(
        id=7,
        telegram_user_id=2,
        repo_name="owner/repo",
        pr_number=123,
        usage_type="daily",
        usage_category="pr_review",
        created_at=created_at,
    )

    data = _serialize_quota_usage_log(log)

    assert data == {
        "id": 7,
        "quota_type": "daily",
        "usage_type": "daily",
        "usage_category": "pr_review",
        "repo_name": "owner/repo",
        "pr_number": 123,
        "used_count": 1,
        "created_at": "2026-05-22T12:30:45.000000Z",
    }
