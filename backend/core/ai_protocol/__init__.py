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
    UnifiedMessage,
    UnifiedRequest,
    UnifiedResponse,
    UnifiedStreamEvent,
    UnifiedTool,
    UnifiedToolCall,
    UnifiedUsage,
)

__all__ = [
    "AIErrorCategory",
    "AuthScheme",
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
    "UnifiedMessage",
    "UnifiedRequest",
    "UnifiedResponse",
    "UnifiedStreamEvent",
    "UnifiedTool",
    "UnifiedToolCall",
    "UnifiedUsage",
]
