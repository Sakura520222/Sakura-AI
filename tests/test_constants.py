"""共享常量与辅助函数测试。"""

import pytest

from backend.core.constants import (
    ANDROID_APK_KEY_HASH_ORIGINS,
    ANDROID_SHA256_CERT_FINGERPRINTS,
    _fingerprint_to_apk_key_hash,
)


class TestFingerprintToApkKeyHash:
    """_fingerprint_to_apk_key_hash 单元测试。"""

    def test_normal_fingerprint(self):
        fp = "AB:CD:EF:00:11:22:33:44:55:66:77:88:99:AA:BB:CC:DD:EE:FF:00:11:22:33:44:55:66:77:88:99:AA:BB:CC"
        result = _fingerprint_to_apk_key_hash(fp)
        assert result.startswith("android:apk-key-hash:")
        # base64url of 32 bytes, no padding
        b64_part = result.removeprefix("android:apk-key-hash:")
        assert len(b64_part) == 43
        assert "=" not in b64_part

    def test_fingerprint_without_colons(self):
        # 使用生产配置中的第一个指纹（去除冒号）验证无分隔符格式
        fp = "4B576DC765074CEC1A5037E2F1FEF1AC47437E87E6704CF87E0BD15CB4A7CB38"
        result = _fingerprint_to_apk_key_hash(fp)
        assert result.startswith("android:apk-key-hash:")

    def test_known_value(self):
        # 验证命令: python -c "import base64; print(base64.urlsafe_b64encode(b'\x00'*32).rstrip(b'=').decode())"
        fp = "00:00:00:00:00:00:00:00:00:00:00:00:00:00:00:00:00:00:00:00:00:00:00:00:00:00:00:00:00:00:00:00"
        assert _fingerprint_to_apk_key_hash(fp) == "android:apk-key-hash:AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"

    def test_empty_string_raises(self):
        with pytest.raises(ValueError, match="64 个十六进制字符"):
            _fingerprint_to_apk_key_hash("")

    def test_invalid_hex_raises(self):
        with pytest.raises(ValueError, match="非法十六进制字符"):
            _fingerprint_to_apk_key_hash("ZZ:ZZ:ZZ:ZZ:00:00:00:00:00:00:00:00:00:00:00:00:00:00:00:00:00:00:00:00:00:00:00:00:00:00:00:00")

    def test_wrong_length_raises(self):
        with pytest.raises(ValueError, match="64 个十六进制字符"):
            _fingerprint_to_apk_key_hash("AB:CD:EF")

    def test_derived_origins_match_fingerprints(self):
        assert len(ANDROID_APK_KEY_HASH_ORIGINS) == len(ANDROID_SHA256_CERT_FINGERPRINTS)
        for fp, origin in zip(ANDROID_SHA256_CERT_FINGERPRINTS, ANDROID_APK_KEY_HASH_ORIGINS):
            assert origin == _fingerprint_to_apk_key_hash(fp)
