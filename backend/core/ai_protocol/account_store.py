"""AI 提供商账号持久化层 / AI provider-account persistence layer.

每个 ``ProviderAccount`` 代表一份用户保存的厂商配置（provider + api_key +
api_base + model 等），存储在 ``AppConfig`` 表中以 ``ai_account.<id>`` 为键的
JSON 文档中。用户可保存任意数量的账号并随时切换——角色绑定
（main / summary / agent_team）通过账号 ID 引用，实现"各个厂商持久化保存、
随时切换"。

Each ProviderAccount is a saved vendor configuration persisted as a JSON document
in AppConfig under key ``ai_account.<id>``. Users can save any number of accounts
and switch at any time; role bindings reference accounts by ID.

角色绑定 JSON 结构 / Role-binding shape::

    {
      "main": {
        "primary": {"account": "acc_xxx", "model": "gpt-5.6-sol"},
        "fallback": [{"account": "acc_yyy", "model": "claude-fable-5"}]
      },
      "summary": {"primary": {"account": "main", "model": "follow"}},
      "agent_team": {"primary": {"account": "acc_xxx", "model": "gpt-5.6-sol"}}
    }

``account="main"`` 或 ``model="follow"`` 表示跟随上游角色（仅 summary / agent_team）。
"""

from __future__ import annotations

import json
import secrets
from dataclasses import asdict, dataclass, field
from typing import Any

from loguru import logger

from backend.core.ai_protocol.models import ProtocolFamily
from backend.core.time_service import now_utc

# AppConfig 键前缀 / AppConfig key prefix
_ACCOUNT_PREFIX = "ai_account."
_ROLE_BINDINGS_KEY = "ai_role_bindings"
_ACCOUNT_ID_PREFIX = "acc_"


# =============================================================================
# 数据结构 / Data structures
# =============================================================================


@dataclass
class ProviderAccount:
    """用户保存的一份厂商配置 / A saved vendor configuration.

    对应文件.md 中的 ProviderAccount + ProtocolEndpoint 的运行时投影：
    provider_id 引用内置目录，protocol 决定走哪个协议适配器，api_base / api_key
    是该账号的实际端点与凭据。
    """

    id: str
    name: str
    provider_id: str
    protocol: str = ProtocolFamily.OPENAI_COMPATIBLE.value
    api_base: str = ""
    api_key: str = ""
    region: str = ""
    models: list[str] = field(default_factory=list)
    default_model: str = ""
    enabled: bool = True
    notes: str = ""
    created_at: float = 0.0
    updated_at: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        """完整序列化（含明文 key，仅内部使用）/ Full serialization (internal)."""
        return asdict(self)

    def to_public_dict(self) -> dict[str, Any]:
        """API/UI 安全视图：api_key 脱敏 / Safe view with masked api_key."""
        data = asdict(self)
        data["api_key"] = _mask_api_key(self.api_key)
        data["has_key"] = bool(self.api_key)
        return data


@dataclass
class RoleAssignment:
    """角色→账号绑定条目 / A role→account binding entry."""

    account: str  # 账号 ID，或 "main" 表示跟随主角色 / account id or "main"
    model: str  # 模型 ID，或 "follow" 表示跟随上游 / model id or "follow"


@dataclass
class RoleBindingConfig:
    """单个角色的完整绑定（主 + 回退链）/ Full binding for a role."""

    primary: RoleAssignment
    fallback: list[RoleAssignment] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        """序列化 / Serialize."""
        return {
            "primary": asdict(self.primary),
            "fallback": [asdict(f) for f in self.fallback],
        }


# =============================================================================
# 辅助 / Helpers
# =============================================================================


def _mask_api_key(key: str) -> str:
    """脱敏 API Key / Mask an API key for display."""
    if not key:
        return ""
    if len(key) <= 8:
        return "****"
    return key[:4] + "****" + key[-4:]


def generate_account_id() -> str:
    """生成新账号 ID / Generate a new account id."""
    return _ACCOUNT_ID_PREFIX + secrets.token_hex(6)


def _now() -> float:
    return now_utc().timestamp()


def _safe_json_loads(raw: str | None) -> Any:
    if not raw:
        return None
    try:
        return json.loads(raw)
    except json.JSONDecodeError, TypeError:
        return None


def _account_from_dict(data: dict[str, Any]) -> ProviderAccount:
    """从 dict 构造 ProviderAccount（容忍缺字段）/ Build account from dict."""
    return ProviderAccount(
        id=str(data.get("id") or ""),
        name=str(data.get("name") or ""),
        provider_id=str(data.get("provider_id") or data.get("provider") or "custom"),
        protocol=str(
            data.get("protocol")
            or data.get("family")
            or ProtocolFamily.OPENAI_COMPATIBLE.value
        ),
        api_base=str(data.get("api_base") or data.get("base_url") or ""),
        api_key=str(data.get("api_key") or ""),
        region=str(data.get("region") or ""),
        models=list(data.get("models") or []),
        default_model=str(data.get("default_model") or ""),
        enabled=bool(data.get("enabled", True)),
        notes=str(data.get("notes") or ""),
        created_at=float(data.get("created_at") or 0.0),
        updated_at=float(data.get("updated_at") or 0.0),
    )


def _role_binding_from_dict(data: dict[str, Any]) -> RoleBindingConfig | None:
    """从 dict 构造 RoleBindingConfig / Build role binding from dict."""
    primary_raw = data.get("primary") or {}
    if isinstance(primary_raw, dict):
        primary = RoleAssignment(
            account=str(
                primary_raw.get("account") or primary_raw.get("provider") or ""
            ),
            model=str(primary_raw.get("model") or ""),
        )
    else:
        return None
    fallback: list[RoleAssignment] = []
    for item in data.get("fallback") or []:
        if isinstance(item, dict):
            fallback.append(
                RoleAssignment(
                    account=str(item.get("account") or item.get("provider") or ""),
                    model=str(item.get("model") or ""),
                )
            )
    return RoleBindingConfig(primary=primary, fallback=fallback)


def validate_role_bindings_payload(
    payload: dict[str, Any],
    account_ids: set[str],
) -> tuple[dict[str, RoleBindingConfig], str | None]:
    """Validate and normalize API-supplied role bindings.

    Bindings are security-sensitive configuration: accepting a dangling account
    reference makes the saved role silently unusable and can unexpectedly alter
    its fallback behaviour.  Keep the accepted shape aligned with the runtime
    resolver and reject malformed or unknown references before persistence.

    Returns ``(bindings, None)`` on success or ``({}, error_message)`` when the
    payload is rejected. ``error_message`` is a controlled, user-facing string
    so callers can surface it directly without leaking exception internals.
    """
    supported_roles = {"main", "summary", "agent_team"}
    result: dict[str, RoleBindingConfig] = {}

    for raw_role, raw_binding in payload.items():
        role = str(raw_role)
        if role not in supported_roles:
            return {}, f"不支持的 AI 角色: {role}"
        if not isinstance(raw_binding, dict):
            return {}, f"角色 {role} 的绑定必须是对象"

        primary = raw_binding.get("primary")
        fallback = raw_binding.get("fallback", [])
        if not isinstance(primary, dict):
            return {}, f"角色 {role} 缺少有效的 primary 绑定"
        if not isinstance(fallback, list) or any(
            not isinstance(item, dict) for item in fallback
        ):
            return {}, f"角色 {role} 的 fallback 必须是对象列表"

        binding = _role_binding_from_dict(raw_binding)
        if binding is None:
            return {}, f"角色 {role} 的绑定格式无效"

        assignments = [binding.primary, *binding.fallback]
        for assignment in assignments:
            if not assignment.account or not assignment.model:
                return {}, f"角色 {role} 的账号和模型不能为空"

            follows_main = assignment.account == "main" or assignment.model == "follow"
            if follows_main:
                if role == "main" or not (
                    assignment.account == "main" and assignment.model == "follow"
                ):
                    return {}, f"角色 {role} 的跟随绑定无效"
                continue

            if assignment.account not in account_ids:
                return (
                    {},
                    f"角色 {role} 引用了不存在的 AI 账号: {assignment.account}",
                )

        result[role] = binding

    return result, None


# =============================================================================
# 数据库访问 / DB access (lazy import to avoid circular deps)
# =============================================================================


async def _load_app_config_map(keys: list[str]) -> dict[str, str]:
    """从 AppConfig 批量加载明文配置 / Load plaintext values from AppConfig."""
    try:
        from sqlalchemy import select

        from backend.models.database import AppConfig, async_session
    except Exception:
        return {}

    if async_session is None:
        return {}

    try:
        async with async_session() as session:
            result = await session.execute(
                select(AppConfig).where(AppConfig.key_name.in_(keys))
            )
            return {c.key_name: c.key_value for c in result.scalars().all()}
    except Exception as exc:
        logger.debug("加载 AppConfig 失败 / failed to load AppConfig: {}", exc)
        return {}


async def _fetch_all_account_keys() -> list[str]:
    """扫描 AppConfig 获取所有 ai_account.* 键 / Scan all account keys."""
    try:
        from sqlalchemy import select

        from backend.models.database import AppConfig, async_session
    except Exception:
        return []

    if async_session is None:
        return []

    try:
        async with async_session() as session:
            result = await session.execute(
                select(AppConfig.key_name).where(
                    AppConfig.key_name.like(f"{_ACCOUNT_PREFIX}%")
                )
            )
            return [str(row) for row in result.scalars().all()]
    except Exception as exc:
        logger.debug("扫描账号键失败 / failed to scan account keys: {}", exc)
        return []


async def _upsert_app_config(key: str, value: str, description: str = "") -> None:
    """插入或更新 AppConfig 行 / Upsert an AppConfig row."""
    try:
        from sqlalchemy import select

        from backend.models.database import AppConfig, async_session
    except Exception:
        return

    if async_session is None:
        return

    async with async_session() as session:
        result = await session.execute(
            select(AppConfig).where(AppConfig.key_name == key)
        )
        row = result.scalar_one_or_none()
        if row is None:
            session.add(
                AppConfig(key_name=key, key_value=value, description=description)
            )
        else:
            row.key_value = value
        await session.commit()


async def _delete_app_config(key: str) -> None:
    """删除 AppConfig 行 / Delete an AppConfig row."""
    try:
        from sqlalchemy import delete

        from backend.models.database import AppConfig, async_session
    except Exception:
        return

    if async_session is None:
        return

    async with async_session() as session:
        await session.execute(delete(AppConfig).where(AppConfig.key_name == key))
        await session.commit()


# =============================================================================
# 账号 CRUD / Account CRUD
# =============================================================================


async def list_accounts(*, include_disabled: bool = True) -> list[ProviderAccount]:
    """列出所有已保存的账号 / List all saved accounts."""
    keys = await _fetch_all_account_keys()
    if not keys:
        return []
    raw_map = await _load_app_config_map(keys)
    accounts: list[ProviderAccount] = []
    for key, raw in raw_map.items():
        data = _safe_json_loads(raw)
        if not isinstance(data, dict):
            continue
        acct = _account_from_dict(data)
        if not acct.id:
            acct.id = key.removeprefix(_ACCOUNT_PREFIX)
        if not include_disabled and not acct.enabled:
            continue
        accounts.append(acct)
    accounts.sort(key=lambda a: (a.name.lower(), a.id))
    return accounts


async def get_account(account_id: str) -> ProviderAccount | None:
    """按 ID 获取单个账号 / Get a single account by id."""
    if not account_id:
        return None
    raw_map = await _load_app_config_map([f"{_ACCOUNT_PREFIX}{account_id}"])
    raw = raw_map.get(f"{_ACCOUNT_PREFIX}{account_id}")
    data = _safe_json_loads(raw)
    if not isinstance(data, dict):
        return None
    acct = _account_from_dict(data)
    if not acct.id:
        acct.id = account_id
    return acct


async def save_account(account: ProviderAccount) -> ProviderAccount:
    """创建或更新账号 / Create or update an account."""
    from backend.core.ai_protocol.endpoint_security import validate_provider_base_url

    ok, message = validate_provider_base_url(
        account.provider_id,
        account.api_base,
        protocol=account.protocol,
    )
    if not ok:
        raise ValueError(message)

    if not account.id:
        account.id = generate_account_id()
    now = _now()
    if account.created_at == 0.0:
        account.created_at = now
    account.updated_at = now
    await _upsert_app_config(
        f"{_ACCOUNT_PREFIX}{account.id}",
        json.dumps(account.to_dict(), ensure_ascii=False),
        description=f"AI provider account {account.name}",
    )
    return account


async def delete_account(account_id: str) -> bool:
    """删除账号；若被角色绑定引用则拒绝 / Delete an account unless referenced."""
    if not account_id:
        return False
    bindings = await get_role_bindings()
    for binding in bindings.values():
        if binding.primary.account == account_id:
            return False
        for fb in binding.fallback:
            if fb.account == account_id:
                return False
    await _delete_app_config(f"{_ACCOUNT_PREFIX}{account_id}")
    return True


async def count_accounts() -> int:
    """返回已保存账号数量 / Return the number of saved accounts."""
    return len(await _fetch_all_account_keys())


# =============================================================================
# 角色绑定读写 / Role-binding read/write
# =============================================================================


async def get_role_bindings() -> dict[str, RoleBindingConfig]:
    """读取角色绑定 / Read role bindings.

    返回 dict[role_str, RoleBindingConfig]。未配置的角色不出现在结果中。
    """
    raw_map = await _load_app_config_map([_ROLE_BINDINGS_KEY])
    raw = raw_map.get(_ROLE_BINDINGS_KEY)
    parsed = _safe_json_loads(raw)
    result: dict[str, RoleBindingConfig] = {}
    if isinstance(parsed, dict):
        for role, entry in parsed.items():
            if not isinstance(entry, dict):
                continue
            binding = _role_binding_from_dict(entry)
            if binding is not None:
                result[str(role)] = binding
    return result


async def get_role_bindings_raw() -> dict[str, Any]:
    """读取角色绑定的原始 JSON dict / Read raw role-binding JSON dict."""
    raw_map = await _load_app_config_map([_ROLE_BINDINGS_KEY])
    parsed = _safe_json_loads(raw_map.get(_ROLE_BINDINGS_KEY))
    return parsed if isinstance(parsed, dict) else {}


async def save_role_bindings(bindings: dict[str, RoleBindingConfig]) -> None:
    """保存角色绑定 / Persist role bindings."""
    payload = {role: b.to_dict() for role, b in bindings.items()}
    await _upsert_app_config(
        _ROLE_BINDINGS_KEY,
        json.dumps(payload, ensure_ascii=False),
        description="AI role → account bindings",
    )


async def save_role_bindings_raw(payload: dict[str, Any]) -> None:
    """保存原始 JSON dict 形式的角色绑定 / Persist raw role-binding JSON."""
    await _upsert_app_config(
        _ROLE_BINDINGS_KEY,
        json.dumps(payload, ensure_ascii=False),
        description="AI role → account bindings",
    )


__all__ = [
    "ProviderAccount",
    "RoleAssignment",
    "RoleBindingConfig",
    "count_accounts",
    "delete_account",
    "generate_account_id",
    "get_account",
    "get_role_bindings",
    "get_role_bindings_raw",
    "list_accounts",
    "save_account",
    "save_role_bindings",
    "save_role_bindings_raw",
    "validate_role_bindings_payload",
]
