"""OpenAI Responses 协议适配器 / OpenAI Responses protocol adapter.

Responses API 与 Chat Completions 不是 URL 差异：它使用 typed input/output
items、``max_output_tokens``、``reasoning`` 与 typed SSE events。本适配器提供
Sakura 内部 UnifiedRequest/UnifiedResponse 与 Responses wire format 的转换，
使 OpenAI/xAI/Groq/MiniMax 等支持 Responses 的账号可作为独立协议参与故障转移。
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from typing import Any

import httpx

from backend.core.ai_protocol.adapters.openai_compatible import OpenAICompatibleAdapter
from backend.core.ai_protocol.models import (
    AIErrorCategory,
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
    safe_provider_event_metadata,
    usage_from_mapping,
)

_RESPONSES_STATUS_MAP: dict[str, StopReason] = {
    "completed": StopReason.END_TURN,
    "incomplete": StopReason.MAX_TOKENS,
}


class OpenAIResponsesAdapter(OpenAICompatibleAdapter):
    """OpenAI Responses API 适配器 / OpenAI Responses API adapter."""

    family = ProtocolFamily.OPENAI_RESPONSES

    @staticmethod
    def resolve_chat_url(endpoint: ResolvedEndpoint) -> str:
        return f"{endpoint.base_url}{endpoint.chat_path}"

    # 模型发现复用 OpenAI-compatible /models 端点。
    async def list_models(
        self,
        client: httpx.AsyncClient,
        endpoint: ResolvedEndpoint,
        credential: str,
    ) -> list[ModelDiscoveryResult]:
        return await super().list_models(client, endpoint, credential)

    async def fetch_model_metadata(
        self,
        client: httpx.AsyncClient,
        endpoint: ResolvedEndpoint,
        credential: str,
        model_id: str,
    ) -> ModelDiscoveryResult | None:
        return await super().fetch_model_metadata(
            client, endpoint, credential, model_id
        )

    def serialize_request(self, request: UnifiedRequest) -> dict[str, Any]:
        """UnifiedRequest → Responses JSON body."""
        instructions, input_items = self._serialize_input(
            request.messages, request.system
        )
        body: dict[str, Any] = {
            "model": request.model,
            "input": input_items,
            "max_output_tokens": request.max_tokens,
        }
        if instructions:
            body["instructions"] = instructions
        if request.temperature is not None:
            body["temperature"] = request.temperature
        if request.top_p is not None:
            body["top_p"] = request.top_p
        if request.tools:
            body["tools"] = [self._serialize_response_tool(t) for t in request.tools]
        if request.tool_choice:
            body["tool_choice"] = self._serialize_response_tool_choice(
                request.tool_choice
            )
        if request.stream:
            body["stream"] = True
        if request.effort:
            body["reasoning"] = {"effort": request.effort}
        if request.thinking:
            # 兼容部分 Responses 风格网关的 reasoning 配置。
            body.setdefault("reasoning", {}).update(request.thinking)
        return body

    @staticmethod
    def _serialize_input(
        messages: list[UnifiedMessage], explicit_system: str | None
    ) -> tuple[str, list[dict[str, Any]]]:
        instructions: list[str] = []
        if explicit_system:
            instructions.append(explicit_system)
        input_items: list[dict[str, Any]] = []
        for msg in messages:
            if msg.role == "system":
                if msg.content:
                    instructions.append(msg.content)
                continue
            if msg.tool_calls:
                # Responses function_call items are typed output items. When replaying
                # previous assistant tool calls, preserve them as function_call items.
                for tc in msg.tool_calls:
                    input_items.append(
                        {
                            "type": "function_call",
                            "call_id": tc.id,
                            "name": tc.name,
                            "arguments": tc.arguments,
                        }
                    )
                if msg.content:
                    input_items.append({"role": "assistant", "content": msg.content})
                continue
            if msg.role == "tool":
                input_items.append(
                    {
                        "type": "function_call_output",
                        "call_id": msg.tool_call_id or "",
                        "output": msg.content or "",
                    }
                )
                continue
            if msg.images and msg.role != "tool":
                # Responses 多模态消息：input_text + input_image / multimodal input
                content_items: list[dict[str, Any]] = []
                if msg.content:
                    content_items.append({"type": "input_text", "text": msg.content})
                for image in msg.images:
                    if image.data:
                        url = f"data:{image.media_type or 'image/png'};base64,{image.data}"
                    elif image.url:
                        url = image.url
                    else:
                        continue
                    content_items.append({"type": "input_image", "image_url": url})
                input_items.append({"role": msg.role, "content": content_items})
                continue
            input_items.append({"role": msg.role, "content": msg.content or ""})
        return "\n\n".join(instructions), input_items

    @staticmethod
    def _serialize_response_tool(tool: UnifiedTool) -> dict[str, Any]:
        body = {
            "type": "function",
            "name": tool.name,
            "description": tool.description,
            "parameters": tool.parameters,
        }
        if tool.strict:
            body["strict"] = True
        return body

    @staticmethod
    def _serialize_response_tool_choice(choice: str) -> Any:
        if choice in ("auto", "none", "required"):
            return choice
        return {"type": "function", "name": choice}

    def parse_response(self, payload: dict[str, Any], raw: Any) -> UnifiedResponse:
        """Responses JSON → UnifiedResponse."""
        content = self._extract_output_text(payload)
        tool_calls = self._parse_response_tool_calls(payload.get("output"))
        usage = self._parse_responses_usage(payload.get("usage"))
        status = str(payload.get("status") or "completed")
        stop_reason = _RESPONSES_STATUS_MAP.get(status, StopReason.END_TURN)
        incomplete = payload.get("incomplete_details")
        if (
            isinstance(incomplete, dict)
            and incomplete.get("reason") == "max_output_tokens"
        ):
            stop_reason = StopReason.MAX_TOKENS
        return UnifiedResponse(
            content=content,
            tool_calls=tool_calls,
            stop_reason=stop_reason,
            usage=usage,
            raw=raw,
        )

    @staticmethod
    def _extract_output_text(payload: dict[str, Any]) -> str:
        text = payload.get("output_text")
        if isinstance(text, str) and text:
            return text
        parts: list[str] = []
        for item in payload.get("output") or []:
            if not isinstance(item, dict):
                continue
            if item.get("type") == "message":
                for block in item.get("content") or []:
                    if not isinstance(block, dict):
                        continue
                    value = block.get("text") or block.get("output_text")
                    if isinstance(value, str):
                        parts.append(value)
            elif item.get("type") in ("output_text", "text"):
                value = item.get("text") or item.get("content")
                if isinstance(value, str):
                    parts.append(value)
        return "".join(parts)

    @staticmethod
    def _parse_response_tool_calls(raw: Any) -> list[UnifiedToolCall]:
        if not raw:
            return []
        result: list[UnifiedToolCall] = []
        for item in raw:
            if not isinstance(item, dict) or item.get("type") != "function_call":
                continue
            name = str(item.get("name") or "")
            if not name:
                continue
            args = item.get("arguments") or ""
            if not isinstance(args, str):
                args = json.dumps(args, ensure_ascii=False)
            result.append(
                UnifiedToolCall(
                    id=str(item.get("call_id") or item.get("id") or ""),
                    name=name,
                    arguments=args,
                )
            )
        return result

    @staticmethod
    def _parse_responses_usage(raw: Any) -> UnifiedUsage:
        return usage_from_mapping(raw)

    async def chat(
        self,
        client: httpx.AsyncClient,
        endpoint: ResolvedEndpoint,
        credential: str,
        request: UnifiedRequest,
        *,
        timeout: float | None = None,
    ) -> UnifiedResponse:
        url = self.resolve_chat_url(endpoint)
        body = self.serialize_request(request)
        headers = self.build_headers(credential, endpoint)
        try:
            resp = await client.post(url, json=body, headers=headers, timeout=timeout)
        except httpx.HTTPError as exc:
            raise self.raise_error(
                AIErrorCategory.NETWORK,
                f"OpenAI Responses 请求失败: {exc}",
                provider=endpoint.base_url,
                model=request.model,
                cause=exc,
            )
        self._raise_for_status(resp, endpoint)
        payload = self.parse_json_response(
            resp,
            endpoint,
            model=request.model,
            operation="OpenAI Responses 请求",
        )
        if str(payload.get("status") or "").lower() == "failed":
            raise self.raise_error(
                AIErrorCategory.SERVER_ERROR,
                "OpenAI Responses 端点报告请求失败",
                provider=endpoint.base_url,
                model=request.model,
            )
        response = self.parse_response(payload, raw=resp)
        if not response.content and not response.tool_calls:
            raise self.raise_error(
                AIErrorCategory.EMPTY_RESPONSE,
                "OpenAI Responses 端点返回空响应",
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
        timeout: float | None = None,
    ) -> AsyncIterator[UnifiedStreamEvent]:
        url = self.resolve_chat_url(endpoint)
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
                    operation="OpenAI Responses stream 请求",
                )
                async for line in resp.aiter_lines():
                    event = self._parse_responses_sse_line(line)
                    if event is not None:
                        if event.type == "error":
                            raise self.raise_error(
                                AIErrorCategory.SERVER_ERROR,
                                event.error or "OpenAI Responses 流报告请求失败",
                                provider=endpoint.base_url,
                                model=request.model,
                            )
                        yield event
        except httpx.HTTPError as exc:
            raise self.raise_error(
                AIErrorCategory.NETWORK,
                f"OpenAI Responses stream 请求失败: {exc}",
                provider=endpoint.base_url,
                model=request.model,
                cause=exc,
            )

    @staticmethod
    def _parse_responses_sse_line(line: str) -> UnifiedStreamEvent | None:
        if not line or not line.startswith("data:"):
            return None
        data = line[5:].strip()
        if data == "[DONE]":
            return UnifiedStreamEvent(type="done")
        try:
            chunk = json.loads(data)
        except json.JSONDecodeError:
            return None
        event_type = str(chunk.get("type") or "")
        metadata = safe_provider_event_metadata({"event": event_type})
        if event_type in ("response.output_text.delta", "response.text.delta"):
            return UnifiedStreamEvent(
                type="text_delta", text=str(chunk.get("delta") or "")
            )
        if event_type in ("response.reasoning_summary_text.delta",):
            delta = chunk.get("delta")
            if isinstance(delta, str) and delta:
                return UnifiedStreamEvent(
                    type="reasoning_delta",
                    text=delta,
                    reasoning_availability="summarized",
                    provider_event_metadata=metadata,
                )
            return UnifiedStreamEvent(
                type="reasoning_end",
                text=None,
                reasoning_availability="summarized",
                provider_event_metadata=metadata,
            )
        if event_type in {
            "response.reasoning_summary_text.done",
            "response.reasoning_summary_text.end",
        }:
            return UnifiedStreamEvent(
                type="reasoning_end",
                text=None,
                reasoning_availability="summarized",
                provider_event_metadata=metadata,
            )
        if event_type in {
            "response.reasoning.started",
            "response.reasoning_summary_part.added",
            "response.reasoning_summary_text.added",
            "response.reasoning_item.added",
            "response.reasoning_summary_text.created",
        }:
            return UnifiedStreamEvent(
                type="reasoning_start",
                text=None,
                reasoning_availability="omitted",
                provider_event_metadata=metadata,
            )
        if event_type in (
            "response.completed",
            "response.done",
            "response.incomplete",
        ):
            usage = None
            response = chunk.get("response")
            if isinstance(response, dict):
                usage = OpenAIResponsesAdapter._parse_responses_usage(
                    response.get("usage")
                )
            return UnifiedStreamEvent(
                type="done",
                usage=usage,
                stop_reason=(
                    StopReason.MAX_TOKENS
                    if event_type == "response.incomplete"
                    else StopReason.END_TURN
                ),
                provider_event_metadata=metadata,
            )
        if event_type in {"response.failed", "error"}:
            return UnifiedStreamEvent(
                type="error",
                error="OpenAI Responses 流报告请求失败",
                provider_event_metadata=metadata,
            )
        if event_type == "response.output_item.added":
            item = chunk.get("item")
            if isinstance(item, dict) and item.get("type") == "function_call":
                return UnifiedStreamEvent(
                    type="tool_call_start",
                    tool_call=UnifiedToolCall(
                        id=str(item.get("call_id") or item.get("id") or ""),
                        name=str(item.get("name") or ""),
                        arguments=str(item.get("arguments") or ""),
                    ),
                    provider_event_metadata=metadata,
                )
        if event_type == "response.function_call_arguments.delta":
            return UnifiedStreamEvent(
                type="tool_call_delta",
                text=str(chunk.get("delta") or ""),
                tool_call=UnifiedToolCall(
                    id=str(chunk.get("call_id") or chunk.get("item_id") or ""),
                    name=str(chunk.get("name") or ""),
                    arguments=str(chunk.get("delta") or ""),
                ),
                provider_event_metadata=metadata,
            )
        return None


__all__ = ["OpenAIResponsesAdapter"]
