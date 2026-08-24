"""用户信息备份导出、校验和导入回归测试。"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from fastapi.routing import APIRoute

from backend.models.database import UserConfig, WebUIConfig
from backend.models.telegram_models import (
    TelegramUser,
    UserRecoveryCode,
    UserWebAuthnCredential,
)
from backend.services import user_backup_service as service
from backend.services.user_backup_service import (
    USER_BACKUP_FORMAT,
    UserBackupError,
    build_user_backup_document,
    parse_user_backup,
    restore_user_backup,
    serialize_user_backup,
)
from backend.services.webauthn_service import credential_id_hash
from backend.webui.deps import require_csrf, require_super_admin
from backend.webui.routes.config import router


def _user(*, user_id: int = 1, telegram_id: int = 1001) -> TelegramUser:
    return TelegramUser(
        id=user_id,
        telegram_id=telegram_id,
        github_username="alice",
        role="user",
        daily_quota=10,
        weekly_quota=50,
        monthly_quota=200,
        daily_used=1,
        weekly_used=2,
        monthly_used=3,
        issue_daily_quota=20,
        issue_weekly_quota=80,
        issue_monthly_quota=300,
        issue_daily_used=4,
        issue_weekly_used=5,
        issue_monthly_used=6,
        agent_daily_quota=1,
        agent_weekly_quota=2,
        agent_monthly_quota=5,
        agent_daily_used=0,
        agent_weekly_used=1,
        agent_monthly_used=2,
        is_active=True,
        mfa_required=True,
        totp_enabled=True,
        totp_secret_encrypted="encrypted",
        totp_enabled_at=datetime(2026, 8, 1, 0, 0, 0, tzinfo=UTC),
        totp_last_used_step=123,
        created_at=datetime(2026, 7, 1, 0, 0, 0, tzinfo=UTC),
        updated_at=datetime(2026, 8, 1, 0, 0, 0, tzinfo=UTC),
    )


class _Rows:
    def __init__(self, rows):
        self._rows = rows

    def scalars(self):
        return self

    def all(self):
        return self._rows


class _ExportSession:
    def __init__(self, rows_by_model):
        self.rows_by_model = rows_by_model

    async def execute(self, query):
        model = query.column_descriptions[0]["entity"]
        return _Rows(self.rows_by_model.get(model, []))


def _passkey(*, user_id: int = 1) -> UserWebAuthnCredential:
    return UserWebAuthnCredential(
        id=7,
        user_id=user_id,
        credential_id="credential-1",
        credential_id_hash=credential_id_hash("credential-1"),
        public_key="public-key",
        sign_count=4,
        transports="internal",
        device_name="Laptop",
        backed_up=True,
        created_at=datetime(2026, 7, 1, 0, 0, 0, tzinfo=UTC),
        last_used_at=datetime(2026, 8, 1, 0, 0, 0, tzinfo=UTC),
    )


@pytest.mark.asyncio
async def test_export_includes_personal_config_totp_recovery_codes_and_passkeys(
    monkeypatch,
):
    user = _user()
    recovery = UserRecoveryCode(
        id=3,
        user_id=user.id,
        code_hash="a" * 64,
        used_at=None,
        created_at=datetime(2026, 7, 1, 0, 0, 0, tzinfo=UTC),
    )
    monkeypatch.setattr(service, "decrypt_totp_secret", lambda value: "TOTP-SECRET")
    monkeypatch.setattr(
        service, "_recovery_code_hash_key_fingerprint", lambda: "b" * 64
    )
    session = _ExportSession(
        {
            TelegramUser: [user],
            UserConfig: [
                UserConfig(
                    user_id=user.id,
                    config_key="output_language",
                    config_value="en",
                    description="Output language",
                )
            ],
            WebUIConfig: [
                WebUIConfig(
                    user_id=user.id,
                    theme="dark",
                    language="en",
                    items_per_page=50,
                )
            ],
            UserRecoveryCode: [recovery],
            UserWebAuthnCredential: [_passkey(user_id=user.id)],
        }
    )

    document = await service.export_user_backup(session)
    parsed = parse_user_backup(serialize_user_backup(document))
    exported = parsed["users"][0]

    assert document["format"] == USER_BACKUP_FORMAT
    assert exported["personal_config"]["dynamic_overrides"][0]["value"] == "en"
    assert exported["personal_config"]["webui"]["theme"] == "dark"
    assert exported["two_factor"]["totp_secret"] == "TOTP-SECRET"
    assert exported["two_factor"]["recovery_codes"][0]["code_hash"] == "a" * 64
    assert exported["passkeys"][0]["credential_id_hash"] == credential_id_hash(
        "credential-1"
    )


def _minimal_document(**user_overrides):
    user = {
        "identity": {"telegram_id": 1001, "github_username": "alice"},
        "profile": {"role": "user", "is_active": True},
        "personal_config": {"dynamic_overrides": [], "webui": None},
        "two_factor": {
            "mfa_required": False,
            "totp_enabled": False,
            "totp_secret": None,
            "totp_enabled_at": None,
            "totp_last_used_step": None,
            "recovery_codes": [],
        },
        "passkeys": [],
    }
    user.update(user_overrides)
    return build_user_backup_document(
        [user], recovery_code_hash_key_fingerprint="c" * 64
    )


def test_parser_rejects_unsupported_personal_config_and_duplicate_identity():
    document = _minimal_document(
        personal_config={
            "dynamic_overrides": [
                {"key": "webui_secret_key", "value": "secret", "description": None}
            ],
            "webui": None,
        }
    )
    with pytest.raises(UserBackupError, match="不允许导入用户配置项"):
        parse_user_backup(serialize_user_backup(document))

    duplicate = _minimal_document()
    duplicate["users"].append(duplicate["users"][0])
    duplicate["user_count"] = 2
    with pytest.raises(UserBackupError, match="telegram_id.*重复"):
        parse_user_backup(serialize_user_backup(duplicate))


class _RestoreSession:
    def __init__(self, users):
        self.users = list(users)
        self.configs = []
        self.webuis = []
        self.recoveries = []
        self.passkeys = []
        self.added = []
        self.committed = False
        self.rolled_back = False
        self._next_id = max((user.id or 0 for user in self.users), default=0) + 1

    async def execute(self, query):
        model = query.column_descriptions[0]["entity"]
        rows = {
            TelegramUser: self.users,
            UserConfig: self.configs,
            WebUIConfig: self.webuis,
            UserRecoveryCode: self.recoveries,
            UserWebAuthnCredential: self.passkeys,
        }[model]
        return _Rows(rows)

    def add(self, row):
        self.added.append(row)

    async def flush(self):
        for row in self.added:
            if getattr(row, "id", None) is None:
                row.id = self._next_id
                self._next_id += 1
            rows = {
                TelegramUser: self.users,
                UserConfig: self.configs,
                WebUIConfig: self.webuis,
                UserRecoveryCode: self.recoveries,
                UserWebAuthnCredential: self.passkeys,
            }[type(row)]
            if row not in rows:
                rows.append(row)
        self.added.clear()

    async def delete(self, row):
        for rows in (
            self.users,
            self.configs,
            self.webuis,
            self.recoveries,
            self.passkeys,
        ):
            if row in rows:
                rows.remove(row)

    async def commit(self):
        self.committed = True

    async def rollback(self):
        self.rolled_back = True


@pytest.mark.asyncio
async def test_restore_matches_user_and_remaps_related_records(monkeypatch):
    target = _user()
    session = _RestoreSession([target])
    monkeypatch.setattr(service, "encrypt_totp_secret", lambda value: f"enc:{value}")
    document = _minimal_document(
        profile={"role": "admin", "daily_quota": 5, "is_active": True},
        personal_config={
            "dynamic_overrides": [
                {"key": "output_language", "value": "en", "description": "lang"}
            ],
            "webui": {"theme": "dark", "language": "en", "items_per_page": 50},
        },
        two_factor={
            "mfa_required": True,
            "totp_enabled": True,
            "totp_secret": "TOTP-SECRET",
            "totp_enabled_at": None,
            "totp_last_used_step": 5,
            "recovery_codes": [],
        },
        passkeys=[],
    )

    result = await restore_user_backup(
        session, parse_user_backup(serialize_user_backup(document))
    )

    assert result.users_updated == 1
    assert target.role == "admin"
    assert target.daily_quota == 5
    assert target.totp_secret_encrypted == "enc:TOTP-SECRET"
    assert result.user_configs_created == 1
    assert result.webui_configs_created == 1
    assert session.committed is True
    assert session.rolled_back is False


@pytest.mark.asyncio
async def test_restore_skips_nonportable_recovery_codes_and_preserves_existing(
    monkeypatch,
):
    target = _user()
    session = _RestoreSession([target])
    existing = UserRecoveryCode(
        id=9,
        user_id=target.id,
        code_hash="e" * 64,
        used_at=None,
        created_at=datetime(2026, 7, 1, 0, 0, 0, tzinfo=UTC),
    )
    session.recoveries.append(existing)
    monkeypatch.setattr(
        service, "_recovery_code_hash_key_fingerprint", lambda: "d" * 64
    )
    document = _minimal_document(
        two_factor={
            "mfa_required": False,
            "totp_enabled": False,
            "totp_secret": None,
            "totp_enabled_at": None,
            "totp_last_used_step": None,
            "recovery_codes": [
                {"code_hash": "f" * 64, "used_at": None, "created_at": None}
            ],
        }
    )

    result = await restore_user_backup(
        session, parse_user_backup(serialize_user_backup(document))
    )

    assert result.recovery_codes_portable is False
    assert result.recovery_codes_imported == 0
    assert result.recovery_codes_deleted == 0
    assert result.recovery_codes_skipped == 1
    assert session.recoveries == [existing]


@pytest.mark.asyncio
async def test_restore_same_secret_replaces_recovery_codes_and_merges_passkey_sign_count(
    monkeypatch,
):
    target = _user()
    session = _RestoreSession([target])
    existing_recovery = UserRecoveryCode(
        id=9,
        user_id=target.id,
        code_hash="e" * 64,
        used_at=None,
        created_at=datetime(2026, 7, 1, 0, 0, 0, tzinfo=UTC),
    )
    existing_passkey = _passkey(user_id=target.id)
    session.recoveries.append(existing_recovery)
    session.passkeys.append(existing_passkey)
    monkeypatch.setattr(
        service, "_recovery_code_hash_key_fingerprint", lambda: "c" * 64
    )
    second_hash = credential_id_hash("credential-2")
    document = _minimal_document(
        two_factor={
            "mfa_required": False,
            "totp_enabled": False,
            "totp_secret": None,
            "totp_enabled_at": None,
            "totp_last_used_step": None,
            "recovery_codes": [
                {"code_hash": "f" * 64, "used_at": None, "created_at": None}
            ],
        },
        passkeys=[
            {
                "credential_id": "credential-1",
                "credential_id_hash": credential_id_hash("credential-1"),
                "public_key": "new-public-key",
                "sign_count": 2,
                "transports": "internal",
                "device_name": "Laptop",
                "backed_up": True,
                "created_at": None,
                "last_used_at": None,
            },
            {
                "credential_id": "credential-2",
                "credential_id_hash": second_hash,
                "public_key": "second-public-key",
                "sign_count": 7,
                "transports": None,
                "device_name": None,
                "backed_up": False,
                "created_at": None,
                "last_used_at": None,
            },
        ],
    )

    result = await restore_user_backup(
        session, parse_user_backup(serialize_user_backup(document))
    )

    assert result.recovery_codes_portable is True
    assert result.recovery_codes_imported == 1
    assert result.recovery_codes_deleted == 1
    assert result.passkeys_updated == 1
    assert result.passkeys_created == 1
    assert existing_passkey.sign_count == 4


def _route(path: str, method: str) -> APIRoute:
    for route in router.routes:
        if (
            isinstance(route, APIRoute)
            and route.path == path
            and method in route.methods
        ):
            return route
    raise AssertionError(f"Route {method} {path} not found")


@pytest.mark.parametrize(
    "path",
    ["/config/backup/export/users", "/config/backup/users/import"],
)
def test_user_backup_routes_are_super_admin_csrf_protected(path: str):
    route = _route(path, "POST")
    dependencies = [dependency.call for dependency in route.dependant.dependencies]
    assert require_super_admin in dependencies
    assert require_csrf in dependencies
