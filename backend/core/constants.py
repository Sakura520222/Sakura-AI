"""项目级共享常量。"""

import base64


# Android APK 签名证书指纹 — 签名密钥轮换时须同步更新。
ANDROID_SHA256_CERT_FINGERPRINTS: tuple[str, ...] = (
    "4B:57:6D:C7:65:07:4C:EC:1A:50:37:E2:F1:FE:F1:AC:47:43:7E:87:E6:70:4C:F8:7E:0B:D1:5C:B4:A7:CB:38",
    "0B:34:0D:3A:BA:A5:13:A6:8E:31:DE:B6:F3:E0:81:D3:66:7C:B2:43:31:AF:90:E5:52:D6:43:8D:F4:41:12:A0",
)


def _fingerprint_to_apk_key_hash(fingerprint: str) -> str:
    """将 SHA-256 指纹（冒号分隔十六进制）转换为 android:apk-key-hash 格式。"""
    raw = bytes.fromhex(fingerprint.replace(":", ""))
    b64 = base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")
    return f"android:apk-key-hash:{b64}"


# 自动从签名证书指纹派生，无需手动维护。
ANDROID_APK_KEY_HASH_ORIGINS: tuple[str, ...] = tuple(
    _fingerprint_to_apk_key_hash(fp) for fp in ANDROID_SHA256_CERT_FINGERPRINTS
)
