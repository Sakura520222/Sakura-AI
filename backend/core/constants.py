"""Project-wide shared constants."""

# Android APK signing certificate fingerprints — must match the app's signing key.
# Update these when the signing key is rotated.
ANDROID_SHA256_CERT_FINGERPRINTS: tuple[str, ...] = (
    "4B:57:6D:C7:65:07:4C:EC:1A:50:37:E2:F1:FE:F1:AC:47:43:7E:87:E6:70:4C:F8:7E:0B:D1:5C:B4:A7:CB:38",
    "0B:34:0D:3A:BA:A5:13:A6:8E:31:DE:B6:F3:E0:81:D3:66:7C:B2:43:31:AF:90:E5:52:D6:43:8D:F4:41:12:A0",
)

# Derived: WebAuthn / passkey android:apk-key-hash origins (base64url of raw SHA-256 bytes).
ANDROID_APK_KEY_HASH_ORIGINS: tuple[str, ...] = (
    "android:apk-key-hash:S1dtx2UHTOwaUDfi8f7xrEdDfofmcEz4fgvRXLSnyzg",
    "android:apk-key-hash:CzQNOrqlE6aOMd628-CB02Z8skMxr5DlUtZDjfRBEqA",
)
