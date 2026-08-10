"""Google Gemini 原生协议适配器 / Google Gemini native protocol adapter.

直接用 httpx 调用 Generative Language API，避免引入 google-generativeai 依赖。
Calls the Generative Language API directly via httpx, avoiding a hard
dependency on google-generativeai.

鉴权: x-goog-api-key 查询参数或 Header
端点: POST /v1beta/models/{model}:generateContent
      POST /v1beta/models/{model}:streamGenerateContent
      GET /v1beta/models
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from typing import Any

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
    safe_provider_event_metadata,
    usage_from_mapping,
)

# Gemini finishReason → 归一化 / Gemini finishReason → normalized
_GEMINI_STOP_MAP: dict[str, StopReason] = {
    "STOP": StopReason.END_TURN,
    "MAX_TOKENS": StopReason.MAX_TOKENS,
    "SAFETY": StopReason.REFUSAL,
    "RECITATION": StopReason.REFUSAL,
    "LANGUAGE": StopReason.REFUSAL,
}


class GeminiNativeAdapter(ProtocolAdapter):
    """Google Gemini generateContent 原生适配器 / Gemini native adapter."""

    family = ProtocolFamily.GEMINI_NATIVE

    def build_headers(
        self, credential: str, endpoint: ResolvedEndpoint
    ) -> dict[str, str]:
        headers = {"Content-Type": "application/json"}
        headers.update(endpoint.extra_headers)
        return headers

    @staticmethod
    def _with_key(url: str, credential: str) -> str:
        sep = "&" if "?" in url else "?"
        return f"{url}{sep}key={credential}"

    @staticmethod
    def resolve_generate_url(endpoint: ResolvedEndpoint, model: str) -> str:
        return f"{endpoint.base_url}models/{model}:generateContent"

    @staticmethod
    def resolve_stream_url(endpoint: ResolvedEndpoint, model: str) -> str:
        return f"{endpoint.base_url}models/{model}:streamGenerateContent"

    @staticmethod
    def resolve_models_url(endpoint: ResolvedEndpoint) -> str:
        return f"{endpoint.base_url}models"

    # ------------------------------------------------------------------
    # 模型发现 / Model discovery
    # ------------------------------------------------------------------
    async def list_models(
        self,
        client: httpx.AsyncClient,
        endpoint: ResolvedEndpoint,
        credential: str,
    ) -> list[ModelDiscoveryResult]:
        url = self._with_key(self.resolve_models_url(endpoint), credential)
        resp = await self._get(client, url, endpoint)
        payload = self.parse_json_response(
            resp,
            endpoint,
            operation="Gemini 模型列表请求",
            allow_list=True,
        )
        return self._parse_model_list(payload)

    async def fetch_model_metadata(
        self,
        client: httpx.AsyncClient,
        endpoint: ResolvedEndpoint,
        credential: str,
        model_id: str,
    ) -> ModelDiscoveryResult | None:
        url = self._with_key(f"{endpoint.base_url}models/{model_id}", credential)
        try:
            resp = await self._get(client, url, endpoint)
            payload = self.parse_json_response(
                resp,
                endpoint,
                operation="Gemini 模型详情请求",
            )
        except AIError as exc:  # type: ignore[name-defined]
            logger.debug("Gemini 模型详情获取失败: model={} err={}", model_id, exc)
            return None
        return self._parse_model_detail(payload, model_id)

    @staticmethod
    def _parse_model_list(payload: Any) -> list[ModelDiscoveryResult]:
        raw = payload.get("models") if isinstance(payload, dict) else None
        if raw is None and isinstance(payload, list):
            raw = payload
        raw = raw or []
        results: list[ModelDiscoveryResult] = []
        for item in raw:
            if isinstance(item, dict):
                name = str(item.get("name") or "").removeprefix("models/")
                display = str(item.get("displayName") or name)
                ctx, max_out = GeminiNativeAdapter._extract_limits(item)
                if name:
                    results.append(
                        ModelDiscoveryResult(
                            model_id=name,
                            display_name=display,
                            context_window_tokens=ctx,
                            max_output_tokens=max_out,
                        )
                    )
        results.sort(key=lambda x: x.model_id)
        return results

    @staticmethod
    def _parse_model_detail(payload: Any, model_id: str) -> ModelDiscoveryResult | None:
        if not isinstance(payload, dict):
            return None
        name = str(payload.get("name") or model_id).removeprefix("models/")
        ctx, max_out = GeminiNativeAdapter._extract_limits(payload)
        return ModelDiscoveryResult(
            model_id=name,
            display_name=str(payload.get("displayName") or name),
            context_window_tokens=ctx,
            max_output_tokens=max_out,
        )

    @staticmethod
    def _extract_limits(payload: dict[str, Any]) -> tuple[int | None, int | None]:
        ctx = None
        max_out = None
        ic = payload.get("inputTokenLimit")
        oc = payload.get("outputTokenLimit")
        if isinstance(ic, (int, float)) and ic > 0:
            ctx = int(ic)
        if isinstance(oc, (int, float)) and oc > 0:
            max_out = int(oc)
        return ctx, max_out

    # ------------------------------------------------------------------
    # 请求序列化 / Request serialization
    # ------------------------------------------------------------------
    def serialize_request(self, request: UnifiedRequest) -> dict[str, Any]:
        """UnifiedRequest → Gemini generateContent body."""
        system, messages = self._split_system(request)
        body: dict[str, Any] = {
            "contents": [self._serialize_message(m) for m in messages],
        }
        if system:
            body["systemInstruction"] = {"parts": [{"text": system}]}
        gen_config: dict[str, Any] = {
            "maxOutputTokens": request.max_tokens,
        }
        if request.temperature is not None:
            gen_config["temperature"] = request.temperature
        if request.top_p is not None:
            gen_config["topP"] = request.top_p
        if request.top_k is not None:
            gen_config["topK"] = request.top_k
        if request.tools:
            body["tools"] = [
                {
                    "functionDeclarations": [
                        self._serialize_tool(t) for t in request.tools
                    ]
                }
            ]
        if request.tool_choice:
            fc = self._serialize_tool_choice(request.tool_choice)
            if fc is not None:
                body["toolConfig"] = fc
        body["generationConfig"] = gen_config
        return body

    @staticmethod
    def _split_system(
        request: UnifiedRequest,
    ) -> tuple[str, list[UnifiedMessage]]:
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
        role = "model" if message.role == "assistant" else "user"
        # 工具结果 / function response
        if message.role == "tool":
            try:
                parsed = json.loads(message.content or "{}")
            except (json.JSONDecodeError, TypeError):
                parsed = {"result": message.content or ""}
            return {
                "role": "user",
                "parts": [
                    {
                        "functionResponse": {
                            "name": message.name or message.tool_call_id or "tool",
                            "response": parsed,
                        }
                    }
                ],
            }
        # assistant 工具调用 / function call parts
        if message.role == "assistant" and message.tool_calls:
            parts: list[dict[str, Any]] = []
            if message.content:
                parts.append({"text": message.content})
            for tc in message.tool_calls:
                try:
                    args = json.loads(tc.arguments) if tc.arguments else {}
                except (json.JSONDecodeError, TypeError):
                    args = {"raw": tc.arguments}
                parts.append({"functionCall": {"name": tc.name, "args": args}})
            return {"role": role, "parts": parts}
        return {
            "role": role,
            "parts": [{"text": message.content or ""}],
        }

    @staticmethod
    def _serialize_tool(tool: UnifiedTool) -> dict[str, Any]:
        return {
            "name": tool.name,
            "description": tool.description,
            "parameters": tool.parameters,
        }

    @staticmethod
    def _serialize_tool_choice(choice: str) -> dict[str, Any] | None:
        if choice in ("auto",):
            return {"functionCallingConfig": {"mode": "AUTO"}}
        if choice in ("none",):
            return {"functionCallingConfig": {"mode": "NONE"}}
        if choice in ("required", "any"):
            return {"functionCallingConfig": {"mode": "ANY"}}
        if choice not in ("", "auto"):
            return {
                "functionCallingConfig": {
                    "mode": "ANY",
                    "allowedFunctionNames": [choice],
                }
            }
        return None

    # ------------------------------------------------------------------
    # 响应反序列化 / Response deserialization
    # ------------------------------------------------------------------
    def parse_response(
        self, payload: dict[str, Any], raw: Any, request_model: str
    ) -> UnifiedResponse:
        candidates = payload.get("candidates") or []
        text_parts: list[str] = []
        tool_calls: list[UnifiedToolCall] = []
        stop_reason = StopReason.END_TURN
        if candidates:
            candidate = candidates[0]
            parts = candidate.get("content", {}).get("parts") or []
            for part in parts:
                if "text" in part:
                    text_parts.append(part["text"])
                elif "functionCall" in part:
                    fc = part["functionCall"]
                    try:
                        arguments = json.dumps(fc.get("args") or {}, ensure_ascii=False)
                    except (TypeError, ValueError):
                        arguments = "{}"
                    tool_calls.append(
                        UnifiedToolCall(
                            id=fc.get("id") or f"call_{fc.get('name', '')}",
                            name=fc.get("name") or "",
                            arguments=arguments,
                        )
                    )
            finish = candidate.get("finishReason")
            stop_reason = _GEMINI_STOP_MAP.get(str(finish), StopReason.END_TURN)
        usage = self._parse_usage(payload.get("usageMetadata"))
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
        return usage_from_mapping(
            {
                "input_tokens": raw.get("promptTokenCount"),
                "output_tokens": raw.get("candidatesTokenCount"),
                "cached_tokens": raw.get("cachedContentTokenCount"),
                "reasoning_tokens": raw.get("thoughtsTokenCount"),
            }
        )

    # ------------------------------------------------------------------
    # HTTP / chat / stream
    # ------------------------------------------------------------------
    async def _get(
        self,
        client: httpx.AsyncClient,
        url: str,
        endpoint: ResolvedEndpoint,
    ) -> httpx.Response:
        headers = self.build_headers("", endpoint)
        try:
            resp = await client.get(url, headers=headers, timeout=15)
        except httpx.HTTPError as exc:
            raise self.raise_error(
                AIErrorCategory.NETWORK,
                f"Gemini 请求失败: {exc}",
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
            # Gemini 的 401 一律视为 API key 无效
            return AIErrorCategory.AUTH_INVALID, message
        if status_code == 403:
            return AIErrorCategory.PERMISSION_DENIED, message
        if status_code == 404:
            return AIErrorCategory.MODEL_NOT_FOUND, message or "模型或端点不存在"
        if status_code == 400:
            if classify_context_overflow(message_lower):
                return AIErrorCategory.CONTEXT_OVERFLOW, message
            return AIErrorCategory.BAD_REQUEST, message
        if status_code == 429:
            return AIErrorCategory.RATE_LIMITED, message or "速率限制"
        if status_code == 503:
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
        timeout: float | None = None,
    ) -> UnifiedResponse:
        base = self.resolve_generate_url(endpoint, request.model)
        url = self._with_key(base, credential)
        body = self.serialize_request(request)
        headers = self.build_headers(credential, endpoint)
        try:
            resp = await client.post(url, json=body, headers=headers, timeout=timeout)
        except httpx.HTTPError as exc:
            raise self.raise_error(
                AIErrorCategory.NETWORK,
                f"Gemini chat 请求失败: {exc}",
                provider=endpoint.base_url,
                model=request.model,
                cause=exc,
            )
        self._raise_for_status(resp, endpoint)
        payload = self.parse_json_response(
            resp,
            endpoint,
            model=request.model,
            operation="Gemini chat 请求",
        )
        response = self.parse_response(payload, raw=resp, request_model=request.model)
        if not response.content and not response.tool_calls:
            # Gemini 在安全拒绝时可能返回空 candidates
            if response.stop_reason == StopReason.REFUSAL:
                raise self.raise_error(
                    AIErrorCategory.REFUSAL,
                    "Gemini 安全拒绝 / safety refusal",
                    provider=endpoint.base_url,
                    model=request.model,
                )
            raise self.raise_error(
                AIErrorCategory.EMPTY_RESPONSE,
                "Gemini 端点返回空响应",
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
        base = self.resolve_stream_url(endpoint, request.model)
        url = self._with_key(
            f"{base}?alt=sse" if "?" not in base else f"{base}&alt=sse",
            credential,
        )
        body = self.serialize_request(request)
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
                    operation="Gemini stream 请求",
                )
                async for line in resp.aiter_lines():
                    if not line.startswith("data:"):
                        continue
                    data = line[5:].strip()
                    try:
                        chunk = json.loads(data)
                    except json.JSONDecodeError:
                        continue
                    event = self._parse_stream_chunk(chunk)
                    if event is not None:
                        yield event
        except httpx.HTTPError as exc:
            raise self.raise_error(
                AIErrorCategory.NETWORK,
                f"Gemini stream 请求失败: {exc}",
                provider=endpoint.base_url,
                model=request.model,
                cause=exc,
            )

    @staticmethod
    def _parse_stream_chunk(chunk: dict[str, Any]) -> UnifiedStreamEvent | None:
        candidates = chunk.get("candidates") or []
        if candidates:
            parts = candidates[0].get("content", {}).get("parts") or []
            for part in parts:
                thought = part.get("thought")
                signature = part.get("thoughtSignature")
                if isinstance(thought, bool) and thought:
                    if isinstance(part.get("text"), str) and part["text"]:
                        return UnifiedStreamEvent(
                            type="reasoning_delta",
                            text=part["text"],
                            reasoning_availability="provider_exposed",
                            provider_event_metadata=safe_provider_event_metadata(
                                {
                                    "event": "candidate.content.part",
                                    "item_type": "thought",
                                }
                            ),
                        )
                    return UnifiedStreamEvent(
                        type="reasoning_start",
                        text=None,
                        reasoning_availability="omitted",
                        provider_event_metadata=safe_provider_event_metadata(
                            {"event": "candidate.content.part", "item_type": "thought"}
                        ),
                    )
                if signature is not None:
                    return UnifiedStreamEvent(
                        type="reasoning_end",
                        text=None,
                        reasoning_availability="encrypted_opaque",
                        provider_event_metadata=safe_provider_event_metadata(
                            {
                                "event": "candidate.content.part",
                                "item_type": "thoughtSignature",
                                "signature_present": True,
                                "encrypted": True,
                            }
                        ),
                    )
                if "text" in part and isinstance(part["text"], str):
                    return UnifiedStreamEvent(type="text_delta", text=part["text"])
                if "functionCall" in part:
                    fc = part["functionCall"]
                    try:
                        arguments = json.dumps(fc.get("args") or {}, ensure_ascii=False)
                    except (TypeError, ValueError):
                        arguments = "{}"
                    return UnifiedStreamEvent(
                        type="tool_call_start",
                        tool_call=UnifiedToolCall(
                            id=fc.get("id") or f"call_{fc.get('name', '')}",
                            name=fc.get("name") or "",
                            arguments=arguments,
                        ),
                    )
        usage = chunk.get("usageMetadata")
        if usage:
            candidates = chunk.get("candidates") or []
            finish_reason = (
                candidates[0].get("finishReason")
                if candidates and isinstance(candidates[0], dict)
                else None
            )
            return UnifiedStreamEvent(
                type="done",
                usage=GeminiNativeAdapter._parse_usage(usage),
                stop_reason=_GEMINI_STOP_MAP.get(
                    str(finish_reason), StopReason.END_TURN
                ),
            )
        return None

    def supports_capability(self, capability: ModelCapabilitySet) -> bool:
        # Gemini 原生支持 tools / streaming / vision；不支持 reasoning_content
        return True
