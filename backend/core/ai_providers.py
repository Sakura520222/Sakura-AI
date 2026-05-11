"""AI provider registry and OpenAI-compatible model metadata helpers."""

from dataclasses import asdict, dataclass
from typing import Any


@dataclass(frozen=True)
class AIProvider:
    """Metadata for a chat-completion model provider."""

    id: str
    label: str
    base_url: str
    default_model: str
    models_endpoint: str = "models"
    model_detail_endpoint: str = "models/{model}"
    supports_model_list: bool = True
    supports_context_window: bool = True
    notes: str = ""

    def to_public_dict(self) -> dict[str, Any]:
        """Return public provider metadata for UI/API responses."""
        return asdict(self)


AI_PROVIDERS: dict[str, AIProvider] = {
    "openai": AIProvider(
        id="openai",
        label="OpenAI",
        base_url="https://api.openai.com/v1",
        default_model="gpt-4o-mini",
    ),
    "deepseek": AIProvider(
        id="deepseek",
        label="DeepSeek",
        base_url="https://api.deepseek.com/v1",
        default_model="deepseek-chat",
    ),
    "qwen": AIProvider(
        id="qwen",
        label="Qwen / DashScope",
        base_url="https://dashscope.aliyuncs.com/compatible-mode/v1",
        default_model="qwen-plus",
    ),
    "zai": AIProvider(
        id="zai",
        label="Z.ai / GLM",
        base_url="https://open.bigmodel.cn/api/paas/v4",
        default_model="glm-4-plus",
    ),
    "doubao": AIProvider(
        id="doubao",
        label="Doubao / Volcano Engine",
        base_url="https://ark.cn-beijing.volces.com/api/v3",
        default_model="doubao-seed-1-6-250615",
    ),
    "siliconflow": AIProvider(
        id="siliconflow",
        label="SiliconFlow",
        base_url="https://api.siliconflow.cn/v1",
        default_model="deepseek-ai/DeepSeek-V3",
    ),
    "gemini": AIProvider(
        id="gemini",
        label="Gemini (OpenAI Compatible)",
        base_url="https://generativelanguage.googleapis.com/v1beta/openai",
        default_model="gemini-2.0-flash",
    ),
    "anthropic": AIProvider(
        id="anthropic",
        label="Claude / Anthropic Compatible",
        base_url="https://api.anthropic.com/v1",
        default_model="claude-3-5-sonnet-20241022",
        supports_model_list=False,
        supports_context_window=False,
        notes="Native Anthropic API is not fully OpenAI-compatible; use a compatible gateway when needed.",
    ),
    "custom": AIProvider(
        id="custom",
        label="Custom OpenAI Compatible",
        base_url="",
        default_model="",
        supports_context_window=False,
    ),
}

_CONTEXT_WINDOW_FIELDS = (
    "context_length",
    "max_context_length",
    "max_model_len",
    "context_window",
    "input_token_limit",
    "max_input_tokens",
    "max_sequence_length",
)


def list_ai_providers(include_summary_follow: bool = False) -> list[dict[str, Any]]:
    """List configured AI providers for API/UI use."""
    providers = [provider.to_public_dict() for provider in AI_PROVIDERS.values()]
    if include_summary_follow:
        summary_follow = AIProvider(
            id="",
            label="跟随主模型 / Follow main model",
            base_url="",
            default_model="",
            models_endpoint="models",
            model_detail_endpoint="models/{model}",
            supports_model_list=False,
            supports_context_window=False,
            notes="",
        )
        providers.insert(0, summary_follow.to_public_dict())
    return providers


def get_ai_provider(provider_id: str | None) -> AIProvider:
    """Return provider metadata, falling back to custom for unknown values."""
    if not provider_id:
        return AI_PROVIDERS["custom"]
    return AI_PROVIDERS.get(provider_id.lower().strip(), AI_PROVIDERS["custom"])


def get_provider_select_options(
    include_summary_follow: bool = False,
    include_main_ai: bool = False,
) -> list[dict[str, str]]:
    """Return options for dynamic config select fields."""
    options = [
        {"value": provider.id, "label": provider.label}
        for provider in AI_PROVIDERS.values()
    ]
    if include_main_ai:
        options.insert(0, {"value": "main", "label": "复用主 AI / Use main AI"})
    if include_summary_follow:
        options.insert(0, {"value": "", "label": "跟随主模型"})
    return options


def _build_base_url(provider_id: str | None, api_base: str | None = None) -> tuple[AIProvider, str]:
    """Return (provider, base_url_with_trailing_slash) for URL building."""
    provider = get_ai_provider(provider_id)
    base_url = (api_base or provider.base_url or "https://api.openai.com/v1").strip()
    if not base_url.endswith("/"):
        base_url += "/"
    return provider, base_url


def _strip_endpoint_prefix(endpoint: str) -> str:
    """Remove one leading slash from provider endpoints."""
    if endpoint.startswith("/"):
        return endpoint[1:]
    return endpoint


def build_models_url(provider_id: str | None, api_base: str | None = None) -> str:
    """Build a provider model-list URL."""
    provider, base_url = _build_base_url(provider_id, api_base)
    endpoint = _strip_endpoint_prefix(provider.models_endpoint)
    return f"{base_url}{endpoint}"


def build_model_detail_url(
    provider_id: str | None, model: str, api_base: str | None = None
) -> str:
    """Build a provider model-detail URL."""
    provider, base_url = _build_base_url(provider_id, api_base)
    endpoint = _strip_endpoint_prefix(provider.model_detail_endpoint.format(model=model))
    return f"{base_url}{endpoint}"


def normalize_model_list_response(payload: Any) -> list[str]:
    """Normalize common OpenAI-compatible model list payloads to model IDs."""
    raw_models: Any
    if isinstance(payload, dict):
        raw_models = payload.get("data") or payload.get("models") or payload.get("items") or []
    elif isinstance(payload, list):
        raw_models = payload
    else:
        raw_models = []

    model_ids: list[str] = []
    for item in raw_models:
        if isinstance(item, str):
            model_ids.append(item)
        elif isinstance(item, dict):
            model_id = item.get("id") or item.get("name") or item.get("model")
            if model_id:
                model_ids.append(str(model_id))
    return sorted(set(model_ids))


def extract_context_window_k(payload: Any) -> int | None:
    """Extract context window size in K tokens from common model metadata fields."""
    if not isinstance(payload, dict):
        return None

    candidates: list[Any] = []
    candidates.extend(payload.get(field) for field in _CONTEXT_WINDOW_FIELDS)
    for container_key in ("metadata", "capabilities", "limits"):
        nested = payload.get(container_key)
        if isinstance(nested, dict):
            candidates.extend(nested.get(field) for field in _CONTEXT_WINDOW_FIELDS)

    for value in candidates:
        if value is None:
            continue
        try:
            numeric = int(float(str(value).strip()))
        except (TypeError, ValueError):
            continue
        if numeric <= 0:
            continue
        return max(1, round(numeric / 1000)) if numeric > 2000 else numeric
    return None
