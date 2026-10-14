"""Configuration contract against real transactions and the HTTP boundary."""

import json

import httpx
import pytest
import pytest_asyncio
from fastapi import FastAPI
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from backend.api.v1 import config_center
from backend.api.v1.deps import require_api_auth
from backend.core.config import Settings, get_settings
from backend.core.config_sections import clear_section_store, get_section_config
from backend.models.admin_action_log import AdminActionLog
from backend.models.database import (
    AppConfig,
    Base,
    ConfigRevision,
    UserConfig,
    WebUIConfig,
)
from backend.services.config_center_service import ConfigCenterService, ConfigError
from backend.services.config_metadata import field_catalog
from backend.services.config_runtime import ConfigRuntimeSync
from backend.webui.deps import get_db, require_csrf_header

ADMIN = {"user_id": 1, "role": "super_admin", "sub": "admin"}
USER = {"user_id": 2, "role": "user", "sub": "user"}


@pytest_asyncio.fixture
async def config_db(tmp_path):
    import os

    from sqlalchemy.engine import make_url

    from backend.models.telegram_models import TelegramUser
    from backend.services.config_runtime import config_runtime_sync

    remote = os.environ.get("SAKURA_CONFIG_TEST_DATABASE_URL")
    if remote:
        destination = make_url(remote)
        assert destination.host in {"127.0.0.1", "localhost"}
        assert destination.database == "settings_test", (
            "Only the isolated CI database may be recreated"
        )
    engine = create_async_engine(
        remote or f"sqlite+aiosqlite:///{tmp_path / 'config.sqlite'}"
    )
    tables = [
        TelegramUser.__table__,
        AppConfig.__table__,
        ConfigRevision.__table__,
        UserConfig.__table__,
        WebUIConfig.__table__,
        AdminActionLog.__table__,
    ]
    async with engine.begin() as connection:
        if remote:
            await connection.run_sync(
                lambda conn: Base.metadata.drop_all(conn, tables=tables)
            )
        await connection.run_sync(
            lambda conn: Base.metadata.create_all(conn, tables=tables)
        )
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    async with sessions() as db:
        db.add_all(
            [
                TelegramUser(
                    id=1, github_username="settings-admin", role="super_admin"
                ),
                TelegramUser(id=2, github_username="settings-user", role="user"),
            ]
        )
        await db.commit()
    old = get_settings().model_dump()
    old_explicit_fields = set(get_settings().model_fields_set)
    config_runtime_sync.pending.clear()
    config_runtime_sync.seen.clear()
    yield sessions
    for key, value in old.items():
        setattr(get_settings(), key, value)
    get_settings().__pydantic_fields_set__ = old_explicit_fields
    config_runtime_sync.pending.clear()
    config_runtime_sync.seen.clear()
    clear_section_store()
    await engine.dispose()


async def field(service, db, field_id, user=ADMIN):
    return next(
        item for item in await service.describe(db, user) if item["id"] == field_id
    )


@pytest_asyncio.fixture
async def config_http(config_db):
    from backend.api.v1 import config, settings
    from backend.api.v1.deps import require_api_super_admin

    app = FastAPI()
    for router in (config.router, settings.router, config_center.router):
        app.include_router(router, prefix="/api/v1")
    app.dependency_overrides[require_api_auth] = lambda: USER
    app.dependency_overrides[require_api_super_admin] = lambda: ADMIN
    app.dependency_overrides[require_csrf_header] = lambda: "valid"

    async def session():
        async with config_db() as db:
            yield db

    app.dependency_overrides[get_db] = session
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        yield client


@pytest.mark.asyncio
async def test_legacy_settings_accept_numeric_strings_but_center_stays_strict(
    config_http,
):
    response = await config_http.patch(
        "/api/v1/settings", json={"theme": "dark", "language": "en"}
    )
    assert response.status_code == 200
    for value in ("10", "20", "50", "100", 20.0):
        response = await config_http.patch(
            "/api/v1/settings", json={"items_per_page": value}
        )
        assert response.status_code == 200 and response.json()["success"]
        saved = (await config_http.get("/api/v1/settings")).json()["data"]
        assert saved == {
            "theme": "dark",
            "language": "en",
            "items_per_page": int(value),
        }
    for value in ("invalid", "21", [], True):
        response = await config_http.patch(
            "/api/v1/settings", json={"items_per_page": value}
        )
        assert not response.json()["success"]
    current = next(
        item
        for item in (await config_http.get("/api/v1/config-center")).json()["fields"]
        if item["id"] == "personal/items_per_page"
    )
    for value in ("20", 20.0):
        response = await config_http.patch(
            "/api/v1/config-center",
            json={
                "field": current["id"],
                "version": current["version"],
                "value": value,
            },
        )
        assert response.status_code == 422
    assert (await config_http.get("/api/v1/settings")).json()["data"] == {
        "theme": "dark",
        "language": "en",
        "items_per_page": 20,
    }


@pytest.mark.asyncio
async def test_legacy_general_projects_inheritance_without_materializing_rows(
    config_db, config_http, monkeypatch
):
    from backend.core.config import get_all_dynamic_config_keys
    from backend.models.database import _build_default_configs

    monkeypatch.setenv("REVIEW_TIMEOUT_SECONDS", "321")
    monkeypatch.setenv("OUTPUT_LANGUAGE", "en")
    monkeypatch.setenv("EMBEDDING_API_KEY", "opaque-environment-credential")
    get_settings().review_timeout_seconds = 999
    async with config_db() as db:
        db.add_all(_build_default_configs())
        await db.commit()
    response = await config_http.get("/api/v1/config/general")
    configs = response.json()["data"]["configs"]
    assert set(get_all_dynamic_config_keys()) <= configs.keys()
    assert configs["review_timeout_seconds"] == "321"
    assert configs["enable_auto_review"] == "true"
    assert configs["output_language"] == "en"
    assert configs["embedding_api_key"] == "****"
    assert "opaque-environment-credential" not in response.text
    async with config_db() as db:
        assert len((await db.execute(select(AppConfig))).scalars().all()) == 1
        db.add_all(
            [
                AppConfig(key_name="review_timeout_seconds", key_value="123"),
                AppConfig(key_name="enable_auto_review", key_value="false"),
                AppConfig(key_name="output_language", key_value=""),
                AppConfig(
                    key_name="embedding_api_key", key_value="opaque-database-credential"
                ),
                AppConfig(key_name="historical_extension", key_value="retained"),
            ]
        )
        await db.commit()
    response = await config_http.get("/api/v1/config/general")
    configs = response.json()["data"]["configs"]
    assert configs["review_timeout_seconds"] == "123"
    assert configs["enable_auto_review"] == "false"
    assert configs["output_language"] == ""
    assert configs["historical_extension"] == "retained"
    assert configs["embedding_api_key"] == "****"
    assert "opaque-database-credential" not in response.text


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "path",
    [
        "reflection/model",
        "reflection/prompt_template",
        "issue_reflection/model",
        "issue_reflection/prompt_template",
        "consolidation/model",
    ],
)
async def test_nullable_section_strings_save_and_reset_without_changing_siblings(
    config_db, path
):
    from backend.services.config_metadata import lookup_path, replace_path

    service = ConfigCenterService()
    section = "strategy.context_enhancement"
    nested_path = ("sakura_memory", *path.split("/"))
    key = f"section/{section}/" + "/".join(nested_path)
    async with config_db() as db:
        await service.save_section(db, section, {"max_file_size": 123456}, mode="patch")
        current = await field(service, db, key)
        assert current["default"] is None and current["type"] == "string"
        saved = await service.mutate(
            db, ADMIN, key, version=current["version"], value="custom-model-or-template"
        )
        assert saved["value"] == "custom-model-or-template"
        assert lookup_path(get_section_config(section), nested_path) == saved["value"]
        for invalid in (42, False, [], {}):
            with pytest.raises(ConfigError, match="invalid_type"):
                await service.save_section(
                    db, section, replace_path({}, nested_path, invalid), mode="patch"
                )
        reset = await service.mutate(
            db, ADMIN, key, version=saved["version"], reset=True
        )
        assert reset["value"] is None and reset["source"] == "builtin"
        raw = (await db.execute(select(AppConfig))).scalar_one().key_value
        assert json.loads(raw) == {"max_file_size": 123456}
        # The legacy whole-section path also accepts the declared nullable type.
        await service.save_section(
            db, section, replace_path({}, nested_path, "legacy-string"), mode="patch"
        )
        assert (await field(service, db, key))["value"] == "legacy-string"
        await service.save_section(
            db, section, replace_path({}, nested_path, None), mode="patch"
        )
        assert (await field(service, db, key))["value"] is None


@pytest.mark.asyncio
async def test_personal_language_inherits_explicit_empty_global_override(
    config_db, monkeypatch
):
    monkeypatch.setenv("OUTPUT_LANGUAGE", "en")
    service = ConfigCenterService()
    async with config_db() as db:
        await service.save_app_updates(db, {"output_language": ""})
        current = await field(service, db, "personal/output_language", USER)
        assert current["value"] == current["inheritedValue"] == ""
        assert current["source"] == "database"
        assert get_settings().output_language == ""
        saved = await service.mutate(
            db, USER, current["id"], version=current["version"], value="zh-CN"
        )
        reset = await service.mutate(
            db, USER, current["id"], version=saved["version"], reset=True
        )
        assert reset["value"] == "" and reset["source"] == "database"
        global_field = await field(service, db, "global/output_language")
        await service.mutate(
            db, ADMIN, global_field["id"], version=global_field["version"], reset=True
        )
        inherited = await field(service, db, current["id"], USER)
        assert inherited["value"] == "en" and inherited["source"] == "environment"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("section", "path", "value"),
    [
        ("strategy.context_enhancement", "max_file_size", 123456),
        ("label.recommendation", "confidence_threshold", 0.91),
    ],
)
async def test_remote_section_writes_and_resets_are_pending_until_reconciled(
    config_db, section, path, value
):
    from backend.services.config_runtime import config_runtime_sync
    from backend.services.config_store import lock_config

    service = ConfigCenterService()
    key = f"section/{section}/{path}"
    async with config_db() as db:
        initial = await field(service, db, key)
        assert initial["effect"] == "live"
        # A different process commits without touching this process's section store.
        async with config_db() as remote:
            revision = await lock_config(remote, "app/" + section)
            remote.add(AppConfig(key_name=section, key_value=json.dumps({path: value})))
            revision.revision += 1
            await remote.commit()
        await db.rollback()
        changed = await field(service, db, key)
        assert changed["value"] == value and changed["effect"] == "pending"
        assert not config_runtime_sync.pending
        assert get_section_config(section)[path] == initial["value"]
        await config_runtime_sync.refresh(db)
        assert (await field(service, db, key))["effect"] == "live"
        assert get_section_config(section)[path] == value
        async with config_db() as remote:
            revision = await lock_config(remote, "app/" + section)
            row = (await remote.execute(select(AppConfig))).scalar_one()
            await remote.delete(row)
            revision.revision += 1
            await remote.commit()
        await db.rollback()
        reset = await field(service, db, key)
        assert reset["value"] == initial["value"] and reset["effect"] == "pending"
        await config_runtime_sync.refresh(db)
        assert (await field(service, db, key))["effect"] == "live"
        # Successful local application should not look pending until the next poll.
        await service.save_section(db, section, {path: value}, mode="patch")
        assert (await field(service, db, key))["effect"] == "live"


@pytest.mark.asyncio
async def test_save_reset_inheritance_and_unsubmitted_booleans(config_db, monkeypatch):
    monkeypatch.setenv("REVIEW_TIMEOUT_SECONDS", "321")
    service = ConfigCenterService()
    async with config_db() as db:
        initial = await field(service, db, "global/review_timeout_seconds")
        assert (
            initial["default"]
            == Settings.model_fields["review_timeout_seconds"].default
        )
        assert initial["value"] == 321 and initial["source"] == "environment"
        saved = await service.mutate(
            db, ADMIN, initial["id"], value=123, version=initial["version"]
        )
        assert saved["value"] == 123 and get_settings().review_timeout_seconds == 123
        assert get_settings().enable_auto_review is True
        reset = await service.mutate(
            db, ADMIN, initial["id"], version=saved["version"], reset=True
        )
        assert reset["value"] == 321 and reset["source"] == "environment"
        assert get_settings().review_timeout_seconds == 321
        assert not (await db.execute(select(AppConfig))).scalars().all()
        assert (await db.execute(select(ConfigRevision))).scalars().all()


@pytest.mark.asyncio
async def test_nested_reset_preserves_siblings_and_conflicts(config_db):
    service = ConfigCenterService()
    key = "section/strategy.context_enhancement/search_in_files/concurrency"
    async with config_db() as db:
        await service.save_section(
            db,
            "strategy.context_enhancement",
            {"max_file_size": 123456, "search_in_files": {"concurrency": 9}},
            mode="patch",
        )
        initial = await field(service, db, key)
        reset = await service.mutate(
            db, ADMIN, key, version=initial["version"], reset=True
        )
        assert reset["source"] == "builtin"
        assert (
            get_section_config("strategy.context_enhancement")["max_file_size"]
            == 123456
        )
        row = (await db.execute(select(AppConfig))).scalar_one()
        assert json.loads(row.key_value) == {"max_file_size": 123456}
        with pytest.raises(ConfigError, match="conflict"):
            await service.mutate(db, ADMIN, key, version=initial["version"], value=7)


@pytest.mark.asyncio
async def test_two_sessions_and_aba_reset_keep_durable_versions(config_db):
    a, b = ConfigCenterService(), ConfigCenterService()
    async with config_db() as one, config_db() as two:
        first = await field(a, one, "global/enable_auto_review")
        second = await field(b, two, first["id"])
        await two.rollback()
        saved = await a.mutate(
            one, ADMIN, first["id"], version=first["version"], value=False
        )
        await a.mutate(one, ADMIN, first["id"], version=saved["version"], reset=True)
        with pytest.raises(ConfigError, match="conflict"):
            await b.mutate(
                two, ADMIN, second["id"], version=second["version"], value=False
            )


@pytest.mark.parametrize(
    ("field_id", "value"),
    [
        ("global/enable_auto_review", "false"),
        ("global/max_concurrent_reviews", True),
        ("global/max_concurrent_reviews", 0),
        ("global/web_search_provider", "invalid"),
        ("global/review_timeout_seconds", float("inf")),
        ("section/strategy.file_filters/skip_extensions", [1]),
        ("section/strategy.context_enhancement/enable_ai_tools", 1),
    ],
)
@pytest.mark.asyncio
async def test_invalid_types_and_domains_do_not_persist(config_db, field_id, value):
    service = ConfigCenterService()
    async with config_db() as db:
        initial = await field(service, db, field_id)
        with pytest.raises(ConfigError):
            await service.mutate(
                db, ADMIN, field_id, value=value, version=initial["version"]
            )
        assert not (await db.execute(select(AppConfig))).scalars().all()


@pytest.mark.asyncio
async def test_user_isolation_and_null_reset_of_one_preference(config_db):
    service = ConfigCenterService()
    async with config_db() as db:
        fields = await service.describe(db, USER)
        assert {item["scope"] for item in fields} == {"user"}
        with pytest.raises(ConfigError, match="unknown_field"):
            await service.mutate(
                db, USER, "global/log_level", version="x", value="DEBUG"
            )
        await service.save_preferences(
            db, USER, {"language": "en", "items_per_page": 50}
        )
        current = await field(service, db, "personal/language", USER)
        await service.mutate(
            db, USER, current["id"], version=current["version"], reset=True
        )
        row = (await db.execute(select(WebUIConfig))).scalar_one()
        assert row.language is None and row.items_per_page == 50
        assert (await field(service, db, "personal/language", ADMIN))[
            "value"
        ] == "zh-CN"


@pytest.mark.asyncio
async def test_sensitive_projection_confirmation_mask_and_audit(config_db):
    service = ConfigCenterService()
    async with config_db() as db:
        secret = "opaque-test-credential-123456"
        initial = await field(service, db, "global/smtp_password")
        assert (
            initial["value"] is None
            and initial["default"] is None
            and not initial["resettable"]
        )
        for value, confirmed in [(secret, False), ("****", True), ("", True)]:
            with pytest.raises(ConfigError):
                await service.mutate(
                    db,
                    ADMIN,
                    initial["id"],
                    value=value,
                    version=initial["version"],
                    confirmed=confirmed,
                )
        saved = await service.mutate(
            db,
            ADMIN,
            initial["id"],
            value=secret,
            version=initial["version"],
            confirmed=True,
        )
        assert secret not in json.dumps(saved) and saved["hasValue"]
        audits = (await db.execute(select(AdminActionLog))).scalars().all()
        assert audits and all(secret not in row.detail for row in audits)


@pytest.mark.asyncio
async def test_runtime_reconciliation_reloads_deletion_and_restart(config_db):
    service = ConfigCenterService()
    sync = ConfigRuntimeSync()
    async with config_db() as db:
        await service.save_app_updates(
            db, {"review_timeout_seconds": 246, "app_timezone": "UTC"}
        )
        frozen = get_settings().app_timezone
        get_settings().review_timeout_seconds = 999
        await sync.refresh(db)
        assert (
            get_settings().review_timeout_seconds == 246
            and get_settings().app_timezone == frozen
        )
        value = await field(service, db, "global/review_timeout_seconds")
        await service.mutate(
            db, ADMIN, value["id"], version=value["version"], reset=True
        )
        get_settings().review_timeout_seconds = 999
        await sync.refresh(db)
        assert (
            get_settings().review_timeout_seconds == Settings().review_timeout_seconds
        )


@pytest.mark.asyncio
async def test_http_csrf_validation_conflict_and_secret_errors(config_db):
    app = FastAPI()
    app.include_router(config_center.router, prefix="/api/v1")
    app.dependency_overrides[require_api_auth] = lambda: ADMIN

    async def session():
        async with config_db() as db:
            yield db

    app.dependency_overrides[get_db] = session
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        data = await client.get("/api/v1/config-center?category=review")
        assert data.status_code == 200 and data.headers["cache-control"] == "no-store"
        initial = next(
            item
            for item in data.json()["fields"]
            if item["id"] == "global/enable_auto_review"
        )
        payload = {
            "field": initial["id"],
            "version": initial["version"],
            "value": False,
        }
        assert (
            await client.patch("/api/v1/config-center", json=payload)
        ).status_code in {403, 422}
        app.dependency_overrides[require_csrf_header] = lambda: "valid"
        assert (
            await client.patch("/api/v1/config-center", json=payload)
        ).status_code == 200
        assert (
            await client.patch("/api/v1/config-center", json=payload)
        ).status_code == 409
        bad = await client.patch(
            "/api/v1/config-center", json={**payload, "secret": "DO-NOT-ECHO"}
        )
        assert bad.status_code == 422 and "DO-NOT-ECHO" not in bad.text
        app.dependency_overrides[require_api_auth] = lambda: USER
        response = await client.get("/api/v1/config-center?category=system")
        assert response.status_code == 404 and "smtp" not in response.text


def test_catalog_defaults_do_not_follow_mutable_settings():
    settings = get_settings()
    original = settings.review_timeout_seconds
    try:
        settings.review_timeout_seconds = 9999
        assert (
            field_catalog()["global/review_timeout_seconds"].default
            == Settings.model_fields["review_timeout_seconds"].default
        )
    finally:
        settings.review_timeout_seconds = original


@pytest.mark.asyncio
async def test_legacy_dependency_graph_mode_resets_without_touching_prompts(config_db):
    service = ConfigCenterService()
    async with config_db() as db:
        db.add(
            AppConfig(
                key_name="strategy.pr_dependency_graph",
                key_value=json.dumps({"mode": "ai", "system_prompt": "Custom prompt"}),
            )
        )
        db.add(AppConfig(key_name="pr_dependency_graph_mode", key_value="ai"))
        await db.commit()
        current = await field(service, db, "global/pr_dependency_graph_mode")
        assert current["value"] == "ai"
        value = await service.mutate(
            db, ADMIN, current["id"], version=current["version"], reset=True
        )
        assert value["value"] == "static" and value["source"] == "builtin"
        rows = (await db.execute(select(AppConfig))).scalars().all()
        assert len(rows) == 1 and json.loads(rows[0].key_value) == {
            "system_prompt": "Custom prompt"
        }


@pytest.mark.asyncio
async def test_model_leaf_reset_and_legacy_partial_updates_preserve_siblings(config_db):
    service = ConfigCenterService()
    async with config_db() as db:
        await service.save_model_overrides(
            db,
            "openai",
            "test-model",
            {
                "capabilities": {"vision": True, "thinking": True},
                "context_window_tokens": 54321,
            },
        )
        fields = await service.describe(db, ADMIN, model=("openai", "test-model"))
        vision = next(
            item for item in fields if item["path"] == ["capabilities", "vision"]
        )
        await service.mutate(
            db, ADMIN, vision["id"], version=vision["version"], reset=True
        )
        row = (await db.execute(select(AppConfig))).scalar_one()
        assert json.loads(row.key_value) == {
            "capabilities": {"thinking": True},
            "context_window_tokens": 54321,
        }
        await service.save_model_overrides(
            db, "openai", "test-model", {"max_output_tokens": 1000}
        )
        assert json.loads(row.key_value)["capabilities"]["thinking"] is True
        with pytest.raises(ConfigError):
            await service.save_model_overrides(
                db, "openai", "test-model", {"capabilities": {"vision": "false"}}
            )


@pytest.mark.asyncio
async def test_binding_has_domain_validation_and_cannot_reset_to_unconfigured(
    config_db,
):
    service = ConfigCenterService()
    async with config_db() as db:
        db.add(
            AppConfig(
                key_name="ai_account.acc_existing",
                key_value=json.dumps(
                    {"id": "acc_existing", "provider_id": "openai", "api_key": "secret"}
                ),
            )
        )
        await db.commit()
        current = await field(service, db, "binding/main")
        with pytest.raises(ConfigError):
            await service.mutate(
                db,
                ADMIN,
                current["id"],
                version=current["version"],
                value={
                    "primary": {"account": "acc_missing", "model": "model"},
                    "fallback": [],
                },
            )
        saved = await service.mutate(
            db,
            ADMIN,
            current["id"],
            version=current["version"],
            value={
                "primary": {"account": "acc_existing", "model": "model"},
                "fallback": [],
            },
        )
        assert not saved["resettable"]
        with pytest.raises(ConfigError, match="not_resettable"):
            await service.mutate(
                db, ADMIN, saved["id"], version=saved["version"], reset=True
            )
        account = (
            await db.execute(
                select(AppConfig).where(AppConfig.key_name == "ai_account.acc_existing")
            )
        ).scalar_one()
        assert json.loads(account.key_value)["api_key"] == "secret"


@pytest.mark.asyncio
async def test_committed_runtime_failure_is_reported_pending_and_can_reconcile(
    config_db, monkeypatch
):
    from backend.services import config_center_service as module

    service = ConfigCenterService()
    async with config_db() as db:
        current = await field(service, db, "global/review_timeout_seconds")

        async def fail(_values):
            raise RuntimeError("runtime unavailable")

        with monkeypatch.context() as patch:
            patch.setattr(module, "apply_config_values", fail)
            saved = await service.mutate(
                db, ADMIN, current["id"], version=current["version"], value=155
            )
        assert saved["persisted"] and saved["effect"] == "pending"
        await ConfigRuntimeSync().refresh(db)
        assert get_settings().review_timeout_seconds == 155


@pytest.mark.asyncio
async def test_invalid_batch_rolls_back_and_deployment_connection_is_protected(
    config_db,
):
    service = ConfigCenterService()
    async with config_db() as db:
        with pytest.raises(ConfigError):
            await service.save_app_updates(
                db, {"enable_auto_review": False, "max_concurrent_reviews": 0}
            )
        assert not (await db.execute(select(AppConfig))).scalars().all()
        current = await field(service, db, "global/database_url")
        assert not current["writable"] and current["source"] == "deployment"
        with pytest.raises(ConfigError, match="deployment_owned"):
            await service.mutate(
                db,
                ADMIN,
                current["id"],
                version=current["version"],
                value="mysql://new-host/other",
                confirmed=True,
            )


def _process_write(url, version, queue):
    """Independent process: no asyncio locks or configuration caches are shared."""
    import asyncio

    async def write():
        engine = create_async_engine(url)
        try:
            async with async_sessionmaker(engine, expire_on_commit=False)() as db:
                try:
                    await ConfigCenterService().mutate(
                        db,
                        ADMIN,
                        "global/enable_auto_review",
                        version=version,
                        value=False,
                    )
                    queue.put("saved")
                except ConfigError as exc:
                    queue.put(exc.code)
        finally:
            await engine.dispose()

    asyncio.run(write())


@pytest.mark.asyncio
async def test_independent_processes_cannot_overwrite_same_version(config_db):
    import asyncio
    import multiprocessing

    async with config_db() as db:
        current = await field(ConfigCenterService(), db, "global/enable_auto_review")
        url = db.bind.url.render_as_string(hide_password=False)
    context = multiprocessing.get_context("spawn")
    queue = context.Queue()
    processes = [
        context.Process(target=_process_write, args=(url, current["version"], queue))
        for _ in range(2)
    ]
    try:
        for process in processes:
            process.start()
        for process in processes:
            await asyncio.to_thread(process.join, 45)
            assert process.exitcode == 0
        assert sorted([queue.get(timeout=5), queue.get(timeout=5)]) == [
            "conflict",
            "saved",
        ]
    finally:
        for process in processes:
            if process.is_alive():
                process.terminate()
                process.join()
        queue.close()


def test_mfa_enrollment_still_reaches_the_personal_center_only():
    from backend.webui.deps import _is_mfa_enrollment_path

    assert _is_mfa_enrollment_path("/settings-center/personal")
    assert not _is_mfa_enrollment_path("/settings-center/system")


@pytest.mark.asyncio
async def test_account_edit_version_cannot_restore_an_old_credential(
    config_db, monkeypatch
):
    from backend.core.ai_protocol import account_store
    from backend.models import database

    monkeypatch.setattr(database, "async_session", config_db)
    saved = await account_store.save_account(
        account_store.ProviderAccount(
            id="", name="test", provider_id="openai", api_key="original-private-value"
        )
    )
    first = await account_store.get_account(saved.id)
    stale = await account_store.get_account(saved.id)
    first.api_key = "replacement-private-value"
    await account_store.save_account(first)
    stale.notes = "stale editor"
    with pytest.raises(account_store.AccountConflictError):
        await account_store.save_account(stale)
    current = await account_store.get_account(saved.id)
    assert current.api_key == "replacement-private-value" and current.notes == ""
    assert current.to_public_dict()["api_key"] == "****"


@pytest.mark.asyncio
async def test_account_delete_rechecks_binding_under_database_lock(
    config_db, monkeypatch
):
    from backend.core.ai_protocol import account_store
    from backend.models import database

    monkeypatch.setattr(database, "async_session", config_db)
    saved = await account_store.save_account(
        account_store.ProviderAccount(id="", name="test", provider_id="openai")
    )
    async with config_db() as db:
        db.add(
            AppConfig(
                key_name="ai_role_bindings",
                key_value=json.dumps(
                    {
                        "main": {
                            "primary": {"account": saved.id, "model": "test"},
                            "fallback": [],
                        }
                    }
                ),
            )
        )
        await db.commit()

    async def stale_precheck():
        return {}  # A concurrent bind committed after the early precheck.

    monkeypatch.setattr(account_store, "get_role_bindings", stale_precheck)
    assert not await account_store.delete_account(saved.id)
    assert await account_store.get_account(saved.id)


@pytest.mark.asyncio
@pytest.mark.parametrize("key", ["embedding_model", "rerank_model"])
async def test_existing_provider_client_refresh_remains_live(config_db, key):
    service = ConfigCenterService()
    async with config_db() as db:
        current = await field(service, db, "global/" + key)
        saved = await service.mutate(
            db, ADMIN, current["id"], version=current["version"], value="changed-model"
        )
        assert not saved["requiresRestart"] and saved["effect"] == "live"
        assert getattr(get_settings(), key) == "changed-model"


@pytest.mark.asyncio
async def test_frozen_listener_and_github_identity_require_restart(config_db):
    service = ConfigCenterService()
    original = get_settings().app_port
    async with config_db() as db:
        current = await field(service, db, "global/app_port")
        saved = await service.mutate(
            db, ADMIN, current["id"], version=current["version"], value=original + 1
        )
        assert saved["effect"] == "restart_required"
        assert get_settings().app_port == original
        assert (await field(service, db, "global/github_app_id"))["requiresRestart"]
