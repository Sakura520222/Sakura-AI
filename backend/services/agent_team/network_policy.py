"""Agent Team network policy and fresh authorization helpers.

The policy is an application setting, while the concrete Docker network is a
deployment setting owned by sandboxd.  Keeping the two concepts separate is
important: callers can choose only ``none`` or ``egress`` on the wire and can
never submit a Docker network name.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Any


class AgentTeamNetworkPolicy(StrEnum):
    """Administrator-selected network policy for Agent work."""

    OFFLINE = "offline"
    WEB_TOOLS = "web_tools"
    FULL_ACCESS = "full_access"

    @property
    def network_mode(self) -> str:
        """Return the only network capability that may cross the UDS.

        ``web_tools`` deliberately keeps shell/build/test runners offline;
        only ``full_access`` enables the daemon's server-owned egress network.
        """

        return "egress" if self is self.FULL_ACCESS else "none"

    @property
    def allows_web_tools(self) -> bool:
        return self is not self.OFFLINE

    @property
    def allows_dependency_network(self) -> bool:
        return self is self.FULL_ACCESS

    @property
    def allows_local_backend(self) -> bool:
        """Whether the policy can explicitly accept host-side execution.

        ``LocalExecutionRunner`` cannot provide an OS network namespace.  It
        is therefore available only when the administrator has explicitly
        selected ``full_access`` and accepted that the host process can reach
        the host network.  ``offline`` and ``web_tools`` must never create a
        false impression of isolation in source deployments.
        """

        return self is self.FULL_ACCESS


# Short alias for integrations that refer to the feature as Agent OS rather
# than Agent Team.  Both names intentionally resolve to the same enum.
AgentNetworkPolicy = AgentTeamNetworkPolicy


DEFAULT_AGENT_TEAM_NETWORK_POLICY = AgentTeamNetworkPolicy.WEB_TOOLS
AGENT_TEAM_NETWORK_POLICY_VALUES = frozenset(item.value for item in AgentTeamNetworkPolicy)


@dataclass(frozen=True, slots=True)
class AgentTeamNetworkPolicyState:
    """Fresh policy plus the durable revision used for audit correlation."""

    policy: AgentTeamNetworkPolicy
    revision: str


def parse_agent_team_network_policy(value: Any) -> AgentTeamNetworkPolicy:
    """Parse a persisted policy strictly and fail closed for unknown values."""

    if isinstance(value, AgentTeamNetworkPolicy):
        return value
    if not isinstance(value, str):
        raise ValueError("agent_team_network_policy must be a string")
    normalized = value.strip().lower()
    try:
        return AgentTeamNetworkPolicy(normalized)
    except ValueError as exc:
        allowed = ", ".join(sorted(AGENT_TEAM_NETWORK_POLICY_VALUES))
        raise ValueError(
            f"agent_team_network_policy must be one of: {allowed}"
        ) from exc


def network_mode_for_policy(policy: AgentTeamNetworkPolicy | str) -> str:
    """Map a policy to the constrained sandbox protocol enum."""

    return parse_agent_team_network_policy(policy).network_mode


async def get_agent_team_network_policy() -> AgentTeamNetworkPolicy:
    """Read the policy fresh immediately before one Agent operation.

    ``get_dynamic_config_fresh`` intentionally bypasses the process-level
    dynamic-config cache.  This matters for long-lived workers and for
    multiple Web workers where a save in one process must affect the next
    runner/tool call in another process without a restart.
    """

    state = await get_agent_team_network_policy_state()
    return state.policy


async def get_agent_team_network_policy_state() -> AgentTeamNetworkPolicyState:
    """Read policy and a fresh cross-worker revision in one admission step."""

    from backend.core.config import get_dynamic_config_fresh_with_revision

    raw, revision = await get_dynamic_config_fresh_with_revision(
        "agent_team_network_policy"
    )
    return AgentTeamNetworkPolicyState(
        policy=parse_agent_team_network_policy(raw),
        revision=revision,
    )


resolve_agent_team_network_policy = get_agent_team_network_policy


async def get_agent_tool_switch(key: str) -> bool:
    """Read a boolean Agent Web-tool switch without using a stale cache."""

    from backend.core.config import get_dynamic_config_fresh

    value = await get_dynamic_config_fresh(key)
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in {"true", "1", "yes", "on"}
    return bool(value)


def web_tool_denial_reason(
    tool_name: str,
    policy: AgentTeamNetworkPolicy | str,
) -> str:
    """Return a stable user-facing explanation for policy denial."""

    selected = parse_agent_team_network_policy(policy)
    if selected is AgentTeamNetworkPolicy.OFFLINE:
        return (
            f"Agent 网络策略为 offline，已禁止 {tool_name}；"
            "请切换为 web_tools 或 full_access 后重试"
        )
    return f"{tool_name} 未获得 Agent 网络策略授权"


__all__ = [
    "AGENT_TEAM_NETWORK_POLICY_VALUES",
    "DEFAULT_AGENT_TEAM_NETWORK_POLICY",
    "AgentNetworkPolicy",
    "AgentTeamNetworkPolicy",
    "AgentTeamNetworkPolicyState",
    "get_agent_team_network_policy",
    "get_agent_team_network_policy_state",
    "get_agent_tool_switch",
    "network_mode_for_policy",
    "parse_agent_team_network_policy",
    "resolve_agent_team_network_policy",
    "web_tool_denial_reason",
]
