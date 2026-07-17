"""角色配置解析 / Role configuration resolution.

仅从 AppConfig 中的 ai_role_bindings、ai_account.* 与
ai_model_override.* 解析为 ResolvedChain。缺失或不可用绑定返回 None，由
角色调用门面转换为明确的配置错误；旧扁平 AI 配置永不参与解析。

Resolves only ai_role_bindings, ai_account.*, and ai_model_override.* from
AppConfig into a ResolvedChain. Missing or unusable bindings return None for
the role facade to report explicitly; legacy flat AI configuration is ignored.
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
)
from backend.core.ai_protocol.registry import resolve_account_endpoint
from backend.core.ai_protocol.endpoint_security import validate_provider_base_url
from backend.core.ai_protocol.resolver import ResolvedChain, _build_metadata
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

    只读取 ai_role_bindings 与 ai_account.*；旧扁平 AI 配置键永不参与解析。
    """
    from backend.core.ai_protocol import account_store

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
            if role in (ROLE_SUMMARY, ROLE_AGENT_TEAM):
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
    if not endpoint.base_url:
        logger.warning(
            "跳过缺少 endpoint 的 AI 账号: account={} provider={}",
            getattr(account, "id", ""),
            decl.id,
        )
        return None
    metadata = metadata_override or _build_metadata(decl.id, model_id)
    return ResolvedModel(
        provider=decl,
        model=metadata,
        credential=account.api_key,
        endpoint=endpoint,
    )


async def resolve_role_from_config(role: str) -> Optional[ResolvedChain]:
    """仅从账号与角色绑定解析候选链 / Resolve a role chain from accounts."""
    try:
        return await _resolve_from_accounts(role)
    except Exception as exc:  # noqa: BLE001
        logger.warning("账号角色解析失败: role={} err={}", role, exc)
        return None


__all__ = [
    "ROLE_MAIN",
    "ROLE_SUMMARY",
    "ROLE_AGENT_TEAM",
    "ALL_ROLES",
    "resolve_role_from_config",
]
