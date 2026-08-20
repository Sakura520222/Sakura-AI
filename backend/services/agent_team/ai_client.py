"""Agent Team role-bound AI client factories."""

from __future__ import annotations

from dataclasses import dataclass

from backend.core.config import get_dynamic_config
from backend.services.ai_reviewer.api_client import AIApiClient


async def resolve_agent_team_max_iterations(
    task_max_iterations: int | None = None,
) -> int:
    """Compatibility shim for callers that still pass a legacy run value.

    The value is no longer read from Settings or dynamic configuration, and
    the implementation Agent does not use it as a lifecycle limit. Keeping
    this import-compatible helper lets older route/worker deployments start
    while they migrate away from the retired task field.
    """
    if task_max_iterations is None:
        return 1
    try:
        return max(1, int(task_max_iterations))
    except (TypeError, ValueError):
        return 1


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
    """Implementation Agent 的角色绑定信息。

    单次 AI 传输的连接、读取、重试和取消保护由统一协议客户端负责。Agent
    不再携带一个会终止整项任务的 timeout 或 max-iterations 快照。
    """

    agent_role: str
    summary_role: str

    def __post_init__(self) -> None:
        # ``agent`` is the persisted session/UI identity, not an AI registry
        # role. Normalize legacy callers at construction time as well as when
        # unpickling an old compatibility snapshot.
        if self.agent_role == "agent":
            object.__setattr__(self, "agent_role", "agent_team")

    def safe_snapshot(self) -> dict[str, str]:
        """Return the non-sensitive role binding for compatibility readers.

        This is intentionally not persisted by the worker and contains no
        endpoint, credential, timeout, model, or task-loop budget.
        """
        return {
            "agent_role": self.agent_role,
            "summary_role": self.summary_role,
        }

    as_safe_dict = safe_snapshot

    def __getstate__(self) -> dict[str, str]:
        return self.safe_snapshot()

    def __setstate__(self, state: dict[str, str]) -> None:
        agent_role = state.get("agent_role", "agent_team")
        # Older snapshots used the persisted session identity here.  Normalize
        # that legacy value at the AI boundary; session role migration remains
        # a separate compatibility concern.
        if agent_role == "agent":
            agent_role = "agent_team"
        object.__setattr__(
            self,
            "agent_role",
            agent_role,
        )
        object.__setattr__(self, "summary_role", state.get("summary_role", "summary"))


async def load_agent_team_ai_config() -> AgentTeamAIConfig:
    """加载唯一 implementation Agent 的角色绑定。

    temperature/max_tokens 以及单次请求的超时和重试策略由 unified client
    按角色绑定实时解析；这里不读取 Agent 专属动态配置。
    """
    return AgentTeamAIConfig(
        # ``agent`` is the persisted session/UI identity.  The unified AI
        # role registry uses the historical role ID ``agent_team``.
        agent_role="agent_team",
        summary_role="summary",
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
