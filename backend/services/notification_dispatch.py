"""Durable event dispatch using the existing delivery rows and Provider registry."""
from __future__ import annotations

import asyncio
import json
import secrets
from contextlib import suppress
from datetime import timedelta

from loguru import logger
from sqlalchemy import exists, or_, select, update

from backend.core.config import get_settings
from backend.core.time_service import now_utc
from backend.models.announcement_models import Announcement, NotificationDelivery
from backend.models.identity_models import NotificationEndpoint
from backend.models.notification_models import (
    NotificationEvent,
    NotificationProviderBudget,
    UserNotification,
)
from backend.models.telegram_models import TelegramUser
from backend.services.notification_events import (
    NotificationMessage,
    candidates,
    insert_once,
    is_sensitive_event,
    resource_allowed,
    routing_rules,
)


def _due(model, now):
    return (
        model.status == "pending",
        or_(model.next_retry_at.is_(None), model.next_retry_at <= now),
        or_(model.claim_token.is_(None), model.claim_until.is_(None), model.claim_until <= now),
    )


def _private_endpoint(endpoint):
    if not endpoint.verified:
        return False
    channel = str(endpoint.provider).lower()
    if channel == "email":
        return True
    if channel == "telegram":
        return endpoint.address.isdecimal() and int(endpoint.address) > 0
    # A URL challenge proves control of a URL, not a private person's inbox.
    return False


def _routed(event, user_id, endpoint, rules):
    if event.event_type == "endpoint.test":
        return endpoint is not None and endpoint.id == json.loads(event.payload_json).get("endpoint_id")
    target = endpoint.id if endpoint else None
    if user_id in rules:
        return target in rules[user_id]
    # Preserve existing announcement routing; new business events require
    # explicit external opt-in. A station inbox is independent of providers.
    return endpoint is None or event.event_type == "announcement.published"


class EventDeliveryMixin:
    async def materialize_event(self, db, event_id):
        """Create inbox/delivery rows and finish fanout in one transaction."""
        event = (await db.execute(select(NotificationEvent).where(
            NotificationEvent.id == event_id, *_due(NotificationEvent, now_utc())
        ).with_for_update(skip_locked=True))).scalar_one_or_none()
        if not event or event.status != "pending":
            return False
        token = secrets.token_urlsafe(32)
        now = now_utc()
        won = await db.execute(update(NotificationEvent).where(
            NotificationEvent.id == event_id, *_due(NotificationEvent, now)
        ).values(claim_token=token, claim_until=now + timedelta(seconds=self._delivery_lease_seconds())))
        if won.rowcount != 1:
            await db.rollback()
            return False
        # This claim is deliberately held in the fanout transaction. A crash
        # rolls back both ownership and rows. Unique keys protect replays.
        ids, rules = await candidates(db, event)
        count = 0
        resource_cache = {}  # Only this fanout transaction; sends/history read afresh.
        batch_size = max(1, int(get_settings().notification_batch_size))
        for offset in range(0, len(ids), batch_size):
            batch_ids = ids[offset:offset + batch_size]
            users = (await db.execute(select(TelegramUser).where(
                TelegramUser.id.in_(batch_ids)
            ).order_by(TelegramUser.id).with_for_update(read=True)
              .execution_options(populate_existing=True))).scalars().all()
            allowed_users = []
            for user in users:
                if await resource_allowed(db, user, event, resource_cache=resource_cache):
                    allowed_users.append(user)
            endpoints_by_user = {}
            if allowed_users:
                endpoints = (await db.execute(select(NotificationEndpoint).where(
                    NotificationEndpoint.user_id.in_([user.id for user in allowed_users]),
                    NotificationEndpoint.enabled.is_(True)
                ).order_by(NotificationEndpoint.id).with_for_update()
                  .execution_options(populate_existing=True))).scalars().all()
                for endpoint in endpoints:
                    endpoints_by_user.setdefault(endpoint.user_id, []).append(endpoint)
            for user in allowed_users:
                count += await self._materialize_recipient(db, event, user.id,
                    endpoints_by_user.get(user.id, ()), rules, now)
        await db.execute(update(NotificationEvent).where(
            NotificationEvent.id == event_id, NotificationEvent.claim_token == token
        ).values(status="ready" if count else "no_recipients", error_category=None if count else "no_recipients",
                 claim_token=None, claim_until=None))
        await db.commit()
        return True

    async def _materialize_recipient(self, db, event, user_id, endpoints, rules, now):
        """Write one recipient using prefiltered, grouped routing and endpoints."""
        event_id = event.id
        routed = []
        if _routed(event, user_id, None, rules):
            await insert_once(db, UserNotification,
                {"event_id": event_id, "user_id": user_id, "created_at": now}, ["event_id", "user_id"])
            routed.append(("web", None, "web"))
        for endpoint in endpoints:
            channel = str(endpoint.provider).lower()
            if self.registry.get(channel) is None or channel == "web":
                continue
            if not endpoint.verified or (is_sensitive_event(event) and not _private_endpoint(endpoint)):
                continue
            if _routed(event, user_id, endpoint, rules):
                routed.append((channel, endpoint.id, f"endpoint:{endpoint.id}"))
        for channel, endpoint_id, key in routed:
            await insert_once(db, NotificationDelivery, {
                "event_id": event_id, "user_id": user_id, "endpoint_id": endpoint_id,
                "endpoint_key": key, "channel": channel, "status": "pending", "attempts": 0,
                "publication_version": int(event.run_key) if event.resource_type == "announcement" else 1,
                "announcement_id": int(event.resource_id) if event.resource_type == "announcement" else None,
                "created_at": now, "updated_at": now,
            }, ["event_id", "user_id", "endpoint_key"])
        return len(routed)

    async def _event_eligible(self, db, delivery, event):
        user = await db.get(TelegramUser, delivery.user_id, populate_existing=True)
        if not await resource_allowed(db, user, event):
            return False, None
        endpoint = None
        if delivery.channel != "web":
            # A missing/deleted target is never replaced with another address.
            endpoint = await db.get(NotificationEndpoint, delivery.endpoint_id, populate_existing=True) if delivery.endpoint_id else None
            if not endpoint or endpoint.user_id != user.id or str(endpoint.provider).lower() != str(delivery.channel).lower():
                return False, None
            if not endpoint.enabled or not endpoint.verified:
                return False, None
            if is_sensitive_event(event) and not _private_endpoint(endpoint):
                return False, None
        rules = await routing_rules(db, event, user_ids=[user.id])
        return _routed(event, user.id, endpoint, rules), endpoint

    async def _finish_event_delivery(self, db, delivery_id, token, **values):
        values.update(claim_token=None, claim_until=None, updated_at=now_utc())
        conditions = [NotificationDelivery.id == delivery_id, NotificationDelivery.claim_token == token]
        if values.get("status") != "cancelled":
            conditions.append(or_(NotificationDelivery.announcement_id.is_(None), exists(select(Announcement.id).where(
                Announcement.id == NotificationDelivery.announcement_id,
                Announcement.status == "published",
                Announcement.publication_version == NotificationDelivery.publication_version,
            ))))
        result = await db.execute(update(NotificationDelivery).where(
            *conditions,
        ).values(**values))
        await db.commit()
        return result.rowcount == 1

    async def _reserve_provider_start(self, db, channel):
        """Atomic deployment-wide start budget. No in-memory lock is required."""
        now = now_utc()
        channel = str(channel).lower()
        interval = float(get_settings().notification_rate_limit_seconds)
        await insert_once(db, NotificationProviderBudget,
                          {"channel": channel, "next_available_at": now - timedelta(seconds=1)}, ["channel"])
        result = await db.execute(update(NotificationProviderBudget).where(
            NotificationProviderBudget.channel == channel,
            NotificationProviderBudget.next_available_at <= now,
        ).values(next_available_at=now + timedelta(seconds=interval)))
        if result.rowcount == 1:
            await db.commit()
            return None
        available = (await db.execute(select(NotificationProviderBudget.next_available_at).where(
            NotificationProviderBudget.channel == channel
        ))).scalar_one()
        await db.rollback()
        return available

    async def _record_provider_cooldown(self, db, budget_key, delay):
        until = now_utc() + timedelta(seconds=delay)
        await insert_once(db, NotificationProviderBudget,
            {"channel": budget_key, "next_available_at": now_utc() - timedelta(seconds=1)}, ["channel"])
        await db.execute(update(NotificationProviderBudget).where(
            NotificationProviderBudget.channel == budget_key,
            NotificationProviderBudget.next_available_at < until,
        ).values(next_available_at=until))
        await db.commit()

    async def _event_heartbeat(self, fallback, row_id, token, stop):
        interval = max(0.1, self._delivery_lease_seconds() / 3)
        while not stop.is_set():
            try:
                await asyncio.wait_for(stop.wait(), interval)
                return
            except TimeoutError:
                pass

            async def renew(session):
                result = await session.execute(update(NotificationDelivery).where(
                    NotificationDelivery.id == row_id, NotificationDelivery.claim_token == token
                ).values(claim_until=now_utc() + timedelta(seconds=self._delivery_lease_seconds())))
                await session.commit()
                return result.rowcount == 1

            try:
                if not await self._run_in_fresh_session(fallback, renew):
                    return
            except Exception:
                logger.warning("Notification lease renewal unavailable: delivery={}", row_id)
                return

    async def deliver_event_row(self, db, row_id):
        from backend.services.announcement_service import sanitize_markdown
        from backend.services.notification_service import (
            NotificationProviderDisabled,
            NotificationProviderPermanentError,
            NotificationProviderRetryAfter,
        )

        token = secrets.token_urlsafe(32)
        now = now_utc()
        won = await db.execute(update(NotificationDelivery).where(
            NotificationDelivery.id == row_id, NotificationDelivery.event_id.is_not(None),
            *_due(NotificationDelivery, now),
        ).values(claim_token=token, claim_until=now + timedelta(seconds=self._delivery_lease_seconds())))
        if won.rowcount != 1:
            await db.rollback()
            return False
        await db.commit()
        delivery = await db.get(NotificationDelivery, row_id, populate_existing=True)
        event = await db.get(NotificationEvent, delivery.event_id, populate_existing=True)
        db.expunge(delivery)
        db.expunge(event)
        attempts = delivery.attempts
        settings = get_settings()
        try:
            if attempts >= settings.notification_retry_max_attempts:
                return await self._finish_event_delivery(db, row_id, token,
                    status="failed", error_category="exhausted", error_message="Delivery attempt budget exhausted", next_retry_at=None)
            eligible, _ = await self._event_eligible(db, delivery, event)
            if not eligible:
                return await self._finish_event_delivery(db, row_id, token,
                    status="cancelled", error_category="ineligible", error_message="Recipient no longer eligible", next_retry_at=None)
            await db.rollback()  # End snapshot before the final authorization read.
            provider = self.registry.get(delivery.channel)
            if provider is None:
                raise NotificationProviderDisabled("Provider not registered")
            if delivery.channel != "web":
                cooldown_key = f"endpoint:{str(delivery.channel).lower()}:{delivery.endpoint_id}"
                cooldown = (await db.execute(select(NotificationProviderBudget.next_available_at).where(
                    NotificationProviderBudget.channel == cooldown_key
                ))).scalar_one_or_none()
                if cooldown and cooldown > now_utc():
                    return await self._finish_event_delivery(db, row_id, token,
                        status="pending", error_category="rate_limit", next_retry_at=cooldown)
                available = await self._reserve_provider_start(db, delivery.channel)
                if available:
                    return await self._finish_event_delivery(db, row_id, token,
                        status="pending", error_category="rate_limit", next_retry_at=available)
            # Refresh both resource scope and the exact address immediately before send.
            await db.rollback()
            eligible, endpoint = await self._event_eligible(db, delivery, event)
            if not eligible:
                return await self._finish_event_delivery(db, row_id, token,
                    status="cancelled", error_category="ineligible", error_message="Recipient no longer eligible", next_retry_at=None)
            message = NotificationMessage.from_event(event)
            # Snapshot values before closing the read transaction; rollback expires ORM objects.
            if endpoint:
                db.expunge(endpoint)
            await db.rollback()
            # Fence a stale worker after slow authorization reads, and persist
            # the send attempt before external I/O so a crash cannot reset it.
            owned = await db.execute(update(NotificationDelivery).where(
                NotificationDelivery.id == row_id, NotificationDelivery.claim_token == token,
                NotificationDelivery.claim_until > now_utc(),
                or_(NotificationDelivery.announcement_id.is_(None), exists(select(Announcement.id).where(
                    Announcement.id == NotificationDelivery.announcement_id,
                    Announcement.status == "published",
                    Announcement.publication_version == NotificationDelivery.publication_version,
                ))),
            ).values(attempts=NotificationDelivery.attempts + 1,
                     claim_until=now_utc() + timedelta(seconds=self._delivery_lease_seconds())))
            if owned.rowcount != 1:
                await db.rollback()
                return False
            await db.commit()
            attempts += 1
            stop = asyncio.Event()
            heartbeat = asyncio.create_task(self._event_heartbeat(db, row_id, token, stop))
            try:
                if hasattr(provider, "send_message"):
                    operation = provider.send_message(endpoint=endpoint, message=message)
                else:
                    operation = provider.send(endpoint=endpoint, title=message.title, content=message.content,
                        content_html=sanitize_markdown(message.content), announcement_type=message.event_type)
                await asyncio.wait_for(operation, timeout=settings.notification_send_timeout_seconds)
            finally:
                stop.set()
                heartbeat.cancel()
                with suppress(asyncio.CancelledError):
                    await heartbeat
            return await self._finish_event_delivery(db, row_id, token, status="sent", attempts=attempts,
                sent_at=now_utc(), next_retry_at=None, error_message=None, error_category=None)
        except asyncio.CancelledError:
            # Unacknowledged sends retain their lease and may be redelivered after expiry.
            raise
        except Exception as exc:
            with suppress(Exception):
                await db.rollback()
            category = "infrastructure"
            retry_after = 0
            terminal = False
            if isinstance(exc, NotificationProviderDisabled):
                category, terminal = "configuration", True
            elif isinstance(exc, NotificationProviderPermanentError):
                category, terminal = exc.category, True
            elif isinstance(exc, NotificationProviderRetryAfter):
                category, retry_after = "rate_limit", exc.retry_after
            elif attempts:
                category = "transient"
            terminal = terminal or attempts >= settings.notification_retry_max_attempts
            delay = max(1.0, retry_after, settings.notification_retry_initial_delay_seconds *
                        settings.notification_retry_backoff_factor ** max(0, attempts - 1))
            if retry_after:
                # Webhook Retry-After is endpoint scoped: an arbitrary user
                # receiver must not pause unrelated webhook destinations.
                budget_key = str(delivery.channel).lower()
                if getattr(exc, "scope", "provider") == "endpoint":
                    budget_key = f"endpoint:{budget_key}:{delivery.endpoint_id}"
                await self._record_provider_cooldown(db, budget_key, delay)
            logger.warning("Notification delivery failure: delivery={}, category={}", row_id, category)
            return await self._finish_event_delivery(db, row_id, token,
                status="failed" if terminal else "pending", attempts=attempts, error_category=category,
                # Persist only normalized classes, never a URL/response body/provider secret.
                error_message=f"Notification delivery: {category}",
                next_retry_at=None if terminal else now_utc() + timedelta(seconds=delay))

    async def dispatch_event(self, event_id):
        from backend.models import database as db_module
        factory = db_module.async_session
        outcome = {"sent": 0, "failed": 0, "skipped": 0}
        if factory is None:
            return outcome
        async with factory() as db:
            await self.materialize_event(db, event_id)
            ids = (await db.execute(select(NotificationDelivery.id).where(
                NotificationDelivery.event_id == event_id, *_due(NotificationDelivery, now_utc())
            ).limit(get_settings().notification_batch_size))).scalars().all()
        for row_id in ids:
            async with factory() as db:
                acknowledged = await self.deliver_event_row(db, row_id)
                row = await db.get(NotificationDelivery, row_id, populate_existing=True)
                if not acknowledged or row is None:
                    outcome["skipped"] += 1
                elif row.status == "sent":
                    outcome["sent"] += 1
                elif row.status == "failed" or (row.status == "pending" and row.error_category == "transient"):
                    outcome["failed"] += 1
                else:
                    outcome["skipped"] += 1
        return outcome

    async def recover_events_once(self):
        from backend.models import database as db_module
        factory = db_module.async_session
        if factory is None:
            return
        # Legacy backlog never waits for another process's lease in this poller.
        async with factory() as db:
            legacy = (await db.execute(select(NotificationDelivery.announcement_id, NotificationDelivery.publication_version).where(
                NotificationDelivery.event_id.is_(None), *_due(NotificationDelivery, now_utc())
            ).distinct().limit(get_settings().notification_batch_size))).all()
        for announcement_id, version in legacy:
            try:
                async with factory() as db:
                    await self.broadcast_announcement(db, announcement_id, expected_version=version, pending_only=True)
            except Exception:
                logger.warning("Legacy notification recovery unavailable: announcement={}", announcement_id)
        async with factory() as db:
            events = (await db.execute(select(NotificationEvent.id).where(
                *_due(NotificationEvent, now_utc())
            ).order_by(NotificationEvent.occurred_at).limit(get_settings().notification_batch_size))).scalars().all()
        for event_id in events:
            try:
                async with factory() as db:
                    await self.materialize_event(db, event_id)
            except Exception:
                logger.warning("Notification fanout unavailable: event={}", event_id)
                async with factory() as db:
                    await db.execute(update(NotificationEvent).where(NotificationEvent.id == event_id,
                        NotificationEvent.status == "pending").values(error_category="infrastructure",
                        next_retry_at=now_utc() + timedelta(seconds=get_settings().notification_poll_seconds)))
                    await db.commit()
        async with factory() as db:
            rows = (await db.execute(select(NotificationDelivery.id).where(
                NotificationDelivery.event_id.is_not(None), *_due(NotificationDelivery, now_utc())
            ).order_by(NotificationDelivery.id).limit(get_settings().notification_batch_size))).scalars().all()
        semaphore = asyncio.Semaphore(get_settings().notification_max_concurrency)

        async def send(row_id):
            async with semaphore:
                try:
                    async with factory() as db:
                        await self.deliver_event_row(db, row_id)
                except Exception:
                    logger.warning("Notification worker unavailable: delivery={}", row_id)

        await asyncio.gather(*(send(row_id) for row_id in rows))

    async def run_event_dispatcher(self):
        while True:
            try:
                await self.recover_events_once()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.warning("Notification recovery unavailable; retrying after poll interval")
            await asyncio.sleep(get_settings().notification_poll_seconds)
