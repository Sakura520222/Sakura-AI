import json
from unittest.mock import AsyncMock

import pytest

import backend.services.ai_reviewer.pr_dependency_graph as depgraph_module
import backend.services.section_config_service as section_service_module
from backend.services.ai_reviewer.pr_dependency_graph import PRDependencyGraphService
from backend.webui.routes import config as config_routes


class _FormRequest:
    def __init__(self, fields: dict[str, str]) -> None:
        self._fields = fields

    async def form(self) -> dict[str, str]:
        return self._fields


class _JsonRequest:
    def __init__(self, payload: dict) -> None:
        self._payload = payload

    async def json(self) -> dict:
        return self._payload


class _FakeSectionConfigService:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict, str]] = []

    async def save_section(
        self, _db, section_key: str, data: dict, *, mode: str
    ) -> dict:
        self.calls.append((section_key, dict(data), mode))
        return {"changes": {}}

    @staticmethod
    def build_audit_log(result: dict) -> dict:
        return result["changes"]


def _patch_strategy_save(monkeypatch: pytest.MonkeyPatch):
    service = _FakeSectionConfigService()
    monkeypatch.setattr(config_routes, "section_config_service", service)
    monkeypatch.setattr(config_routes, "log_admin_action", AsyncMock())
    monkeypatch.setattr(config_routes, "detect_language", lambda *_args: "zh-CN")
    return service


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("submitted_mode", "expected_mode"),
    [
        (None, "static"),
        ("invalid", "static"),
        ("ai", "ai"),
        ("static", "static"),
    ],
    ids=["missing", "invalid", "explicit-ai", "explicit-static"],
)
async def test_depgraph_post_normalizes_missing_and_invalid_modes(
    monkeypatch: pytest.MonkeyPatch,
    submitted_mode: str | None,
    expected_mode: str,
):
    service = _patch_strategy_save(monkeypatch)
    fields = {"section": "depgraph"}
    if submitted_mode is not None:
        fields["pr_dependency_graph_mode"] = submitted_mode

    response = await config_routes.save_strategies_section(
        _FormRequest(fields),
        db=object(),
        user={"sub": "admin", "user_id": 1},
        csrf_token="token",
        section="depgraph",
    )

    assert response.status_code == 302
    assert service.calls == [
        (
            "strategy.pr_dependency_graph",
            {
                "mode": expected_mode,
                "system_prompt": "",
                "user_template": "",
            },
            "replace",
        )
    ]


@pytest.mark.asyncio
async def test_save_all_depgraph_reuses_post_mode_default(
    monkeypatch: pytest.MonkeyPatch,
):
    service = _patch_strategy_save(monkeypatch)
    payload = {
        "requests": [
            {
                "action": "/config/strategies/save",
                "anchor": "section-strategy-depgraph",
                "fields": {
                    "section": "depgraph",
                    "pr_dependency_graph_mode": "not-a-mode",
                },
            }
        ]
    }

    response = await config_routes.save_all_config(
        _JsonRequest(payload),
        db=object(),
        user={"sub": "admin", "user_id": 1},
        csrf_token="token",
    )

    body = json.loads(response.body)
    assert body["ok"] is True
    assert service.calls[0][1]["mode"] == "static"


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
