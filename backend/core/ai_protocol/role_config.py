"""角色配置解析 / Role configuration resolution.

从 AppConfig 数据库表读取 ai_role_bindings、ai_provider_configs、
ai_model_configs、凭据，解析为 ResolvedChain。

Reads ai_role_bindings / ai_provider_configs / ai_model_configs / credentials
from the AppConfig table and resolves them into a ResolvedChain.

迁移期间（PR-1/2/3）：配置键尚未落库时返回 None，触发 AIApiClient 回退到
旧 OpenAI SDK 路径，保证零中断。
During migration (PR-1/2/3), returns None when config keys are not yet
persisted, which triggers the legacy OpenAI SDK fallback in AIApiClient.
"""

from __future__ import annotations

import json
from typing import Any, Optional

from loguru import logger

from backend.core.ai_protocol.models import (
    ModelCapabilitySet,
    ModelMetadata,
    MetadataSource,
    ProtocolFamily,
    ReasoningParams,
    ResolvedModel,
    RoleBinding,
)
from backend.core.ai_protocol.registry import resolve_account_endpoint, resolve_endpoint
from backend.core.ai_protocol.endpoint_security import validate_provider_base_url
from backend.core.ai_protocol.resolver import ResolvedChain, _build_metadata, resolve_role
from backend.core.ai_providers import get_builtin_provider

# 角色（role）常量 / Role constants
ROLE_MAIN = "main"
ROLE_SUMMARY = "summary"
ROLE_AGENT_TEAM = "agent_team"
ALL_ROLES = (ROLE_MAIN, ROLE_SUMMARY, ROLE_AGENT_TEAM)


def _safe_json(value: Any) -> Any:
    """容忍字符串或已解析结构 / Accept str or parsed JSON."""
    if value is None:
        return None
    if isinstance(value, (dict, list)):
        return value
    if isinstance(value, str):
        try:
            return json.loads(value)
        except json.JSONDecodeError:
            return None
    return None


async def _load_app_config_map(keys: list[str]) -> dict[str, str]:
    """从 AppConfig 加载明文配置（非脱敏值）/ Load plaintext AppConfig values."""
    try:
        from backend.models.database import AppConfig, async_session
        from sqlalchemy import select
    except Exception:  # noqa: BLE001
        return {}

    if async_session is None:
        return {}

    try:
        async with async_session() as session:
            result = await session.execute(
                select(AppConfig).where(AppConfig.key_name.in_(keys))
            )
            return {c.key_name: c.key_value for c in result.scalars().all()}
    except Exception as exc:  # noqa: BLE001
        logger.debug("读取 AI 角色配置失败 / failed to load ai role config: {}", exc)
        return {}


def _parse_bindings(config_map: dict[str, str]) -> dict[str, RoleBinding]:
    """解析 ai_role_bindings.<role>.* / Parse role bindings."""
    bindings: dict[str, RoleBinding] = {}
    raw = config_map.get("ai_role_bindings")
    parsed = _safe_json(raw)
    if isinstance(parsed, dict):
        for role in ALL_ROLES:
            entry = parsed.get(role)
            if isinstance(entry, dict):
                primary = entry.get("primary") or {}
                fallback = entry.get("fallback") or []
                if isinstance(primary, dict) and primary:
                    bindings[role] = RoleBinding(
                        primary=primary,
                        fallback=[f for f in fallback if isinstance(f, dict)],
                    )
    # 兼容旧字段：未配置新结构时，从旧 openai_*/summary_* 字段构造 main/summary
    if ROLE_MAIN not in bindings:
        main_provider = config_map.get("ai_provider", "")
        main_model = config_map.get("openai_model", "")
        if main_provider and main_model:
            bindings[ROLE_MAIN] = RoleBinding(
                primary={"provider": main_provider, "model": main_model}
            )
    if ROLE_SUMMARY not in bindings:
        summary_provider = config_map.get("summary_provider", "")
        summary_model = config_map.get("summary_model", "")
        if summary_provider and summary_model:
            # summary_provider="" 或 "跟随" 视为跟随主模型
            if summary_provider in ("", "follow", "main"):
                bindings[ROLE_SUMMARY] = RoleBinding(
                    primary={"provider": "main", "model": "follow"}
                )
            else:
                bindings[ROLE_SUMMARY] = RoleBinding(
                    primary={"provider": summary_provider, "model": summary_model}
                )
    return bindings


def _collect_credentials(
    config_map: dict[str, str], bindings: dict[str, RoleBinding]
) -> dict[str, str]:
    """收集候选提供商对应的 API key / Collect API keys for referenced providers."""
    refs: list[tuple[str, str]] = []
    for binding in bindings.values():
        refs.append((binding.primary.get("provider", ""), binding.primary.get("model", "")))
        for fb in binding.fallback:
            refs.append((fb.get("provider", ""), fb.get("model", "")))

    credentials: dict[str, str] = {}
    # 新键 / new keys
    for provider_id, _model in refs:
        if not provider_id or provider_id == "main":
            continue
        key = config_map.get(f"ai_credential.{provider_id}")
        if key:
            credentials[provider_id] = key
    # 兼容旧键 / legacy keys
    if "openai" in {p for p, _ in refs} and "openai" not in credentials:
        legacy = config_map.get("openai_api_key", "")
        if legacy:
            credentials["openai"] = legacy
    if "openai" in {p for p, _ in refs} and "openai" not in credentials:
        # 自定义 / custom 也可能复用 openai_api_key
        pass
    # custom 提供商：用 openai_api_key 作为凭据
    for provider_id, _ in refs:
        if provider_id == "custom" and "custom" not in credentials:
            legacy = config_map.get("openai_api_key", "")
            if legacy:
                credentials["custom"] = legacy
    return credentials


def _collect_base_urls(config_map: dict[str, str]) -> dict[str, str]:
    """收集自定义 base_url 覆盖（主要针对 custom）/ Collect base_url overrides."""
    overrides: dict[str, str] = {}
    custom_base = config_map.get("openai_api_base", "")
    if custom_base:
        overrides["custom"] = custom_base
        # 旧配置若主提供商是某个内置 id 但 base_url 被覆盖，也透传
        overrides["openai"] = custom_base
    return overrides


def _parse_metadata_overrides(
    config_map: dict[str, str],
) -> dict[tuple[str, str], ModelMetadata]:
    """解析 ai_model_overrides（JSON：{provider|model: {ctx, max_out, ...}}）.

    简化格式：键 "ai_model_override.<provider>.<model>" → JSON {ctx, max_out}
    """
    overrides: dict[tuple[str, str], ModelMetadata] = {}
    prefix = "ai_model_override."
    for key, value in config_map.items():
        if not key.startswith(prefix):
            continue
        rest = key[len(prefix) :]
        parts = rest.split(".", 1)
        if len(parts) != 2:
            continue
        provider_id, model_id = parts
        parsed = _safe_json(value)
        if not isinstance(parsed, dict):
            continue
        builtin = _build_metadata(provider_id, model_id)
        ctx = int(parsed.get("context_window_tokens", 0) or 0)
        max_out = int(parsed.get("max_output_tokens", 0) or 0)
        raw_caps = parsed.get("capabilities")
        raw_params = parsed.get("reasoning_params")
        default_caps = builtin.capabilities
        if isinstance(raw_caps, dict):
            caps = ModelCapabilitySet(
                vision=bool(raw_caps.get("vision", default_caps.vision)),
                tools=bool(raw_caps.get("tools", default_caps.tools)),
                streaming=bool(raw_caps.get("streaming", default_caps.streaming)),
                reasoning_content=bool(
                    raw_caps.get("reasoning_content", default_caps.reasoning_content)
                ),
                thinking=bool(raw_caps.get("thinking", default_caps.thinking)),
                effort=bool(raw_caps.get("effort", default_caps.effort)),
                prompt_caching=bool(
                    raw_caps.get("prompt_caching", default_caps.prompt_caching)
                ),
                temperature=bool(
                    raw_caps.get("temperature", default_caps.temperature)
                ),
                top_p=bool(raw_caps.get("top_p", default_caps.top_p)),
                top_k=bool(raw_caps.get("top_k", default_caps.top_k)),
            )
        else:
            caps = default_caps

        effective_max_out = max_out or builtin.max_output_tokens
        default_params = builtin.reasoning_params
        if isinstance(raw_params, dict):
            params = ReasoningParams(
                max_output_tokens=effective_max_out,
                temperature=raw_params.get("temperature", default_params.temperature),
                top_p=raw_params.get("top_p", default_params.top_p),
                top_k=raw_params.get("top_k", default_params.top_k),
                thinking=raw_params.get("thinking", default_params.thinking),
                effort=raw_params.get("effort", default_params.effort),
            )
        else:
            params = ReasoningParams(
                max_output_tokens=effective_max_out,
                temperature=default_params.temperature,
                top_p=default_params.top_p,
                top_k=default_params.top_k,
                thinking=default_params.thinking,
                effort=default_params.effort,
            )

        overrides[(provider_id, model_id)] = ModelMetadata(
            model_id=model_id,
            provider_id=provider_id,
            display_name=str(parsed.get("display_name") or builtin.display_name),
            context_window_tokens=ctx or builtin.context_window_tokens,
            max_output_tokens=effective_max_out,
            capabilities=caps,
            reasoning_params=params,
            source=MetadataSource.USER_OVERRIDE,
        )
    return overrides


def _candidate_ref_provider_id(ref_provider: str) -> str:
    """聚合器前缀识别：anthropic/x → anthropic（预留，本期统一走 openai-compatible）."""
    return ref_provider


def _parse_protocol_family(value: str) -> ProtocolFamily:
    """容错解析 ProtocolFamily / Parse ProtocolFamily tolerantly."""
    try:
        return ProtocolFamily(value)
    except ValueError:
        return ProtocolFamily.OPENAI_COMPATIBLE


async def _load_model_overrides(
    bindings: dict[str, Any],
    accounts: dict[str, Any],
) -> dict[tuple[str, str], ModelMetadata]:
    """加载角色绑定涉及的所有 (provider, model) 用户覆盖。

    覆盖键为 ai_model_override.<provider>.<model>，由「AI 配置」页的单模型
    高级配置写入，优先级高于内置目录元数据。
    """
    needed: set[tuple[str, str]] = set()
    for b in bindings.values():
        refs = [(b.primary.account, b.primary.model)]
        refs += [(fb.account, fb.model) for fb in b.fallback]
        for acct_id, mdl in refs:
            if not acct_id or acct_id == "main" or not mdl or mdl == "follow":
                continue
            acct = accounts.get(acct_id)
            if acct is not None:
                needed.add((acct.provider_id, mdl))
    if not needed:
        return {}
    keys = [f"ai_model_override.{p}.{m}" for p, m in needed]
    raw_map = await _load_app_config_map(keys)
    return _parse_metadata_overrides(raw_map)


async def _resolve_from_accounts(
    role: str,
) -> Optional[ResolvedChain]:
    """从持久化账号解析角色链 / Resolve a role chain from saved accounts.

    读取 ai_role_bindings 与 ai_account.* 账号文档，构建 ResolvedChain。
    若无账号或该角色未绑定，返回 None 交由旧路径回退。
    """
    from backend.core.ai_protocol import account_store

    # 首次访问时自动迁移旧扁平配置 / auto-migrate legacy config on first access
    await account_store.ensure_default_account_from_legacy()

    bindings = await account_store.get_role_bindings()
    if not bindings:
        return None
    binding = bindings.get(role)
    if binding is None:
        return None

    accounts = {a.id: a for a in await account_store.list_accounts()}
    overrides = await _load_model_overrides(bindings, accounts)

    candidates: list[ResolvedModel] = []

    def _add_assignment(account_id: str, model_id: str) -> None:
        """把一个 (account, model) 引用解析为 ResolvedModel 并追加到候选链."""
        if not account_id or not model_id:
            return
        # 跟随上游角色 / follow upstream role
        if account_id == "main" or model_id == "follow":
            if role != ROLE_MAIN:
                upstream = _resolve_from_accounts_sync_cached(
                    ROLE_MAIN, accounts, bindings, overrides
                )
                for c in upstream:
                    if c not in candidates:
                        candidates.append(c)
            return
        acct = accounts.get(account_id)
        if acct is None or not acct.enabled:
            return
        resolved = _build_candidate_from_account(
            acct,
            model_id,
            metadata_override=overrides.get((acct.provider_id, model_id)),
        )
        if resolved is not None and resolved not in candidates:
            candidates.append(resolved)

    _add_assignment(binding.primary.account, binding.primary.model)
    for fb in binding.fallback:
        _add_assignment(fb.account, fb.model)

    if not candidates and role != ROLE_MAIN:
        upstream = _resolve_from_accounts_sync_cached(
            ROLE_MAIN, accounts, bindings, overrides
        )
        candidates.extend(upstream)

    if not candidates:
        return None
    return ResolvedChain(role=role, candidates=candidates)


def _resolve_from_accounts_sync_cached(
    main_role: str,
    accounts: dict,
    bindings: dict,
    overrides: Optional[dict[tuple[str, str], ModelMetadata]] = None,
) -> list[ResolvedModel]:
    """同步解析 main 角色（避免重复 IO）/ Resolve main role synchronously."""
    binding = bindings.get(main_role)
    if binding is None:
        return []
    overrides = overrides or {}
    candidates: list[ResolvedModel] = []

    def _add(account_id: str, model_id: str) -> None:
        if not account_id or not model_id:
            return
        acct = accounts.get(account_id)
        if acct is None or not acct.enabled:
            return
        resolved = _build_candidate_from_account(
            acct,
            model_id,
            metadata_override=overrides.get((acct.provider_id, model_id)),
        )
        if resolved is not None and resolved not in candidates:
            candidates.append(resolved)

    _add(binding.primary.account, binding.primary.model)
    for fb in binding.fallback:
        _add(fb.account, fb.model)
    return candidates


def _build_candidate_from_account(
    account: Any,
    model_id: str,
    metadata_override: Optional[ModelMetadata] = None,
) -> Optional[ResolvedModel]:
    """从账号 + 模型 ID 构造 ResolvedModel / Build a ResolvedModel from an account.

    metadata_override 优先于内置目录；用于「AI 配置」页的单模型高级覆盖
    （ai_model_override.<provider>.<model>）。
    """
    decl = get_builtin_provider(account.provider_id)
    family = _parse_protocol_family(account.protocol) if account.protocol else decl.family
    ok, message = validate_provider_base_url(
        decl.id,
        account.api_base,
        protocol=family,
    )
    if not ok:
        logger.warning(
            "跳过不安全 AI 账号 endpoint: account={} provider={} reason={}",
            getattr(account, "id", ""),
            decl.id,
            message,
        )
        return None
    endpoint = resolve_account_endpoint(decl, family=family, base_url=account.api_base)
    metadata = metadata_override or _build_metadata(decl.id, model_id)
    return ResolvedModel(
        provider=decl,
        model=metadata,
        credential=account.api_key,
        endpoint=endpoint,
    )


async def resolve_role_from_config(role: str) -> Optional[ResolvedChain]:
    """从数据库配置解析角色链 / Resolve a role chain from DB config.

    优先走账号路径（ai_role_bindings + ai_account.*）；若未配置则回退旧扁平键。
    返回 None 表示配置未就绪，调用方应回退旧路径。
    Returns None when config is not ready; callers should fall back.
    """
    # 优先：账号持久化路径 / account-based path first
    try:
        chain = await _resolve_from_accounts(role)
    except Exception as exc:  # noqa: BLE001
        logger.debug("账号解析失败，回退旧路径 / account resolution failed: {}", exc)
        chain = None
    if chain is not None:
        return chain

    # 回退：旧扁平键路径 / legacy flat-key path
    keys = [
        "ai_role_bindings",
        "ai_provider",
        "openai_model",
        "openai_api_key",
        "openai_api_base",
        "summary_provider",
        "summary_model",
        "summary_api_key",
        "summary_api_base",
    ]
    # 动态收集 ai_credential.* / ai_model_override.* 键
    config_map = await _load_app_config_map(keys)

    # 若既无新绑定也无旧 main 配置，返回 None 触发回退
    has_new = bool(config_map.get("ai_role_bindings"))
    has_legacy_main = bool(
        config_map.get("ai_provider") and config_map.get("openai_model")
    )
    if not has_new and not has_legacy_main:
        return None

    # 扩展读取凭据与 override 键
    extra_keys: list[str] = []
    for k in list(config_map.keys()):
        if k.startswith("ai_credential.") or k.startswith("ai_model_override."):
            extra_keys.append(k)
    # 额外尝试常见键名（首次可能未命中，补充一次读取）
    probable_extra = [
        "ai_credential.openai",
        "ai_credential.anthropic",
        "ai_credential.custom",
    ]
    extra_map = await _load_app_config_map(list(set(extra_keys + probable_extra)))
    config_map.update(extra_map)

    bindings = _parse_bindings(config_map)
    if role not in bindings:
        return None

    credentials = _collect_credentials(config_map, bindings)
    base_urls = _collect_base_urls(config_map)
    metadata_overrides = _parse_metadata_overrides(config_map)

    # 把 base_url 注入 resolve_candidate（通过 monkey-patch 兜底：custom 用 openai_api_base）
    # 为保持 resolver 纯净，这里在解析后对 custom 候选重建 endpoint
    upstream = (
        await resolve_role_from_config(ROLE_MAIN) if role != ROLE_MAIN else None
    )

    chain = resolve_role(
        role=role,
        bindings=bindings,
        credentials=credentials,
        upstream_chain=upstream,
        metadata_overrides=metadata_overrides,
    )

    # 应用 base_url 覆盖（custom 提供商）/ apply base_url overrides for custom
    adjusted: list = []
    for candidate in chain.candidates:
        if candidate.provider.id in base_urls:
            decl = candidate.provider
            new_endpoint = resolve_endpoint(decl, base_urls[candidate.provider.id])
            from dataclasses import replace as dc_replace

            adjusted.append(
                dc_replace(candidate, endpoint=new_endpoint)
            )
        else:
            adjusted.append(candidate)
    chain.candidates = adjusted  # type: ignore[assignment]

    return chain


__all__ = [
    "ROLE_MAIN",
    "ROLE_SUMMARY",
    "ROLE_AGENT_TEAM",
    "ALL_ROLES",
    "resolve_role_from_config",
]
