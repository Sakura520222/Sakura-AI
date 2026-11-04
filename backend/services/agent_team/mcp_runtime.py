"""Trusted remote MCP transport boundary, with fresh policy and no call replay.

Only the administrator's configuration selects endpoints and credentials. Remote
schemas/results are untrusted data; they never select local files or processes.
A fresh SDK client belongs to one coroutine, including cancellation and teardown.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import logging
import re
from collections.abc import Awaitable, Callable
from contextlib import asynccontextmanager
from contextvars import ContextVar
from typing import Any
from urllib.parse import urlsplit

from jsonschema import Draft202012Validator
from jsonschema.validators import validator_for
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from referencing import Registry
from referencing.exceptions import NoSuchResource

from backend.services.agent_team.capability_policy import Capability
from backend.services.agent_team.tools.base import ToolResult


class MCPBoundaryError(ValueError):
    """Stable, credential-free reason only."""


class MCPToolPolicy(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)
    enabled: bool = True
    read_only: bool = False
    required_capabilities: tuple[str, ...] = ()

    @field_validator("required_capabilities", mode="before")
    @classmethod
    def capabilities(cls, value):
        return tuple(Capability(item).value for item in value)


class MCPServerConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)
    id: str = Field(pattern=r"^[a-zA-Z0-9_-]+$")
    url: str = Field(repr=False)
    enabled: bool = True
    timeout_seconds: float = Field(gt=0, allow_inf_nan=False)
    headers: dict[str, str] = Field(default_factory=dict, repr=False)
    credential_headers: dict[str, str] = Field(default_factory=dict, repr=False)
    # A configured service is available to the main Agent without a per-tool
    # setup ceremony. Exact overrides can disable a tool or attest read-only.
    default_policy: MCPToolPolicy = Field(default_factory=MCPToolPolicy)
    tools: dict[str, MCPToolPolicy] = Field(default_factory=dict)

    def tool_policy(self, remote_name: str) -> MCPToolPolicy:
        return self.tools.get(remote_name, self.default_policy)

    @field_validator("url")
    @classmethod
    def endpoint(cls, value):
        parts = urlsplit(value)
        if (
            parts.scheme not in {"http", "https"}
            or not parts.hostname
            or parts.username is not None
            or parts.password is not None
            or parts.query
            or parts.fragment
            or any(ord(c) < 33 for c in value)
        ):
            raise ValueError("invalid_mcp_endpoint")
        return value

    @model_validator(mode="after")
    def header_names(self):
        for name, value in (self.headers | self.credential_headers).items():
            if (
                not re.fullmatch(r"[!#$%&'*+.^_`|~0-9A-Za-z-]+", name)
                or name.lower() in {"host", "content-length", "transfer-encoding"}
                or name.lower().startswith("mcp-")
                or "\r" in value
                or "\n" in value
            ):
                raise ValueError("invalid_mcp_header")
        if len({k.lower() for k in self.headers | self.credential_headers}) != len(
            self.headers
        ) + len(self.credential_headers):
            raise ValueError("duplicate_mcp_header")
        return self


class MCPConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)
    servers: tuple[MCPServerConfig, ...] = ()

    @field_validator("servers", mode="before")
    @classmethod
    def server_tuple(cls, value):
        return tuple(value)

    @model_validator(mode="after")
    def unique_ids(self):
        if len({s.id for s in self.servers}) != len(self.servers):
            raise ValueError("duplicate_mcp_server")
        return self


def tool_name(server_id: str, remote_name: str) -> str:
    """Full SHA256 namespace, within the common function-name wire limit."""
    digest = hashlib.sha256(json.dumps([server_id, remote_name]).encode()).digest()
    return "mcp_" + base64.b32encode(digest).decode().rstrip("=").lower()


def _no_retrieval(uri):
    raise NoSuchResource(ref=uri)


def schema_validator(schema: dict[str, Any]):
    """Honor JSON Schema composition/local references, never fetch remote refs."""
    try:
        dialect = schema.get("$schema")
        validator = (
            validator_for(schema, default=None) if dialect else Draft202012Validator
        )
        if validator is None:
            raise ValueError("unknown_dialect")
        pending = [schema]
        while pending:
            node = pending.pop()
            if isinstance(node, dict):
                for key in ("$ref", "$dynamicRef", "$recursiveRef"):
                    if key in node and (
                        not isinstance(node[key], str) or not node[key].startswith("#")
                    ):
                        raise ValueError("external_reference")
                # Only schema-valued keywords are executable schemas. Literal
                # defaults/examples/const objects can legitimately contain $ref.
                for key in (
                    "properties",
                    "patternProperties",
                    "$defs",
                    "definitions",
                    "dependentSchemas",
                    "dependencies",
                ):
                    value = node.get(key)
                    if isinstance(value, dict):
                        pending.extend(v for v in value.values() if isinstance(v, dict))
                for key in (
                    "additionalProperties",
                    "unevaluatedProperties",
                    "propertyNames",
                    "contains",
                    "additionalItems",
                    "unevaluatedItems",
                    "not",
                    "if",
                    "then",
                    "else",
                    "contentSchema",
                    "items",
                    "allOf",
                    "anyOf",
                    "oneOf",
                    "prefixItems",
                ):
                    value = node.get(key)
                    if isinstance(value, dict):
                        pending.append(value)
                    elif isinstance(value, list):
                        pending.extend(value)
        validator.check_schema(schema)
        return validator(schema, registry=Registry(retrieve=_no_retrieval))
    except Exception:
        raise MCPBoundaryError("invalid_schema") from None


def _redact(value, secrets):
    if isinstance(value, str):
        for secret in sorted(secrets, key=len, reverse=True):
            if secret:
                value = value.replace(secret, "[REDACTED]")
        return value
    if isinstance(value, dict):
        return {_redact(k, secrets): _redact(v, secrets) for k, v in value.items()}
    if isinstance(value, list):
        return [_redact(v, secrets) for v in value]
    return value


# SDK background tasks inherit this context. Suppress their raw diagnostics,
# including exception tracebacks and server-controlled notification text. The
# runtime emits the awaited, stable audit records instead. Other MCP users and
# application loggers are unaffected outside this operation's context.
_transport_active: ContextVar[bool] = ContextVar("sakura_mcp_transport", default=False)


class _TransportLogFilter(logging.Filter):
    def filter(self, record):
        return not _transport_active.get()


_log_filter = _TransportLogFilter()


def _filter_transport_logs():
    # MCP 2.x uses a literal "client" session logger outside its package
    # namespace. Attach to the SDK's actual logger; suppress only records from
    # this transport context, including server-controlled ValidationErrors.
    from mcp.client.session import logger as session_logger

    session_logger.addFilter(_log_filter)
    for name in list(logging.Logger.manager.loggerDict):
        if name == "mcp" or name.startswith(("mcp.", "httpx2", "httpcore2")):
            logging.getLogger(name).addFilter(_log_filter)


class MCPRuntime:
    def __init__(
        self,
        *,
        load_config: Callable[[], Awaitable[MCPConfig]],
        authorize: Callable[[tuple[str, ...], bool], Awaitable[bool]],
        audit: Callable[[dict[str, Any]], Awaitable[None]],
        resolve_credential: Callable[[str], Awaitable[str]] | None = None,
        read_only: bool = False,
    ):
        self.load_config = load_config
        self.authorize = authorize
        self.audit = audit
        self.resolve_credential = resolve_credential
        self.read_only = read_only
        self._closed = False
        self._active: set[asyncio.Task] = set()

    async def close(self):
        self._closed = True
        tasks = tuple(self._active - {asyncio.current_task()})
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    async def _record(
        self, action, status, server_id="", name="", reason="", schema_digest=""
    ):
        try:
            await self.audit(
                {
                    "event": "mcp_" + action,
                    "status": status,
                    "server_id": server_id,
                    "tool": name,
                    "reason": reason,
                    "schema_digest": schema_digest,
                }
            )
        except Exception:
            raise MCPBoundaryError("audit_unavailable") from None

    async def _config(self, *, cleanup=False):
        if self._closed and not cleanup:
            raise MCPBoundaryError("runtime_closed")
        try:
            config = await self.load_config()
            if not isinstance(config, MCPConfig):
                raise TypeError
            return config.model_copy(deep=True)
        except MCPBoundaryError:
            raise
        except Exception:
            raise MCPBoundaryError("configuration_unavailable") from None

    async def _admit(self, server, policy=None, *, cleanup=False):
        config = await self._config(cleanup=cleanup)
        current = next((s for s in config.servers if s.id == server.id), None)
        if current != server or not current.enabled:
            raise MCPBoundaryError("configuration_changed")
        if policy is not None and (
            not policy.enabled or self.read_only and not policy.read_only
        ):
            raise MCPBoundaryError("policy_denied")
        caps = tuple(
            sorted(
                {
                    "mcp.invoke",
                    "network.web",
                    *(policy.required_capabilities if policy else ()),
                }
            )
        )
        try:
            allowed = await self.authorize(caps, policy.read_only if policy else True)
        except Exception:
            allowed = False
        if not allowed or (self._closed and not cleanup):
            raise MCPBoundaryError("policy_denied")

    @asynccontextmanager
    async def _client(self, server, policy=None):
        await self._admit(server, policy)
        import httpx2
        from mcp import Client
        from mcp.client.streamable_http import streamable_http_client

        headers = dict(server.headers)
        for name, reference in server.credential_headers.items():
            if self.resolve_credential is None:
                raise MCPBoundaryError("credential_unavailable")
            try:
                value = await self.resolve_credential(reference)
                if (
                    not isinstance(value, str)
                    or not value
                    or "\n" in value
                    or "\r" in value
                ):
                    raise ValueError
                headers[name] = value
            except Exception:
                raise MCPBoundaryError("credential_unavailable") from None
        secrets = set(headers.values())
        for value in tuple(secrets):
            if " " in value:
                secrets.add(value.split(" ", 1)[1])

        async def before_request(request):
            # Closing denies all new work, but must still allow protocol-only
            # cancellation/termination under the CURRENT network/config policy.
            cleanup = request.method == "DELETE"
            if request.method == "POST":
                try:
                    cleanup = (
                        json.loads(request.content).get("method")
                        == "notifications/cancelled"
                    )
                except ValueError, AttributeError:
                    cleanup = False
            await self._admit(server, None if cleanup else policy, cleanup=cleanup)

        _filter_transport_logs()
        token = _transport_active.set(True)
        try:
            async with httpx2.AsyncClient(
                headers=headers,
                timeout=httpx2.Timeout(server.timeout_seconds),
                trust_env=False,
                event_hooks={"request": [before_request]},
            ) as http:
                transport = streamable_http_client(
                    server.url, http_client=http, max_sse_event_size=None
                )
                # SDK read_timeout_seconds is a total reply deadline, even
                # when progress is arriving. Only HTTP connect/read/write/pool
                # IO inactivity is bounded; active tools have no run-time cap.
                async with Client(transport, read_timeout_seconds=None) as client:
                    yield client, secrets
        finally:
            _transport_active.reset(token)

    async def _definitions(self, client, server):
        cursor = None
        cursors = set()
        definitions = {}
        while True:
            await self._admit(server)
            page = await client.list_tools(cursor=cursor, cache_mode="bypass")
            for definition in page.tools:
                if definition.name in definitions:
                    raise MCPBoundaryError("duplicate_remote_tool")
                definitions[definition.name] = definition
            cursor = page.next_cursor
            if cursor is None:
                return definitions
            if cursor in cursors:
                raise MCPBoundaryError("invalid_pagination")
            cursors.add(cursor)

    def _definition(self, definition, secrets):
        raw = {
            "description": definition.description or "Remote MCP tool (untrusted data)",
            "input_schema": definition.input_schema,
            "output_schema": definition.output_schema,
        }
        clean = _redact(raw, secrets)
        # Redaction must not alter schema semantics silently. Reject credential-
        # bearing schemas; descriptions and results may be safely redacted.
        if (
            raw["input_schema"] != clean["input_schema"]
            or raw["output_schema"] != clean["output_schema"]
        ):
            raise MCPBoundaryError("credential_in_schema")
        schema_validator(clean["input_schema"])
        if clean["output_schema"] is not None:
            schema_validator(clean["output_schema"])
        return clean, hashlib.sha256(
            json.dumps(clean, sort_keys=True).encode()
        ).hexdigest()

    async def discover(self):
        from backend.services.agent_team.tools.mcp_tool import MCPTool

        task = asyncio.current_task()
        self._active.add(task)
        result = []
        try:
            try:
                config = await self._config()
            except MCPBoundaryError as exc:
                await self._record("discovery", "failed", reason=str(exc))
                return []
            for server in config.servers:
                if not server.enabled:
                    continue
                try:
                    async with self._client(server) as (client, secrets):
                        definitions = await self._definitions(client, server)
                        server_tools = []
                        for remote_name, definition in definitions.items():
                            policy = server.tool_policy(remote_name)
                            if policy is None or not policy.enabled:
                                continue
                            try:
                                await self._admit(server, policy)
                                clean, digest = self._definition(definition, secrets)
                                server_tools.append(
                                    MCPTool(
                                        self,
                                        server.id,
                                        remote_name,
                                        policy,
                                        clean,
                                        digest,
                                    )
                                )
                            except MCPBoundaryError as exc:
                                await self._record(
                                    "discovery",
                                    "denied",
                                    server.id,
                                    tool_name(server.id, remote_name),
                                    str(exc),
                                )
                    await self._record("discovery", "completed", server.id)
                    result.extend(server_tools)
                except asyncio.CancelledError:
                    await self._record("discovery", "cancelled", server.id)
                    raise
                except Exception as exc:
                    reason = (
                        str(exc)
                        if isinstance(exc, MCPBoundaryError)
                        else "transport_failure"
                    )
                    await self._record(
                        "discovery",
                        "denied"
                        if reason in {"policy_denied", "configuration_changed"}
                        else "failed",
                        server.id,
                        reason=reason,
                    )
            return result
        finally:
            self._active.discard(task)

    async def invoke(
        self,
        server_id,
        remote_name,
        args,
        *,
        expected_digest=None,
        expected_policy=None,
    ):
        name = tool_name(server_id, remote_name)
        task = asyncio.current_task()
        self._active.add(task)
        try:
            config = await self._config()
            server = next((s for s in config.servers if s.id == server_id), None)
            policy = server.tool_policy(remote_name) if server else None
            if server is None or policy is None or not server.enabled:
                raise MCPBoundaryError("policy_denied")
            if expected_policy is not None and expected_policy != policy:
                raise MCPBoundaryError("policy_changed")
            await self._admit(server, policy)
            async with self._client(server, policy) as (client, secrets):
                definitions = await self._definitions(client, server)
                if remote_name not in definitions:
                    raise MCPBoundaryError("tool_unavailable")
                clean, digest = self._definition(definitions[remote_name], secrets)
                if expected_digest is not None and digest != expected_digest:
                    raise MCPBoundaryError("schema_changed")
                if not schema_validator(clean["input_schema"]).is_valid(args):
                    raise MCPBoundaryError("invalid_arguments")
                await self._admit(server, policy)
                await self._record(
                    "invocation", "started", server_id, name, schema_digest=digest
                )

                # The low-level method performs a single call. High-level
                # Client.call_tool may retry HEADER_MISMATCH/input_required.
                async def discard_progress(_progress, _total=None, _message=None):
                    # Opt into protocol progress to keep active SSE streams
                    # alive. Remote progress text is untrusted and never logged.
                    pass

                response = await client.session.call_tool(
                    remote_name, args, progress_callback=discard_progress
                )
                if not response.is_error and clean["output_schema"] is not None:
                    if not schema_validator(clean["output_schema"]).is_valid(
                        response.structured_content
                    ):
                        raise MCPBoundaryError("invalid_result")
                output = _redact(
                    response.model_dump(mode="json", by_alias=True, exclude_none=True),
                    secrets,
                )
                await self._record(
                    "invocation",
                    "failed" if response.is_error else "completed",
                    server_id,
                    name,
                    schema_digest=digest,
                )
                return ToolResult(
                    not response.is_error,
                    {"untrusted_mcp_result": output},
                    "MCP tool failed" if response.is_error else "",
                    "MCP_TOOL_ERROR" if response.is_error else "",
                )
        except asyncio.CancelledError:
            await self._record("invocation", "cancelled", server_id, name)
            raise
        except Exception as exc:
            reason = (
                str(exc) if isinstance(exc, MCPBoundaryError) else "transport_failure"
            )
            await self._record(
                "invocation",
                "denied"
                if reason
                in {"policy_denied", "policy_changed", "configuration_changed"}
                else "failed",
                server_id,
                name,
                reason,
            )
            return ToolResult(
                False,
                error="MCP operation failed: " + reason,
                error_code="MCP_" + reason.upper(),
            )
        finally:
            self._active.discard(task)
