"""Updater IPC server — HTTP over UDS，协议 v1 body envelope（spec §7）。

Slice 3a：``envelope()`` helper + ``GET /v1/status`` + ``GET /v1/health``。
动作端点（check / preflight / update / rollback / jobs）在 Slice 4 接入。

所有成功（2xx）响应经 ``envelope()`` 包成 ``{protocol_version, updater_version, data}``
（§7.2）。版本字段**只在 envelope 顶层**，``data`` 不重复（避免内外两份漂移）。错误响应
（4xx/5xx，如 Slice 4 的 409 Conflict）直接返回，不走 envelope——spec §7.5 的 409 用
``{error, job_id}`` 格式。
"""

from __future__ import annotations

from fastapi import FastAPI
from fastapi.responses import JSONResponse

from sakura_ai_updater import PROTOCOL_VERSION, __version__
from sakura_ai_updater.state import UpdateStateStore, load_state


def envelope(data: dict, status_code: int = 200) -> JSONResponse:
    """包 body envelope（spec §7.2）。成功响应统一用此；版本字段只在顶层。"""
    return JSONResponse(
        {
            "protocol_version": PROTOCOL_VERSION,
            "updater_version": __version__,
            "data": data,
        },
        status_code=status_code,
        headers={"Cache-Control": "no-store"},
    )


def create_app(state_path: str) -> FastAPI:
    """构建 updater IPC app。

    Args:
        state_path: ``update-state.json`` 路径。``/v1/status`` 每次请求读最新 state
            （更新过程中 state 文件由 Slice 4 ImageAdapter 写入，非启动快照）。
            state 文件损坏时 ``load_state`` fail-closed 抛异常 → 500（不返回假数据）。
    """
    app = FastAPI(title="Sakura AI Updater", version=__version__)
    app.state.state_path = state_path

    @app.get("/v1/status")
    async def get_status() -> JSONResponse:
        """当前 updater 状态 + 是否有进行中的 job（spec §7.3）。"""
        store: UpdateStateStore = load_state(app.state.state_path)
        job = store.current_job
        has_active = (
            store.active_job_id is not None
            and job is not None
            and not job.is_terminal()
        )
        return envelope(
            {
                "state": job.state if job else "idle",
                "has_active_job": has_active,
                "active_job_id": store.active_job_id,
                "deployment": job.deployment if job else None,
            }
        )

    @app.get("/v1/health")
    async def health() -> JSONResponse:
        """健康检查（liveness）。"""
        return envelope({"ok": True})

    return app
