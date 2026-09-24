"""AI 协议适配层 / AI protocol adaptation layer.

本包封装多厂商 AI API 的协议差异，对外提供统一的请求/响应中间表示。
This package isolates protocol differences across AI vendors and exposes
a unified intermediate representation for requests and responses.
"""

from backend.core.ai_protocol.models import (
    AIErrorCategory,
    AuthScheme,
    MetadataSource,
    ModelCapabilitySet,
    ModelDiscoveryResult,
    ModelMetadata,
    ProtocolFamily,
    ProviderDeclaration,
    ReasoningParams,
    ResolvedEndpoint,
    ResolvedModel,
    RoleBinding,
    UnifiedImagePart,
    UnifiedMessage,
    UnifiedRequest,
    UnifiedResponse,
    UnifiedStreamEvent,
    UnifiedTool,
    UnifiedToolCall,
    UnifiedUsage,
    images_from_mapping,
    strip_message_images,
)
from backend.core.ai_protocol.request_policy import (
    EffectiveRequestPolicy,
    default_safety_reserve,
    estimate_unified_messages,
    filter_reasoning_params,
    resolve_effective_request_policy,
)

__all__ = [
    "AIErrorCategory",
    "AuthScheme",
    "EffectiveRequestPolicy",
    "MetadataSource",
    "ModelCapabilitySet",
    "ModelDiscoveryResult",
    "ModelMetadata",
    "ProtocolFamily",
    "ProviderDeclaration",
    "ReasoningParams",
    "ResolvedEndpoint",
    "ResolvedModel",
    "RoleBinding",
    "UnifiedImagePart",
    "UnifiedMessage",
    "UnifiedRequest",
    "UnifiedResponse",
    "UnifiedStreamEvent",
    "UnifiedTool",
    "UnifiedToolCall",
    "UnifiedUsage",
    "default_safety_reserve",
    "estimate_unified_messages",
    "filter_reasoning_params",
    "images_from_mapping",
    "resolve_effective_request_policy",
    "strip_message_images",
]
