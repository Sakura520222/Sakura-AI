"""Anthropic 原生协议适配器 / Anthropic native protocol adapter.

直接用 httpx 调用 Anthropic Messages API，避免引入 anthropic SDK 依赖。
Calls Anthropic Messages API directly via httpx, avoiding a hard dependency
on the anthropic SDK.

鉴权: x-api-key + anthropic-version 头
端点: POST /v1/messages, GET /v1/models, GET /v1/models/{id}
"""

from __future__ import annotations

import json
from typing import Any, AsyncIterator, Optional

import httpx
from loguru import logger

from backend.core.ai_protocol.adapters.base import ProtocolAdapter
from backend.core.ai_protocol.errors import AIError, classify_context_overflow
from backend.core.ai_protocol.models import (
    AIErrorCategory,
    ModelCapabilitySet,
    ModelDiscoveryResult,
    ProtocolFamily,
    ResolvedEndpoint,
    StopReason,
    UnifiedMessage,
    UnifiedRequest,
    UnifiedResponse,
    UnifiedStreamEvent,
    UnifiedTool,
    UnifiedToolCall,
    UnifiedUsage,
)

_ANTHROPIC_VERSION = "2023-06-01"

# Anthropic stop_reason → 归一化 / Anthropic stop_reason → normalized
_ANTHROPIC_STOP_MAP: dict[str, StopReason] = {
    "end_turn": StopReason.END_TURN,
    "max_tokens": StopReason.MAX_TOKENS,
    "stop_sequence": StopReason.END_TURN,
    "tool_use": StopReason.TOOL_USE,
    "pause_turn": StopReason.PAUSE_TURN,
    "refusal": StopReason.REFUSAL,
}


class AnthropicNativeAdapter(ProtocolAdapter):
    """Anthropic Messages API 原生适配器 / Anthropic Messages API native adapter."""

    family = ProtocolFamily.ANTHROPIC_NATIVE

    def build_headers(
        self, credential: str, endpoint: ResolvedEndpoint
    ) -> dict[str, str]:
        headers = {
            "x-api-key": credential,
            "anthropic-version": _ANTHROPIC_VERSION,
            "Content-Type": "application/json",
        }
        headers.update(endpoint.extra_headers)
        return headers

    @staticmethod
    def resolve_messages_url(endpoint: ResolvedEndpoint) -> str:
        return f"{endpoint.base_url}{endpoint.chat_path}"

    @staticmethod
    def resolve_models_url(endpoint: ResolvedEndpoint) -> str:
        return f"{endpoint.base_url}models"

    @staticmethod
    def resolve_model_detail_url(endpoint: ResolvedEndpoint, model_id: str) -> str:
        return f"{endpoint.base_url}models/{model_id}"

    # ------------------------------------------------------------------
    # 模型发现 / Model discovery
    # ------------------------------------------------------------------
    async def list_models(
        self,
        client: httpx.AsyncClient,
        endpoint: ResolvedEndpoint,
        credential: str,
    ) -> list[ModelDiscoveryResult]:
        url = self.resolve_models_url(endpoint)
        resp = await self._get(client, url, credential, endpoint)
        payload = self.parse_json_response(
            resp,
            endpoint,
            operation="Anthropic 模型列表请求",
            allow_list=True,
        )
        return self._parse_model_list(payload)

    async def fetch_model_metadata(
        self,
        client: httpx.AsyncClient,
        endpoint: ResolvedEndpoint,
        credential: str,
        model_id: str,
    ) -> Optional[ModelDiscoveryResult]:
        url = self.resolve_model_detail_url(endpoint, model_id)
        try:
            resp = await self._get(client, url, credential, endpoint)
            payload = self.parse_json_response(
                resp,
                endpoint,
                operation="Anthropic 模型详情请求",
            )
        except AIError as exc:  # type: ignore[name-defined]
            logger.debug("Anthropic 模型详情获取失败: model={} err={}", model_id, exc)
            return None
        return self._parse_model_detail(payload, model_id)

    @staticmethod
    def _parse_model_list(payload: Any) -> list[ModelDiscoveryResult]:
        raw = payload.get("data") if isinstance(payload, dict) else None
        if raw is None and isinstance(payload, list):
            raw = payload
        raw = raw or []
        results: list[ModelDiscoveryResult] = []
        for item in raw:
            if isinstance(item, dict):
                model_id = item.get("id") or item.get("name")
                if model_id:
                    results.append(
                        ModelDiscoveryResult(
                            model_id=str(model_id),
                            display_name=str(item.get("display_name") or model_id),
                            context_window_tokens=_to_int(item.get("max_input_tokens")),
                            max_output_tokens=_to_int(item.get("max_tokens")),
                        )
                    )
        results.sort(key=lambda x: x.model_id)
        return results

    @staticmethod
    def _parse_model_detail(payload: Any, model_id: str) -> Optional[ModelDiscoveryResult]:
        if not isinstance(payload, dict):
            return None
        return ModelDiscoveryResult(
            model_id=str(payload.get("id") or model_id),
            display_name=str(payload.get("display_name") or payload.get("id") or model_id),
            context_window_tokens=_to_int(payload.get("max_input_tokens")),
            max_output_tokens=_to_int(payload.get("max_tokens")),
        )

    # ------------------------------------------------------------------
    # 请求序列化 / Request serialization
    # ------------------------------------------------------------------
    def serialize_request(self, request: UnifiedRequest) -> dict[str, Any]:
        """UnifiedRequest → Anthropic Messages body."""
        system, messages = self._split_system(request)
        body: dict[str, Any] = {
            "model": request.model,
            "messages": [self._serialize_message(m) for m in messages],
            "max_tokens": request.max_tokens,
        }
        if system:
            body["system"] = system
        if request.temperature is not None:
            body["temperature"] = request.temperature
        if request.top_p is not None:
            body["top_p"] = request.top_p
        if request.top_k is not None:
            body["top_k"] = request.top_k
        if request.thinking is not None:
            body["thinking"] = request.thinking
        if request.effort is not None:
            body["effort"] = request.effort
        if request.tools:
            body["tools"] = [self._serialize_tool(t) for t in request.tools]
        if request.tool_choice:
            body["tool_choice"] = self._serialize_tool_choice(request.tool_choice)
        if request.stream:
            body["stream"] = True
        return body

    @staticmethod
    def _split_system(
        request: UnifiedRequest,
    ) -> tuple[str, list[UnifiedMessage]]:
        """提取 system（Anthropic 要求顶层 system 字段）/ Extract system prompt."""
        system_parts: list[str] = []
        other: list[UnifiedMessage] = []
        for msg in request.messages:
            if msg.role == "system":
                if msg.content:
                    system_parts.append(msg.content)
            else:
                other.append(msg)
        if request.system and request.system not in system_parts:
            system_parts.insert(0, request.system)
        return "\n\n".join(system_parts), other

    @staticmethod
    def _serialize_message(message: UnifiedMessage) -> dict[str, Any]:
        role = message.role
        # Anthropic 工具结果用 role=user + tool_result block
        if role == "tool":
            content = message.content or ""
            try:
                parsed = json.loads(content)
                content_text = json.dumps(parsed, ensure_ascii=False)
            except (json.JSONDecodeError, TypeError):
                content_text = content
            return {
                "role": "user",
                "content": [
                    {
                        "type": "tool_result",
                        "tool_use_id": message.tool_call_id or "",
                        "content": content_text,
                    }
                ],
            }
        # assistant 工具调用 / assistant tool_use blocks
        if role == "assistant" and message.tool_calls:
            blocks: list[dict[str, Any]] = []
            if message.content:
                blocks.append({"type": "text", "text": message.content})
            for tc in message.tool_calls:
                try:
                    input_obj = json.loads(tc.arguments) if tc.arguments else {}
                except (json.JSONDecodeError, TypeError):
                    input_obj = {"raw": tc.arguments}
                blocks.append(
                    {
                        "type": "tool_use",
                        "id": tc.id,
                        "name": tc.name,
                        "input": input_obj,
                    }
                )
            return {"role": "assistant", "content": blocks}
        # 普通文本消息 / plain text
        return {
            "role": role if role in ("user", "assistant") else "user",
            "content": message.content or "",
        }

    @staticmethod
    def _serialize_tool(tool: UnifiedTool) -> dict[str, Any]:
        return {
            "name": tool.name,
            "description": tool.description,
            "input_schema": tool.parameters,
        }

    @staticmethod
    def _serialize_tool_choice(choice: str) -> Any:
        if choice in ("auto", "any"):
            return {"type": choice}
        if choice == "none":
            # Anthropic 无 none；用 any 并提供空工具列表由上层规避，此处保留 auto
            return {"type": "auto"}
        if choice == "required":
            return {"type": "any"}
        return {"type": "tool", "name": choice}

    # ------------------------------------------------------------------
    # 响应反序列化 / Response deserialization
    # ------------------------------------------------------------------
    def parse_response(
        self, payload: dict[str, Any], raw: Any
    ) -> UnifiedResponse:
        content_blocks = payload.get("content") or []
        text_parts: list[str] = []
        tool_calls: list[UnifiedToolCall] = []
        for block in content_blocks:
            if not isinstance(block, dict):
                continue
            btype = block.get("type")
            if btype == "text":
                text_parts.append(block.get("text") or "")
            elif btype == "tool_use":
                try:
                    arguments = json.dumps(block.get("input") or {}, ensure_ascii=False)
                except (TypeError, ValueError):
                    arguments = "{}"
                tool_calls.append(
                    UnifiedToolCall(
                        id=block.get("id") or "",
                        name=block.get("name") or "",
                        arguments=arguments,
                    )
                )
        stop_reason = _ANTHROPIC_STOP_MAP.get(
            str(payload.get("stop_reason")), StopReason.END_TURN
        )
        usage = self._parse_usage(payload.get("usage"))
        return UnifiedResponse(
            content="".join(text_parts),
            tool_calls=tool_calls,
            stop_reason=stop_reason,
            usage=usage,
            raw=raw,
        )

    @staticmethod
    def _parse_usage(raw: Any) -> UnifiedUsage:
        if not isinstance(raw, dict):
            return UnifiedUsage()
        return UnifiedUsage(
            input_tokens=int(raw.get("input_tokens", 0) or 0),
            output_tokens=int(raw.get("output_tokens", 0) or 0),
            cache_read_tokens=int(raw.get("cache_read_input_tokens", 0) or 0),
            cache_creation_tokens=int(raw.get("cache_creation_input_tokens", 0) or 0),
        )

    # ------------------------------------------------------------------
    # HTTP / chat / stream
    # ------------------------------------------------------------------
    async def _get(
        self,
        client: httpx.AsyncClient,
        url: str,
        credential: str,
        endpoint: ResolvedEndpoint,
    ) -> httpx.Response:
        headers = self.build_headers(credential, endpoint)
        try:
            resp = await client.get(url, headers=headers, timeout=15)
        except httpx.HTTPError as exc:
            raise self.raise_error(
                AIErrorCategory.NETWORK,
                f"Anthropic 请求失败: {exc}",
                provider=endpoint.base_url,
                cause=exc,
            )
        self._raise_for_status(resp, endpoint)
        return resp

    def _raise_for_status(
        self, resp: httpx.Response, endpoint: ResolvedEndpoint
    ) -> None:
        if resp.status_code < 400:
            return
        try:
            body = resp.json()
        except Exception:
            body = resp.text
        category, message = self.translate_error(resp.status_code, body)
        raise self.raise_error(
            category,
            message,
            status_code=resp.status_code,
            provider=endpoint.base_url,
        )

    def translate_error(
        self, status_code: int, body: Any
    ) -> tuple[AIErrorCategory, str]:
        message = self._extract_error_message(body)
        message_lower = (message or "").lower()

        if status_code == 401:
            return AIErrorCategory.AUTH_INVALID, message or "API Key 无效"
        if status_code == 403:
            return AIErrorCategory.PERMISSION_DENIED, message or "权限被拒绝"
        if status_code == 404:
            return AIErrorCategory.MODEL_NOT_FOUND, message or "模型或端点不存在"
        if status_code == 400:
            if classify_context_overflow(message_lower):
                return AIErrorCategory.CONTEXT_OVERFLOW, message
            return AIErrorCategory.BAD_REQUEST, message
        if status_code == 429:
            return AIErrorCategory.RATE_LIMITED, message or "速率限制"
        if status_code == 529:
            return AIErrorCategory.OVERLOADED, message or "上游过载"
        if status_code >= 500:
            return AIErrorCategory.SERVER_ERROR, message
        return AIErrorCategory.UNKNOWN, message or f"HTTP {status_code}"

    @staticmethod
    def _extract_error_message(body: Any) -> str:
        if isinstance(body, dict):
            err = body.get("error")
            if isinstance(err, dict):
                return str(err.get("message") or err)
            if isinstance(err, str):
                return err
            return str(body.get("message") or body)
        if isinstance(body, str):
            return body
        return ""

    async def chat(
        self,
        client: httpx.AsyncClient,
        endpoint: ResolvedEndpoint,
        credential: str,
        request: UnifiedRequest,
        *,
        timeout: Optional[float] = None,
    ) -> UnifiedResponse:
        url = self.resolve_messages_url(endpoint)
        body = self.serialize_request(request)
        headers = self.build_headers(credential, endpoint)
        try:
            resp = await client.post(url, json=body, headers=headers, timeout=timeout)
        except httpx.HTTPError as exc:
            raise self.raise_error(
                AIErrorCategory.NETWORK,
                f"Anthropic chat 请求失败: {exc}",
                provider=endpoint.base_url,
                model=request.model,
                cause=exc,
            )
        self._raise_for_status(resp, endpoint)
        payload = self.parse_json_response(
            resp,
            endpoint,
            model=request.model,
            operation="Anthropic chat 请求",
        )
        response = self.parse_response(payload, raw=resp)
        if not response.content and not response.tool_calls:
            raise self.raise_error(
                AIErrorCategory.EMPTY_RESPONSE,
                "Anthropic 端点返回空响应",
                provider=endpoint.base_url,
                model=request.model,
            )
        return response

    async def stream(
        self,
        client: httpx.AsyncClient,
        endpoint: ResolvedEndpoint,
        credential: str,
        request: UnifiedRequest,
        *,
        timeout: Optional[float] = None,
    ) -> AsyncIterator[UnifiedStreamEvent]:
        url = self.resolve_messages_url(endpoint)
        body = self.serialize_request(request)
        body["stream"] = True
        headers = self.build_headers(credential, endpoint)
        headers["Accept"] = "text/event-stream"
        try:
            async with client.stream(
                "POST", url, json=body, headers=headers, timeout=timeout
            ) as resp:
                self.ensure_sse_response(
                    resp,
                    endpoint,
                    model=request.model,
                    operation="Anthropic stream 请求",
                )
                event_type = ""
                current_tool: dict[str, Any] = {}
                async for line in resp.aiter_lines():
                    if not line:
                        continue
                    if line.startswith("event:"):
                        event_type = line[6:].strip()
                        continue
                    if not line.startswith("data:"):
                        continue
                    data = line[5:].strip()
                    try:
                        chunk = json.loads(data)
                    except json.JSONDecodeError:
                        continue
                    event = self._parse_stream_event(event_type, chunk, current_tool)
                    if event is not None:
                        yield event
        except httpx.HTTPError as exc:
            raise self.raise_error(
                AIErrorCategory.NETWORK,
                f"Anthropic stream 请求失败: {exc}",
                provider=endpoint.base_url,
                model=request.model,
                cause=exc,
            )

    @staticmethod
    def _parse_stream_event(
        event_type: str,
        chunk: dict[str, Any],
        current_tool: dict[str, Any],
    ) -> Optional[UnifiedStreamEvent]:
        if event_type == "message_stop":
            return UnifiedStreamEvent(type="done")
        if event_type == "content_block_delta":
            delta = chunk.get("delta") or {}
            dtype = delta.get("type")
            if dtype == "text_delta":
                return UnifiedStreamEvent(type="text_delta", text=delta.get("text") or "")
            if dtype == "input_json_delta":
                current_tool["arguments"] = (
                    current_tool.get("arguments", "") + (delta.get("partial_json") or "")
                )
                return UnifiedStreamEvent(
                    type="tool_call_delta",
                    tool_call=UnifiedToolCall(
                        id=current_tool.get("id", ""),
                        name=current_tool.get("name", ""),
                        arguments=current_tool.get("arguments", ""),
                    ),
                )
        if event_type == "content_block_start":
            block = chunk.get("content_block") or {}
            if block.get("type") == "tool_use":
                current_tool.clear()
                current_tool["id"] = block.get("id", "")
                current_tool["name"] = block.get("name", "")
                current_tool["arguments"] = ""
                return UnifiedStreamEvent(
                    type="tool_call_start",
                    tool_call=UnifiedToolCall(
                        id=block.get("id", ""),
                        name=block.get("name", ""),
                        arguments="",
                    ),
                )
        if event_type == "message_delta":
            usage = chunk.get("usage")
            if usage:
                return UnifiedStreamEvent(
                    type="done",
                    usage=AnthropicNativeAdapter._parse_usage(usage),
                )
        return None

    def supports_capability(self, capability: ModelCapabilitySet) -> bool:
        # Anthropic 原生支持 thinking / effort / tools / streaming / caching
        return True


def _to_int(value: Any) -> Optional[int]:
    if value is None:
        return None
    try:
        result = int(value)
        return result if result > 0 else None
    except (TypeError, ValueError):
        return None
