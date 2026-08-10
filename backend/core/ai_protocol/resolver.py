"""角色解析器 / Role resolver.

将 ai_role_bindings 中的角色配置解析为 ResolvedModel 候选链。
Resolves role bindings (ai_role_bindings) into a chain of ResolvedModel
candidates for the UnifiedAIClient.
"""

from __future__ import annotations

from dataclasses import dataclass

from backend.core.ai_protocol.models import (
    DEFAULT_CONTEXT_WINDOW_TOKENS,
    MetadataSource,
    ModelCapabilitySet,
    ModelMetadata,
    ReasoningParams,
    ResolvedModel,
    RoleBinding,
)
from backend.core.ai_protocol.registry import resolve_endpoint
from backend.core.ai_providers import get_builtin_provider

# 保守默认能力与推理参数 / Conservative default capability & reasoning params
_DEFAULT_CAPS = ModelCapabilitySet()
_DEFAULT_PARAMS = ReasoningParams(max_output_tokens=4096)


def _build_metadata(provider_id: str, model_id: str) -> ModelMetadata:
    """构造元数据：优先内置目录，回退保守默认 / Build metadata.

    优先从目录的 BuiltinModel 取准确上下文窗口/最大输出/能力；目录未覆盖时
    回退到默认 128K，运行时可被用户覆盖或发现结果替换。
    """
    decl = get_builtin_provider(provider_id)
    for builtin in decl.models:
        if builtin.model_id == model_id or model_id in builtin.aliases:
            return builtin.to_metadata(decl.id, MetadataSource.BUILTIN)
    return ModelMetadata(
        model_id=model_id,
        provider_id=provider_id,
        display_name=model_id,
        context_window_tokens=DEFAULT_CONTEXT_WINDOW_TOKENS,
        max_output_tokens=4096,
        capabilities=_DEFAULT_CAPS,
        reasoning_params=_DEFAULT_PARAMS,
        source=MetadataSource.FALLBACK,
    )


@dataclass
class ResolvedChain:
    """角色解析结果：候选链 / A resolved role chain."""

    role: str
    candidates: list[ResolvedModel]
    compressed: bool = False

    @property
    def primary(self) -> ResolvedModel | None:
        return self.candidates[0] if self.candidates else None


def resolve_candidate(
    provider_id: str,
    model_id: str,
    credential: str,
    base_url: str | None = None,
    *,
    metadata_override: ModelMetadata | None = None,
) -> ResolvedModel:
    """构造单个 ResolvedModel / Build a single ResolvedModel."""
    decl = get_builtin_provider(provider_id)
    endpoint = resolve_endpoint(decl, base_url)
    metadata = metadata_override or _build_metadata(decl.id, model_id)
    return ResolvedModel(
        provider=decl,
        model=metadata,
        credential=credential,
        endpoint=endpoint,
        protocol=decl.family,
    )


def resolve_role(
    role: str,
    bindings: dict[str, RoleBinding],
    credentials: dict[str, str],
    *,
    upstream_chain: ResolvedChain | None = None,
    metadata_overrides: dict[tuple[str, str], ModelMetadata] | None = None,
) -> ResolvedChain:
    """解析角色 → ResolvedChain / Resolve a role into a candidate chain.

    bindings: 角色绑定配置；credentials: {provider_id: api_key}；
    upstream_chain: 当 primary 为 {"provider":"main","model":"follow"} 时使用；
    metadata_overrides: {(provider_id, model_id): ModelMetadata}。
    """
    binding = bindings.get(role)
    if binding is None:
        # 无绑定 → 回退到 upstream 或空链 / no binding → upstream or empty
        if upstream_chain is not None:
            return ResolvedChain(role=role, candidates=list(upstream_chain.candidates))
        return ResolvedChain(role=role, candidates=[])

    candidates: list[ResolvedModel] = []

    def _add(ref: dict[str, str]) -> ResolvedModel | None:
        provider_id = ref.get("provider", "")
        model_id = ref.get("model", "")
        if not provider_id or not model_id:
            return None
        # follow 上游角色 / follow upstream role
        if provider_id == "main" or model_id == "follow":
            if upstream_chain is not None and upstream_chain.candidates:
                for c in upstream_chain.candidates:
                    if c not in candidates:
                        candidates.append(c)
            return None
        credential = credentials.get(provider_id, "")
        meta = (metadata_overrides or {}).get((provider_id, model_id))
        resolved = resolve_candidate(
            provider_id=provider_id,
            model_id=model_id,
            credential=credential,
            metadata_override=meta,
        )
        if resolved not in candidates:
            candidates.append(resolved)
        return resolved

    _add(binding.primary)
    for ref in binding.fallback:
        _add(ref)

    if not candidates and upstream_chain is not None:
        return ResolvedChain(role=role, candidates=list(upstream_chain.candidates))

    return ResolvedChain(role=role, candidates=candidates)


__all__ = [
    "ResolvedChain",
    "resolve_candidate",
    "resolve_role",
]
