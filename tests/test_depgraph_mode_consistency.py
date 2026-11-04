from unittest.mock import AsyncMock

import pytest

import backend.services.ai_reviewer.pr_dependency_graph as depgraph_module
import backend.services.section_config_service as section_service_module
from backend.services.ai_reviewer.pr_dependency_graph import PRDependencyGraphService


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("section_config", "legacy_mode", "expected_mode"),
    [
        ({"mode": "ai"}, "static", "ai"),
        ({"mode": "static"}, "ai", "static"),
        ({}, None, "static"),
        ({}, "ai", "ai"),
        ({}, "static", "static"),
        ({}, "invalid", "static"),
        ({"mode": "invalid"}, "ai", "ai"),
    ],
    ids=[
        "section-ai-wins",
        "section-static-wins",
        "legacy-missing",
        "legacy-ai",
        "legacy-static",
        "legacy-invalid",
        "invalid-section-uses-legacy",
    ],
)
async def test_resolve_depgraph_mode_validates_section_and_legacy_values(
    monkeypatch: pytest.MonkeyPatch,
    section_config: dict,
    legacy_mode: str | None,
    expected_mode: str,
):
    legacy_get = AsyncMock(return_value=legacy_mode)
    monkeypatch.setattr(
        section_service_module,
        "get_section_config",
        lambda _section_key: section_config,
    )
    monkeypatch.setattr(section_service_module, "get_dynamic_config", legacy_get)

    result = await section_service_module.section_config_service.resolve_depgraph_mode()

    assert result == expected_mode
    if section_config.get("mode", "").strip().lower() in {"ai", "static"}:
        legacy_get.assert_not_awaited()
    else:
        legacy_get.assert_awaited_once_with("pr_dependency_graph_mode")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("resolved_mode", "expected_mode"),
    [("ai", "ai"), ("static", "static"), ("invalid", "static"), (None, "static")],
    ids=["ai", "static", "invalid-fallback", "missing-fallback"],
)
async def test_runtime_graph_mode_keeps_explicit_values_and_falls_back_static(
    monkeypatch: pytest.MonkeyPatch,
    resolved_mode: str | None,
    expected_mode: str,
):
    resolver = AsyncMock(return_value=resolved_mode)
    warnings: list[str] = []
    monkeypatch.setattr(
        depgraph_module.section_config_service,
        "resolve_depgraph_mode",
        resolver,
    )
    monkeypatch.setattr(
        depgraph_module.logger,
        "warning",
        lambda message: warnings.append(message),
    )

    result = await PRDependencyGraphService._get_graph_mode()

    assert result == expected_mode
    if resolved_mode == "invalid":
        assert warnings == ["未知 PR 依赖图模式: invalid，回退到 static"]
    else:
        assert warnings == []
