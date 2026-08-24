"""Agent Team 候选缓存配置测试。"""

import warnings
from unittest.mock import AsyncMock

import pytest

from backend.services.agent_team import candidate_service as candidate_service_module
from backend.services.agent_team.candidate_service import AgentTeamCandidateService


@pytest.mark.asyncio
async def test_collect_candidates_awaits_dynamic_cache_ttl(monkeypatch):
    service = AgentTeamCandidateService()
    service.invalidate_cache()

    requested_keys: list[str] = []

    async def fake_get_dynamic_config(key: str):
        requested_keys.append(key)
        return 0

    collect_issue_candidates = AsyncMock(return_value=[])
    collect_scan_candidates = AsyncMock(return_value=[])

    monkeypatch.setattr(
        candidate_service_module,
        "get_dynamic_config",
        fake_get_dynamic_config,
    )
    monkeypatch.setattr(
        service,
        "_load_repo_allowlist",
        AsyncMock(return_value=set()),
    )
    monkeypatch.setattr(
        service,
        "_collect_issue_candidates",
        collect_issue_candidates,
    )
    monkeypatch.setattr(
        service,
        "_collect_scan_candidates",
        collect_scan_candidates,
    )
    monkeypatch.setattr(
        service,
        "_filter_closed_issues",
        AsyncMock(side_effect=lambda candidates: candidates),
    )

    try:
        with warnings.catch_warnings(record=True) as captured:
            warnings.simplefilter("always", RuntimeWarning)
            first = await service.collect_candidates(object(), limit=3)
            second = await service.collect_candidates(object(), limit=3)

        runtime_warnings = [
            warning for warning in captured if warning.category is RuntimeWarning
        ]
        assert first == second == []
        assert runtime_warnings == []
        assert requested_keys == [
            "agent_team_candidate_cache_ttl",
            "agent_team_candidate_cache_ttl",
        ]
        assert collect_issue_candidates.await_count == 2
        assert collect_scan_candidates.await_count == 2
    finally:
        service.invalidate_cache()
