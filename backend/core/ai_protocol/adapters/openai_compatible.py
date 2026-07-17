"""OpenAI 兼容协议适配器 / OpenAI-compatible protocol adapter.

覆盖 OpenAI 官方、DeepSeek、Qwen、Z.ai、Doubao、Moonshot、MiniMax、
Hunyuan、Yi、Stepfun、Baichuan、OpenRouter、SiliconFlow、Together、Groq、
Fireworks、Perplexity、xAI、Ollama、vLLM、LM Studio 与自定义端点。

Covers OpenAI itself and all OpenAI-compatible providers (DeepSeek, Qwen,
Z.ai, Doubao, Moonshot, MiniMax, Hunyuan, Yi, Stepfun, Baichuan,
OpenRouter, SiliconFlow, Together, Groq, Fireworks, Perplexity, xAI,
Ollama, vLLM, LM Studio, and custom endpoints).
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

# OpenAI 停止原因 → 归一化 / OpenAI stop reasons → normalized
_OPENAI_STOP_MAP: dict[str, StopReason] = {
    "stop": StopReason.END_TURN,
    "length": StopReason.MAX_TOKENS,
    "tool_calls": StopReason.TOOL_USE,
    "function_call": StopReason.TOOL_USE,
}


class OpenAICompatibleAdapter(ProtocolAdapter):
    """OpenAI Chat Completions 协议适配器 / OpenAI Chat Completions adapter."""

    family = ProtocolFamily.OPENAI_COMPATIBLE

    # ------------------------------------------------------------------
    # 鉴权与端点 / Auth & endpoint
    # ------------------------------------------------------------------
    def build_headers(
        self, credential: str, endpoint: ResolvedEndpoint
    ) -> dict[str, str]:
        headers = {
            "Authorization": f"Bearer {credential}",
            "Content-Type": "application/json",
        }
        headers.update(endpoint.extra_headers)
        return headers

    @staticmethod
    def resolve_chat_url(endpoint: ResolvedEndpoint) -> str:
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
        payload = resp.json()
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
        except AIError as exc:
            logger.debug("OpenAI 兼容模型详情获取失败: model={} err={}", model_id, exc)
            return None
        payload = resp.json()
        return self._parse_model_detail(payload, model_id)

    @staticmethod
    def _parse_model_list(payload: Any) -> list[ModelDiscoveryResult]:
        raw_models: Any
        if isinstance(payload, dict):
            raw_models = (
                payload.get("data") or payload.get("models") or payload.get("items") or []
            )
        elif isinstance(payload, list):
            raw_models = payload
        else:
            raw_models = []

        results: list[ModelDiscoveryResult] = []
        for item in raw_models:
            if isinstance(item, str):
                results.append(ModelDiscoveryResult(model_id=item))
            elif isinstance(item, dict):
                model_id = item.get("id") or item.get("name") or item.get("model")
                if model_id:
                    results.append(
                        ModelDiscoveryResult(
                            model_id=str(model_id),
                            display_name=str(item.get("name") or item.get("id") or ""),
                        )
                    )
        # 去重并排序 / dedupe + sort
        seen: set[str] = set()
        unique = []
        for r in results:
            if r.model_id not in seen:
                seen.add(r.model_id)
                unique.append(r)
        unique.sort(key=lambda x: x.model_id)
        return unique

    @staticmethod
    def _parse_model_detail(payload: Any, model_id: str) -> Optional[ModelDiscoveryResult]:
        if not isinstance(payload, dict):
            return None
        ctx_tokens = OpenAICompatibleAdapter._extract_context_tokens(payload)
        max_output = OpenAICompatibleAdapter._extract_max_output(payload)
        return ModelDiscoveryResult(
            model_id=str(payload.get("id") or model_id),
            display_name=str(payload.get("name") or payload.get("id") or model_id),
            context_window_tokens=ctx_tokens,
            max_output_tokens=max_output,
        )

    @staticmethod
    def _extract_context_tokens(payload: dict[str, Any]) -> Optional[int]:
        # 常见字段名（OpenAI / OpenAI 兼容 / vLLM / Together 等）
        for field in (
            "context_length",
            "context_window",
            "max_context_length",
            "max_model_len",
            "context_length_tokens",
        ):
            value = payload.get(field)
            if isinstance(value, (int, float)) and value > 0:
                return int(value)
        # 嵌套 metadata / config
        for container_key in ("metadata", "config", "capabilities", "limits"):
            nested = payload.get(container_key)
            if isinstance(nested, dict):
                for field in (
                    "context_length",
                    "max_model_len",
                    "context_window",
                    "max_context_length",
                ):
                    value = nested.get(field)
                    if isinstance(value, (int, float)) and value > 0:
                        return int(value)
        return None

    @staticmethod
    def _extract_max_output(payload: dict[str, Any]) -> Optional[int]:
        for field in ("max_output_tokens", "max_tokens", "max_completion_tokens"):
            value = payload.get(field)
            if isinstance(value, (int, float)) and value > 0:
                return int(value)
        return None

    # ------------------------------------------------------------------
    # 请求序列化 / Request serialization
    # ------------------------------------------------------------------
    def serialize_request(self, request: UnifiedRequest) -> dict[str, Any]:
        """UnifiedRequest → OpenAI Chat Completions JSON body."""
        body: dict[str, Any] = {
            "model": request.model,
            "messages": [self._serialize_message(m) for m in request.messages],
            "max_tokens": request.max_tokens,
        }
        if request.temperature is not None:
            body["temperature"] = request.temperature
        if request.top_p is not None:
            body["top_p"] = request.top_p
        if request.top_k is not None:
            body["top_k"] = request.top_k
        if request.tools:
            body["tools"] = [self._serialize_tool(t) for t in request.tools]
        if request.tool_choice:
            body["tool_choice"] = self._serialize_tool_choice(request.tool_choice)
        if request.stream:
            body["stream"] = True
        # thinking / effort 等通过 extra body 透传给支持的兼容端点（少数厂商支持）
        if request.thinking is not None:
            body["thinking"] = request.thinking
        if request.effort is not None:
            body["effort"] = request.effort
        return body

    @staticmethod
    def _serialize_message(message: UnifiedMessage) -> dict[str, Any]:
        msg: dict[str, Any] = {"role": message.role}
        if message.content is not None:
            msg["content"] = message.content
        elif message.role == "assistant":
            msg["content"] = None  # 工具调用但无文本 / tool-only assistant turn
        else:
            msg["content"] = ""
        if message.tool_calls:
            msg["tool_calls"] = [
                {
                    "id": tc.id,
                    "type": "function",
                    "function": {"name": tc.name, "arguments": tc.arguments},
                }
                for tc in message.tool_calls
            ]
        if message.tool_call_id:
            msg["tool_call_id"] = message.tool_call_id
        if message.name:
            msg["name"] = message.name
        if message.reasoning_content:
            msg["reasoning_content"] = message.reasoning_content
        return msg

    @staticmethod
    def _serialize_tool(tool: UnifiedTool) -> dict[str, Any]:
        return {
            "type": "function",
            "function": {
                "name": tool.name,
                "description": tool.description,
                "parameters": tool.parameters,
                **({"strict": True} if tool.strict else {}),
            },
        }

    @staticmethod
    def _serialize_tool_choice(choice: str) -> Any:
        if choice in ("auto", "none", "required"):
            return choice
        # 指定工具名 / specific tool name
        return {"type": "function", "function": {"name": choice}}

    # ------------------------------------------------------------------
    # 响应反序列化 / Response deserialization
    # ------------------------------------------------------------------
    def parse_response(
        self, payload: dict[str, Any], raw: Any
    ) -> UnifiedResponse:
        choices = payload.get("choices") or []
        if not choices:
            return UnifiedResponse(
                content="",
                tool_calls=[],
                stop_reason=StopReason.END_TURN,
                usage=self._parse_usage(payload.get("usage")),
                raw=raw,
            )
        choice = choices[0]
        message = choice.get("message") or {}
        content = message.get("content") or ""
        tool_calls = self._parse_tool_calls(message.get("tool_calls"))
        reasoning_content = message.get("reasoning_content")
        stop_reason = _OPENAI_STOP_MAP.get(
            str(choice.get("finish_reason")), StopReason.END_TURN
        )
        usage = self._parse_usage(payload.get("usage"))
        return UnifiedResponse(
            content=content,
            tool_calls=tool_calls,
            stop_reason=stop_reason,
            usage=usage,
            reasoning_content=reasoning_content,
            raw=raw,
        )

    @staticmethod
    def _parse_tool_calls(raw: Any) -> list[UnifiedToolCall]:
        if not raw:
            return []
        result: list[UnifiedToolCall] = []
        for item in raw:
            if isinstance(item, dict):
                function = item.get("function") or {}
                call_id = item.get("id") or ""
                name = function.get("name") or ""
                arguments = function.get("arguments") or ""
                if name:
                    result.append(
                        UnifiedToolCall(id=call_id, name=name, arguments=arguments)
                    )
        return result

    @staticmethod
    def _parse_usage(raw: Any) -> UnifiedUsage:
        if not isinstance(raw, dict):
            return UnifiedUsage()
        return UnifiedUsage(
            input_tokens=int(raw.get("prompt_tokens", 0) or 0),
            output_tokens=int(raw.get("completion_tokens", 0) or 0),
            cache_read_tokens=int(
                raw.get("prompt_tokens_details", {}).get("cached_tokens", 0) or 0
            )
            if isinstance(raw.get("prompt_tokens_details"), dict)
            else 0,
            reasoning_tokens=int(
                raw.get("completion_tokens_details", {}).get("reasoning_tokens", 0) or 0
            )
            if isinstance(raw.get("completion_tokens_details"), dict)
            else 0,
        )

    # ------------------------------------------------------------------
    # HTTP 调用 / HTTP calls
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
                f"OpenAI 兼容请求失败: {exc}",
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

    # ------------------------------------------------------------------
    # chat / stream
    # ------------------------------------------------------------------
    async def chat(
        self,
        client: httpx.AsyncClient,
        endpoint: ResolvedEndpoint,
        credential: str,
        request: UnifiedRequest,
        *,
        timeout: Optional[float] = None,
    ) -> UnifiedResponse:
        url = self.resolve_chat_url(endpoint)
        body = self.serialize_request(request)
        headers = self.build_headers(credential, endpoint)
        try:
            resp = await client.post(url, json=body, headers=headers, timeout=timeout)
        except httpx.HTTPError as exc:
            raise self.raise_error(
                AIErrorCategory.NETWORK,
                f"OpenAI 兼容 chat 请求失败: {exc}",
                provider=endpoint.base_url,
                model=request.model,
                cause=exc,
            )
        self._raise_for_status(resp, endpoint)
        payload = resp.json()
        response = self.parse_response(payload, raw=resp)
        # 空响应检测 / empty-response detection
        if not response.content and not response.tool_calls:
            raise self.raise_error(
                AIErrorCategory.EMPTY_RESPONSE,
                "OpenAI 兼容端点返回空响应",
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
        url = self.resolve_chat_url(endpoint)
        body = self.serialize_request(request)
        body["stream"] = True
        headers = self.build_headers(credential, endpoint)
        headers["Accept"] = "text/event-stream"
        try:
            async with client.stream(
                "POST", url, json=body, headers=headers, timeout=timeout
            ) as resp:
                if resp.status_code >= 400:
                    text = await resp.aread()
                    self._raise_for_status(
                        httpx.Response(
                            resp.status_code, content=text, request=resp.request
                        ),
                        endpoint,
                    )
                async for line in resp.aiter_lines():
                    event = self._parse_sse_line(line)
                    if event is not None:
                        yield event
        except httpx.HTTPError as exc:
            raise self.raise_error(
                AIErrorCategory.NETWORK,
                f"OpenAI 兼容 stream 请求失败: {exc}",
                provider=endpoint.base_url,
                model=request.model,
                cause=exc,
            )

    @staticmethod
    def _parse_sse_line(line: str) -> Optional[UnifiedStreamEvent]:
        if not line or not line.startswith("data:"):
            return None
        data = line[5:].strip()
        if data == "[DONE]":
            return UnifiedStreamEvent(type="done")
        try:
            chunk = json.loads(data)
        except json.JSONDecodeError:
            return None
        choices = chunk.get("choices") or []
        if not choices:
            usage = chunk.get("usage")
            if usage:
                return UnifiedStreamEvent(
                    type="done",
                    usage=OpenAICompatibleAdapter._parse_usage(usage),
                )
            return None
        delta = choices[0].get("delta") or {}
        if delta.get("content"):
            return UnifiedStreamEvent(type="text_delta", text=delta["content"])
        tool_calls = delta.get("tool_calls")
        if tool_calls:
            first = tool_calls[0]
            function = first.get("function") or {}
            return UnifiedStreamEvent(
                type="tool_call_delta",
                tool_call=UnifiedToolCall(
                    id=first.get("id") or "",
                    name=function.get("name") or "",
                    arguments=function.get("arguments") or "",
                ),
            )
        return None

    def supports_capability(self, capability: ModelCapabilitySet) -> bool:
        # OpenAI 兼容协议族基础能力齐备 / base capabilities are widely supported
        return True
