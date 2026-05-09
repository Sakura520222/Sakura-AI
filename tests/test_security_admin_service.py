"""Security management service tests."""

import asyncio

from backend.models.telegram_models import TelegramUser, UserWebAuthnCredential
from backend.services.security_admin_service import (
    GLOBAL_MFA_REQUIRED_CONFIG_KEY,
    reset_user_totp,
    set_global_mfa_required,
    set_user_mfa_required,
)
from backend.services.security_audit_service import sanitize_detail


class DummySession:
    def __init__(self):
        self.statements = []
        self.added = []

    async def execute(self, statement):
        self.statements.append(statement)

        class Result:
            @staticmethod
            def scalar_one_or_none():
                return None

        return Result()

    def add(self, item):
        self.added.append(item)


def test_sanitize_detail_removes_sensitive_values():
    detail = sanitize_detail(
        {
            "code": "123456",
            "totp_secret": "SECRET",
            "credential": {"raw": "secret"},
            "deleted_count": 2,
            "device_name": "Laptop",
        }
    )

    assert detail is not None
    assert "123456" not in detail
    assert "SECRET" not in detail
    assert "credential" not in detail
    assert "deleted_count" in detail
    assert "Laptop" in detail


def test_reset_user_totp_disables_totp_and_removes_recovery_codes():
    session = DummySession()
    user = TelegramUser(id=1, telegram_id=1001, github_username="alice")
    user.totp_enabled = True
    user.totp_secret_encrypted = "encrypted"
    user.totp_enabled_at = object()
    user.totp_last_used_step = 123

    asyncio.run(reset_user_totp(session, user))

    assert user.totp_enabled is False
    assert user.totp_secret_encrypted is None
    assert user.totp_enabled_at is None
    assert user.totp_last_used_step is None
    assert len(session.statements) == 1


def test_set_user_mfa_required_updates_policy_flag():
    session = DummySession()
    user = TelegramUser(id=1, telegram_id=1001, github_username="alice")
    user.mfa_required = False

    asyncio.run(set_user_mfa_required(session, user, True))

    assert user.mfa_required is True


def test_set_global_mfa_required_creates_app_config():
    session = DummySession()

    asyncio.run(set_global_mfa_required(session, True))

    assert len(session.added) == 1
    assert session.added[0].key_name == GLOBAL_MFA_REQUIRED_CONFIG_KEY
    assert session.added[0].key_value == "true"


def test_webauthn_credential_has_hash_column_for_unique_index():
    columns = UserWebAuthnCredential.__table__.columns

    assert "credential_id_hash" in columns
    assert columns["credential_id_hash"].unique is True


def test_telegram_user_has_mfa_required_column():
    columns = TelegramUser.__table__.columns

    assert "mfa_required" in columns
    assert columns["mfa_required"].nullable is False
