"""Agent Team role-bound AI client factories."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from backend.core.config import get_dynamic_config, get_settings
from backend.services.ai_reviewer.api_client import AIApiClient


async def resolve_agent_team_max_iterations(
    task_max_iterations: int | None = None,
) -> int:
    """读取 Agent 单任务最大迭代轮数（公共辅助函数）。"""
    fallback = get_settings().agent_team_max_iterations_per_task
    raw_value = await get_dynamic_config("agent_team_max_iterations_per_task")
    effective_fallback = (
        task_max_iterations if task_max_iterations is not None else fallback
    )
    try:
        value = int(raw_value if raw_value is not None else effective_fallback)
    except (TypeError, ValueError):
        value = fallback
    return max(1, value)


async def resolve_agent_team_bool_config(key: str, fallback: bool) -> bool:
    """读取布尔动态配置，保留显式 False（公共辅助函数）。"""
    value = await get_dynamic_config(key)
    if value is None:
        return fallback
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"1", "true", "yes", "on", "启用", "是"}


@dataclass(frozen=True)
class AgentTeamAIConfig:
    """Agent Team 的角色绑定与调用策略快照。

    Endpoint、凭据和模型均由统一协议层按角色解析，不在此快照中保存。
    """

    agent_role: str
    summary_role: str
    temperature: float
    max_tokens: int
    timeout_seconds: int

    def safe_snapshot(self) -> dict[str, Any]:
        """返回可持久化的配置快照。"""
        return {
            "agent_role": self.agent_role,
            "summary_role": self.summary_role,
            "temperature": self.temperature,
            "max_tokens": self.max_tokens,
            "timeout_seconds": self.timeout_seconds,
        }

    def as_safe_dict(self) -> dict[str, Any]:
        """返回安全字典。"""
        return self.safe_snapshot()

    def __getstate__(self) -> dict[str, Any]:
        """pickle 等隐式序列化时仅暴露安全快照。"""
        return self.safe_snapshot()

    def __setstate__(self, state: dict[str, Any]) -> None:
        """从安全快照恢复对象。"""
        object.__setattr__(self, "agent_role", state.get("agent_role", "agent_team"))
        object.__setattr__(self, "summary_role", state.get("summary_role", "summary"))
        object.__setattr__(self, "temperature", state.get("temperature", 0.0))
        object.__setattr__(self, "max_tokens", state.get("max_tokens", 0))
        object.__setattr__(self, "timeout_seconds", state.get("timeout_seconds", 0))


def _value_or(value: Any, default: Any) -> Any:
    """仅在值为 None 时使用默认值，保留显式 0/False。"""
    return default if value is None else value


async def _config_value(key: str, default: Any = "") -> Any:
    return _value_or(await get_dynamic_config(key), default)


async def load_agent_team_ai_config() -> AgentTeamAIConfig:
    """只加载 Agent Team 的角色名称和调用策略。"""
    temperature = await _config_value("agent_team_temperature", 0.2)
    max_tokens = await _config_value("agent_team_max_tokens", 8192)
    timeout_seconds = await _config_value("agent_team_timeout_seconds", 600)
    return AgentTeamAIConfig(
        agent_role="agent_team",
        summary_role="summary",
        temperature=float(temperature),
        max_tokens=int(max_tokens),
        timeout_seconds=int(timeout_seconds),
    )


async def create_agent_team_client(
    validate: bool = True,
) -> tuple[AIApiClient, AgentTeamAIConfig]:
    """创建无参数统一 AI 客户端及角色策略配置。"""
    del validate  # 角色绑定完整性由 AIApiClient 在实际调用时验证。
    config = await load_agent_team_ai_config()
    return AIApiClient(), config


async def create_agent_team_summary_client(
    fallback_config: AgentTeamAIConfig | None = None,
) -> tuple[AIApiClient, str, AgentTeamAIConfig]:
    """创建上下文压缩用客户端，固定使用 summary 角色。"""
    config = fallback_config or await load_agent_team_ai_config()
    return AIApiClient(), config.summary_role, config
