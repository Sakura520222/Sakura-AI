"""机密字段对称加密服务 / Symmetric encryption for secret fields.

为仓库互助功能加密 GitHub App user-to-server token / refresh token 而设。
复用 ``two_factor_service`` 的 Fernet 派生思路，但独立成服务，避免把 token
加密逻辑塞进 TOTP 服务。

密钥来源链（取首个非空值）::

    star_aid_token_encryption_key
      -> two_factor_encryption_key
      -> webui_secret_key

安全要求：

- 加密后的密文不可读；只有持有相同派生密钥的实例才能解密。
- 密钥不匹配或密文损坏时抛 ``SecretCryptoError``，异常消息不含任何 token 片段。
- 本模块绝不打印、记录 token 前缀或全文。
"""

from __future__ import annotations

import base64
import hashlib

from cryptography.fernet import Fernet, InvalidToken

from backend.core.config import get_settings


class SecretCryptoError(Exception):
    """机密加解密错误（解密失败、密钥不匹配、密文损坏等）。"""


def _derive_fernet_key(raw_key: str) -> bytes:
    """从文本密钥材料派生 Fernet 密钥（SHA-256 -> urlsafe base64）。"""
    digest = hashlib.sha256(raw_key.encode("utf-8")).digest()
    return base64.urlsafe_b64encode(digest)


def _get_key_material() -> str:
    """按优先级取首个非空密钥材料。"""
    settings = get_settings()
    return (
        settings.star_aid_token_encryption_key
        or settings.two_factor_encryption_key
        or settings.webui_secret_key
    )


def get_fernet() -> Fernet:
    """获取当前配置派生的 Fernet 实例。"""
    material = _get_key_material()
    if not material:
        # webui_secret_key 有默认值，理论上不会走到这里；保留防御性校验
        raise SecretCryptoError("encryption key material is empty")
    return Fernet(_derive_fernet_key(material))


def encrypt_secret(plaintext: str | None) -> str:
    """加密机密字符串，返回可入库的密文。

    ``None`` / 空串原样返回空串，避免把"未设置"与"空密文"混淆。
    """
    if not plaintext:
        return ""
    return get_fernet().encrypt(plaintext.encode("utf-8")).decode("utf-8")


def decrypt_secret(encrypted: str | None) -> str:
    """解密机密字符串。

    Raises:
        SecretCryptoError: 密钥不匹配或密文损坏。
    """
    if not encrypted:
        return ""
    try:
        return get_fernet().decrypt(encrypted.encode("utf-8")).decode("utf-8")
    except InvalidToken as exc:
        # 异常消息刻意不含任何密文片段
        raise SecretCryptoError("secret cannot be decrypted") from exc
