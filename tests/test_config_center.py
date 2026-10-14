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
