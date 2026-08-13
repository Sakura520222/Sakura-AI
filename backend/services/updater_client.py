"""Backend → Host Updater UDS client（spec §7.1）。

容器内 backend 经 ``/run/sakura-ai/updater.sock`` 调 updater 受限 IPC。连不上（updater
未运行 / socket 不可达 / 平台无 AF_UNIX）、**envelope shape 非法**、**malformed JSON**
均返回 None——``/version/info`` 据此标 ``updater_connected=false``（不当 connected，
不使 /version/info 500）。

``get_status`` keeps its deliberately forgiving ``None`` semantics for navbar
polling.  Destructive/readiness actions use the typed exception API below so a
route can distinguish an unavailable host, a protocol violation, and an updater
HTTP error.

性能：UDS 连接轻量，每次请求新建 transport；连不存在的 socket 立即 OSError（不像 TCP
timeout），故 ``/version/info``（navbar 周期性调）在 updater 未起时也快。
"""

from __future__ import annotations

import httpx

from backend.core.config import get_settings

# v1 协议常量（spec §7.2）。backend 不 import updater 包，故本地定义。
_PROTOCOL_VERSION = 1


class UpdaterUnavailableError(RuntimeError):
    """The host updater UDS could not be reached."""


class UpdaterProtocolError(RuntimeError):
    """The updater returned malformed JSON or an incompatible envelope."""


class UpdaterActionError(RuntimeError):
    """The updater returned a non-2xx action response."""

    def __init__(self, status_code: int, body: dict):
        self.status_code = status_code
        self.body = body
        super().__init__(f"updater action failed with HTTP {status_code}: {body}")


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
        timeout: 状态与 job 轮询超时（秒）。
        action_timeout: check/preflight/update 动作超时（秒）；这些动作会同步执行
            GitHub、镜像仓库和 Docker 预检，不能复用短轮询超时。
    """

    def __init__(
        self,
        socket_path: str | None = None,
        timeout: float = 2.0,
        action_timeout: float = 120.0,
    ):
        self._socket_path = socket_path or get_settings().sakura_updater_socket_path
        self._timeout = timeout
        self._action_timeout = action_timeout

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

    async def _request(
        self,
        method: str,
        path: str,
        json_body: dict | None = None,
    ) -> dict:
        """Perform one typed action request.

        Unlike ``get_status``, this method never folds an error into ``None``.
        UDS transport failures become :class:`UpdaterUnavailableError`, bad
        JSON/envelopes become :class:`UpdaterProtocolError`, and every HTTP
        error preserves its status and structured body in
        :class:`UpdaterActionError`.
        """

        transport = httpx.AsyncHTTPTransport(uds=self._socket_path)
        request_timeout = (
            self._action_timeout
            if path in {"/v1/check", "/v1/preflight", "/v1/update"}
            else self._timeout
        )
        try:
            async with httpx.AsyncClient(
                transport=transport,
                base_url="http://updater",
                timeout=request_timeout,
            ) as client:
                response = await client.request(method, path, json=json_body)
        except (httpx.HTTPError, OSError, AttributeError) as exc:
            raise UpdaterUnavailableError(str(exc)) from exc

        try:
            payload = response.json()
        except (ValueError, TypeError) as exc:
            if response.status_code >= 200 and response.status_code < 300:
                raise UpdaterProtocolError("updater returned malformed JSON") from exc
            raise UpdaterActionError(
                response.status_code,
                {"error": "malformed_error_response"},
            ) from exc

        if response.status_code < 200 or response.status_code >= 300:
            body = payload if isinstance(payload, dict) else {"error": "invalid_error_body"}
            raise UpdaterActionError(response.status_code, body)
        if not is_valid_v1_envelope(payload):
            raise UpdaterProtocolError("updater returned an invalid v1 envelope")
        return payload

    async def check(self) -> dict:
        return await self._request("POST", "/v1/check")

    async def preflight(
        self,
        target_version: str | None = None,
        *,
        target: dict | None = None,
        confirm_channel_switch: bool = False,
    ) -> dict:
        body = {"target": target, "confirm_channel_switch": confirm_channel_switch} if target is not None else {"target_version": target_version}
        return await self._request(
            "POST", "/v1/preflight", body
        )

    async def update(
        self,
        target_version: str | None = None,
        *,
        target: dict | None = None,
        confirm_channel_switch: bool = False,
    ) -> dict:
        if target is not None:
            body = {"target": target, "confirm_channel_switch": confirm_channel_switch}
        else:
            body = {"target_version": target_version} if target_version is not None else {}
        return await self._request("POST", "/v1/update", body)

    async def get_job(self, job_id: str) -> dict:
        return await self._request("GET", f"/v1/jobs/{job_id}")

    async def get_job_logs(self, job_id: str) -> dict:
        return await self._request("GET", f"/v1/jobs/{job_id}/logs")
