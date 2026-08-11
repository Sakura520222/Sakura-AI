"""Secret crypto service tests."""

from cryptography.fernet import Fernet

from backend.services import secret_crypto_service as scs
from backend.services.secret_crypto_service import (
    SecretCryptoError,
    _derive_fernet_key,
    decrypt_secret,
    encrypt_secret,
)


def test_encrypt_then_decrypt_roundtrip():
    token = "gho_abcdef1234567890_refresh_token_example"
    encrypted = encrypt_secret(token)

    assert encrypted != token
    assert decrypt_secret(encrypted) == token


def test_ciphertext_is_not_plaintext_and_changes_per_call():
    token = "gho_secret_value"
    a = encrypt_secret(token)
    b = encrypt_secret(token)

    # Fernet 自带时间戳/IV，同一明文每次密文不同
    assert a != token
    assert b != token
    assert a != b


def test_wrong_key_raises_secret_crypto_error(monkeypatch):
    plaintext = "gho_some_token"
    encrypted = encrypt_secret(plaintext)

    # 用不同密钥材料解密，应当失败
    monkeypatch.setattr(scs, "_get_key_material", lambda: "a-different-key-material")

    try:
        decrypt_secret(encrypted)
    except SecretCryptoError:
        pass
    else:
        raise AssertionError("Expected SecretCryptoError on wrong key")


def test_decrypt_corrupted_ciphertext_raises():
    try:
        decrypt_secret("not-a-valid-fernet-token")
    except SecretCryptoError:
        pass
    else:
        raise AssertionError("Expected SecretCryptoError on corrupted ciphertext")


def test_empty_and_none_pass_through():
    assert encrypt_secret("") == ""
    assert encrypt_secret(None) == ""
    assert decrypt_secret("") == ""
    assert decrypt_secret(None) == ""


def test_key_derivation_is_deterministic():
    # 相同密钥材料派生相同 Fernet key
    assert _derive_fernet_key("material-x") == _derive_fernet_key("material-x")


def test_encrypt_with_explicit_key_round_trip():
    # 直接用独立 key 构造 Fernet，验证加解密一致性（脱离全局 settings）
    key = _derive_fernet_key("explicit-test-key")
    f = Fernet(key)
    token = "gho_explicit"
    cipher = f.encrypt(token.encode("utf-8")).decode("utf-8")

    assert f.decrypt(cipher.encode("utf-8")).decode("utf-8") == token
