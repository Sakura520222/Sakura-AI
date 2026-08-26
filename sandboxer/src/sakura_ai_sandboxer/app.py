"""FastAPI application for the independent sandboxd protocol."""

from __future__ import annotations

import json
import time
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from . import PROTOCOL_VERSION, __version__
from .config import SandboxdConfig
from .errors import SandboxError
from .models import ExecutionRequest, validate_protocol_envelope
from .runtime import RuntimeAdapter
from .service import SandboxExecutionService


def _success(
    data: dict[str, Any],
    status_code: int = 200,
    *,
    max_response_bytes: int | None = None,
) -> JSONResponse:
    payload = {
        "protocol_version": PROTOCOL_VERSION,
        "sandboxd_version": __version__,
        "data": data,
    }
    if max_response_bytes is not None:
        encoded = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode(
            "utf-8"
        )
        if len(encoded) > max_response_bytes:
            return _error(
                "OUTPUT_LIMIT",
                "sandboxd response exceeds the byte limit",
                413,
                max_response_bytes=max_response_bytes,
            )
    return JSONResponse(
        payload,
        status_code=status_code,
        headers={"Cache-Control": "no-store"},
    )


def _error(
    error: str,
    detail: str | None = None,
    status_code: int = 500,
    *,
    max_response_bytes: int | None = None,
) -> JSONResponse:
    payload: dict[str, Any] = {
        "protocol_version": PROTOCOL_VERSION,
        "sandboxd_version": __version__,
        "error": error,
    }
    if detail:
        payload["detail"] = detail
    if max_response_bytes is not None:
        encoded = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode(
            "utf-8"
        )
        if len(encoded) > max_response_bytes:
            # Error details are diagnostic only.  Drop them before allowing a
            # response to exceed the configured envelope budget.
            payload.pop("detail", None)
    return JSONResponse(payload, status_code=status_code)


def create_app(
    config: SandboxdConfig | None = None,
    *,
    runtime: RuntimeAdapter | None = None,
) -> FastAPI:
    """Build an ASGI app; tests inject ``FakeRuntimeAdapter`` here."""

    service = SandboxExecutionService(config, runtime)

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        recover_orphans = getattr(service.runtime, "recover_orphans", None)
        if service.runtime.name == "docker":
            # A Docker daemon must never advertise a usable socket before it
            # has removed only its own orphaned task containers.  Missing
            # recovery support is a startup error, not an ``unavailable``
            # runtime fallback.
            if not callable(recover_orphans):
                raise RuntimeError("Docker runtime does not implement orphan recovery")
            await recover_orphans(
                deadline=time.monotonic() + service.config.cleanup_margin_seconds
            )
            service.mark_runtime_ready()
        try:
            yield
        finally:
            await service.shutdown()

    app = FastAPI(
        title="Sakura AI Sandbox Daemon",
        version=__version__,
        lifespan=lifespan,
    )
    app.state.sandbox_service = service

    @app.exception_handler(RequestValidationError)
    async def validation_error(request: Request, exc: RequestValidationError) -> JSONResponse:
        del request, exc
        return _error(
            "INVALID_REQUEST",
            "request failed strict protocol validation",
            422,
            max_response_bytes=service.config.max_response_bytes,
        )

    @app.get("/v1/health")
    async def health() -> JSONResponse:
        return _success(
            service.health().model_dump(mode="json"),
            max_response_bytes=service.config.max_response_bytes,
        )

    @app.post("/v1/executions")
    async def execute(request: ExecutionRequest) -> JSONResponse:
        try:
            result = await service.execute(request)
        except SandboxError as exc:
            return _error(
                exc.code.value,
                exc.detail,
                exc.status_code,
                max_response_bytes=service.config.max_response_bytes,
            )
        except Exception:
            return _error(
                "INTERNAL_ERROR",
                "sandbox execution failed",
                500,
                max_response_bytes=service.config.max_response_bytes,
            )
        return _success(
            result.model_dump(mode="json"),
            max_response_bytes=service.config.max_response_bytes,
        )

    @app.post("/v1/executions/{request_id}/cancel")
    async def cancel(request_id: str) -> JSONResponse:
        # Route IDs are validated by the service lookup and are never echoed in
        # a runtime command.  Reject malformed IDs without revealing paths.
        if not request_id or len(request_id) > 128 or "/" in request_id or "\\" in request_id:
            return _error(
                "INVALID_REQUEST",
                "request id is invalid",
                422,
                max_response_bytes=service.config.max_response_bytes,
            )
        try:
            result = await service.cancel(request_id)
        except SandboxError as exc:
            return _error(
                exc.code.value,
                exc.detail,
                exc.status_code,
                max_response_bytes=service.config.max_response_bytes,
            )
        return _success(
            result.model_dump(mode="json"),
            max_response_bytes=service.config.max_response_bytes,
        )

    return app


__all__ = ["create_app", "validate_protocol_envelope"]
