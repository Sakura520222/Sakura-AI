"""Android App Links 数字资产链接验证路由。"""

from fastapi import APIRouter
from fastapi.responses import JSONResponse

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
            "sha256_cert_fingerprints": [
                "4B:57:6D:C7:65:07:4C:EC:1A:50:37:E2:F1:FE:F1:AC:47:43:7E:87:E6:70:4C:F8:7E:0B:D1:5C:B4:A7:CB:38",
                "0B:34:0D:3A:BA:A5:13:A6:8E:31:DE:B6:F3:E0:81:D3:66:7C:B2:43:31:AF:90:E5:52:D6:43:8D:F4:41:12:A0",
            ],
        },
    }
]


@router.get("/.well-known/assetlinks.json", include_in_schema=False)
async def assetlinks():
    """Android App Links 数字资产链接验证。"""
    return JSONResponse(content=_ASSETLINKS_DATA, media_type="application/json")
