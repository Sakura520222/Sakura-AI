"""Capability decisions must restrict actual executions, including cancellation."""

from __future__ import annotations

import asyncio

import pytest


@pytest.mark.parametrize(
    ("profile", "capability", "allowed"),
    [
        ("read_only", "filesystem.read", True),
        ("read_only", "filesystem.write", False),
        ("read_only", "shell.execute", False),
        ("read_only", "git.write", False),
        ("workspace_write", "filesystem.write", True),
        ("workspace_write", "shell.execute", True),
        ("workspace_write", "network.web", False),
        ("workspace_write", "dependency.install", False),
        ("autonomous", "subagent.spawn", True),
        ("autonomous", "dependency.install", True),
        ("autonomous", "network.egress", False),
        ("full_access", "git.push", True),
    ],
)
def test_profiles_restrict_operations(profile, capability, allowed):
    from backend.services.agent_team.capability_policy import PolicySnapshot

    policy = PolicySnapshot(profile=profile, network_policy="web_tools")
    assert policy.evaluate({capability}).allowed is allowed


def test_dependency_grant_is_scoped_and_does_not_make_ordinary_egress_available():
    from backend.services.agent_team.capability_policy import PolicySnapshot

    policy = PolicySnapshot(profile="autonomous", network_policy="web_tools")
    assert policy.evaluate({"dependency.install", "network.egress"}).allowed
    assert not policy.evaluate({"network.egress"}).allowed


@pytest.mark.parametrize("profile", ["read_only", "autonomous", "full_access"])
def test_offline_intersects_profiles_including_admin_full_access(profile):
    from backend.services.agent_team.capability_policy import PolicySnapshot

    policy = PolicySnapshot(profile=profile, network_policy="offline")
    assert not policy.evaluate({"network.web"}).allowed
    assert not policy.evaluate({"dependency.install", "network.egress"}).allowed
    assert policy.evaluate({"filesystem.read"}).allowed


@pytest.mark.parametrize("value", ["docker.host", "network=host", "", None])
def test_unknown_capabilities_fail_closed_without_echoing_input(value):
    from backend.services.agent_team.capability_policy import PolicySnapshot

    policy = PolicySnapshot(profile="full_access", network_policy="full_access")
    decision = policy.evaluate({value})
    assert not decision.allowed
    assert decision.reason == "unknown_capability"
    assert not decision.capabilities


@pytest.mark.parametrize("profile", ["administrator", "", None])
def test_invalid_permission_profile_never_defaults_to_full_access(profile):
    from backend.services.agent_team.capability_policy import PolicySnapshot

    with pytest.raises(ValueError):
        PolicySnapshot(profile=profile, network_policy="web_tools")


@pytest.mark.asyncio
async def test_boundary_check_refreshes_and_parent_ceiling_cannot_be_expanded():
    from backend.services.agent_team.capability_policy import (
        CapabilitySession,
        PolicySnapshot,
    )

    snapshot = PolicySnapshot(profile="full_access", network_policy="full_access")

    async def load():
        return snapshot

    session = CapabilitySession(load, task_id=7, ceiling={"filesystem.read"})
    assert (await session.check("tool", {"filesystem.read"})).allowed
    assert not (await session.check("tool", {"filesystem.write"})).allowed
    snapshot = PolicySnapshot(profile="read_only", network_policy="offline")
    assert not (await session.check("tool", {"network.web"})).allowed


@pytest.mark.asyncio
async def test_checks_record_policy_decisions_with_task_identity():
    from backend.services.agent_team.capability_policy import (
        CapabilitySession,
        PolicySnapshot,
    )

    events = []

    async def load():
        return PolicySnapshot(
            profile="autonomous", network_policy="web_tools", revision="r2"
        )

    async def audit(event):
        events.append(event)

    session = CapabilitySession(load, task_id=7, session_id=12, audit=audit)

    allowed = await session.check(
        "dependency", {"dependency.install", "network.egress"}
    )
    denied = await session.check("shell", {"network.egress"})
    assert allowed.allowed
    assert not denied.allowed
    assert [event["event"] for event in events] == [
        "capability_allowed",
        "capability_denied",
    ]
    assert all(event["task_id"] == 7 and event["session_id"] == 12 for event in events)
    assert events[0]["revision"] == "r2"


@pytest.mark.asyncio
async def test_cancelled_policy_read_propagates_without_authorizing_operation():
    from backend.services.agent_team.capability_policy import (
        CapabilitySession,
        PolicySnapshot,
    )

    entered = asyncio.Event()

    async def load():
        entered.set()
        await asyncio.Event().wait()
        return PolicySnapshot(profile="autonomous", network_policy="web_tools")

    session = CapabilitySession(load, task_id=7)

    task = asyncio.create_task(session.check("shell", {"shell.execute"}))
    await entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


@pytest.mark.asyncio
async def test_unreadable_policy_returns_denial_without_leaking_configuration():
    from backend.services.agent_team.capability_policy import (
        CapabilitySession,
    )

    events = []

    async def load():
        raise RuntimeError("SECRET_CONFIGURATION_FAILURE")

    async def audit(event):
        events.append(event)

    session = CapabilitySession(load, task_id=7, audit=audit)
    decision = await session.check("shell", {"shell.execute"})
    assert not decision.allowed
    assert decision.reason == "policy_unavailable"
    assert events[0]["event"] == "capability_denied"
    assert "SECRET" not in str(events)


@pytest.mark.asyncio
async def test_task_close_prevents_stale_context_from_starting_operations():
    from backend.services.agent_team.capability_policy import (
        CapabilitySession,
        PolicySnapshot,
    )

    async def load():
        return PolicySnapshot(profile="autonomous", network_policy="web_tools")

    session = CapabilitySession(load, task_id=7)
    assert (await session.check("tool", {"filesystem.read"})).allowed
    await session.close()
    assert not (await session.check("tool", {"filesystem.read"})).allowed


@pytest.mark.asyncio
async def test_audit_failure_prevents_operation_from_proceeding():
    from backend.services.agent_team.capability_policy import (
        CapabilityDenied,
        CapabilitySession,
        PolicySnapshot,
    )

    async def load():
        return PolicySnapshot(profile="autonomous", network_policy="web_tools")

    async def audit(_event):
        raise RuntimeError("audit unavailable")

    session = CapabilitySession(load, task_id=7, audit=audit)
    with pytest.raises(CapabilityDenied, match="audit_unavailable"):
        await session.check("shell", {"shell.execute"})


@pytest.mark.asyncio
async def test_context_closed_during_audit_cannot_dispatch_afterward():
    from backend.services.agent_team.capability_policy import (
        CapabilitySession,
        PolicySnapshot,
    )

    async def load():
        return PolicySnapshot(profile="autonomous", network_policy="web_tools")

    async def audit(event):
        if event["event"] == "capability_allowed":
            await session.close()

    session = CapabilitySession(load, task_id=7, audit=audit)
    decision = await session.check("shell", {"shell.execute"})
    assert not decision.allowed
    assert decision.reason == "session_closed"
