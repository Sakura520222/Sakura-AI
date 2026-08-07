"""Version & deployment info route.

Slice 1：只读展示当前版本与部署模式（无 checker、无 updater）。
- update_supported 恒为 False（尚无 updater 连接）。
- update_available / latest_version 恒为 None（Slice 2 UpdateChecker 接入后填充）。

build_version_info 是纯函数（接收明确的 deploy_mode，不读环境变量）；
route 层从 Settings 读 SAKURA_DEPLOY_MODE 后传参，避免环境变量读取出现两个入口。
"""

from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse

from backend import __version__
from backend.core.config import get_settings
from backend.webui.deps import require_auth

router = APIRouter(tags=["Version"])

_VALID_MODES = {"image", "source"}


def build_version_info(deploy_mode: str) -> dict:
    """构造版本与部署信息（纯函数）。

    Args:
        deploy_mode: 部署模式。非法值归一化为 "unknown"。

    Returns:
        版本与部署信息 dict。deployment_type 归一化为 image/source/unknown。
    """
    mode = deploy_mode if deploy_mode in _VALID_MODES else "unknown"

    update_supported = False
    if mode == "source":
        reason = "source_updater_not_available"
    elif mode == "image":
        reason = "updater_not_connected"  # Slice 4 接入 updater 后改判
    else:
        reason = "unknown_deployment"

    return {
        "current_version": __version__,
        "deployment_type": mode,
        "update_supported": update_supported,
        "update_unsupported_reason": reason,
        "update_available": None,  # Slice 2 填
        "latest_version": None,  # Slice 2 填
    }


@router.get("/version/info")
async def get_version_info(user: dict = Depends(require_auth)):
    """返回当前版本与部署模式（所有登录用户可读）。

    deploy_mode 从 Settings.sakura_deploy_mode 读取（BaseSettings 自动从
    SAKURA_DEPLOY_MODE 环境变量加载，由 compose env_file 注入）。
    """
    mode = get_settings().sakura_deploy_mode or "unknown"
    info = build_version_info(mode)
    return JSONResponse(
        info,
        headers={"Cache-Control": "no-store, max-age=0"},
    )
