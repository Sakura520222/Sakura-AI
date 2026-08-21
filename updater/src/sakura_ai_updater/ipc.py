"""Updater IPC server — HTTP over UDS，协议 v1 body envelope（spec §7）。

The app deliberately only knows HTTP.  The daemon owns the socket and injects a
``JobOrchestrator`` instance through ``app.state``; this keeps transport details
out of the action handlers and makes the handlers straightforward to exercise
with a fake orchestrator in tests.

所有成功（2xx）响应经 ``envelope()`` 包成 ``{protocol_version, updater_version, data}``
（§7.2）。版本字段**只在 envelope 顶层**，``data`` 不重复（避免内外两份漂移）。错误响应
（4xx/5xx，如 Slice 4 的 409 Conflict）直接返回，不走 envelope——spec §7.5 的 409 用
``{error, job_id}`` 格式。
"""

from __future__ import annotations

from typing import Any

from fastapi import FastAPI, Request
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


def _error_response(status_code: int, code: str, **extra: Any) -> JSONResponse:
    """Return an unwrapped v1 error body.

    Error responses intentionally do not use :func:`envelope`; clients need to
    distinguish an action failure from a successful response carrying an error
    field.  ``extra`` is kept typed/structured (not a free-form command body).
    """

    return JSONResponse({"error": code, **extra}, status_code=status_code)


def _as_data(value: Any) -> dict:
    """Convert orchestrator result models to the JSON data object."""

    if isinstance(value, dict):
        return value
    to_dict = getattr(value, "to_dict", None)
    if callable(to_dict):
        result = to_dict()
        return result if isinstance(result, dict) else {"value": result}
    # Dataclasses from older/fake orchestrators are supported without coupling
    # the IPC layer to a concrete jobs module.
    try:
        from dataclasses import asdict, is_dataclass

        if is_dataclass(value):
            return asdict(value)
    except TypeError, ValueError:
        pass
    return {"value": value}


def _exception_details(exc: Exception) -> dict[str, Any]:
    """Extract structured checks/job id from orchestrator exceptions."""

    details: dict[str, Any] = {}
    for name in (
        "checks",
        "result",
        "job_id",
        "target_version",
        "target_channel",
        "target_revision",
        "target_digest",
        "detail",
    ):
        value = getattr(exc, name, None)
        if value is not None:
            details[name] = value
    return details


def _action_error(exc: Exception) -> JSONResponse:
    """Map typed orchestrator failures to the frozen IPC error contract.

    ``jobs.py`` is intentionally optional at import time (the daemon's status
    endpoint must remain usable while the action implementation is unavailable),
    so mapping uses the stable exception class names and attributes rather than a
    hard import of implementation classes.
    """

    name = type(exc).__name__.lower()
    details = _exception_details(exc)
    if "updateinprogress" in name or "conflict" in name:
        return _error_response(
            409,
            "update_in_progress",
            **({"job_id": details.pop("job_id")} if "job_id" in details else {}),
        )
    if "maintenance" in name:
        return _error_response(503, "updater_maintenance")
    if "targetnotfound" in name or name in {
        "releasenotfound",
        "releasenotfounderror",
        "notfounderror",
    }:
        return _error_response(404, "target_not_found", **details)
    if "manifest" in name and (
        "invalid" in name or "notfound" in name or "missing" in name
    ):
        return _error_response(422, "manifest_invalid")
    if "registry" in name or "target" in name and "notfound" not in name:
        return _error_response(422, "invalid_target")
    if "preflight" in name or "gate" in name:
        checks = details.get("checks")
        return _error_response(
            422,
            "preflight_failed",
            **({"checks": checks} if checks is not None else {}),
        )
    if "protocol" in name:
        return _error_response(502, "protocol_error", **details)
    if "unavailable" in name or "network" in name:
        detail = details.get("detail")
        return _error_response(
            502,
            "release_unavailable",
            **({"detail": detail} if isinstance(detail, str) else {}),
        )
    # Unknown orchestrator failures are not silently reported as success.
    return _error_response(500, "internal_error", **details)


async def _request_json(request: Request) -> dict:
    """Read a typed action request body, returning an empty object for no body."""

    try:
        body = await request.json()
    except ValueError, TypeError:
        return {}
    return body if isinstance(body, dict) else {}


def _confirm_channel_switch(body: dict) -> tuple[bool | None, JSONResponse | None]:
    """Accept only a JSON boolean; omitted means the safe false default."""

    if "confirm_channel_switch" not in body:
        return False, None
    value = body["confirm_channel_switch"]
    if type(value) is not bool:
        return None, _error_response(422, "invalid_confirm_channel_switch")
    return value, None


def create_app(state_path: str, *, orchestrator: Any | None = None) -> FastAPI:
    """构建 updater IPC app。

    Args:
        state_path: ``update-state.json`` 路径。``/v1/status`` 每次请求读最新 state
            （更新过程中 state 文件由 Slice 4 ImageAdapter 写入，非启动快照）。
            state 文件损坏时 ``load_state`` fail-closed 抛异常 → 500（不返回假数据）。
        orchestrator: Host-side ``JobOrchestrator`` injected by ``serve``.  When
            omitted, status/health remain available but action endpoints return
            503 ``updater_not_ready``.
    """
    app = FastAPI(title="Sakura AI Updater", version=__version__)
    app.state.state_path = state_path
    app.state.orchestrator = orchestrator

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
        data: dict[str, Any] = {
            "state": job.state if job else "idle",
            "has_active_job": has_active,
            "active_job_id": store.active_job_id,
            "deployment": job.deployment if job else None,
        }
        # A status poll must remain a synchronous projection: use only the
        # already-computed in-memory readiness snapshot from the injected
        # orchestrator.  The legacy status-only app has no orchestrator and keeps
        # the original response shape/semantics.
        orchestrator = getattr(app.state, "orchestrator", None)
        snapshot = getattr(orchestrator, "readiness_snapshot", None)
        if isinstance(snapshot, dict):
            for key in ("update_ready", "readiness", "target"):
                if key in snapshot:
                    data[key] = snapshot[key]
        return envelope(data)

    @app.get("/v1/health")
    async def health() -> JSONResponse:
        """健康检查（liveness）。"""
        return envelope({"ok": True})

    def _orchestrator_or_error() -> Any | JSONResponse:
        value = getattr(app.state, "orchestrator", None)
        if value is None:
            return _error_response(503, "updater_not_ready")
        return value

    @app.post("/v1/check")
    async def check() -> JSONResponse:
        orchestrator_value = _orchestrator_or_error()
        if isinstance(orchestrator_value, JSONResponse):
            return orchestrator_value
        try:
            return envelope(_as_data(await orchestrator_value.check()))
        except Exception as exc:
            return _action_error(exc)

    @app.post("/v1/preflight")
    async def preflight(request: Request) -> JSONResponse:
        orchestrator_value = _orchestrator_or_error()
        if isinstance(orchestrator_value, JSONResponse):
            return orchestrator_value
        body = await _request_json(request)
        confirm_channel_switch, confirm_error = _confirm_channel_switch(body)
        if confirm_error is not None:
            return confirm_error
        target = body.get("target")
        target_version = body.get("target_version")
        if target is not None and not isinstance(target, dict):
            return _error_response(422, "invalid_target")
        if target is None and (
            not isinstance(target_version, str) or not target_version
        ):
            return _error_response(422, "invalid_target_version")
        try:
            if target is not None:
                if target.get("channel") in {"development", "stable"}:
                    result = await orchestrator_value.preflight(
                        target,
                        confirm_channel_switch=confirm_channel_switch is True,
                    )
                else:
                    return _error_response(422, "invalid_target")
            else:
                result = await orchestrator_value.preflight(target_version)
            return envelope(_as_data(result))
        except Exception as exc:
            return _action_error(exc)

    @app.post("/v1/update")
    async def update(request: Request) -> JSONResponse:
        orchestrator_value = _orchestrator_or_error()
        if isinstance(orchestrator_value, JSONResponse):
            return orchestrator_value
        body = await _request_json(request)
        confirm_channel_switch, confirm_error = _confirm_channel_switch(body)
        if confirm_error is not None:
            return confirm_error
        target = body.get("target")
        target_version = body.get("target_version")
        if target is not None and not isinstance(target, dict):
            return _error_response(422, "invalid_target")
        if (
            target is None
            and target_version is not None
            and not isinstance(target_version, str)
        ):
            return _error_response(422, "invalid_target_version")
        try:
            if target is not None:
                if target.get("channel") in {"development", "stable"}:
                    result = await orchestrator_value.submit_update(
                        target,
                        confirm_channel_switch=confirm_channel_switch is True,
                    )
                else:
                    return _error_response(422, "invalid_target")
            else:
                result = await orchestrator_value.submit_update(target_version)
            if isinstance(result, str):
                data = {
                    "job_id": result,
                    "state": "checking",
                    "target_version": target_version,
                }
            else:
                data = _as_data(result)
                data.setdefault("state", "checking")
                if target_version is not None:
                    data.setdefault("target_version", target_version)
                if target is not None:
                    data.setdefault("target", target)
            return envelope(data, status_code=202)
        except Exception as exc:
            return _action_error(exc)

    @app.post("/v1/lifecycle/prepare-stop")
    async def prepare_stop() -> JSONResponse:
        """Atomically close update submission before host lifecycle changes."""

        orchestrator_value = _orchestrator_or_error()
        if isinstance(orchestrator_value, JSONResponse):
            return orchestrator_value
        try:
            return envelope(_as_data(await orchestrator_value.prepare_stop()))
        except Exception as exc:
            return _action_error(exc)

    @app.post("/v1/lifecycle/cancel-stop")
    async def cancel_stop() -> JSONResponse:
        """Undo a prepared stop if the host lifecycle operation aborts."""

        orchestrator_value = _orchestrator_or_error()
        if isinstance(orchestrator_value, JSONResponse):
            return orchestrator_value
        try:
            return envelope(_as_data(await orchestrator_value.cancel_stop()))
        except Exception as exc:
            return _action_error(exc)

    @app.get("/v1/jobs/{job_id}")
    async def get_job(job_id: str) -> JSONResponse:
        orchestrator_value = _orchestrator_or_error()
        if isinstance(orchestrator_value, JSONResponse):
            return orchestrator_value
        try:
            result = orchestrator_value.get_job(job_id)
            if hasattr(result, "__await__"):
                result = await result
            if result is None:
                return _error_response(404, "job_not_found")
            return envelope(_as_data(result))
        except Exception as exc:
            return _action_error(exc)

    @app.get("/v1/jobs/{job_id}/logs")
    async def get_job_logs(job_id: str) -> JSONResponse:
        orchestrator_value = _orchestrator_or_error()
        if isinstance(orchestrator_value, JSONResponse):
            return orchestrator_value
        try:
            # ``JobLogStore`` intentionally returns an empty payload for a
            # missing buffer, so consult the durable job record first to retain
            # the IPC 404 contract for unknown ids.
            job_getter = getattr(orchestrator_value, "get_job", None)
            if job_getter is not None:
                job = job_getter(job_id)
                if hasattr(job, "__await__"):
                    job = await job
                if job is None:
                    return _error_response(404, "job_not_found")
            getter = getattr(orchestrator_value, "get_job_logs_payload", None)
            if getter is None:
                getter = orchestrator_value.get_job_logs
            result = getter(job_id)
            if hasattr(result, "__await__"):
                result = await result
            if result is None:
                return _error_response(404, "job_not_found")
            if isinstance(result, list):
                result = {"job_id": job_id, "logs": result, "truncated": False}
            return envelope(_as_data(result))
        except Exception as exc:
            return _action_error(exc)

    @app.post("/v1/rollback")
    async def rollback() -> JSONResponse:
        return _error_response(501, "not_implemented")

    return app
