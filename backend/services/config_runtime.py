"""Post-commit configuration effects and durable cross-process reconciliation."""

from __future__ import annotations

import asyncio
import json

from loguru import logger
from sqlalchemy import select

from backend.core.config import (
    Settings,
    get_settings,
    invalidate_dynamic_config_cache,
    invalidate_user_dynamic_config_cache,
    reload_label_config,
    reload_strategy_config,
    restore_settings_field,
    update_settings_field,
)
from backend.core.config_sections import (
    SECTION_REGISTRY,
    clear_section_store,
    update_section_store,
)
from backend.models.database import AppConfig, ConfigRevision
from backend.services.config_metadata import RESTART_KEYS, field_catalog

_scan_scheduler = None


def register_scan_scheduler(scheduler):
    global _scan_scheduler
    _scan_scheduler = scheduler


async def apply_config_values(values: dict) -> None:
    """Apply committed values, including inheritance after deletion."""
    invalidate_dynamic_config_cache(list(values))
    changed_runtime_keys = set()
    for key, value in values.items():
        if key in SECTION_REGISTRY:
            if value:
                update_section_store(key, json.loads(value))
            else:
                clear_section_store(key)
            if SECTION_REGISTRY[key]["target"] == "strategy":
                reload_strategy_config()
            else:
                reload_label_config()
                from backend.services.label_service import label_service

                label_service.reload_labels()
        elif key in Settings.model_fields and key not in RESTART_KEYS:
            before = getattr(get_settings(), key)
            if value is None:
                restore_settings_field(key)
            else:
                update_settings_field(key, value)
            if before != getattr(get_settings(), key):
                changed_runtime_keys.add(key)
    if "max_concurrent_reviews" in changed_runtime_keys:
        from backend.workers.review_worker import reset_review_semaphore

        reset_review_semaphore()
    if "max_concurrent_issues" in changed_runtime_keys:
        from backend.workers.issue_worker import reset_issue_semaphore

        reset_issue_semaphore()
    if any(key.startswith("star_aid_") for key in values):
        from backend.services.star_aid_scheduler import get_star_aid_scheduler

        scheduler = get_star_aid_scheduler()
        if scheduler:
            scheduler.restart_if_needed()
    if (
        _scan_scheduler
        and {"enable_repo_scan", "scan_interval_minutes"} & values.keys()
    ):
        _scan_scheduler.reconfigure()


class ConfigRuntimeSync:
    """Each process reconciles committed revisions; missed notifications recover.

    No configuration values are broadcast. A failed apply keeps its revision
    pending and is retried, including after an API committed but lost its reply.
    """

    def __init__(self):
        self.seen: dict[str, int] = {}
        self.pending: set[str] = set()
        self.lock = asyncio.Lock()

    def record_applied(self, revisions: dict[str, int]) -> None:
        """Acknowledge only committed revisions whose local effects completed.

        Writers and the poller hold ``lock`` while applying/acknowledging values.
        Capture integer revisions before committing or rolling back the ORM session.
        """
        self.seen.update(revisions)
        self.pending.difference_update(revisions)

    async def refresh(self, db):
        async with self.lock:
            # Only synchronize state this process actually caches. Model/account
            # resolution already reads the DB, and inactive users need no invalidation.
            from backend.core.config import _user_dynamic_config_cache
            from backend.webui.deps import _USER_PREFS_CACHE

            units = {
                "app/" + item.key
                for item in field_catalog().values()
                if item.storage in {"app", "section"}
            }
            user_ids = set(_USER_PREFS_CACHE) | {
                user_id for user_id, _ in _user_dynamic_config_cache
            }
            units.update(
                f"user/{uid}/{key}"
                for uid in user_ids
                for key in ("language", "theme", "items_per_page", "output_language")
            )
            self.seen = {
                key: revision for key, revision in self.seen.items() if key in units
            }
            revisions = (
                (
                    await db.execute(
                        select(ConfigRevision).where(
                            ConfigRevision.scope_key.in_(units)
                        )
                    )
                )
                .scalars()
                .all()
            )
            changed = [
                row for row in revisions if self.seen.get(row.scope_key) != row.revision
            ]
            keys = [
                row.scope_key[4:] for row in changed if row.scope_key.startswith("app/")
            ]
            if keys:
                result = await db.execute(
                    select(AppConfig).where(AppConfig.key_name.in_(keys))
                )
                saved = {row.key_name: row.key_value for row in result.scalars().all()}
                try:
                    await apply_config_values({key: saved.get(key) for key in keys})
                except BaseException:
                    self.pending.update("app/" + key for key in keys)
                    raise
                self.pending.difference_update("app/" + key for key in keys)
            for row in changed:
                if row.scope_key.startswith("user/"):
                    from backend.webui.deps import invalidate_user_prefs_cache

                    user_id = int(row.scope_key.split("/")[1])
                    invalidate_user_prefs_cache(user_id)
                    invalidate_user_dynamic_config_cache(user_id)
                self.seen[row.scope_key] = row.revision

    async def run(self):
        from backend.models import database

        while True:
            try:
                async with database.async_session() as db:
                    await self.refresh(db)
            except Exception:
                logger.exception(
                    "Configuration runtime synchronization failed; retrying"
                )
            await asyncio.sleep(get_settings().config_sync_interval_seconds)


config_runtime_sync = ConfigRuntimeSync()
