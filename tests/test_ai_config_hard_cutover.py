"""旧扁平 AI 配置硬切换回归测试。"""

import pytest

from backend.core.ai_protocol import account_store
from backend.core.ai_protocol.account_store import (
    ProviderAccount,
    RoleAssignment,
    RoleBindingConfig,
)
from backend.core.ai_protocol.role_config import resolve_role_from_config
from backend.services.ai_reviewer.api_client import AIApiClient


@pytest.mark.asyncio
async def test_role_resolution_ignores_legacy_flat_config_without_accounts():
    """遗留 openai_* 键不能再自动创建账号或构造 main 候选。"""
    assert not hasattr(account_store, "ensure_default_account_from_legacy")
    assert await resolve_role_from_config("main") is None


@pytest.mark.asyncio
async def test_api_client_requires_role_and_never_constructs_legacy_sdk():
    """统一客户端不得再接受无角色调用或要求旧 endpoint/key 构造参数。"""
    client = AIApiClient()

    with pytest.raises(ValueError, match="role"):
        await client.call_with_retry(
            messages=[{"role": "user", "content": "hello"}],
            model="gpt-5.6-terra",
        )


@pytest.mark.asyncio
async def test_unusable_explicit_summary_binding_does_not_fall_back_to_main(
    monkeypatch,
):
    """显式但不可用的 summary 绑定必须失败，而非暗中改用 main。"""
    main_account = ProviderAccount(
        id="main-account",
        name="Main",
        provider_id="openai",
        api_base="https://api.openai.com/v1",
        api_key="main-key",
    )
    disabled_summary_account = ProviderAccount(
        id="summary-account",
        name="Summary",
        provider_id="openai",
        api_base="https://api.openai.com/v1",
        api_key="summary-key",
        enabled=False,
    )
    bindings = {
        "main": RoleBindingConfig(
            primary=RoleAssignment(account="main-account", model="gpt-5.6-sol")
        ),
        "summary": RoleBindingConfig(
            primary=RoleAssignment(account="summary-account", model="gpt-5.6-summary")
        ),
    }

    async def get_bindings():
        return bindings

    async def list_accounts():
        return [main_account, disabled_summary_account]

    monkeypatch.setattr(account_store, "get_role_bindings", get_bindings)
    monkeypatch.setattr(account_store, "list_accounts", list_accounts)

    assert await resolve_role_from_config("summary") is None


@pytest.mark.asyncio
@pytest.mark.parametrize("role", ["summary", "agent_team"])
async def test_explicit_follow_binding_uses_main_candidates(monkeypatch, role):
    """仅受支持的角色可通过显式 follow 复用 main 候选。"""
    main_account = ProviderAccount(
        id="main-account",
        name="Main",
        provider_id="openai",
        api_base="https://api.openai.com/v1",
        api_key="main-key",
    )
    bindings = {
        "main": RoleBindingConfig(
            primary=RoleAssignment(account="main-account", model="gpt-5.6-sol")
        ),
        role: RoleBindingConfig(primary=RoleAssignment(account="main", model="follow")),
    }

    async def get_bindings():
        return bindings

    async def list_accounts():
        return [main_account]

    monkeypatch.setattr(account_store, "get_role_bindings", get_bindings)
    monkeypatch.setattr(account_store, "list_accounts", list_accounts)

    chain = await resolve_role_from_config(role)

    assert chain is not None
    assert [candidate.model.model_id for candidate in chain.candidates] == [
        "gpt-5.6-sol"
    ]
    assert chain.candidates[0].account_id == "main-account"


@pytest.mark.asyncio
async def test_valid_fallback_survives_unusable_primary_binding(monkeypatch):
    """不可用 primary 不得抹去同角色显式配置的有效 fallback。"""
    disabled_account = ProviderAccount(
        id="disabled-account",
        name="Disabled",
        provider_id="openai",
        api_base="https://api.openai.com/v1",
        api_key="disabled-key",
        enabled=False,
    )
    fallback_account = ProviderAccount(
        id="fallback-account",
        name="Fallback",
        provider_id="openai",
        api_base="https://api.openai.com/v1",
        api_key="fallback-key",
    )
    bindings = {
        "summary": RoleBindingConfig(
            primary=RoleAssignment(account="disabled-account", model="primary-model"),
            fallback=[
                RoleAssignment(account="fallback-account", model="fallback-model")
            ],
        )
    }

    async def get_bindings():
        return bindings

    async def list_accounts():
        return [disabled_account, fallback_account]

    monkeypatch.setattr(account_store, "get_role_bindings", get_bindings)
    monkeypatch.setattr(account_store, "list_accounts", list_accounts)

    chain = await resolve_role_from_config("summary")

    assert chain is not None
    assert [candidate.model.model_id for candidate in chain.candidates] == [
        "fallback-model"
    ]


@pytest.mark.asyncio
async def test_unrecognized_role_cannot_follow_main_binding(monkeypatch):
    """只有 summary 与 agent_team 能显式跟随 main。"""
    main_account = ProviderAccount(
        id="main-account",
        name="Main",
        provider_id="openai",
        api_base="https://api.openai.com/v1",
        api_key="main-key",
    )
    bindings = {
        "main": RoleBindingConfig(
            primary=RoleAssignment(account="main-account", model="gpt-5.6-sol")
        ),
        "unexpected_role": RoleBindingConfig(
            primary=RoleAssignment(account="main", model="follow")
        ),
    }

    async def get_bindings():
        return bindings

    async def list_accounts():
        return [main_account]

    monkeypatch.setattr(account_store, "get_role_bindings", get_bindings)
    monkeypatch.setattr(account_store, "list_accounts", list_accounts)

    assert await resolve_role_from_config("unexpected_role") is None


@pytest.mark.asyncio
async def test_custom_account_without_endpoint_is_not_a_candidate(monkeypatch):
    """custom 账号没有 endpoint 时必须使绑定不可用。"""
    custom_account = ProviderAccount(
        id="custom-account",
        name="Custom",
        provider_id="custom",
        api_base="",
        api_key="custom-key",
    )
    bindings = {
        "summary": RoleBindingConfig(
            primary=RoleAssignment(account="custom-account", model="custom-model")
        )
    }

    async def get_bindings():
        return bindings

    async def list_accounts():
        return [custom_account]

    monkeypatch.setattr(account_store, "get_role_bindings", get_bindings)
    monkeypatch.setattr(account_store, "list_accounts", list_accounts)

    assert await resolve_role_from_config("summary") is None


@pytest.mark.asyncio
async def test_sakura_legacy_supplier_surface_is_removed(monkeypatch):
    """Sakura 后台任务只能由 role binding 供应模型，旧供应商键必须硬删除。"""
    from backend.core import config as config_module
    from backend.core.config import (
        BASIC_CONFIG_KEYS,
        CORE_CONFIG_KEYS,
        DYNAMIC_CONFIG_GROUPS,
        DYNAMIC_CONFIG_LABELS,
        DYNAMIC_CONFIG_RANGES,
        DYNAMIC_CONFIG_SELECT_OPTIONS,
        DYNAMIC_CONFIG_SENSITIVE_KEYS,
        Settings,
        get_all_db_config_keys,
        get_all_dynamic_config_keys,
        get_dynamic_config,
    )

    legacy_keys = {
        "sakura_reflection_model",
        "sakura_issue_reflection_model",
        "sakura_consolidation_model",
        "sakura_use_summary_model",
        "sakura_extraction_provider",
        "sakura_extraction_api_base",
        "sakura_extraction_api_key",
        "sakura_extraction_model",
    }

    settings = Settings()
    assert legacy_keys.isdisjoint(Settings.model_fields)
    assert all(not hasattr(settings, key) for key in legacy_keys)

    registries = (
        set(get_all_dynamic_config_keys()),
        set(get_all_db_config_keys()),
        set(DYNAMIC_CONFIG_LABELS),
        set(DYNAMIC_CONFIG_RANGES),
        set(DYNAMIC_CONFIG_SELECT_OPTIONS),
        set(DYNAMIC_CONFIG_SENSITIVE_KEYS),
        set(CORE_CONFIG_KEYS),
        set(BASIC_CONFIG_KEYS),
        {
            key
            for group in DYNAMIC_CONFIG_GROUPS.values()
            for key in group.get("keys", [])
        },
    )
    for registry in registries:
        assert legacy_keys.isdisjoint(registry)

    async def fail_if_read(key: str):
        raise AssertionError(f"historical Sakura key must not reach DB: {key}")

    monkeypatch.setattr(config_module, "_read_config_from_db", fail_if_read)
    config_module.invalidate_dynamic_config_cache()
    for key in legacy_keys:
        assert await get_dynamic_config(key) is None
