"""Regression tests for versioned announcement publication and rendering."""

from __future__ import annotations

import asyncio
import ssl
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from backend.models import Base
from backend.models.announcement_models import (
    Announcement,
    AnnouncementDeliveryHistory,
    AnnouncementPublicationHistory,
    AnnouncementRead,
    DeliveryStatus,
    NotificationDelivery,
)
from backend.models.identity_models import NotificationEndpoint
from backend.models.telegram_models import TelegramUser
from backend.services import announcement_service, notification_service
from backend.webui.routes.announcements import _publish_action


class _AsyncSQLiteSession:
    """Async-shaped adapter so tests use real SQLite without aiosqlite."""

    def __init__(self, session: Session):
        self._session = session

    def add(self, value):
        self._session.add(value)

    async def execute(self, statement, *args, **kwargs):
        return self._session.execute(statement, *args, **kwargs)

    async def commit(self):
        self._session.commit()

    async def rollback(self):
        self._session.rollback()

    async def flush(self):
        self._session.flush()

    async def refresh(self, value):
        self._session.refresh(value)

    async def get(self, model, value, **kwargs):
        return self._session.get(model, value, **kwargs)

    async def delete(self, value):
        self._session.delete(value)


@pytest.fixture
def sqlite_sessions():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    first = _AsyncSQLiteSession(Session(engine))
    second = _AsyncSQLiteSession(Session(engine))
    try:
        yield first, second
    finally:
        first._session.close()
        second._session.close()
        engine.dispose()


@pytest.mark.parametrize(
    ("renderer", "anchor"),
    [
        (
            announcement_service.sanitize_markdown,
            '<a href="https://example.invalid/foo_bar_baz"',
        ),
        (
            announcement_service.markdown_to_telegram_html,
            '<a href="https://example.invalid/foo_bar_baz">',
        ),
    ],
)
def test_markdown_renderers_protect_link_attributes_and_code(renderer, anchor):
    rendered = renderer(
        "[open](https://example.invalid/foo_bar_baz) `code_with_underscores`"
    )

    assert anchor in rendered
    assert "<em>bar</em>" not in rendered
    assert "<i>bar</i>" not in rendered
    assert "<code>code_with_underscores</code>" in rendered
    assert "\x00" not in rendered


class _StrictSMTP:
    created: list[_StrictSMTP] = []

    def __init__(self, host, port, *, timeout):
        self.host = host
        self.port = port
        self.timeout = timeout
        self.starttls_context = None
        self.logged_in = False
        self.sent = False
        self.created.append(self)

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return None

    def starttls(self, *, context):
        self.starttls_context = context

    def login(self, *_args):
        self.logged_in = True

    def send_message(self, _message):
        self.sent = True


class _StrictSMTPSSL(_StrictSMTP):
    created: list[_StrictSMTPSSL] = []

    def __init__(self, host, port, *, timeout, context):
        super().__init__(host, port, timeout=timeout)
        self.constructor_context = context


@pytest.mark.asyncio
@pytest.mark.parametrize("security", ["none", "starttls", "ssl"])
async def test_email_security_modes_use_only_supported_context_arguments(
    monkeypatch, security
):
    _StrictSMTP.created.clear()
    _StrictSMTPSSL.created.clear()
    monkeypatch.setattr(notification_service.smtplib, "SMTP", _StrictSMTP)
    monkeypatch.setattr(notification_service.smtplib, "SMTP_SSL", _StrictSMTPSSL)
    monkeypatch.setattr(
        notification_service,
        "get_settings",
        lambda: SimpleNamespace(
            email_enabled=True,
            smtp_host="smtp.example.invalid",
            smtp_from="from@example.invalid",
            smtp_username="user",
            smtp_password="secret",
            smtp_port=465 if security == "ssl" else 587,
            smtp_security=security,
        ),
    )
    endpoint = NotificationEndpoint(
        id=1,
        user_id=1,
        provider="email",
        address="to@example.invalid",
        enabled=True,
    )

    await notification_service.EmailNotificationProvider().send(
        endpoint=endpoint,
        title="Title",
        content="Body",
        content_html="<p>Body</p>",
        announcement_type="general",
    )

    if security == "ssl":
        assert len(_StrictSMTP.created) == 0
        assert len(_StrictSMTPSSL.created) == 1
        session = _StrictSMTPSSL.created[0]
        assert isinstance(session.constructor_context, ssl.SSLContext)
        assert session.starttls_context is None
    else:
        assert len(_StrictSMTP.created) == 1
        assert len(_StrictSMTPSSL.created) == 0
        session = _StrictSMTP.created[0]
        assert (
            isinstance(session.starttls_context, ssl.SSLContext)
            if security == "starttls"
            else session.starttls_context is None
        )
    assert session.logged_in is True
    assert session.sent is True


@pytest.mark.asyncio
async def test_republish_resets_current_state_and_archives_old_round(sqlite_sessions, monkeypatch):
    db, _other = sqlite_sessions
    monkeypatch.setattr(announcement_service, "schedule_announcement_broadcast", lambda *_a, **_k: None)
    db.add(TelegramUser(id=1, telegram_id=1001, is_active=True))
    db.add(
        NotificationEndpoint(
            id=1,
            user_id=1,
            provider="email",
            address="one@example.invalid",
            enabled=True,
        )
    )
    await db.commit()

    announcement = await announcement_service.create_announcement(
        db,
        title="Old title",
        content="Old body",
        publish=True,
    )
    deliveries = (await db.execute(select(NotificationDelivery))).scalars().all()
    for delivery in deliveries:
        delivery.status = DeliveryStatus.SENT.value
        delivery.attempts = 1
    await db.commit()
    assert await announcement_service.mark_read(db, 1, announcement.id)

    announcement = await announcement_service.update_announcement(
        db,
        announcement.id,
        title="New title",
        content="New body",
        publish=True,
    )
    assert announcement.publication_version == 2
    assert not (await db.execute(select(AnnouncementRead))).scalars().all()

    current = (await db.execute(select(NotificationDelivery))).scalars().all()
    assert current
    assert {(row.publication_version, row.status, row.attempts) for row in current} == {
        (2, DeliveryStatus.PENDING.value, 0)
    }
    history = (
        await db.execute(
            select(AnnouncementPublicationHistory).order_by(
                AnnouncementPublicationHistory.publication_version
            )
        )
    ).scalars().all()
    assert [(row.publication_version, row.title, row.content) for row in history] == [
        (1, "Old title", "Old body"),
        (2, "New title", "New body"),
    ]
    old_delivery = (
        await db.execute(select(AnnouncementDeliveryHistory))
    ).scalars().all()
    assert [(row.status, row.attempts) for row in old_delivery] == [
        (DeliveryStatus.SENT.value, 1),
        (DeliveryStatus.SENT.value, 1),
    ]
    stats = await announcement_service.delivery_stats(db, announcement.id)
    assert stats.as_dict()["current"] == {"pending": 2, "sent": 0, "failed": 0}
    assert stats.as_dict()["history"][0]["sent"] == 2
    assert stats.as_dict()["history"][0]["content"] == "Old body"


@pytest.mark.asyncio
async def test_stale_worker_cannot_write_after_republish(sqlite_sessions, monkeypatch):
    db, other = sqlite_sessions
    monkeypatch.setattr(announcement_service, "schedule_announcement_broadcast", lambda *_a, **_k: None)
    db.add(TelegramUser(id=1, telegram_id=1001, is_active=True))
    await db.commit()
    announcement = await announcement_service.create_announcement(
        db, title="Before", content="Before body", publish=True
    )
    delivery = (
        await db.execute(select(NotificationDelivery))
    ).scalars().first()
    provider_calls = []

    class _RepublishOnSend:
        channel = "web"

        async def send(self, **_kwargs):
            provider_calls.append(True)
            await announcement_service.update_announcement(
                other,
                announcement.id,
                title="After",
                content="After body",
                publish=True,
            )

    service = notification_service.NotificationService(
        notification_service.NotificationProviderRegistry(
            {"web": _RepublishOnSend()}
        )
    )
    result = await service._deliver_one(
        db,
        delivery,
        announcement,
        None,
        asyncio.Lock(),
        expected_version=1,
    )
    assert provider_calls == [True]
    assert result is None
    current = await db.get(NotificationDelivery, delivery.id)
    assert current.status == DeliveryStatus.PENDING.value
    assert current.publication_version == 2


@pytest.mark.asyncio
async def test_legacy_withdrawn_edit_captures_old_body_before_draft_save(
    sqlite_sessions, monkeypatch
):
    db, _other = sqlite_sessions
    monkeypatch.setattr(
        announcement_service,
        "schedule_announcement_broadcast",
        lambda *_a, **_k: None,
    )
    db.add(TelegramUser(id=1, telegram_id=1001, is_active=True))
    await db.flush()
    legacy = Announcement(
        id=9,
        title="Legacy old",
        content="Legacy body",
        status="withdrawn",
        publication_version=1,
    )
    db.add(legacy)
    await db.flush()
    db.add(
        NotificationDelivery(
            announcement_id=legacy.id,
            user_id=1,
            channel="web",
            status=DeliveryStatus.SENT.value,
            attempts=1,
            publication_version=1,
        )
    )
    await db.commit()

    await announcement_service.update_announcement(
        db, legacy.id, content="Edited draft"
    )
    snapshot = (
        await db.execute(select(AnnouncementPublicationHistory))
    ).scalars().one()
    assert snapshot.content == "Legacy body"
    assert snapshot.archived_at is not None

    published = await announcement_service.publish_announcement(db, legacy.id)
    assert published.publication_version == 2
    assert published.content == "Edited draft"


def test_admin_actions_keep_publish_primary_and_draft_secondary():
    assert _publish_action("save_and_publish") is True
    assert _publish_action("draft") is False
    assert _publish_action(None) is True
