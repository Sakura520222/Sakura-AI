"""Immutable task-local correlation, with explicit reset at execution boundaries."""

from __future__ import annotations

import inspect
from contextlib import contextmanager
from contextvars import ContextVar
from functools import wraps
from types import MappingProxyType
from urllib.parse import parse_qsl
from uuid import uuid4

from loguru import logger

from backend.core.log_sanitizer import sensitive_key, sensitive_log_scope

CORRELATION_FIELDS = (
    "request_id",
    "session_id",
    "task_id",
    "invocation_id",
    "work_unit_id",
    "thread_id",
    "attempt_id",
    "logical_call_id",
    "tool_call_id",
    "worker",
)
_context: ContextVar[dict] = ContextVar("log_context", default=MappingProxyType({}))


def get_log_context() -> dict:
    return dict(_context.get())


def update_log_context(**values) -> None:
    _context.set(
        {
            **_context.get(),
            **{k: v for k, v in values.items() if k in CORRELATION_FIELDS},
        }
    )


@contextmanager
def log_context(*, clear: bool = False, **values):
    token = _context.set({} if clear else get_log_context())
    try:
        update_log_context(**values)
        yield
    finally:
        _context.reset(token)


def task_logging(worker: str):
    """Keep the submission request ID, but never inherit another task's lane."""

    def decorate(function):
        @wraps(function)
        async def wrapped(*args, **kwargs):
            request_id = _context.get().get("request_id") or uuid4().hex
            with log_context(clear=True, request_id=request_id, worker=worker):
                arguments = inspect.signature(function).bind(*args, **kwargs).arguments
                if "task_id" in arguments:
                    update_log_context(task_id=arguments["task_id"])
                return await function(*args, **kwargs)

        return wrapped

    return decorate


def traced_operation(fields=None, *, events: bool = False):
    """Scope coroutine/generator work; restore the consumer context before yield.

    Save context changes made by the operation between generator advances. Closing
    from another task must not reset a token created in the producer's context.
    """

    def decorate(function):
        signature = inspect.signature(function)

        def initial(args, kwargs):
            arguments = signature.bind(*args, **kwargs).arguments
            values = fields(arguments) if fields else {}
            candidate = arguments.get("candidate")
            secrets = (
                arguments.get("credential"),
                getattr(candidate, "credential", None),
            )
            return {**get_log_context(), **values}, secrets

        def failed(exc, secrets):
            # Carry only the redaction knowledge with an escaping exception so a
            # worker logging it after this scope closes still cannot print a key.
            existing = getattr(exc, "_log_redaction_secrets", ())
            exc._log_redaction_secrets = tuple(
                {*existing, *(v for v in secrets if isinstance(v, str) and v)}
            )
            if not hasattr(exc, "_log_correlation"):
                exc._log_correlation = get_log_context()
            if events:
                logger.bind(operation=function.__qualname__).opt(exception=exc).warning(
                    "Operation failed"
                )

        def proposed_tools(result):
            if not events:
                return
            calls = getattr(result, "tool_calls", None) or []
            if getattr(result, "type", None) == "tool_call_start":
                calls = [result.tool_call]
            for tool in calls:
                ident = getattr(tool, "id", None)
                if ident:
                    logger.bind(tool_call_id=ident).info("Model proposed tool call")

        if inspect.isasyncgenfunction(function):

            @wraps(function)
            async def stream(*args, **kwargs):
                state, secrets = initial(args, kwargs)
                iterator = function(*args, **kwargs)
                started = False
                try:
                    while True:
                        with (
                            log_context(clear=True, **state),
                            sensitive_log_scope(*secrets),
                        ):
                            try:
                                if events and not started:
                                    logger.bind(operation=function.__qualname__).info(
                                        "Operation started"
                                    )
                                    started = True
                                event = await anext(iterator)
                                proposed_tools(event)
                            except StopAsyncIteration:
                                if events:
                                    logger.bind(operation=function.__qualname__).info(
                                        "Operation completed"
                                    )
                                return
                            except BaseException as exc:
                                failed(exc, secrets)
                                raise
                            finally:
                                state = get_log_context()
                        yield event
                finally:
                    with (
                        log_context(clear=True, **state),
                        sensitive_log_scope(*secrets),
                    ):
                        await iterator.aclose()

            return stream

        @wraps(function)
        async def call(*args, **kwargs):
            state, secrets = initial(args, kwargs)
            with log_context(clear=True, **state), sensitive_log_scope(*secrets):
                if events:
                    logger.bind(operation=function.__qualname__).info(
                        "Operation started"
                    )
                try:
                    result = await function(*args, **kwargs)
                except BaseException as exc:
                    failed(exc, secrets)
                    raise
                if events:
                    proposed_tools(result)
                    status = getattr(result, "success", None)
                    logger.bind(operation=function.__qualname__, success=status).info(
                        "Operation completed"
                    )
                return result

        return call

    return decorate


def model_log_fields(arguments) -> dict:
    context = arguments.get("context") or getattr(
        arguments.get("self"), "context", None
    )
    return {
        **invocation_log_fields(context),
        "logical_call_id": arguments.get("logical_call_id")
        or _context.get().get("logical_call_id"),
        "attempt_id": None,
    }


def provider_log_fields(_arguments) -> dict:
    return {"attempt_id": _context.get().get("attempt_id") or "send-" + uuid4().hex}


def tool_log_fields(arguments) -> dict:
    call = arguments.get("tool_call")
    return {"tool_call_id": getattr(call, "id", None)}


def agent_log_fields(arguments) -> dict:
    session_id = getattr(arguments.get("self"), "session_id", None)
    # Agent conversations predate Activity sessions and use a separate table.
    return {"session_id": f"agent:{session_id}"} if session_id is not None else {}


def invocation_log_fields(context) -> dict:
    if context is None:
        return {}
    return {
        name: getattr(context, name)
        for name in ("session_id", "invocation_id", "work_unit_id", "thread_id")
        if getattr(context, name, None) is not None
    }


class RequestLogContextMiddleware:
    """Pure ASGI scope includes streaming, access logs and background submission.

    Generate IDs locally: accepting arbitrary client headers lets credentials or
    spoofed correlations enter logs. Return the server ID for support diagnostics.
    """

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        request_id = uuid4().hex
        scope.setdefault("state", {})["request_id"] = request_id

        async def send_with_id(message):
            if message["type"] == "http.response.start":
                message = dict(message)
                message["headers"] = [
                    (key, value)
                    for key, value in message.get("headers", [])
                    if key.lower() != b"x-request-id"
                ] + [(b"x-request-id", request_id.encode("ascii"))]
            await send(message)

        secrets = [
            value.decode("latin-1")
            for key, value in scope.get("headers", [])
            if sensitive_key(key.decode("latin-1"))
        ]
        secrets.extend(
            value
            for key, value in parse_qsl(
                scope.get("query_string", b"").decode("latin-1")
            )
            if sensitive_key(key)
            or key.lower() in {"code", "state", "key", "sig", "signature"}
        )
        with (
            log_context(clear=True, request_id=request_id),
            sensitive_log_scope(*secrets),
        ):
            try:
                await self.app(scope, receive, send_with_id)
            except Exception as exc:
                if not hasattr(exc, "_log_correlation"):
                    exc._log_correlation = get_log_context()
                logger.opt(exception=True).error("HTTP request failed")
                raise
