"""AI 提供商目录与协议族元数据 / AI provider catalog & protocol-family metadata.

本模块在协议层（backend.core.ai_protocol）之上，提供面向配置层与 UI 的
“提供商目录”。旧代码保留 `AIProvider` / `AI_PROVIDERS` / `get_ai_provider` /
`list_ai_providers` / `get_provider_select_options` / `build_models_url` 等兼容
入口，但其数据源已切换为新的协议族目录 `BUILTIN_PROVIDERS`。

This module sits above the protocol layer (backend.core.ai_protocol) and exposes
the provider catalog to configuration/UI code. Legacy entry points
(`AIProvider`, `AI_PROVIDERS`, `get_ai_provider`, ...) are preserved for
backward compatibility, but their data source is now the protocol-family-driven
catalog `BUILTIN_PROVIDERS`.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any

from backend.core.ai_protocol.models import (
    AuthScheme,
    BillingMode,
    BuiltinModel,
    ModelCapabilitySet,
    ProtocolFamily,
    ProviderDeclaration,
    ReasoningParams,
    UsageScope,
)
from backend.core.ai_protocol.registry import ensure_trailing_slash, resolve_endpoint

# =============================================================================
# 旧版数据类（兼容入口）/ Legacy dataclass (compat shim)
# =============================================================================


@dataclass(frozen=True)
class AIProvider:
    """旧版提供商元数据（兼容入口）/ Legacy provider metadata (compat shim).

    新代码应直接使用 backend.core.ai_protocol.models.ProviderDeclaration。
    New code should use backend.core.ai_protocol.models.ProviderDeclaration.
    """

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


# =============================================================================
# 能力与推理参数预设 / Capability & reasoning-param presets
# =============================================================================

# 上下文窗口常量（tokens）/ Context-window constants
_CTX_200K = 200_000
_CTX_256K = 256_000
_CTX_500K = 500_000
_CTX_1M = 1_000_000
_CTX_1_05M = 1_050_000
_CTX_GEMINI = 1_048_576

# 能力预设 / Capability presets
_CAP_TEXT = ModelCapabilitySet()
_CAP_TOOLS = ModelCapabilitySet(tools=True)
_CAP_VISION_TOOLS = ModelCapabilitySet(vision=True, tools=True)
# 推理模型（DeepSeek-R1 / Qwen 风格 reasoning_content）
_CAP_REASON = ModelCapabilitySet(
    vision=False, tools=True, reasoning_content=True, effort=True, temperature=False
)
_CAP_REASON_VISION = ModelCapabilitySet(
    vision=True, tools=True, reasoning_content=True, effort=True, temperature=False
)
# Anthropic / GLM / Kimi 风格 thinking
_CAP_THINK_VISION = ModelCapabilitySet(
    vision=True, tools=True, thinking=True, effort=True, temperature=False
)


def _params(max_out: int, **kw: Any) -> ReasoningParams:
    """快捷构造 ReasoningParams / Quick ReasoningParams builder."""
    return ReasoningParams(max_output_tokens=max_out, **kw)


# =============================================================================
# 内置提供商目录 / Built-in provider catalog
# =============================================================================

_BUILTIN: list[ProviderDeclaration] = [
    # ─────────────────────────────────────────────────────────────────────
    # 国际主要厂商 / International vendors
    # ─────────────────────────────────────────────────────────────────────
    ProviderDeclaration(
        id="openai",
        label="OpenAI",
        family=ProtocolFamily.OPENAI_RESPONSES,
        base_url="https://api.openai.com/v1",
        auth_scheme=AuthScheme.BEARER,
        website="https://platform.openai.com",
        default_models=["gpt-5.6-sol", "gpt-5.6-terra", "gpt-5.6-luna"],
        endpoints={
            ProtocolFamily.OPENAI_COMPATIBLE: "https://api.openai.com/v1",
        },
        models=[
            BuiltinModel(
                model_id="gpt-5.6-sol",
                display_name="GPT-5.6 Sol",
                context_window_tokens=_CTX_1_05M,
                max_output_tokens=131072,
                capabilities=_CAP_VISION_TOOLS,
                reasoning_params=_params(131072, effort="medium"),
                aliases=["gpt-5.6"],
                notes="Flagship; supports text+image input, reasoning effort.",
            ),
            BuiltinModel(
                model_id="gpt-5.6-terra",
                display_name="GPT-5.6 Terra (balanced)",
                context_window_tokens=_CTX_1_05M,
                max_output_tokens=131072,
                capabilities=_CAP_VISION_TOOLS,
                reasoning_params=_params(131072, effort="medium"),
            ),
            BuiltinModel(
                model_id="gpt-5.6-luna",
                display_name="GPT-5.6 Luna (low cost)",
                context_window_tokens=_CTX_1_05M,
                max_output_tokens=131072,
                capabilities=_CAP_VISION_TOOLS,
                reasoning_params=_params(131072, effort="low"),
            ),
        ],
        notes="Official Chat Completions + Responses API. reasoning effort: none/low/medium/high/xhigh/max.",
    ),
    ProviderDeclaration(
        id="anthropic",
        label="Anthropic (Claude)",
        family=ProtocolFamily.ANTHROPIC_NATIVE,
        base_url="https://api.anthropic.com/v1",
        auth_scheme=AuthScheme.X_API_KEY,
        website="https://console.anthropic.com",
        default_models=[
            "claude-fable-5",
            "claude-sonnet-5",
            "claude-opus-4-8",
            "claude-haiku-4-5-20251001",
        ],
        endpoints={
            ProtocolFamily.OPENAI_COMPATIBLE: "https://api.anthropic.com/v1/",
        },
        models=[
            BuiltinModel(
                model_id="claude-fable-5",
                display_name="Claude Fable 5 (highest capability)",
                context_window_tokens=_CTX_1M,
                max_output_tokens=131072,
                capabilities=_CAP_THINK_VISION,
                reasoning_params=_params(131072, thinking={"type": "adaptive"}),
            ),
            BuiltinModel(
                model_id="claude-sonnet-5",
                display_name="Claude Sonnet 5 (balanced)",
                context_window_tokens=_CTX_1M,
                max_output_tokens=131072,
                capabilities=_CAP_THINK_VISION,
                reasoning_params=_params(131072, thinking={"type": "adaptive"}),
            ),
            BuiltinModel(
                model_id="claude-opus-4-8",
                display_name="Claude Opus 4.8 (agent/coding)",
                context_window_tokens=_CTX_1M,
                max_output_tokens=131072,
                capabilities=_CAP_THINK_VISION,
                reasoning_params=_params(131072, thinking={"type": "adaptive"}),
            ),
            BuiltinModel(
                model_id="claude-haiku-4-5-20251001",
                display_name="Claude Haiku 4.5 (fast)",
                context_window_tokens=_CTX_200K,
                max_output_tokens=65536,
                capabilities=_CAP_THINK_VISION,
                reasoning_params=_params(65536),
            ),
        ],
        notes="Native Messages API with content blocks, thinking & signatures. "
        "OpenAI-compat layer exists but native is preferred for production.",
    ),
    ProviderDeclaration(
        id="google",
        label="Google AI Studio (Gemini)",
        family=ProtocolFamily.GEMINI_NATIVE,
        base_url="https://generativelanguage.googleapis.com/v1beta/",
        auth_scheme=AuthScheme.GOOGLE_ADC,
        website="https://ai.google.dev",
        default_models=["gemini-3.5-flash"],
        endpoints={
            ProtocolFamily.OPENAI_COMPATIBLE: "https://generativelanguage.googleapis.com/v1beta/openai/",
        },
        models=[
            BuiltinModel(
                model_id="gemini-3.5-flash",
                display_name="Gemini 3.5 Flash",
                context_window_tokens=_CTX_GEMINI,
                max_output_tokens=65536,
                capabilities=ModelCapabilitySet(
                    vision=True, tools=True, thinking=True, effort=True,
                    temperature=True, top_p=True, top_k=True,
                ),
                reasoning_params=_params(65536, effort="medium"),
                notes="Multimodal: text/image/video/audio/PDF input. Thinking cannot be disabled on Gemini 3.",
            ),
        ],
        notes="Native generateContent API. OpenAI-compat layer maps reasoning_effort to thinking_level.",
    ),
    ProviderDeclaration(
        id="xai",
        label="xAI (Grok)",
        family=ProtocolFamily.OPENAI_COMPATIBLE,
        base_url="https://api.x.ai/v1",
        auth_scheme=AuthScheme.BEARER,
        website="https://x.ai",
        default_models=["grok-4.5", "grok-4.5-latest"],
        endpoints={
            ProtocolFamily.OPENAI_RESPONSES: "https://api.x.ai/v1",
        },
        models=[
            BuiltinModel(
                model_id="grok-4.5",
                display_name="Grok 4.5",
                context_window_tokens=_CTX_500K,
                max_output_tokens=32768,
                capabilities=_CAP_VISION_TOOLS,
                reasoning_params=_params(32768, effort="medium"),
                aliases=["grok-4.5-latest"],
            ),
        ],
        notes="Chat Completions + Responses. Supports thinking, tools, search.",
    ),
    ProviderDeclaration(
        id="mistral",
        label="Mistral AI",
        family=ProtocolFamily.OPENAI_COMPATIBLE,
        base_url="https://api.mistral.ai/v1",
        auth_scheme=AuthScheme.BEARER,
        website="https://mistral.ai",
        default_models=["mistral-medium-3-5", "mistral-small-2603"],
        models=[
            BuiltinModel(
                model_id="mistral-medium-3-5",
                display_name="Mistral Medium 3.5",
                context_window_tokens=_CTX_256K,
                max_output_tokens=32768,
                capabilities=_CAP_TOOLS,
                reasoning_params=_params(32768, effort="medium"),
            ),
            BuiltinModel(
                model_id="mistral-small-2603",
                display_name="Mistral Small (2603)",
                context_window_tokens=_CTX_256K,
                max_output_tokens=32768,
                capabilities=_CAP_TEXT,
                reasoning_params=_params(32768),
            ),
        ],
        notes="Chat Completions. Model aliases change frequently; refresh /models at startup.",
    ),
    # ─────────────────────────────────────────────────────────────────────
    # 国内主要厂商 / Domestic vendors
    # ─────────────────────────────────────────────────────────────────────
    ProviderDeclaration(
        id="deepseek",
        label="DeepSeek",
        family=ProtocolFamily.OPENAI_COMPATIBLE,
        base_url="https://api.deepseek.com",
        auth_scheme=AuthScheme.BEARER,
        website="https://platform.deepseek.com",
        default_models=["deepseek-v4-pro", "deepseek-v4-flash"],
        endpoints={
            ProtocolFamily.ANTHROPIC_NATIVE: "https://api.deepseek.com/anthropic",
        },
        models=[
            BuiltinModel(
                model_id="deepseek-v4-pro",
                display_name="DeepSeek V4 Pro",
                context_window_tokens=_CTX_1M,
                max_output_tokens=393216,
                capabilities=_CAP_REASON,
                reasoning_params=_params(393216, effort="medium"),
                notes="Thinking enabled by default. Returns reasoning_content.",
            ),
            BuiltinModel(
                model_id="deepseek-v4-flash",
                display_name="DeepSeek V4 Flash",
                context_window_tokens=_CTX_1M,
                max_output_tokens=393216,
                capabilities=_CAP_REASON,
                reasoning_params=_params(393216, effort="low"),
            ),
        ],
        notes="deepseek-chat/reasoner deprecated 2026-07-24. Anthropic-compat layer "
        "does not support image/document blocks. Must replay reasoning_content in "
        "tool loops.",
    ),
    ProviderDeclaration(
        id="qwen",
        label="Qwen / DashScope",
        family=ProtocolFamily.OPENAI_COMPATIBLE,
        base_url="https://dashscope.aliyuncs.com/compatible-mode/v1",
        auth_scheme=AuthScheme.BEARER,
        website="https://help.aliyun.com/zh/model-studio/",
        default_models=[
            "qwen3.7-plus",
            "qwen3.7-max-2026-06-08",
            "qwen3.6-flash",
        ],
        endpoints={
            ProtocolFamily.OPENAI_RESPONSES: "https://dashscope.aliyuncs.com/compatible-mode/v1",
            ProtocolFamily.ANTHROPIC_NATIVE: "https://dashscope.aliyuncs.com/apps/anthropic",
        },
        models=[
            BuiltinModel(
                model_id="qwen3.7-plus",
                display_name="Qwen 3.7 Plus",
                context_window_tokens=_CTX_1M,
                max_output_tokens=16384,
                capabilities=_CAP_REASON_VISION,
                reasoning_params=_params(16384, effort="medium"),
            ),
            BuiltinModel(
                model_id="qwen3.7-max-2026-06-08",
                display_name="Qwen 3.7 Max",
                context_window_tokens=_CTX_1M,
                max_output_tokens=16384,
                capabilities=_CAP_REASON_VISION,
                reasoning_params=_params(16384, effort="high"),
            ),
            BuiltinModel(
                model_id="qwen3.6-flash",
                display_name="Qwen 3.6 Flash",
                context_window_tokens=_CTX_1M,
                max_output_tokens=8192,
                capabilities=_CAP_REASON_VISION,
                reasoning_params=_params(8192, effort="low"),
            ),
        ],
        notes="Region variants: cn / intl / us. enable_thinking (Chat) or reasoning.effort (Responses).",
    ),
    ProviderDeclaration(
        id="glm",
        label="Zhipu GLM",
        family=ProtocolFamily.OPENAI_COMPATIBLE,
        base_url="https://open.bigmodel.cn/api/paas/v4",
        auth_scheme=AuthScheme.BEARER,
        website="https://open.bigmodel.cn",
        default_models=["glm-5.2", "glm-5-turbo", "glm-4.7"],
        endpoints={
            ProtocolFamily.ANTHROPIC_NATIVE: "https://open.bigmodel.cn/api/anthropic",
        },
        models=[
            BuiltinModel(
                model_id="glm-5.2",
                display_name="GLM 5.2",
                context_window_tokens=_CTX_1M,
                max_output_tokens=131072,
                capabilities=_CAP_REASON,
                reasoning_params=_params(131072, effort="high"),
                aliases=["glm-5.1", "glm-5"],
                notes="Historical glm-5.1 / glm-5 auto-redirect to glm-5.2.",
            ),
            BuiltinModel(
                model_id="glm-5-turbo",
                display_name="GLM 5 Turbo",
                context_window_tokens=_CTX_1M,
                max_output_tokens=32768,
                capabilities=_CAP_REASON,
                reasoning_params=_params(32768, effort="medium"),
            ),
            BuiltinModel(
                model_id="glm-4.7",
                display_name="GLM 4.7",
                context_window_tokens=_CTX_1M,
                max_output_tokens=32768,
                capabilities=_CAP_REASON,
                reasoning_params=_params(32768),
            ),
        ],
        notes="Distinguish modelContextWindow (1M upstream) from clientEffectiveContextWindow "
        "(e.g. 204,800 for Coding Plan). Vision via GLM-V MCP, not native image input.",
    ),
    ProviderDeclaration(
        id="minimax",
        label="MiniMax",
        family=ProtocolFamily.OPENAI_COMPATIBLE,
        base_url="https://api.minimaxi.com/v1",
        auth_scheme=AuthScheme.BEARER,
        website="https://platform.minimaxi.com",
        default_models=["MiniMax-M3"],
        endpoints={
            ProtocolFamily.OPENAI_RESPONSES: "https://api.minimaxi.com/v1",
            ProtocolFamily.ANTHROPIC_NATIVE: "https://api.minimaxi.com/anthropic",
        },
        models=[
            BuiltinModel(
                model_id="MiniMax-M3",
                display_name="MiniMax M3",
                context_window_tokens=_CTX_1M,
                max_output_tokens=32768,
                capabilities=ModelCapabilitySet(
                    vision=True, tools=True, thinking=True, temperature=True, top_p=True,
                ),
                reasoning_params=_params(32768, thinking={"type": "disabled"}),
                notes="Thinking off by default; enable via thinking={type:adaptive}.",
            ),
        ],
        notes="Anthropic-compat: replay full content (thinking+text+tool_use). M3 supports image/video.",
    ),
    ProviderDeclaration(
        id="moonshot",
        label="Moonshot / Kimi",
        family=ProtocolFamily.OPENAI_COMPATIBLE,
        base_url="https://api.moonshot.ai/v1",
        auth_scheme=AuthScheme.BEARER,
        website="https://platform.moonshot.ai",
        default_models=["kimi-k2.7-code", "kimi-k2.7-code-highspeed", "kimi-k2.6"],
        endpoints={
            ProtocolFamily.ANTHROPIC_NATIVE: "https://api.moonshot.ai/anthropic",
        },
        models=[
            BuiltinModel(
                model_id="kimi-k2.7-code",
                display_name="Kimi K2.7 Code",
                context_window_tokens=_CTX_256K,
                max_output_tokens=32768,
                capabilities=_CAP_REASON,
                reasoning_params=_params(32768, effort="high"),
                notes="Thinking forced on. Default max_tokens=32K; compaction window 262144.",
            ),
            BuiltinModel(
                model_id="kimi-k2.7-code-highspeed",
                display_name="Kimi K2.7 Code Highspeed",
                context_window_tokens=_CTX_256K,
                max_output_tokens=32768,
                capabilities=_CAP_REASON,
                reasoning_params=_params(32768, effort="medium"),
            ),
            BuiltinModel(
                model_id="kimi-k2.6",
                display_name="Kimi K2.6 (multimodal)",
                context_window_tokens=_CTX_256K,
                max_output_tokens=32768,
                capabilities=_CAP_REASON_VISION,
                reasoning_params=_params(32768),
                notes="Thinking optional.",
            ),
        ],
        notes="CN endpoint: https://api.moonshot.cn/v1. K2.7 thinking cannot be disabled.",
    ),
    ProviderDeclaration(
        id="doubao",
        label="Doubao / Volcano Engine Ark",
        family=ProtocolFamily.OPENAI_COMPATIBLE,
        base_url="https://ark.cn-beijing.volces.com/api/v3",
        auth_scheme=AuthScheme.BEARER,
        website="https://www.volcengine.com/product/ark",
        default_models=["doubao-seed-1-8"],
        models=[
            BuiltinModel(
                model_id="doubao-seed-1-8",
                display_name="Doubao Seed 1.8",
                context_window_tokens=_CTX_256K,
                max_output_tokens=32768,
                capabilities=_CAP_TOOLS,
                reasoning_params=_params(32768),
            ),
        ],
        notes="model field is typically the account's deployment Endpoint ID (ep-xxx). "
        "Region cn-beijing. Do not assume a shared model slug across accounts.",
    ),
    ProviderDeclaration(
        id="hunyuan",
        label="Tencent Hunyuan",
        family=ProtocolFamily.OPENAI_COMPATIBLE,
        base_url="https://api.hunyuan.cloud.tencent.com/v1",
        auth_scheme=AuthScheme.BEARER,
        website="https://cloud.tencent.com/product/hunyuan",
        default_models=["hunyuan-turbos-latest"],
        models=[
            BuiltinModel(
                model_id="hunyuan-turbos-latest",
                display_name="Hunyuan Turbo S",
                context_window_tokens=_CTX_256K,
                max_output_tokens=8192,
                capabilities=_CAP_TOOLS,
                reasoning_params=_params(8192),
            ),
        ],
    ),
    # ─────────────────────────────────────────────────────────────────────
    # Coding Plan / Token Plan 套餐（交互式编程专用）/ Coding/Token plans
    # ─────────────────────────────────────────────────────────────────────
    ProviderDeclaration(
        id="qwen-coding-plan",
        label="Qwen Coding Plan (阿里云百炼)",
        family=ProtocolFamily.OPENAI_COMPATIBLE,
        base_url="https://coding.dashscope.aliyuncs.com/v1",
        auth_scheme=AuthScheme.BEARER,
        website="https://help.aliyun.com/zh/model-studio/coding-plan",
        billing_mode=BillingMode.CODING_PLAN,
        usage_scope=UsageScope.INTERACTIVE_CODING_ONLY,
        usage_scope_note=(
            "仅限交互式编程工具。禁止用于自定义后端、机器人、自动化脚本、批处理、"
            "SaaS 服务。违规可能导致套餐暂停或 Key 封禁。模型 ID 为精确白名单，"
            "区分大小写。"
        ),
        default_models=[
            "qwen3.7-plus",
            "qwen3.6-plus",
            "kimi-k2.5",
            "glm-5",
            "MiniMax-M2.5",
            "qwen3.5-plus",
            "qwen3-coder-next",
            "qwen3-coder-plus",
        ],
        endpoints={
            ProtocolFamily.ANTHROPIC_NATIVE: "https://coding.dashscope.aliyuncs.com/apps/anthropic",
        },
        notes="Model whitelist is exact & case-sensitive. Intl: coding-intl.dashscope.aliyuncs.com.",
    ),
    ProviderDeclaration(
        id="glm-coding-plan",
        label="GLM Coding Plan (智谱)",
        family=ProtocolFamily.OPENAI_COMPATIBLE,
        base_url="https://open.bigmodel.cn/api/coding/paas/v4",
        auth_scheme=AuthScheme.BEARER,
        website="https://docs.bigmodel.cn/cn/coding-plan/overview",
        billing_mode=BillingMode.CODING_PLAN,
        usage_scope=UsageScope.INTERACTIVE_CODING_ONLY,
        usage_scope_note=(
            "仅允许在官方支持的 Coding 工具与产品环境中使用；自建网站、机器人、"
            "SaaS 或普通 API 后端应改用标准按量 API。"
        ),
        default_models=["glm-5.2", "glm-5-turbo", "glm-4.7"],
        endpoints={
            ProtocolFamily.ANTHROPIC_NATIVE: "https://open.bigmodel.cn/api/anthropic",
        },
        notes="glm-5.1 / glm-5 auto-redirect to glm-5.2. Vision via GLM-V MCP server.",
    ),
    ProviderDeclaration(
        id="minimax-token-plan",
        label="MiniMax Token Plan",
        family=ProtocolFamily.OPENAI_COMPATIBLE,
        base_url="https://api.minimaxi.com/v1",
        auth_scheme=AuthScheme.BEARER,
        website="https://platform.minimaxi.com/docs/token-plan/quickstart",
        billing_mode=BillingMode.TOKEN_PLAN,
        usage_scope=UsageScope.PERSONAL_INTERACTIVE_CODING,
        usage_scope_note="独立订阅 Key 与资源池；协议端点与普通 API 相同。生产前以实际套餐协议为准。",
        default_models=["MiniMax-M3"],
        endpoints={
            ProtocolFamily.ANTHROPIC_NATIVE: "https://api.minimaxi.com/anthropic",
        },
    ),
    # ─────────────────────────────────────────────────────────────────────
    # 聚合网关 / Aggregators
    # ─────────────────────────────────────────────────────────────────────
    ProviderDeclaration(
        id="openrouter",
        label="OpenRouter",
        family=ProtocolFamily.OPENAI_COMPATIBLE,
        base_url="https://openrouter.ai/api/v1",
        auth_scheme=AuthScheme.BEARER,
        website="https://openrouter.ai",
        default_models=[
            "anthropic/claude-sonnet-5",
            "openai/gpt-5.6-sol",
            "deepseek/deepseek-v4-pro",
        ],
        endpoints={
            ProtocolFamily.ANTHROPIC_NATIVE: "https://openrouter.ai/api",
        },
        notes="Dynamic /models catalog. Model IDs prefixed with vendor/. "
        "Anthropic skin supports thinking blocks & native tools.",
    ),
    ProviderDeclaration(
        id="siliconflow",
        label="SiliconFlow",
        family=ProtocolFamily.OPENAI_COMPATIBLE,
        base_url="https://api.siliconflow.cn/v1",
        auth_scheme=AuthScheme.BEARER,
        website="https://siliconflow.cn",
        default_models=[
            "deepseek-ai/DeepSeek-V3",
            "Qwen/Qwen3-235B-A22B",
        ],
    ),
    ProviderDeclaration(
        id="together",
        label="Together AI",
        family=ProtocolFamily.OPENAI_COMPATIBLE,
        base_url="https://api.together.ai/v1",
        auth_scheme=AuthScheme.BEARER,
        website="https://www.together.ai",
        default_models=["meta-llama/Llama-3.3-70B-Instruct-Turbo"],
        notes="Context window per Together deployment. reasoning returned in message.reasoning.",
    ),
    ProviderDeclaration(
        id="groq",
        label="Groq",
        family=ProtocolFamily.OPENAI_COMPATIBLE,
        base_url="https://api.groq.com/openai/v1",
        auth_scheme=AuthScheme.BEARER,
        website="https://groq.com",
        default_models=["llama-3.3-70b-versatile", "deepseek-r1-distill-llama-70b"],
        endpoints={
            ProtocolFamily.OPENAI_RESPONSES: "https://api.groq.com/openai/v1",
        },
        notes="Supports Chat Completions + Responses. Dynamic model catalog.",
    ),
    ProviderDeclaration(
        id="fireworks",
        label="Fireworks AI",
        family=ProtocolFamily.OPENAI_COMPATIBLE,
        base_url="https://api.fireworks.ai/inference/v1",
        auth_scheme=AuthScheme.BEARER,
        website="https://fireworks.ai",
        default_models=["accounts/fireworks/models/llama-v3p3-70b-instruct"],
        endpoints={
            ProtocolFamily.ANTHROPIC_NATIVE: "https://api.fireworks.ai/inference",
        },
        notes="Anthropic SDK base URL has no /v1. max effort maps to high. No adaptive thinking.",
    ),
    ProviderDeclaration(
        id="perplexity",
        label="Perplexity",
        family=ProtocolFamily.OPENAI_COMPATIBLE,
        base_url="https://api.perplexity.ai",
        auth_scheme=AuthScheme.BEARER,
        website="https://docs.perplexity.ai",
        default_models=["sonar", "sonar-pro"],
    ),
    # ─────────────────────────────────────────────────────────────────────
    # 本地 / 自托管 / Local & self-hosted
    # ─────────────────────────────────────────────────────────────────────
    ProviderDeclaration(
        id="ollama",
        label="Ollama (local)",
        family=ProtocolFamily.OPENAI_COMPATIBLE,
        base_url="http://localhost:11434/v1",
        auth_scheme=AuthScheme.BEARER,
        default_models=["qwen3:32b", "deepseek-r1:32b"],
        supports_model_discovery=True,
        notes="Local; API key optional.",
    ),
    ProviderDeclaration(
        id="vllm",
        label="vLLM (local)",
        family=ProtocolFamily.OPENAI_COMPATIBLE,
        base_url="http://localhost:8000/v1",
        auth_scheme=AuthScheme.BEARER,
        default_models=[],
        notes="Local; API key optional.",
    ),
    ProviderDeclaration(
        id="lmstudio",
        label="LM Studio (local)",
        family=ProtocolFamily.OPENAI_COMPATIBLE,
        base_url="http://localhost:1234/v1",
        auth_scheme=AuthScheme.BEARER,
        default_models=[],
        notes="Local; API key optional.",
    ),
    # ─────────────────────────────────────────────────────────────────────
    # 自定义 / Custom
    # ─────────────────────────────────────────────────────────────────────
    ProviderDeclaration(
        id="custom",
        label="Custom OpenAI Compatible",
        family=ProtocolFamily.OPENAI_COMPATIBLE,
        base_url="",
        auth_scheme=AuthScheme.BEARER,
        default_models=[],
        supports_model_discovery=True,
        notes="User-supplied OpenAI-compatible base URL.",
    ),
    ProviderDeclaration(
        id="custom-anthropic",
        label="Custom Anthropic Compatible",
        family=ProtocolFamily.ANTHROPIC_NATIVE,
        base_url="",
        auth_scheme=AuthScheme.X_API_KEY,
        default_models=[],
        supports_model_discovery=True,
        notes="User-supplied Anthropic Messages-compatible base URL.",
    ),
]

BUILTIN_PROVIDERS: dict[str, ProviderDeclaration] = {p.id: p for p in _BUILTIN}
# 旧内置 key 兼容：zai 曾代表智谱 GLM。新 UI 使用 glm，旧配置/测试仍可解析 zai。
BUILTIN_PROVIDERS["zai"] = BUILTIN_PROVIDERS["glm"]

# 旧版扁平字典（兼容）/ Legacy flat dict (compat)
# 从新目录派生，旧字段尽可能映射；旧 anthropic/gemini 仍保留 id 兼容历史配置值。
_LEGACY_OVERRIDE: dict[str, dict[str, Any]] = {
    "gemini": {
        # 旧配置中 gemini 走 OpenAI 兼容端点；新目录默认原生。保留旧端点作为兼容回退。
        "base_url": "https://generativelanguage.googleapis.com/v1beta/openai",
        "default_model": "gemini-2.0-flash",
    },
}


def _to_legacy_view(decl: ProviderDeclaration) -> AIProvider:
    """新声明 → 旧 AIProvider 视图 / Project new declaration to legacy AIProvider."""
    override = _LEGACY_OVERRIDE.get(decl.id, {})
    supports_list = decl.supports_model_discovery
    # 旧视图默认支持 context window 提取，Anthropic/Gemini 在旧逻辑中曾被关闭
    supports_ctx = True
    if decl.id == "anthropic":
        supports_ctx = True  # 新协议族已支持 /v1/models 元数据
    default_model = (
        override.get("default_model")
        or (decl.default_models[0] if decl.default_models else "")
    )
    base_url = override.get("base_url") or decl.base_url
    notes = decl.notes
    if decl.family != ProtocolFamily.OPENAI_COMPATIBLE and not notes:
        notes = f"Native {decl.family.value} API."
    return AIProvider(
        id=decl.id,
        label=decl.label,
        base_url=base_url,
        default_model=default_model,
        models_endpoint="models",
        model_detail_endpoint="models/{model}",
        supports_model_list=supports_list,
        supports_context_window=supports_ctx,
        notes=notes,
    )


AI_PROVIDERS: dict[str, AIProvider] = {
    decl.id: _to_legacy_view(decl) for decl in _BUILTIN
}

# 别名：旧 gemini 配置 key 仍保留 OpenAI 兼容视图，避免历史配置失效。
# 新部署推荐使用 'google'（原生 generateContent API）。
AI_PROVIDERS["gemini"] = AIProvider(
    id="gemini",
    label="Gemini (OpenAI Compatible)",
    base_url="https://generativelanguage.googleapis.com/v1beta/openai",
    default_model="gemini-2.0-flash",
    models_endpoint="models",
    model_detail_endpoint="models/{model}",
    supports_model_list=True,
    supports_context_window=False,
    notes="Legacy OpenAI-compatible Gemini endpoint. Prefer 'google' for native API.",
)

# 别名：旧 zai 配置 key 映射到新 glm，避免历史配置失效。
# 目录已将原 'zai' 重命名为 'glm' 以与官方品牌一致。
_zai_legacy = _to_legacy_view(BUILTIN_PROVIDERS["glm"])
AI_PROVIDERS["zai"] = AIProvider(
    id="zai",
    label=f"{_zai_legacy.label} (legacy id 'zai')",
    base_url=_zai_legacy.base_url,
    default_model=_zai_legacy.default_model,
    models_endpoint=_zai_legacy.models_endpoint,
    model_detail_endpoint=_zai_legacy.model_detail_endpoint,
    supports_model_list=_zai_legacy.supports_model_list,
    supports_context_window=_zai_legacy.supports_context_window,
    notes="Legacy provider id 'zai' now maps to 'glm'. Update config to use 'glm'.",
)


_CONTEXT_WINDOW_FIELDS = (
    "context_length",
    "max_context_length",
    "max_model_len",
    "context_window",
    "input_token_limit",
    "max_input_tokens",
    "max_sequence_length",
)


# =============================================================================
# 公共 API（兼容旧调用方）/ Public API (backward compatible)
# =============================================================================


def list_builtin_providers() -> list[ProviderDeclaration]:
    """返回内置提供商声明列表（新 API）/ Return built-in provider declarations."""
    return list(_BUILTIN)


def get_builtin_provider(provider_id: str | None) -> ProviderDeclaration:
    """按 id 获取内置声明，未知或空回退到 custom / Get declaration, fallback to custom."""
    if not provider_id:
        return BUILTIN_PROVIDERS["custom"]
    return BUILTIN_PROVIDERS.get(provider_id.lower().strip(), BUILTIN_PROVIDERS["custom"])


# =============================================================================
# 目录序列化（供新 AI 配置页使用）/ Catalog serialization for the new config page
# =============================================================================


def _capability_to_dict(caps: Any) -> dict[str, bool]:
    """ModelCapabilitySet → dict（供 UI 展示）/ Caps → dict for UI."""
    return {
        "vision": caps.vision,
        "tools": caps.tools,
        "streaming": caps.streaming,
        "reasoning_content": caps.reasoning_content,
        "thinking": caps.thinking,
        "effort": caps.effort,
        "temperature": caps.temperature,
        "top_p": caps.top_p,
        "top_k": caps.top_k,
    }


def _builtin_model_to_dict(model: Any) -> dict[str, Any]:
    """BuiltinModel → dict / BuiltinModel → dict."""
    return {
        "model_id": model.model_id,
        "display_name": model.display_name or model.model_id,
        "context_window_tokens": model.context_window_tokens,
        "max_output_tokens": model.max_output_tokens,
        "capabilities": _capability_to_dict(model.capabilities),
        "aliases": list(model.aliases),
        "notes": model.notes,
    }


def provider_declaration_to_dict(decl: ProviderDeclaration) -> dict[str, Any]:
    """提供商声明 → 可序列化 dict（供 WebUI/API）/ Declaration → serializable dict."""
    endpoints: dict[str, str] = {}
    for family, url in decl.endpoints.items():
        endpoints[family.value] = url
    return {
        "id": decl.id,
        "label": decl.label,
        "family": decl.family.value,
        "base_url": decl.base_url,
        "auth_scheme": decl.auth_scheme.value,
        "website": decl.website,
        "default_models": list(decl.default_models),
        "supports_model_discovery": decl.supports_model_discovery,
        "notes": decl.notes,
        "endpoints": endpoints,
        "billing_mode": decl.billing_mode.value,
        "usage_scope": decl.usage_scope.value,
        "usage_scope_note": decl.usage_scope_note,
        "models": [_builtin_model_to_dict(m) for m in decl.models],
        "supported_families": [f.value for f in decl.supported_families()],
    }


def list_provider_catalog() -> list[dict[str, Any]]:
    """返回完整提供商目录（含模型元数据），供 AI 配置页渲染.

    Return the full provider catalog with per-model metadata for the config page.
    """
    return [provider_declaration_to_dict(decl) for decl in _BUILTIN]


def get_provider_catalog_entry(provider_id: str | None) -> dict[str, Any]:
    """返回单个提供商目录条目 / Return a single catalog entry."""
    return provider_declaration_to_dict(get_builtin_provider(provider_id))


def list_ai_providers(include_summary_follow: bool = False) -> list[dict[str, Any]]:
    """List configured AI providers for API/UI use (legacy-compatible)."""
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


# =============================================================================
# URL 构建（保留给旧 setup_service 调用）/ URL builders (legacy compat)
# =============================================================================


def _build_base_url(
    provider_id: str | None, api_base: str | None = None
) -> tuple[AIProvider, str]:
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
    """Build a provider model-list URL (legacy compat, OpenAI-style endpoints)."""
    provider, base_url = _build_base_url(provider_id, api_base)
    endpoint = _strip_endpoint_prefix(provider.models_endpoint)
    return f"{base_url}{endpoint}"


def build_model_detail_url(
    provider_id: str | None, model: str, api_base: str | None = None
) -> str:
    """Build a provider model-detail URL (legacy compat)."""
    provider, base_url = _build_base_url(provider_id, api_base)
    endpoint = _strip_endpoint_prefix(
        provider.model_detail_endpoint.format(model=model)
    )
    return f"{base_url}{endpoint}"


def build_discovery_endpoint(
    provider_id: str | None, api_base: str | None = None
) -> tuple[ProviderDeclaration, Any]:
    """新 API：返回 (声明, ResolvedEndpoint) 用于模型发现 / New API for discovery."""
    decl = get_builtin_provider(provider_id)
    endpoint = resolve_endpoint(decl, api_base)
    return decl, endpoint


def build_chat_endpoint(
    provider_id: str | None, api_base: str | None = None
) -> tuple[ProviderDeclaration, Any]:
    """新 API：返回 (声明, ResolvedEndpoint) 用于 chat / New API for chat."""
    return build_discovery_endpoint(provider_id, api_base)


def ensure_base_url_trailing_slash(base_url: str) -> str:
    """公开的 base_url 规范化（供 setup/config 复用）/ Public trailing-slash helper."""
    return ensure_trailing_slash(base_url)


def normalize_model_list_response(payload: Any) -> list[str]:
    """Normalize common OpenAI-compatible model list payloads to model IDs."""
    raw_models: Any
    if isinstance(payload, dict):
        raw_models = (
            payload.get("data") or payload.get("models") or payload.get("items") or []
        )
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
        except TypeError, ValueError:
            continue
        if numeric <= 0:
            continue
        return max(1, round(numeric / 1000)) if numeric > 2000 else numeric
    return None
