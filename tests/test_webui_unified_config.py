"""配置中心回归测试（SPA 迁移后保留的活跃契约）。

覆盖：旧配置链接经 entrypoints 表 302 到 SPA /app/* 入口、
issue 模板投影与校验、动态配置组结构契约、agent-team 配置端点移除。
旧模板页与其表单保存端点已随 legacy WebUI 一并退役。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient

from backend.api.v1.agent import router as agent_team_router
from backend.core import config_sections
from backend.core.config import DYNAMIC_CONFIG_GROUPS, DYNAMIC_CONFIG_RANGES
from backend.webui.routes.entrypoints import router as entrypoints_router
from tests.config_sqlite_session import SQLiteConfigSession as _FakeSession


@pytest.fixture(autouse=True)
def _clean_section_store():
    """每个测试前后清空进程级节存储，避免测试间串扰。"""
    config_sections.clear_section_store()
    yield
    config_sections.clear_section_store()


def test_agent_team_config_save_endpoint_removed():
    """团队页配置保存端点已移除（A9：其余功能保留）。"""
    paths = [
        route.path for route in agent_team_router.routes if isinstance(route, APIRoute)
    ]
    assert "/agent-team/config/save" not in paths


# ---------- 旧链接入口重定向（entrypoints 挂载路由） ----------


def _entrypoints_client() -> TestClient:
    app = FastAPI()
    app.include_router(entrypoints_router)
    return TestClient(app)


@pytest.mark.parametrize(
    ("legacy", "destination"),
    [
        ("/config", "/app/settings/review"),
        ("/config/", "/app/settings/review"),
        ("/config/ai", "/app/settings/ai"),
        ("/config/backup", "/app/settings/backup"),
        ("/settings-center/review", "/app/settings/review"),
        ("/system-config", "/app/settings/system"),
        ("/version/manager", "/app/admin/version"),
    ],
)
def test_legacy_config_urls_redirect_to_app_settings(legacy, destination):
    """旧配置/版本页链接经挂载的 entrypoints 表 302 到 SPA /app/* 入口。"""
    response = _entrypoints_client().get(legacy, follow_redirects=False)
    assert response.status_code == 302
    assert response.headers["location"] == destination
    assert response.headers["cache-control"] == "no-store"


def test_entrypoint_redirect_strips_token_queries_and_keeps_the_rest():
    """重定向剥离 token/access_token/csrf_token 查询参数，其余查询保留。"""
    client = _entrypoints_client()
    response = client.get("/config?token=secret&next=/x", follow_redirects=False)
    assert response.status_code == 302
    assert response.headers["location"] == "/app/settings/review?next=%2Fx"


# ---------- 统一页渲染 ----------


@pytest.mark.asyncio
@pytest.mark.parametrize("language", ["zh-CN", "en"])
@pytest.mark.parametrize("custom", [False, True])
async def test_issue_template_editor_displays_defaults_or_existing_custom_values(
    monkeypatch, language, custom
):
    from backend.models.database import AppConfig
    from backend.services.config_center_service import config_center_service

    db = _FakeSession(
        [
            AppConfig(
                key_name="strategy.issue_analysis",
                key_value=json.dumps(
                    {
                        "comment_template": "自定义\n{summary}\n{feasibility}",
                        "comment_template_en": "Custom\n{summary}\n{feasibility}",
                    }
                ),
            )
        ]
        if custom
        else []
    )
    # A projection uses persisted overrides, not a mutated process-local facade.
    # Supply only the real app/revision rows needed by this unit projection.
    user = {"sub": "admin", "role": "super_admin", "user_id": 1}
    from backend.models.database import Base, UserConfig, WebUIConfig

    Base.metadata.create_all(
        db.engine, tables=[UserConfig.__table__, WebUIConfig.__table__]
    )
    fields = {
        item["id"]: item
        for item in await config_center_service.describe(
            db, user, category="issue", lang=language
        )
    }
    for key in ("comment_template", "comment_template_en"):
        item = fields[f"section/strategy.issue_analysis/{key}"]
        assert "{summary}" in item["value"] and "{feasibility}" in item["value"]
        assert item["source"] == ("database" if custom else "builtin")
        if custom:
            assert item["value"].startswith(
                "Custom" if key.endswith("_en") else "自定义"
            )


# ---------- 分组重组（任务 5） ----------


def test_issue_label_behavior_keys_unified_into_recommendation():
    """issue_auto_create_labels / issue_confidence_threshold 彻底移除：
    PR 与 Issue 标签统一读 label.recommendation 节设置。"""
    from backend.core.config import DYNAMIC_CONFIG_GROUPS, Settings

    for group in DYNAMIC_CONFIG_GROUPS.values():
        keys = set(group["keys"])
        assert "issue_auto_create_labels" not in keys
        assert "issue_confidence_threshold" not in keys
    assert "issue_auto_create_labels" not in Settings.model_fields
    assert "issue_confidence_threshold" not in Settings.model_fields
    assert "label_behavior" not in DYNAMIC_CONFIG_GROUPS


def test_protocol_repair_registered_once_in_review_basic():
    """protocol_repair_max_attempts 双保存路径合一（A10）：仅 review_basic 组。"""
    issue_keys = set(DYNAMIC_CONFIG_GROUPS["issue_analysis"]["keys"])
    assert "protocol_repair_max_attempts" not in issue_keys
    assert "protocol_repair_max_attempts" in set(
        DYNAMIC_CONFIG_GROUPS["review_basic"]["keys"]
    )
    assert DYNAMIC_CONFIG_RANGES["protocol_repair_max_attempts"] == (1, 10)


def test_depgraph_mode_removed_from_dynamic_group():
    """pr_dependency_graph_mode 由节表单单源管理，平铺组不再暴露。"""
    depgraph_keys = set(DYNAMIC_CONFIG_GROUPS["pr_dependency_graph"]["keys"])
    assert "pr_dependency_graph_mode" not in depgraph_keys
    assert "enable_pr_dependency_graph" in depgraph_keys


def test_depgraph_template_falls_back_to_static_mode():
    """模板上下文缺少 mode 时应与运行时默认值一致地选择 static。"""
    from backend.services.config_metadata import field_catalog

    assert field_catalog()["global/pr_dependency_graph_mode"].default == "static"


def test_issue_config_hint_excludes_removed_auto_comment_toggle():
    zh = (
        Path(__file__).parents[1] / "backend" / "webui" / "translations" / "zh-CN.yaml"
    ).read_text(encoding="utf-8")
    en = (
        Path(__file__).parents[1] / "backend" / "webui" / "translations" / "en.yaml"
    ).read_text(encoding="utf-8")

    zh_hint = next(line for line in zh.splitlines() if "issue_config_hint:" in line)
    en_hint = next(line for line in en.splitlines() if "issue_config_hint:" in line)
    assert "自动评论" not in zh_hint
    assert "auto comment" not in en_hint.lower()


# ---------- general/save 通用循环 ----------


@pytest.mark.parametrize("saved_delay", ["0", "60", "0.001"])
@pytest.mark.asyncio
async def test_dependency_retry_inputs_render_bounds_and_typed_steps(
    monkeypatch, saved_delay
):
    from backend.services.config_metadata import field_catalog

    fields = field_catalog()
    attempts = fields["global/agent_team_dependency_install_attempts"]
    delay = fields["global/agent_team_dependency_retry_delay_seconds"]
    assert attempts.type == "integer" and attempts.constraints == {"ge": 1, "le": 5}
    assert delay.type == "number" and delay.constraints == {"ge": 0.0, "le": 60.0}
    db = _FakeSession()
    from backend.services.config_center_service import config_center_service

    await config_center_service.save_app_updates(db, {delay.key: saved_delay})
    assert float(db.rows[0].key_value) == float(saved_delay)
