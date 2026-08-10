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
    except TypeError, ValueError:
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

    Endpoint、凭据、模型及其推理参数（temperature/max_tokens/上下文窗口）均由
    统一协议层按角色绑定的 reasoning_params 实时解析，不在此快照中保存。
    这里仅保留角色名与 HTTP 超时策略。
    """

    agent_role: str
    summary_role: str
    timeout_seconds: int

    def safe_snapshot(self) -> dict[str, Any]:
        """返回可持久化的配置快照。"""
        return {
            "agent_role": self.agent_role,
            "summary_role": self.summary_role,
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
        object.__setattr__(self, "timeout_seconds", state.get("timeout_seconds", 0))


def _value_or(value: Any, default: Any) -> Any:
    """仅在值为 None 时使用默认值，保留显式 0/False。"""
    return default if value is None else value


async def _config_value(key: str, default: Any = "") -> Any:
    return _value_or(await get_dynamic_config(key), default)


async def load_agent_team_ai_config() -> AgentTeamAIConfig:
    """只加载 Agent Team 的角色名称与 HTTP 超时策略。

    temperature/max_tokens 不再在此读取：由 unified client 按角色绑定的
    reasoning_params 实时解析（call_with_retry 传入 None 即回退到 candidate）。
    """
    timeout_seconds = await _config_value("agent_team_timeout_seconds", 600)
    return AgentTeamAIConfig(
        agent_role="agent_team",
        summary_role="summary",
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
