"""Two-factor authentication service helpers."""

from __future__ import annotations

import base64
import hashlib
import hmac
import io
import secrets
import string
import time
from dataclasses import dataclass
from datetime import UTC, datetime

import pyotp
import qrcode
from cryptography.fernet import Fernet, InvalidToken
from loguru import logger
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.core.config import get_settings
from backend.models.telegram_models import TelegramUser, UserRecoveryCode


@dataclass(frozen=True)
class TotpSetupData:
    """TOTP setup payload returned to WebUI/API clients."""

    secret: str
    provisioning_uri: str
    qr_code_data_uri: str


class TwoFactorError(Exception):
    """Base 2FA service error."""


class TwoFactorNotConfiguredError(TwoFactorError):
    """Raised when the user has no encrypted TOTP secret."""


class TwoFactorReplayError(TwoFactorError):
    """Raised when the same TOTP time step is reused."""


def _derive_fernet_key(raw_key: str) -> bytes:
    """Derive a Fernet key from configured text material."""
    digest = hashlib.sha256(raw_key.encode("utf-8")).digest()
    return base64.urlsafe_b64encode(digest)


def _get_fernet() -> Fernet:
    settings = get_settings()
    key_material = settings.two_factor_encryption_key or settings.webui_secret_key
    return Fernet(_derive_fernet_key(key_material))


def encrypt_totp_secret(secret: str) -> str:
    """Encrypt a TOTP secret for database storage."""
    return _get_fernet().encrypt(secret.encode("utf-8")).decode("utf-8")


def decrypt_totp_secret(encrypted_secret: str) -> str:
    """Decrypt a TOTP secret from database storage."""
    try:
        return _get_fernet().decrypt(encrypted_secret.encode("utf-8")).decode("utf-8")
    except InvalidToken as exc:
        raise TwoFactorNotConfiguredError("TOTP secret cannot be decrypted") from exc


def _normalize_account_name(user: TelegramUser) -> str:
    github_username = user.github_username or f"telegram-{user.telegram_id}"
    return f"{github_username}@Sakura-AI-Reviewer"


def _build_qr_data_uri(provisioning_uri: str) -> str:
    image = qrcode.make(provisioning_uri)
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    encoded = base64.b64encode(buffer.getvalue()).decode("ascii")
    return f"data:image/png;base64,{encoded}"


def create_totp_setup(user: TelegramUser) -> TotpSetupData:
    """Create a new TOTP setup secret and QR code data URI."""
    settings = get_settings()
    secret = pyotp.random_base32()
    provisioning_uri = pyotp.TOTP(secret).provisioning_uri(
        name=_normalize_account_name(user),
        issuer_name=settings.two_factor_issuer,
    )
    return TotpSetupData(
        secret=secret,
        provisioning_uri=provisioning_uri,
        qr_code_data_uri=_build_qr_data_uri(provisioning_uri),
    )


def get_current_totp_step() -> int:
    """Return the current TOTP timecode for a standard 30-second interval."""
    return int(time.time()) // 30


def verify_totp_secret(
    secret: str,
    code: str,
    last_used_step: int | None = None,
    valid_window: int = 1,
) -> int | None:
    """Verify a TOTP code and return the matching time step.

    Returns None for invalid codes. Raises TwoFactorReplayError when the matching
    step has already been used for this user.
    """
    normalized = "".join(ch for ch in code.strip() if ch.isdigit())
    if len(normalized) != 6:
        return None

    totp = pyotp.TOTP(secret)
    current_step = get_current_totp_step()
    for offset in range(-valid_window, valid_window + 1):
        step = current_step + offset
        timestamp = step * int(totp.interval)
        if totp.verify(normalized, for_time=timestamp, valid_window=0):
            if last_used_step is not None and step <= int(last_used_step):
                raise TwoFactorReplayError("TOTP code was already used")
            return step
    return None


def verify_user_totp(user: TelegramUser, code: str) -> int | None:
    """Verify a user's stored encrypted TOTP secret."""
    if not user.totp_secret_encrypted:
        raise TwoFactorNotConfiguredError("TOTP is not configured")
    secret = decrypt_totp_secret(user.totp_secret_encrypted)
    return verify_totp_secret(secret, code, user.totp_last_used_step)


def hash_recovery_code(code: str) -> str:
    """Hash a recovery code with the application secret."""
    settings = get_settings()
    normalized = normalize_recovery_code(code)
    return hmac.new(
        settings.webui_secret_key.encode("utf-8"),
        normalized.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()


def normalize_recovery_code(code: str) -> str:
    """Normalize recovery code input for comparison."""
    return code.strip().replace("-", "").replace(" ", "").upper()


def _format_recovery_code(raw: str) -> str:
    midpoint = len(raw) // 2
    return f"{raw[:midpoint]}-{raw[midpoint:]}"


def generate_recovery_codes() -> list[str]:
    """Generate display recovery codes."""
    settings = get_settings()
    alphabet = string.ascii_uppercase + string.digits
    codes = []
    for _ in range(settings.two_factor_recovery_code_count):
        raw = "".join(
            secrets.choice(alphabet)
            for _ in range(settings.two_factor_recovery_code_length)
        )
        codes.append(_format_recovery_code(raw))
    return codes


async def replace_recovery_codes(
    session: AsyncSession,
    user_id: int,
    codes: list[str] | None = None,
) -> list[str]:
    """Replace all active recovery codes and return plaintext codes once."""
    display_codes = codes or generate_recovery_codes()
    await session.execute(
        delete(UserRecoveryCode).where(UserRecoveryCode.user_id == user_id)
    )
    for code in display_codes:
        session.add(
            UserRecoveryCode(user_id=user_id, code_hash=hash_recovery_code(code))
        )
    return display_codes


async def consume_recovery_code(
    session: AsyncSession,
    user_id: int,
    code: str,
) -> bool:
    """Consume a recovery code if it exists and has not been used."""
    code_hash = hash_recovery_code(code)
    result = await session.execute(
        select(UserRecoveryCode).where(
            UserRecoveryCode.user_id == user_id,
            UserRecoveryCode.code_hash == code_hash,
            UserRecoveryCode.used_at.is_(None),
        )
    )
    recovery_code = result.scalar_one_or_none()
    if not recovery_code:
        return False
    recovery_code.used_at = datetime.now(UTC)
    return True


async def count_unused_recovery_codes(session: AsyncSession, user_id: int) -> int:
    """Count unused recovery codes for the user."""
    result = await session.execute(
        select(UserRecoveryCode).where(
            UserRecoveryCode.user_id == user_id,
            UserRecoveryCode.used_at.is_(None),
        )
    )
    return len(result.scalars().all())


async def disable_totp(session: AsyncSession, user: TelegramUser) -> None:
    """Disable TOTP and remove recovery codes."""
    user.totp_enabled = False
    user.totp_secret_encrypted = None
    user.totp_enabled_at = None
    user.totp_last_used_step = None
    await session.execute(
        delete(UserRecoveryCode).where(UserRecoveryCode.user_id == user.id)
    )
    logger.info("TOTP disabled for user_id={}", user.id)
