"""协议层核心数据结构 / Core data models for the protocol layer.

定义与具体厂商无关的统一中间表示。适配器负责在这些结构与本厂商的
wire format 之间双向转换。
These dataclasses are the vendor-neutral intermediate representation.
Adapters translate between these structures and each vendor's wire format.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from enum import Enum
from typing import Any
from urllib.parse import urlsplit

# 未发现或未配置单模型元数据时的默认上下文窗口（tokens）。
# Default context window when a model has no discovered or user-provided metadata.
DEFAULT_CONTEXT_WINDOW_TOKENS = 128_000


# =============================================================================
# 枚举 / Enums
# =============================================================================


class ProtocolFamily(str, Enum):
    """协议族 / Protocol family."""

    OPENAI_COMPATIBLE = "openai-compatible"
    OPENAI_RESPONSES = "openai_responses"
    ANTHROPIC_NATIVE = "anthropic-native"
    GEMINI_NATIVE = "gemini-native"
    # 预留：云平台托管（本期不实现运行时适配 / reserved for later）
    BEDROCK = "bedrock"
    VERTEX = "vertex"
    FOUNDRY = "foundry"


class AuthScheme(str, Enum):
    """鉴权方式 / Authentication scheme."""

    BEARER = "bearer"
    X_API_KEY = "x_api_key"
    GOOGLE_ADC = "google_adc"


class AIErrorCategory(str, Enum):
    """归一化错误类别 / Normalized error category."""

    AUTH_INVALID = "auth_invalid"
    PERMISSION_DENIED = "permission_denied"
    MODEL_NOT_FOUND = "model_not_found"
    BAD_REQUEST = "bad_request"
    CONTEXT_OVERFLOW = "context_overflow"
    RATE_LIMITED = "rate_limited"
    SERVER_ERROR = "server_error"
    OVERLOADED = "overloaded"
    NETWORK = "network"
    EMPTY_RESPONSE = "empty_response"
    REFUSAL = "refusal"
    UNKNOWN = "unknown"


class MetadataSource(str, Enum):
    """模型元数据来源 / Source of model metadata."""

    BUILTIN = "builtin"
    DISCOVERED = "discovered"
    USER_OVERRIDE = "user_override"
    FALLBACK = "fallback"  # 保守兜底 / conservative fallback


class StopReason(str, Enum):
    """归一化停止原因 / Normalized stop reason."""

    END_TURN = "end_turn"
    TOOL_USE = "tool_use"
    MAX_TOKENS = "max_tokens"
    CONTEXT_OVERFLOW = "context_overflow"
    REFUSAL = "refusal"
    PAUSE_TURN = "pause_turn"


# =============================================================================
# 能力与推理参数 / Capabilities & reasoning params
# =============================================================================


@dataclass(frozen=True)
class ModelCapabilitySet:
    """模型能力集 / Model capability set.

    每个布尔表示该模型是否支持对应特性；不支持的字段在构建请求时被剔除。
    Each flag marks whether the model supports the feature; unsupported fields
    are stripped when building requests.
    """

    vision: bool = False
    tools: bool = True
    streaming: bool = True
    reasoning_content: bool = False  # DeepSeek-R1 风格 / DeepSeek-R1 style
    thinking: bool = False  # Anthropic adaptive thinking
    effort: bool = False  # effort 参数 / effort parameter
    prompt_caching: bool = False
    temperature: bool = True
    top_p: bool = True
    top_k: bool = False


@dataclass(frozen=True)
class ReasoningParams:
    """推理参数 / Reasoning parameters.

    所有模型必填 max_output_tokens；其余字段按模型能力可选。
    max_output_tokens is required for all models; the rest are optional and
    filtered by model capability.
    """

    max_output_tokens: int = 4096
    temperature: float | None = None
    top_p: float | None = None
    top_k: int | None = None
    thinking: dict[str, Any] | None = None  # e.g. {"type": "adaptive"}
    effort: str | None = None  # "low".."max"


# =============================================================================
# 计费模式与使用范围 / Billing mode & usage scope
# =============================================================================


class BillingMode(str, Enum):
    """提供商计费模式 / Provider billing mode.

    文件.md 要求区分按量 API、Coding Plan 与 Token Plan——它们的资源池、
    Key、可用模型白名单和使用约束不同，不能复用。
    """

    PAYG = "payg"  # 按量计费 / pay-as-you-go
    CODING_PLAN = "coding_plan"  # 交互式编程套餐 / interactive coding plan
    TOKEN_PLAN = "token_plan"  # Token 团队版套餐 / token team plan


class UsageScope(str, Enum):
    """套餐使用范围约束 / Usage-scope constraint.

    Coding Plan 类套餐通常禁止用于自定义后端/机器人/批处理/SaaS，需在目录与
    配置页明确标注，避免用户误用导致 Key 被封禁。
    """

    GENERAL_API = "general_api"
    INTERACTIVE_CODING_ONLY = "interactive_coding_only"
    PERSONAL_INTERACTIVE_CODING = "personal_interactive_coding"


# =============================================================================
# 提供商与模型元数据 / Provider & model metadata
# =============================================================================


@dataclass(frozen=True)
class ProviderDeclaration:
    """内置提供商声明 / Built-in provider declaration.

    ``base_url`` / ``family`` / ``auth_scheme`` 描述默认（主）协议端点。
    ``endpoints`` 为可选的多协议端点映射：例如 DeepSeek 同时提供 OpenAI 兼容
    与 Anthropic 兼容端点，Qwen/GLM/Kimi/MiniMax 同理。键为协议族，值为该
    协议的 base_url。
    """

    id: str
    label: str
    family: ProtocolFamily
    base_url: str
    auth_scheme: AuthScheme
    default_models: list[str] = field(default_factory=list)
    supports_model_discovery: bool = True
    notes: str = ""
    # 多协议端点 / multi-protocol endpoints: {family: base_url}
    endpoints: dict[ProtocolFamily, str] = field(default_factory=dict)
    # 计费模式与使用范围 / billing mode & usage scope
    billing_mode: BillingMode = BillingMode.PAYG
    usage_scope: UsageScope = UsageScope.GENERAL_API
    usage_scope_note: str = ""
    website: str = ""
    # 内置模型元数据（旗舰模型）/ built-in per-model metadata
    models: list[BuiltinModel] = field(default_factory=list)

    def endpoint_for(self, family: ProtocolFamily) -> tuple[str, AuthScheme]:
        """返回指定协议族的 (base_url, auth_scheme) / Resolve endpoint for a family.

        优先取 endpoints[family]，否则回退到默认 base_url。
        """
        if family in self.endpoints:
            return self.endpoints[family], self.auth_scheme
        return self.base_url, self.auth_scheme

    def supported_families(self) -> list[ProtocolFamily]:
        """返回该提供商支持的协议族列表 / List supported protocol families."""
        families = {self.family}
        families.update(self.endpoints.keys())
        return sorted(families, key=lambda f: f.value)


@dataclass(frozen=True)
class BuiltinModel:
    """内置模型元数据条目 / Built-in model metadata entry.

    旗舰模型在目录中预填上下文窗口、最大输出、能力与推理参数，使用户无需
    动态发现即可获得准确配置。``aliases`` 标注自动切换的移动别名。
    """

    model_id: str
    display_name: str = ""
    context_window_tokens: int = 0
    max_output_tokens: int = 0
    capabilities: ModelCapabilitySet = field(default_factory=ModelCapabilitySet)
    reasoning_params: ReasoningParams = field(default_factory=ReasoningParams)
    aliases: list[str] = field(default_factory=list)
    notes: str = ""

    def to_metadata(
        self,
        provider_id: str,
        source: MetadataSource = MetadataSource.BUILTIN,
    ) -> ModelMetadata:
        """转换为 ModelMetadata / Convert to ModelMetadata."""
        return ModelMetadata(
            model_id=self.model_id,
            provider_id=provider_id,
            display_name=self.display_name or self.model_id,
            context_window_tokens=(
                self.context_window_tokens or DEFAULT_CONTEXT_WINDOW_TOKENS
            ),
            max_output_tokens=self.max_output_tokens or 4096,
            capabilities=self.capabilities,
            reasoning_params=self.reasoning_params,
            source=source,
        )


@dataclass(frozen=True)
class ModelMetadata:
    """模型元数据 / Model metadata.

    context_window_tokens 按模型独立保存，替代旧的全局 MODEL_CONTEXT_WINDOW。
    context_window_tokens is stored per-model, replacing the legacy global
    MODEL_CONTEXT_WINDOW.
    """

    model_id: str
    provider_id: str
    display_name: str
    context_window_tokens: int
    max_output_tokens: int
    capabilities: ModelCapabilitySet
    reasoning_params: ReasoningParams
    source: MetadataSource = MetadataSource.BUILTIN


@dataclass(frozen=True)
class ModelDiscoveryResult:
    """模型发现单条结果 / A single model-discovery result."""

    model_id: str
    display_name: str = ""
    context_window_tokens: int | None = None
    max_output_tokens: int | None = None


# =============================================================================
# 统一消息结构 / Unified message structures
# =============================================================================


@dataclass
class UnifiedToolCall:
    """归一化工具调用 / Normalized tool call.

    id 在 OpenAI 为 tool_call_id，在 Anthropic 也为 id；统一为同一字段。
    ``name`` 与 ``arguments`` 是协议层规范字段；``function`` 属性保留 OpenAI
    SDK 兼容视图，供仍依赖 ``tool_call.function.name`` 的业务工具执行器使用。
    """

    id: str
    name: str
    arguments: str  # JSON 字符串 / JSON string

    @property
    def function(self) -> Any:
        """返回与 OpenAI SDK ``function`` 字段兼容的只读视图。"""
        return type(
            "_UnifiedToolFunction",
            (),
            {"name": self.name, "arguments": self.arguments},
        )()


@dataclass
class UnifiedMessage:
    """归一化消息 / Normalized message.

    role: system / user / assistant / tool
    content: 文本内容（None 表示非文本或仅工具调用）
    tool_calls: assistant 消息携带的工具调用
    tool_call_id: role=tool 时对应的工具调用 id
    reasoning_content: DeepSeek-R1 / Qwen 等的推理过程（可选）
    name: role=tool 时的工具名（部分协议需要）
    """

    role: str
    content: str | None = None
    tool_calls: list[UnifiedToolCall] | None = None
    tool_call_id: str | None = None
    reasoning_content: str | None = None
    name: str | None = None


@dataclass
class UnifiedTool:
    """归一化工具定义 / Normalized tool definition.

    使用 OpenAI 风格的 function schema 作为内部表示；适配器负责转换到
    目标协议（Anthropic 的 input_schema、Gemini 的 functionDeclarations）。
    """

    name: str
    description: str
    parameters: dict[str, Any]  # JSON Schema
    strict: bool = False


@dataclass
class UnifiedUsage:
    """归一化用量 / Normalized token usage.

    Numeric fields retain the legacy zero defaults, while ``reported_fields``
    records which counters the provider actually supplied.  This prevents an
    absent counter from being persisted or displayed as a provider-reported
    zero.  ``details`` contains only safe provider token-detail counters.
    """

    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_creation_tokens: int = 0
    reasoning_tokens: int = 0
    reported_fields: frozenset[str] = field(default_factory=frozenset)
    details: dict[str, int] = field(default_factory=dict)

    @property
    def prompt_tokens(self) -> int:
        """OpenAI 风格别名 / OpenAI-style alias for input tokens."""
        return self.input_tokens

    @property
    def completion_tokens(self) -> int:
        """OpenAI 风格别名 / OpenAI-style alias for output tokens."""
        return self.output_tokens

    def add(self, other: UnifiedUsage) -> UnifiedUsage:
        """累加另一用量，同时保留提供商真实报告字段。"""
        return UnifiedUsage(
            input_tokens=self.input_tokens + other.input_tokens,
            output_tokens=self.output_tokens + other.output_tokens,
            cache_read_tokens=self.cache_read_tokens + other.cache_read_tokens,
            cache_creation_tokens=self.cache_creation_tokens
            + other.cache_creation_tokens,
            reasoning_tokens=self.reasoning_tokens + other.reasoning_tokens,
            reported_fields=self.reported_fields | other.reported_fields,
            details={
                key: self.details.get(key, 0) + other.details.get(key, 0)
                for key in self.details.keys() | other.details.keys()
            },
        )


@dataclass
class UnifiedRequest:
    """归一化请求 / Normalized request.

    max_tokens 在所有协议中必填（Anthropic 强制要求显式指定）。
    其余可选字段按模型能力与调用方意图填充，适配器再次过滤。
    """

    model: str
    messages: list[UnifiedMessage]
    max_tokens: int
    system: str | None = None
    tools: list[UnifiedTool] | None = None
    tool_choice: str | None = None  # "auto" / "none" / "required" / tool name
    temperature: float | None = None
    top_p: float | None = None
    top_k: int | None = None
    thinking: dict[str, Any] | None = None
    effort: str | None = None
    stream: bool = False


@dataclass
class UnifiedResponseMeta:
    """响应元数据：记录实际服务方与尝试链 / Response metadata."""

    served_by: str = ""  # "provider/model"
    attempt_chain: list[dict[str, Any]] = field(default_factory=list)
    fallback_reason: str = ""
    compressed: bool = False
    # 实际服务候选（winner）的上下文窗口（tokens），供调用方按实际模型
    # 而非角色首选重算上下文预算（如 Issue 分析的 safe_context）。
    # Context window (tokens) of the winning candidate that actually served
    # the request, so callers budget against the real model, not the primary.
    context_window_tokens: int | None = None
    # 实际服务候选（winner）的有效能力集（已应用 ai_model_override 覆盖），
    # 供调用方按实际 winner 判断 reasoning_content 等字段是否可回传，
    # 而不是退回目录默认能力（Issue #529）。
    # Effective capabilities (post ai_model_override) of the winning
    # candidate, so callers replay fields like reasoning_content based on
    # the real winner instead of catalog defaults.
    served_capabilities: ModelCapabilitySet | None = None


class _AttributeProxy:
    """透传属性访问以兼容旧代码 / Proxy that forwards attribute access.

    UnifiedResponse 需要兼容现有调用方对 OpenAI 响应对象的直接属性访问模式
    （response.choices[0].message.content 等）。本类将未识别属性透传到原始
    raw 对象，缺失时返回安全默认值而非抛出 AttributeError，避免业务逻辑中断。
    UnifiedResponse must stay compatible with legacy direct-attribute access on
    the OpenAI response object. Unknown attributes fall back to safe defaults
    instead of raising AttributeError.
    """

    def __init__(
        self,
        content: Any,
        tool_calls: Any,
        reasoning_content: Any,
        raw: Any,
    ):
        self._content = content
        self._tool_calls = tool_calls
        self._reasoning_content = reasoning_content
        self._raw = raw

    @property
    def content(self) -> Any:
        return self._content

    @property
    def tool_calls(self) -> Any:
        return self._tool_calls

    @property
    def reasoning_content(self) -> Any:
        return self._reasoning_content

    def __getattr__(self, item: str) -> Any:
        if self._raw is not None:
            return getattr(self._raw, item)
        raise AttributeError(item)


@dataclass
class UnifiedResponse:
    """归一化响应 / Normalized response.

    choices[0].message 通过 _AttributeProxy 兼容旧代码的直接属性访问。
    stop_reason 归一化为 StopReason 枚举；usage 为 UnifiedUsage。
    raw 保留原始响应对象供 __getattr__ 透传。
    """

    content: str
    tool_calls: list[UnifiedToolCall]
    stop_reason: StopReason
    usage: UnifiedUsage
    reasoning_content: str | None = None
    raw: Any = None
    meta: UnifiedResponseMeta = field(default_factory=UnifiedResponseMeta)

    @property
    def choices(self) -> list[Any]:
        """兼容旧代码 response.choices[0].message.xxx 的访问 / Legacy accessor."""
        message = _AttributeProxy(
            content=self.content,
            tool_calls=self.tool_calls,
            reasoning_content=self.reasoning_content,
            raw=getattr(self.raw, "choices", [None])[0].message
            if self.raw is not None and getattr(self.raw, "choices", None)
            else None,
        )
        return [type("_Choice", (), {"message": message})()]

    def to_dict(self) -> dict[str, Any]:
        """轻量序列化（用于日志）/ Lightweight serialization for logging."""
        usage = {
            name: getattr(self.usage, name)
            for name in (
                "input_tokens",
                "output_tokens",
                "cache_read_tokens",
                "cache_creation_tokens",
                "reasoning_tokens",
            )
            if name in self.usage.reported_fields
        }
        return {
            "content": self.content,
            "tool_calls": [
                {"id": tc.id, "name": tc.name, "arguments": tc.arguments}
                for tc in self.tool_calls
            ],
            "stop_reason": self.stop_reason.value,
            "usage": usage,
            "served_by": self.meta.served_by,
        }


@dataclass
class UnifiedStreamEvent:
    """归一化流式事件 / Normalized stream event.

    ``reasoning_*`` events describe only provider-signalled phases or provider
    summaries; they are never inferred from ordinary content.  Metadata is a
    small, already-sanitized projection rather than a raw provider event.
    """

    type: str  # text/tool/reasoning_start|delta|end/usage/error/done
    text: str | None = None
    tool_call: UnifiedToolCall | None = None
    usage: UnifiedUsage | None = None
    stop_reason: StopReason | None = None
    error: str | None = None
    reasoning_availability: str | None = None
    provider_event_metadata: dict[str, Any] | None = None


_SAFE_PROVIDER_METADATA_KEYS = frozenset(
    {
        "event",
        "block_type",
        "delta_type",
        "item_type",
        "response_status",
        "finish_reason",
        "index",
        "signature_present",
        "redacted",
        "encrypted",
        "usage_fields",
    }
)
_SAFE_PROVIDER_METADATA_FORBIDDEN = frozenset(
    {"headers", "endpoint", "url", "request", "body", "credential", "raw", "payload"}
)


def safe_provider_event_metadata(value: Any) -> dict[str, Any] | None:
    """Return a scalar allowlisted provider-event projection, never raw payload."""
    if not isinstance(value, dict):
        return None
    result: dict[str, Any] = {}
    for key, item in value.items():
        key = str(key)
        if (
            key.lower() in _SAFE_PROVIDER_METADATA_FORBIDDEN
            or key not in _SAFE_PROVIDER_METADATA_KEYS
        ):
            continue
        if (
            isinstance(item, bool)
            or (
                isinstance(item, int)
                and not isinstance(item, bool)
                and 0 <= item <= 1_000_000
            )
            or (isinstance(item, str) and len(item) <= 128)
        ):
            result[key] = item
        elif key == "usage_fields" and isinstance(item, (list, tuple, set, frozenset)):
            fields = sorted(
                str(field)
                for field in item
                if str(field)
                in {
                    "input_tokens",
                    "output_tokens",
                    "cached_tokens",
                    "reasoning_tokens",
                }
            )
            if fields:
                result[key] = fields
    return result or None


def usage_from_mapping(value: Any) -> UnifiedUsage:
    """Parse only reported non-negative integer usage counters."""
    if not isinstance(value, dict):
        return UnifiedUsage()
    merged = dict(value)
    for detail_key in (
        "input_tokens_details",
        "output_tokens_details",
        "prompt_tokens_details",
        "completion_tokens_details",
    ):
        nested = value.get(detail_key)
        if isinstance(nested, dict):
            merged.update(nested)
    aliases = {
        "input_tokens": ("input_tokens", "prompt_tokens"),
        "output_tokens": ("output_tokens", "completion_tokens"),
        "cache_read_tokens": (
            "cache_read_tokens",
            "cached_tokens",
            "prompt_cache_hit_tokens",
        ),
        "cache_creation_tokens": ("cache_creation_tokens",),
        "reasoning_tokens": ("reasoning_tokens",),
    }
    fields: set[str] = set()
    parsed: dict[str, int] = {}
    for target, names in aliases.items():
        for name in names:
            candidate = merged.get(name)
            if (
                isinstance(candidate, int)
                and not isinstance(candidate, bool)
                and candidate >= 0
            ):
                parsed[target] = candidate
                fields.add(target)
                break
    details: dict[str, int] = {}
    for key in (
        "input_tokens",
        "output_tokens",
        "cached_tokens",
        "prompt_cache_hit_tokens",
        "prompt_cache_miss_tokens",
        "reasoning_tokens",
    ):
        candidate = merged.get(key)
        if (
            isinstance(candidate, int)
            and not isinstance(candidate, bool)
            and candidate >= 0
        ):
            details[key] = candidate
    return UnifiedUsage(
        **parsed,
        reported_fields=frozenset(fields),
        details=details,
    )


# =============================================================================
# 解析后的运行时结构 / Resolved runtime structures
# =============================================================================


@dataclass(frozen=True)
class ResolvedEndpoint:
    """解析后的端点 / Resolved endpoint.

    base_url 已带尾部斜杠；chat_path 为相对路径。
    """

    base_url: str
    chat_path: str  # e.g. "chat/completions" / "messages"
    auth_scheme: AuthScheme
    extra_headers: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class ResolvedModel:
    """解析后的候选模型 / A resolved model candidate.

    绑定了提供商声明、模型元数据、凭据与适配器引用，供 UnifiedAIClient 直接使用。
    """

    provider: ProviderDeclaration
    model: ModelMetadata
    credential: str  # 明文凭据（API key），由上层从 AppConfig 解析
    endpoint: ResolvedEndpoint
    # 账号可覆盖 provider 的默认协议族；旧 resolver/测试构造未提供时，
    # 通过 effective_protocol 回退到 provider.family。
    # An account may override the provider's default protocol family. Legacy
    # constructors that omit it fall back to provider.family via the property.
    protocol: ProtocolFamily | str | None = None
    # 持久化账号 ID（如果候选来自账号目录）。旧调用方可省略；此字段绝不存储凭据。
    # Persisted account identity when the candidate came from the account catalog.
    # Legacy callers may omit it; credentials are deliberately never part of it.
    account_id: str | None = None

    @property
    def effective_protocol(self) -> ProtocolFamily:
        """返回本候选实际发送所用的协议族 / Return the effective protocol family."""
        if self.protocol is None:
            return self.provider.family
        if isinstance(self.protocol, ProtocolFamily):
            return self.protocol
        try:
            return ProtocolFamily(self.protocol)
        except TypeError, ValueError:
            return self.provider.family

    @property
    def protocol_family(self) -> ProtocolFamily:
        """兼容观测层命名的 effective protocol 别名 / Observability alias."""
        return self.effective_protocol

    @property
    def sticky_identity(self) -> str:
        """返回不含凭据的稳定候选身份 / Return a credential-free sticky identity.

        Saved accounts use their real account ID so two accounts that expose the
        same provider/model pair remain distinct.  Legacy candidates do not
        carry an account ID, so the endpoint and protocol are included in a
        SHA-256 fingerprint.  The endpoint is only part of the hash preimage;
        it is never returned, logged, or persisted in the sticky state.
        """
        provider_id = str(self.provider.id)
        model_id = str(self.model.model_id)
        account_id = (self.account_id or "").strip()
        if account_id:
            return f"account:{account_id}\x1fprovider:{provider_id}\x1fmodel:{model_id}"

        protocol = self.effective_protocol.value
        parsed_endpoint = urlsplit(self.endpoint.base_url)
        try:
            port = f":{parsed_endpoint.port}" if parsed_endpoint.port else ""
        except ValueError:
            # Invalid legacy URLs are still represented by a deterministic,
            # query/userinfo-free path rather than leaking the raw value.
            port = ""
        safe_endpoint = (
            f"{parsed_endpoint.scheme.lower()}://{parsed_endpoint.hostname or ''}"
            f"{port}{parsed_endpoint.path}"
        )
        fingerprint_input = (
            f"{provider_id}\x1f{model_id}\x1f{protocol}\x1f{safe_endpoint}"
            f"\x1f{self.endpoint.chat_path}"
        )
        fingerprint = hashlib.sha256(fingerprint_input.encode("utf-8")).hexdigest()
        return f"legacy:{fingerprint}"


@dataclass
class RoleBinding:
    """角色绑定 / Role binding.

    primary 与 fallback 中的 {"provider", "model"} 引用目录中的条目。
    model="follow" 表示跟随上游角色（仅 summary/agent_team 有效）。
    """

    primary: dict[str, str]
    fallback: list[dict[str, str]] = field(default_factory=list)
