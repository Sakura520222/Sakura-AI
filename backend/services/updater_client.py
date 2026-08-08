"""Backend → Host Updater UDS client（spec §7.1）。

容器内 backend 经 ``/run/sakura-ai/updater.sock`` 调 updater 受限 IPC。连不上（updater
未运行 / socket 不可达 / 平台无 AF_UNIX）、**envelope shape 非法**、**malformed JSON**
均返回 None——``/version/info`` 据此标 ``updater_connected=false``（不当 connected，
不使 /version/info 500）。

Slice 3a：只读 ``get_status``。update / preflight 等动作端点在 Slice 4。

性能：UDS 连接轻量，每次请求新建 transport；连不存在的 socket 立即 OSError（不像 TCP
timeout），故 ``/version/info``（navbar 周期性调）在 updater 未起时也快。
"""

from __future__ import annotations

import httpx

from backend.core.config import get_settings

# v1 协议常量（spec §7.2）。backend 不 import updater 包，故本地定义。
_PROTOCOL_VERSION = 1


def is_valid_v1_envelope(envelope: object) -> bool:
    """校验 body envelope shape（spec §7.2）。

    protocol_version==1 + updater_version is str + data is dict。非法返回 False
    （调用方据此降级为 disconnected，不把坏数据当 connected）。
    """
    if not isinstance(envelope, dict):
        return False
    if envelope.get("protocol_version") != _PROTOCOL_VERSION:
        return False
    if not isinstance(envelope.get("updater_version"), str):
        return False
    return isinstance(envelope.get("data"), dict)


class UpdaterClient:
    """HTTP over UDS client。

    Args:
        socket_path: UDS 路径。None 时从 Settings 读（默认 /run/sakura-ai/updater.sock）。
        timeout: 请求超时（秒）。updater 响应慢时生效；连不上 socket 通常瞬间失败。
    """

    def __init__(self, socket_path: str | None = None, timeout: float = 2.0):
        self._socket_path = socket_path or get_settings().sakura_updater_socket_path
        self._timeout = timeout

    async def get_status(self) -> dict | None:
        """GET /v1/status。成功且 envelope shape 合法返回 envelope，否则 None。

        ValueError 捕获 malformed JSON（resp.json() decode 失败），防 /version/info 500。
        """
        transport = httpx.AsyncHTTPTransport(uds=self._socket_path)
        try:
            async with httpx.AsyncClient(
                transport=transport,
                base_url="http://updater",
                timeout=self._timeout,
            ) as client:
                resp = await client.get("/v1/status")
                resp.raise_for_status()
                envelope = resp.json()  # malformed JSON → ValueError
        except (httpx.HTTPError, OSError, ValueError, AttributeError):
            # AttributeError：无 AF_UNIX 的平台（如部分 Windows Python 构建）连接时
            # 在 anyio 深处抛 socket.AF_UNIX 缺失，同样视为"连不上"→ None。
            return None
        if not is_valid_v1_envelope(envelope):
            return None
        return envelope
