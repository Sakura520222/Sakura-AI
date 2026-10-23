"""Declarative runtime boundaries for autonomous Agent operations.

Checks describe the existing sandbox/network boundary and narrowed delegation;
they do not add an approval workflow, grant tokens or permission handshakes.
Execution runners retain network selection, resource cleanup and the #604
temporary-egress lifecycle. Repository/model data cannot change policy.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from loguru import logger

from backend.services.agent_team.network_policy import (
    AgentTeamNetworkPolicy,
    parse_agent_team_network_policy,
)


class Capability(StrEnum):
    FILESYSTEM_READ = "filesystem.read"
    FILESYSTEM_WRITE = "filesystem.write"
    SHELL_EXECUTE = "shell.execute"
    NETWORK_WEB = "network.web"
    NETWORK_EGRESS = "network.egress"
    DEPENDENCY_INSTALL = "dependency.install"
    GIT_READ = "git.read"
    GIT_WRITE = "git.write"
    GIT_PUSH = "git.push"
    GITHUB_READ = "github.read"
    GITHUB_WRITE = "github.write"
    MCP_INVOKE = "mcp.invoke"
    SUBAGENT_SPAWN = "subagent.spawn"


class PermissionProfile(StrEnum):
    READ_ONLY = "read_only"
    WORKSPACE_WRITE = "workspace_write"
    AUTONOMOUS = "autonomous"
    FULL_ACCESS = "full_access"


READ_ONLY_CAPABILITIES = frozenset(
    {
        Capability.FILESYSTEM_READ,
        Capability.GIT_READ,
        Capability.GITHUB_READ,
        Capability.NETWORK_WEB,
        Capability.MCP_INVOKE,
    }
)
_PROFILE_CAPABILITIES = {
    PermissionProfile.READ_ONLY: READ_ONLY_CAPABILITIES,
    PermissionProfile.WORKSPACE_WRITE: frozenset(
        {
            Capability.FILESYSTEM_READ,
            Capability.FILESYSTEM_WRITE,
            Capability.SHELL_EXECUTE,
            Capability.GIT_READ,
        }
    ),
    # Git push / GitHub write belong to the existing trusted worker publication
    # path, not an Agent tool or a credential-bearing Shell. Keeping them here
    # preserves the administrator-authorized /agent -> draft-PR workflow.
    PermissionProfile.AUTONOMOUS: frozenset(Capability) - {Capability.NETWORK_EGRESS},
    PermissionProfile.FULL_ACCESS: frozenset(Capability),
}


@dataclass(frozen=True, slots=True)
class CapabilityDecision:
    allowed: bool
    reason: str
    capabilities: tuple[str, ...] = ()
    revision: str = ""


@dataclass(frozen=True, slots=True)
class PolicySnapshot:
    """Only construct from trusted administrator configuration, never a prompt."""

    profile: PermissionProfile | str
    network_policy: AgentTeamNetworkPolicy | str
    revision: str = ""

    def __post_init__(self) -> None:
        try:
            profile = PermissionProfile(self.profile)
        except ValueError, TypeError:
            raise ValueError("invalid_permission_profile") from None
        object.__setattr__(self, "profile", profile)
        object.__setattr__(
            self, "network_policy", parse_agent_team_network_policy(self.network_policy)
        )

    @property
    def capabilities(self) -> frozenset[Capability]:
        return _PROFILE_CAPABILITIES[self.profile]

    def evaluate(self, required: Iterable[str]) -> CapabilityDecision:
        try:
            capabilities = frozenset(Capability(item) for item in required)
        except ValueError, TypeError:
            return CapabilityDecision(False, "unknown_capability")
        names = tuple(sorted(item.value for item in capabilities))
        allowed = self.capabilities
        # #604 explicitly authorizes unrestricted egress for the duration of
        # dependency execution; it does not grant ordinary Shell egress.
        if Capability.DEPENDENCY_INSTALL in capabilities and (
            Capability.DEPENDENCY_INSTALL in allowed
        ):
            allowed = allowed | {Capability.NETWORK_EGRESS}
        if not capabilities <= allowed:
            return CapabilityDecision(False, "profile_denied", names, self.revision)
        network = self.network_policy
        if network is AgentTeamNetworkPolicy.OFFLINE and capabilities & {
            Capability.NETWORK_WEB,
            Capability.NETWORK_EGRESS,
        }:
            return CapabilityDecision(False, "network_denied", names, self.revision)
        if (
            Capability.NETWORK_EGRESS in capabilities
            and network is not AgentTeamNetworkPolicy.FULL_ACCESS
            and Capability.DEPENDENCY_INSTALL not in capabilities
        ):
            return CapabilityDecision(False, "network_denied", names, self.revision)
        return CapabilityDecision(True, "allowed", names, self.revision)


class CapabilityDenied(PermissionError):
    """Safe reason code only; never include configuration or caller arguments."""


PolicyLoader = Callable[[], Awaitable[PolicySnapshot]]
AuditSink = Callable[[dict[str, Any]], Awaitable[None]]


@dataclass
class CapabilitySession:
    """Fresh boundary checks with a task/child ceiling; no approval state."""

    load_policy: PolicyLoader
    task_id: int | None = None
    session_id: int | None = None
    ceiling: Iterable[str] | None = None
    audit: AuditSink | None = field(default=None, repr=False)
    _closed: bool = field(default=False, init=False)

    def __post_init__(self) -> None:
        if self.ceiling is not None:
            self.ceiling = frozenset(Capability(value) for value in self.ceiling)

    async def evaluate(
        self, required: Iterable[str], *, read_only: bool = True
    ) -> CapabilityDecision:
        """Check tool visibility without recording an execution that did not occur."""
        if self._closed:
            return CapabilityDecision(False, "session_closed")
        try:
            snapshot = await self.load_policy()
            if not isinstance(snapshot, PolicySnapshot):
                return CapabilityDecision(False, "policy_unavailable")
        except Exception:
            return CapabilityDecision(False, "policy_unavailable")
        if self._closed:
            return CapabilityDecision(False, "session_closed")
        decision = snapshot.evaluate(required)
        if (
            decision.allowed
            and not read_only
            and (
                snapshot.profile is PermissionProfile.READ_ONLY
                or (
                    self.ceiling is not None
                    and Capability.FILESYSTEM_WRITE not in self.ceiling
                )
            )
        ):
            return CapabilityDecision(
                False,
                "readonly_effect_denied",
                decision.capabilities,
                decision.revision,
            )
        if decision.allowed and self.ceiling is not None:
            if not set(decision.capabilities) <= self.ceiling:
                return CapabilityDecision(
                    False,
                    "task_ceiling_denied",
                    decision.capabilities,
                    decision.revision,
                )
        return decision

    async def _record(
        self,
        event: str,
        action: str,
        decision: CapabilityDecision,
    ) -> None:
        # action is a runtime category, not arbitrary model text or a command.
        if action not in {
            "tool",
            "shell",
            "dependency",
            "mcp",
            "subagent",
            "git",
            "github",
            "hook",
            "request",
        }:
            action = "tool"
        payload = {
            "event": event,
            "action": action,
            "task_id": self.task_id,
            "session_id": self.session_id,
            "capabilities": list(decision.capabilities),
            "reason": decision.reason,
            "revision": decision.revision,
        }
        if self.audit is not None:
            try:
                await self.audit(payload)
            except Exception:
                logger.error(
                    "Agent capability audit unavailable: task={}", self.task_id
                )
                raise CapabilityDenied("audit_unavailable") from None
        logger.info("Agent capability audit: {}", payload)

    async def check(
        self, action: str, required: Iterable[str], *, read_only: bool = True
    ) -> CapabilityDecision:
        decision = await self.evaluate(required, read_only=read_only)
        await self._record(
            "capability_allowed" if decision.allowed else "capability_denied",
            action,
            decision,
        )
        if decision.allowed and self._closed:
            decision = CapabilityDecision(
                False, "session_closed", decision.capabilities, decision.revision
            )
            await self._record("capability_denied", action, decision)
        return decision

    async def close(self) -> None:
        """Prevent a stale task/child context from dispatching further operations."""
        self._closed = True


async def load_runtime_policy() -> PolicySnapshot:
    """Read only trusted dynamic settings at each actual boundary."""
    from backend.core.config import get_dynamic_config_fresh
    from backend.services.agent_team.network_policy import get_agent_team_network_policy

    profile = await get_dynamic_config_fresh("agent_team_permission_profile")
    network = await get_agent_team_network_policy()
    return PolicySnapshot(profile, network, revision=f"{profile}:{network}")


async def execution_network_policy(
    policy: AgentTeamNetworkPolicy,
) -> AgentTeamNetworkPolicy:
    """Intersect runner networking with the current non-networked profiles.

    The existing #604 runner still owns network mode selection and cleanup.
    A workspace-only profile cannot inherit egress from a broader deployment
    network setting. Autonomous/full-access preserve that existing setting.
    """
    from backend.core.config import get_dynamic_config_fresh

    profile = PermissionProfile(
        await get_dynamic_config_fresh("agent_team_permission_profile")
    )
    if profile in {PermissionProfile.READ_ONLY, PermissionProfile.WORKSPACE_WRITE}:
        return AgentTeamNetworkPolicy.OFFLINE
    return policy


def tool_capabilities(tool, args=None) -> tuple[str, ...]:
    """Only registered implementations select authority, never model metadata."""
    from backend.services.agent_team.tools.mcp_tool import MCPTool

    if isinstance(tool, MCPTool):
        return tuple(
            {
                Capability.MCP_INVOKE.value,
                Capability.NETWORK_WEB.value,
                *tool.policy.required_capabilities,
            }
        )
    name = tool.name
    if name in {
        "read_file",
        "list_directory",
        "glob",
        "search_in_files",
        "detect_project",
        "use_skill",
    }:
        return (Capability.FILESYSTEM_READ,)
    if name in {
        "write_file",
        "edit_file",
        "replace_lines",
        "insert_lines",
        "revert_file",
    }:
        return (Capability.FILESYSTEM_WRITE,)
    if name == "run_command":
        caps = (Capability.SHELL_EXECUTE, Capability.FILESYSTEM_WRITE)
        if args and args.get("network_capability", "none") != "none":
            from backend.services.agent_team.network_policy import (
                NetworkCapability,
                parse_network_capability,
            )

            try:
                requested = parse_network_capability(args["network_capability"])
            except ValueError, TypeError:
                return ("unknown",)
            if requested is NetworkCapability.DEPENDENCY_EGRESS:
                caps += (Capability.DEPENDENCY_INSTALL, Capability.NETWORK_EGRESS)
        return caps
    if name == "check_changes":
        return (Capability.GIT_READ,)
    if name in {"search_web", "fetch_url"}:
        return (Capability.NETWORK_WEB,)
    if name == "spawn_agent":
        return (Capability.SUBAGENT_SPAWN,)
    if name in {"finish_task", "wait_agent", "cancel_agent"}:
        return ()
    return ("unknown",)
