"""Android App Links 数字资产链接验证路由。"""

from fastapi import APIRouter
from fastapi.responses import JSONResponse

from backend.core.constants import ANDROID_SHA256_CERT_FINGERPRINTS

router = APIRouter()

_ASSETLINKS_DATA = [
    {
        "relation": [
            "delegate_permission/common.handle_all_urls",
            "delegate_permission/common.get_login_creds",
        ],
        "target": {
            "namespace": "android_app",
            "package_name": "com.sakura_ai_reviewer",
            "sha256_cert_fingerprints": list(ANDROID_SHA256_CERT_FINGERPRINTS),
        },
    }
]


@router.get("/.well-known/assetlinks.json", include_in_schema=False)
def assetlinks():
    """Android App Links 数字资产链接验证。"""
    return JSONResponse(content=_ASSETLINKS_DATA, media_type="application/json")
