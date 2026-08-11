"""Authoritative GitHub publication lifecycle.

The publication row is the source of truth for every external side effect.  A
network call is always made after committing ``sending`` and its lease; outcome
transitions happen in a fresh transaction so a timeout can never be mistaken
for a safe retry.
"""

from __future__ import annotations

import asyncio
import hashlib
import inspect
import json
import re
import secrets
from collections.abc import Callable, Iterable, Mapping
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from dataclasses import dataclass
from datetime import timedelta
from typing import Any, Protocol
from urllib.parse import urlsplit

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from backend.models import database as db_module
from backend.models.activity_observability_models import (
    ActivityInvocation,
    ActivityInvocationWorkUnit,
    ActivityObservabilitySession,
    ActivityPublication,
    ActivityThread,
    ActivityWorkUnitResult,
)
from backend.models.database import utc_now
from backend.services.activity_observability.outbox_service import (
    append_event_and_outbox,
)

PUBLICATION_KINDS = frozenset(
    {"issue_comment", "pr_review", "pr_review_comment", "check_run"}
)
PUBLICATION_STATUSES = frozenset(
    {"pending", "sending", "succeeded", "failed", "unknown", "cancelled", "reconciling"}
)
TERMINAL_PUBLICATION_STATUSES = frozenset({"succeeded", "failed", "cancelled"})
_ALLOWED_TRANSITIONS = {
    "pending": frozenset({"sending", "cancelled"}),
    "sending": frozenset({"succeeded", "failed", "unknown", "cancelled"}),
    "unknown": frozenset({"reconciling", "cancelled"}),
    "reconciling": frozenset(
        {"succeeded", "pending", "failed", "unknown", "cancelled"}
    ),
    "succeeded": frozenset(),
    "failed": frozenset(),
    "cancelled": frozenset(),
}
_SAFE_EXTERNAL_KEY = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,511}$")
_MARKER_PATTERN = re.compile(r"<!--\s*sakura-activity:[^>]+-->", re.IGNORECASE)
_SAFE_CATEGORY = frozenset(
    {
        "network",
        "timeout",
        "connection",
        "http_4xx",
        "http_5xx",
        "probe_timeout",
        "probe_unknown",
        "invalid_response",
        "invalid_request",
        "reconcile_max_attempts",
        "cancelled",
        "unknown",
    }
)
_DEFAULT_ALLOWED_GITHUB_HOSTS = ("github.com", "www.github.com", "api.github.com")


class PublicationConflictError(RuntimeError):
    """A requested lifecycle operation conflicts with the authoritative state."""


class PublicationLeaseError(PublicationConflictError):
    """The sender no longer owns the sending lease."""


class PublicationProbe(Protocol):
    async def find_by_marker(
        self, kind: str, marker: str, resource_identity: Mapping[str, Any]
    ) -> Any: ...


class PublicationSender(Protocol):
    async def __call__(
        self, kind: str, body: str, resource_identity: Mapping[str, Any]
    ) -> Any: ...


@dataclass(frozen=True, slots=True)
class PublicationLimits:
    """Reconciliation/recovery limits injected from application configuration."""

    max_reconcile_attempts: int = 3
    stale_sending_seconds: float = 300.0
    max_pending_retries: int = 3

    def __post_init__(self) -> None:
        if self.max_reconcile_attempts < 0 or self.max_pending_retries < 0:
            raise ValueError("publication limits must not be negative")
        if self.stale_sending_seconds < 0:
            raise ValueError("stale_sending_seconds must not be negative")


@dataclass(frozen=True, slots=True)
class PublicationLease:
    publication_id: int
    claim_token: str
    marker: str


def safe_hash(value: Any) -> str:
    """Return a stable SHA-256 digest without retaining the supplied value."""
    if isinstance(value, bytes):
        raw = value
    elif isinstance(value, str):
        raw = value.encode("utf-8")
    else:
        raw = json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            default=str,
        ).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def validate_external_key(external_key: str) -> str:
    """Reject URLs, control characters and credential-shaped idempotency keys."""
    if not isinstance(external_key, str) or not external_key.strip():
        raise ValueError("external_key must be a non-empty string")
    key = external_key.strip()
    if not _SAFE_EXTERNAL_KEY.fullmatch(key):
        raise ValueError("external_key must be an opaque safe identifier")
    if "://" in key or key.startswith("//"):
        raise ValueError("external_key must not be a URL")
    lowered = key.lower()
    if any(
        part in lowered
        for part in (
            "token",
            "secret",
            "password",
            "credential",
            "authorization",
            "bearer",
        )
    ):
        raise ValueError("external_key must not contain credential-shaped text")
    return key


def _is_external_key_integrity_error(error: IntegrityError) -> bool:
    """Recognize only the publication idempotency unique constraint."""
    text_value = str(error.orig).lower()
    if "uq_activity_observability_publication_idempotency" in text_value:
        return True
    if "unique constraint failed:" in text_value:
        return text_value.rstrip().endswith(
            "activity_observability_publications.external_idempotency_key"
        )
    if "duplicate entry" in text_value:
        return "for key 'external_idempotency_key'" in text_value or (
            "for key 'uq_activity_observability_publication_idempotency'" in text_value
        )
    return False


def publication_marker(external_key: str) -> str:
    """Build the deterministic, non-sensitive GitHub marker."""
    return f"<!-- sakura-activity:{safe_hash(validate_external_key(external_key))} -->"


def build_publication_body(body: str, marker: str) -> str:
    """Append a marker while preventing body-side marker spoofing/collision."""
    if not isinstance(body, str):
        raise TypeError("body must be a string")
    if not isinstance(marker, str) or not re.fullmatch(
        r"<!-- sakura-activity:[0-9a-f]{64} -->", marker
    ):
        raise ValueError("marker is not a safe Sakura activity marker")
    if _MARKER_PATTERN.search(body):
        raise ValueError("publication body must not contain a Sakura activity marker")
    return f"{body}\n\n{marker}"


def request_fingerprint(*parts: Any) -> str:
    """Hash request inputs; request text and credentials never reach storage."""
    return safe_hash(list(parts))


def _normalise_allowed_hosts(hosts: Iterable[str] | None) -> frozenset[str]:
    if isinstance(hosts, str):
        values = hosts.split(",")
    else:
        values = _DEFAULT_ALLOWED_GITHUB_HOSTS if hosts is None else hosts
    normalised: set[str] = set()
    for host in values:
        if not isinstance(host, str):
            raise TypeError("allowed GitHub hosts must be strings")
        value = host.strip().lower().rstrip(".")
        if not value or "://" in value or "/" in value or ":" in value:
            raise ValueError("allowed GitHub hosts must be bare DNS names")
        normalised.add(value)
    if not normalised:
        raise ValueError("at least one allowed GitHub host is required")
    return frozenset(normalised)


def _safe_url(value: Any, allowed_hosts: Iterable[str] | None = None) -> str | None:
    if not isinstance(value, str) or not value:
        return None
    parsed = urlsplit(value)
    if parsed.scheme.lower() != "https" or parsed.username or parsed.password:
        return None
    try:
        hostname = parsed.hostname.lower().rstrip(".") if parsed.hostname else None
    except ValueError:
        return None
    if hostname not in _normalise_allowed_hosts(allowed_hosts):
        return None
    return value


def _normalise_external_id(value: Any) -> str | None:
    if isinstance(value, bool) or not isinstance(value, (str, int)):
        return None
    normalised = str(value).strip()
    if not normalised or len(normalised) > 255:
        return None
    if any(ord(char) < 0x20 or ord(char) == 0x7F for char in normalised):
        return None
    return normalised


def _reconcile_attempts(publication: ActivityPublication) -> int:
    try:
        payload = json.loads(publication.reconciliation_json or "{}")
    except TypeError, ValueError, json.JSONDecodeError:
        return 0
    value = payload.get("reconcile_attempts", 0) if isinstance(payload, dict) else 0
    return value if isinstance(value, int) and value >= 0 else 0


def _response_value(response: Any, name: str, default: Any = None) -> Any:
    if isinstance(response, Mapping):
        return response.get(name, default)
    return getattr(response, name, default)


def _status_code(error: Any) -> int | None:
    value = getattr(error, "status_code", getattr(error, "status", None))
    return value if isinstance(value, int) and 100 <= value <= 599 else None


def _error_category(
    error: BaseException | None = None,
    *,
    category: str | None = None,
    status: int | None = None,
) -> str:
    if category in _SAFE_CATEGORY:
        return category
    if status is not None:
        return (
            "http_4xx"
            if 400 <= status < 500
            else "http_5xx"
            if status >= 500
            else "unknown"
        )
    if isinstance(error, asyncio.TimeoutError):
        return "timeout"
    if isinstance(error, ConnectionError):
        return "connection"
    return "network" if error is not None else "unknown"


class PublicationCoordinator(Protocol):
    """Task 9 injection point for domain-specific publication orchestration."""

    async def publish_review(
        self, result: Mapping[str, Any], *, context: Any
    ) -> Any: ...

    async def publish_issue_analysis(
        self, result: Mapping[str, Any], *, context: Any
    ) -> Any: ...


class WorkUnitResultCoordinator:
    """Persist generated domain results before the worker performs a publication."""

    def __init__(self, service: PublicationService) -> None:
        self._service = service

    async def _record(
        self,
        result: Mapping[str, Any],
        *,
        context: Any,
        result_kind: str,
    ) -> dict[str, Any]:
        stored = await self._service.create_work_unit_result(
            context=context,
            result_kind=result_kind,
            payload=result,
            requires_publication=True,
        )
        projected = dict(result)
        projected["_activity_result_id"] = int(stored.id)
        return projected

    async def publish_review(
        self, result: Mapping[str, Any], *, context: Any
    ) -> dict[str, Any]:
        return await self._record(
            result,
            context=context,
            result_kind="review",
        )

    async def publish_issue_analysis(
        self, result: Mapping[str, Any], *, context: Any
    ) -> dict[str, Any]:
        return await self._record(
            result,
            context=context,
            result_kind="issue_analysis",
        )


async def coordinate_publication(
    coordinator: Any,
    *,
    kind: str,
    result: Mapping[str, Any],
    context: Any,
) -> Any:
    """Invoke a coordinator only when an authoritative InvocationContext exists."""
    if coordinator is None or context is None:
        return result
    invocation_context = (
        context.get("invocation_context") if isinstance(context, Mapping) else context
    )
    if invocation_context is None:
        return result
    method_name = "publish_review" if kind == "review" else "publish_issue_analysis"
    method = getattr(coordinator, method_name, None)
    if method is None:
        method = getattr(coordinator, "publish", None)
    if method is None:
        raise TypeError(f"publication coordinator does not support {method_name}")
    published = method(result, context=invocation_context)
    if inspect.isawaitable(published):
        published = await published
    return result if published is None else published


class PublicationService:
    """State machine and send/reconcile orchestrator for external publications."""

    def __init__(
        self,
        db: AsyncSession | None = None,
        *,
        session_factory: Callable[[], AbstractAsyncContextManager[AsyncSession]] | None = None,
        limits: PublicationLimits | None = None,
        recipient_user_ids: tuple[str, ...] | None = None,
        recipient_resolver: Callable[..., Any] | None = None,
        error_category: Callable[[BaseException | None], str] | None = None,
        allowed_github_hosts: Iterable[str] | None = None,
    ) -> None:
        self._db = db
        self._session_factory = session_factory
        self.limits = limits or PublicationLimits()
        self.recipient_user_ids = recipient_user_ids
        self.recipient_resolver = recipient_resolver
        self._error_category = error_category
        if allowed_github_hosts is None:
            try:
                from backend.core.config import get_settings

                allowed_github_hosts = (
                    get_settings().activity_publication_allowed_github_hosts
                )
            except Exception:
                allowed_github_hosts = None
        self.allowed_github_hosts = _normalise_allowed_hosts(allowed_github_hosts)

    @asynccontextmanager
    async def _scope(self, *, commit: bool = True):
        if self._db is not None:
            yield self._db
            return
        factory = self._session_factory or db_module.async_session
        if factory is None:
            raise RuntimeError("异步数据库会话尚未初始化")
        async with factory() as db:
            try:
                yield db
                if commit:
                    await db.commit()
            except Exception:
                await db.rollback()
                raise

    async def _chain(self, db: AsyncSession, result_id: int, *, lock: bool = False):
        result = await db.get(ActivityWorkUnitResult, result_id, with_for_update=lock)
        if result is None:
            raise ValueError("work unit result does not exist")
        work_unit = await db.get(
            ActivityInvocationWorkUnit, result.work_unit_id, with_for_update=lock
        )
        if work_unit is None:
            raise ValueError("work unit parent does not exist")
        invocation = await db.get(
            ActivityInvocation, work_unit.invocation_id, with_for_update=lock
        )
        if invocation is None or invocation.session_id != work_unit.session_id:
            raise ValueError("invocation parent chain is invalid")
        session = await db.get(
            ActivityObservabilitySession, work_unit.session_id, with_for_update=lock
        )
        if session is None:
            raise ValueError("session parent chain is invalid")
        if result.thread_id is not None and result.thread_id != work_unit.thread_id:
            raise ValueError("result thread does not belong to work unit")
        return result, work_unit, invocation, session

    async def create_work_unit_result(
        self,
        *,
        context: Any,
        result_kind: str,
        payload: Mapping[str, Any],
        requires_publication: bool,
    ) -> ActivityWorkUnitResult:
        """Persist one immutable generated result for an authoritative Work Unit."""
        if context is None:
            raise ValueError("InvocationContext is required")
        if not isinstance(result_kind, str) or not result_kind.strip():
            raise ValueError("result_kind is required")
        safe_payload = {
            str(key): value
            for key, value in payload.items()
            if not str(key).startswith("_activity_")
        }
        payload_json = json.dumps(
            safe_payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            default=str,
        )
        work_unit_id = int(context.work_unit_id)
        invocation_id = int(context.invocation_id)
        context_thread_id = getattr(context, "thread_id", None)
        thread_id = int(context_thread_id) if context_thread_id is not None else None
        async with self._scope() as db:
            work_unit = await db.get(
                ActivityInvocationWorkUnit,
                work_unit_id,
                with_for_update=True,
            )
            invocation = await db.get(
                ActivityInvocation,
                invocation_id,
                with_for_update=True,
            )
            if (
                work_unit is None
                or invocation is None
                or work_unit.invocation_id != invocation.id
                or work_unit.session_id != invocation.session_id
                or work_unit.thread_id != thread_id
            ):
                raise ValueError("InvocationContext parent chain is invalid")
            revision_id = None
            if thread_id is not None:
                thread = await db.get(ActivityThread, thread_id, with_for_update=True)
                if thread is None or thread.session_id != work_unit.session_id:
                    raise ValueError("InvocationContext thread is invalid")
                revision_id = thread.current_revision_id
            existing = (
                await db.execute(
                    select(ActivityWorkUnitResult)
                    .where(
                        ActivityWorkUnitResult.work_unit_id == work_unit.id,
                        ActivityWorkUnitResult.result_kind == result_kind.strip(),
                    )
                    .with_for_update()
                )
            ).scalar_one_or_none()
            if existing is not None:
                if (
                    existing.thread_id != thread_id
                    or existing.context_revision_id != revision_id
                    or existing.payload_json != payload_json
                    or bool(existing.requires_publication) != bool(requires_publication)
                ):
                    raise PublicationConflictError(
                        "work unit result conflicts with its prior value"
                    )
                return existing
            stored = ActivityWorkUnitResult(
                work_unit_id=work_unit.id,
                thread_id=thread_id,
                context_revision_id=revision_id,
                result_kind=result_kind.strip(),
                status="generated",
                payload_json=payload_json,
                requires_publication=bool(requires_publication),
            )
            db.add(stored)
            await db.flush()
            return stored

    async def _event(
        self, db: AsyncSession, publication: ActivityPublication, status: str
    ) -> None:
        payload = {
            "publication_id": publication.id,
            "kind": publication.publication_kind,
            "status": status,
            "attempt_count": int(publication.attempt_count or 0),
            "error_category": publication.error_category,
        }
        await append_event_and_outbox(
            db,
            session_id=publication._session_id,
            invocation_id=publication._invocation_id,
            work_unit_id=publication._work_unit_id,
            event_type="publication_status",
            visibility="internal",
            payload=payload,
            recipient_resolver=self.recipient_resolver,
            recipient_user_ids=self.recipient_user_ids,
        )

    async def create_pending(
        self, result_id: int, kind: str, external_key: str
    ) -> ActivityPublication:
        if kind not in PUBLICATION_KINDS:
            raise ValueError(f"unsupported publication kind: {kind}")
        key = validate_external_key(external_key)
        marker = publication_marker(key)
        async with self._scope() as db:
            _result, work_unit, invocation, session = await self._chain(db, result_id)
            existing = (
                await db.execute(
                    select(ActivityPublication).where(
                        ActivityPublication.external_idempotency_key == key
                    )
                )
            ).scalar_one_or_none()
            if existing is not None:
                if (
                    existing.work_unit_result_id != result_id
                    or existing.publication_kind != kind
                ):
                    raise PublicationConflictError(
                        "external key belongs to a different publication"
                    )
                return existing
            publication = ActivityPublication(
                work_unit_result_id=result_id,
                publication_kind=kind,
                external_idempotency_key=key,
                marker=marker,
                status="pending",
                attempt_count=0,
                retry_count=0,
            )
            publication._session_id = session.id
            publication._invocation_id = invocation.id
            publication._work_unit_id = work_unit.id
            # Transient parent IDs are used only for the event projection and
            # are deliberately not persisted on the publication row.
            try:
                async with db.begin_nested():
                    db.add(publication)
                    await db.flush()
            except IntegrityError as exc:
                if not _is_external_key_integrity_error(exc):
                    raise
                existing = (
                    await db.execute(
                        select(ActivityPublication)
                        .where(ActivityPublication.external_idempotency_key == key)
                        .with_for_update()
                    )
                ).scalar_one_or_none()
                if existing is None:
                    raise
                if (
                    existing.work_unit_result_id != result_id
                    or existing.publication_kind != kind
                ):
                    raise PublicationConflictError(
                        "external key belongs to a different publication"
                    )
                return existing
            await self._event(db, publication, "pending")
            return publication

    async def _locked(
        self, db: AsyncSession, publication_id: int
    ) -> ActivityPublication:
        publication = (
            await db.execute(
                select(ActivityPublication)
                .where(ActivityPublication.id == publication_id)
                .with_for_update()
            )
        ).scalar_one_or_none()
        if publication is None:
            raise ValueError("publication does not exist")
        return publication

    async def _transition(
        self,
        publication_id: int,
        target: str,
        *,
        claim_token: str | None = None,
        category: str | None = None,
        http_status: int | None = None,
        external_id: str | None = None,
        external_url: str | None = None,
        retry: bool = False,
    ) -> ActivityPublication:
        if target not in PUBLICATION_STATUSES:
            raise ValueError("unsupported publication status")
        async with self._scope() as db:
            publication = await self._locked(db, publication_id)
            current = publication.status
            if current == target and current in TERMINAL_PUBLICATION_STATUSES:
                if target == "succeeded":
                    normalised_id = _normalise_external_id(external_id)
                    if external_id is not None and normalised_id is None:
                        raise ValueError("external_object_id must be a safe identifier")
                    if (
                        normalised_id is not None
                        and publication.external_object_id is not None
                        and normalised_id != publication.external_object_id
                    ):
                        raise PublicationConflictError(
                            "external identity conflicts with the terminal publication"
                        )
                    if external_url is not None:
                        safe_url = _safe_url(external_url, self.allowed_github_hosts)
                        if safe_url is None:
                            raise ValueError(
                                "external_object_url must be a verified GitHub HTTPS URL"
                            )
                        if (
                            publication.external_object_url is not None
                            and safe_url != publication.external_object_url
                        ):
                            raise PublicationConflictError(
                                "external identity URL conflicts with the terminal publication"
                            )
                return publication
            if target not in _ALLOWED_TRANSITIONS.get(current, frozenset()):
                raise PublicationConflictError(
                    f"illegal publication transition: {current} -> {target}"
                )
            if (
                current in {"sending", "reconciling"}
                and claim_token != publication.claim_token
            ):
                raise PublicationLeaseError("publication lease is missing or expired")
            normalised_id = None
            safe_url = None
            if target == "succeeded":
                normalised_id = _normalise_external_id(external_id)
                if normalised_id is None:
                    raise ValueError("external_object_id must be a safe identifier")
                if (
                    publication.external_object_id is not None
                    and publication.external_object_id != normalised_id
                ):
                    raise PublicationConflictError(
                        "external identity conflicts with the terminal publication"
                    )
            elif external_id is not None:
                normalised_id = _normalise_external_id(external_id)
                if normalised_id is None:
                    raise ValueError("external_object_id must be a safe identifier")
            if external_url is not None:
                safe_url = _safe_url(external_url, self.allowed_github_hosts)
                if safe_url is None:
                    raise ValueError(
                        "external_object_url must be a verified GitHub HTTPS URL"
                    )
                if (
                    publication.external_object_url is not None
                    and publication.external_object_url != safe_url
                ):
                    raise PublicationConflictError(
                        "external identity URL conflicts with the terminal publication"
                    )
            publication.status = target
            if target == "sending":
                publication.attempt_count = int(publication.attempt_count or 0) + 1
                publication.retry_count = publication.attempt_count
                publication.started_at = utc_now()
                publication.claim_token = claim_token or secrets.token_hex(32)
                publication.error_category = None
                publication.error_message = None
            elif target in {"succeeded", "failed", "cancelled"}:
                publication.completed_at = utc_now()
                publication.claim_token = None
                if target == "succeeded":
                    normalised_id = _normalise_external_id(external_id)
                    if normalised_id is None:
                        raise ValueError("external_object_id must be a safe identifier")
                    if (
                        publication.external_object_id is not None
                        and publication.external_object_id != normalised_id
                    ):
                        raise PublicationConflictError(
                            "external identity conflicts with the terminal publication"
                        )
                    publication.external_object_id = normalised_id
                elif external_id is not None:
                    normalised_id = _normalise_external_id(external_id)
                    if normalised_id is None:
                        raise ValueError("external_object_id must be a safe identifier")
                    publication.external_object_id = normalised_id
                if external_url is not None:
                    safe_url = _safe_url(external_url, self.allowed_github_hosts)
                    if safe_url is None:
                        raise ValueError(
                            "external_object_url must be a verified GitHub HTTPS URL"
                        )
                    publication.external_object_url = safe_url
                publication.error_category = (
                    _error_category(category=category, status=http_status)
                    if target != "succeeded"
                    else None
                )
                publication.error_message = publication.error_category
                publication.http_status = (
                    http_status
                    if isinstance(http_status, int) and 100 <= http_status <= 599
                    else None
                )
            elif target == "unknown":
                publication.timed_out_at = utc_now()
                publication.claim_token = None
                publication.error_category = _error_category(category=category)
                publication.error_message = publication.error_category
            elif target == "reconciling":
                publication.claim_token = claim_token or secrets.token_hex(32)
            elif target == "pending":
                if (
                    retry
                    and int(publication.attempt_count or 0)
                    > self.limits.max_pending_retries
                ):
                    raise PublicationConflictError("publication retry limit exceeded")
                publication.claim_token = None
                try:
                    reconciliation = json.loads(publication.reconciliation_json or "{}")
                except TypeError, ValueError, json.JSONDecodeError:
                    reconciliation = {}
                if not isinstance(reconciliation, dict):
                    reconciliation = {}
                reconciliation["confirmed_absent"] = True
                publication.reconciliation_json = json.dumps(
                    reconciliation, separators=(",", ":")
                )
            publication._session_id = (
                await self._chain(db, publication.work_unit_result_id)
            )[3].id
            publication._invocation_id = (
                await self._chain(db, publication.work_unit_result_id)
            )[2].id
            publication._work_unit_id = (
                await self._chain(db, publication.work_unit_result_id)
            )[1].id
            await self._event(db, publication, target)
            return publication

    async def mark_sending(self, publication_id: int) -> ActivityPublication | None:
        async with self._scope() as db:
            publication = await self._locked(db, publication_id)
            if publication.status in TERMINAL_PUBLICATION_STATUSES:
                return publication
            if publication.status != "pending":
                return None
            token = secrets.token_hex(32)
            publication.status = "sending"
            publication.claim_token = token
            publication.attempt_count = int(publication.attempt_count or 0) + 1
            publication.retry_count = publication.attempt_count
            publication.started_at = utc_now()
            publication.error_category = None
            publication.error_message = None
            _result, work_unit, invocation, session = await self._chain(
                db, publication.work_unit_result_id
            )
            (
                publication._session_id,
                publication._invocation_id,
                publication._work_unit_id,
            ) = session.id, invocation.id, work_unit.id
            await self._event(db, publication, "sending")
            return publication

    async def mark_succeeded(
        self,
        publication_id: int,
        external_id: str,
        external_url: str | None = None,
        *,
        claim_token: str | None = None,
    ) -> ActivityPublication:
        return await self._transition(
            publication_id,
            "succeeded",
            claim_token=claim_token,
            external_id=external_id,
            external_url=external_url,
        )

    async def mark_failed(
        self,
        publication_id: int,
        *,
        category: str = "unknown",
        http_status: int | None = None,
        claim_token: str | None = None,
    ) -> ActivityPublication:
        return await self._transition(
            publication_id,
            "failed",
            claim_token=claim_token,
            category=category,
            http_status=http_status,
        )

    async def mark_transport_timeout(
        self,
        publication_id: int,
        *,
        category: str = "timeout",
        claim_token: str | None = None,
    ) -> ActivityPublication:
        return await self._transition(
            publication_id, "unknown", claim_token=claim_token, category=category
        )

    async def cancel(
        self, publication_id: int, *, claim_token: str | None = None
    ) -> ActivityPublication:
        return await self._transition(
            publication_id, "cancelled", claim_token=claim_token, category="cancelled"
        )

    async def recover_stale_sending(self, *, now=None) -> int:
        cutoff = (now or utc_now()) - timedelta(
            seconds=self.limits.stale_sending_seconds
        )
        count = 0
        async with self._scope() as db:
            rows = (
                (
                    await db.execute(
                        select(ActivityPublication)
                        .where(
                            ActivityPublication.status == "sending",
                            ActivityPublication.started_at <= cutoff,
                        )
                        .with_for_update()
                    )
                )
                .scalars()
                .all()
            )
            for publication in rows:
                publication.status = "unknown"
                publication.timed_out_at = now or utc_now()
                publication.error_category = "timeout"
                publication.error_message = "timeout"
                publication.claim_token = None
                _result, work_unit, invocation, session = await self._chain(
                    db, publication.work_unit_result_id
                )
                (
                    publication._session_id,
                    publication._invocation_id,
                    publication._work_unit_id,
                ) = session.id, invocation.id, work_unit.id
                await self._event(db, publication, "unknown")
                count += 1
        return count

    async def reconcile(
        self,
        publication_id: int,
        probe: PublicationProbe,
        resource_identity: Mapping[str, Any],
    ) -> ActivityPublication:
        should_fail = False
        async with self._scope() as db:
            publication = await self._locked(db, publication_id)
            if publication.status in TERMINAL_PUBLICATION_STATUSES:
                return publication
            if publication.status != "unknown":
                raise PublicationConflictError(
                    "only unknown publications can be reconciled"
                )
            attempts = _reconcile_attempts(publication)
            should_fail = attempts >= self.limits.max_reconcile_attempts
            token = secrets.token_hex(32)
            publication.status = "reconciling"
            publication.claim_token = token
            publication.reconciliation_json = json.dumps(
                {"reconcile_attempts": attempts if should_fail else attempts + 1},
                separators=(",", ":"),
            )
            _result, work_unit, invocation, session = await self._chain(
                db, publication.work_unit_result_id
            )
            (
                publication._session_id,
                publication._invocation_id,
                publication._work_unit_id,
            ) = session.id, invocation.id, work_unit.id
            await self._event(db, publication, "reconciling")
        if should_fail:
            return await self._transition(
                publication_id,
                "failed",
                claim_token=token,
                category="reconcile_max_attempts",
            )
        try:
            found = probe.find_by_marker(
                publication.publication_kind, publication.marker, resource_identity
            )
            if inspect.isawaitable(found):
                found = await found
        except TimeoutError:
            return await self._transition(
                publication_id, "unknown", claim_token=token, category="probe_timeout"
            )
        except Exception:
            return await self._transition(
                publication_id, "unknown", claim_token=token, category="probe_unknown"
            )
        if found:
            external_id = _normalise_external_id(
                _response_value(found, "id") or _response_value(found, "external_id")
            )
            url = _response_value(found, "url") or _response_value(found, "html_url")
            if external_id is None:
                return await self._transition(
                    publication_id,
                    "unknown",
                    claim_token=token,
                    category="invalid_response",
                )
            return await self._transition(
                publication_id,
                "succeeded",
                claim_token=token,
                external_id=external_id,
                external_url=url,
            )
        return await self._transition(
            publication_id, "pending", claim_token=token, retry=True
        )

    async def send(
        self,
        publication_id: int,
        *,
        body: str,
        sender: PublicationSender,
        resource_identity: Mapping[str, Any],
    ) -> ActivityPublication:
        publication = await self.mark_sending(publication_id)
        if publication is None:
            async with self._scope(commit=False) as db:
                return await db.get(ActivityPublication, publication_id)
        token = publication.claim_token
        try:
            body_with_marker = build_publication_body(body, publication.marker)
            fingerprint = request_fingerprint(
                publication.publication_kind, resource_identity, body_with_marker
            )
        except Exception:
            return await self.mark_failed(
                publication_id,
                category="invalid_request",
                claim_token=token,
            )
        publication.request_fingerprint = fingerprint
        # Fingerprint is committed separately from the sending claim; the network
        # call must never hold this row lock.
        async with self._scope() as db:
            current = await self._locked(db, publication_id)
            if (
                current.status != "sending"
                or current.claim_token != publication.claim_token
            ):
                raise PublicationLeaseError("publication lease is missing")
            current.request_fingerprint = publication.request_fingerprint
            token = current.claim_token
        try:
            response = sender(
                publication.publication_kind, body_with_marker, resource_identity
            )
            if inspect.isawaitable(response):
                response = await response
            status = _response_value(
                response, "status_code", _response_value(response, "status")
            )
            if isinstance(status, int) and 400 <= status < 500:
                return await self.mark_failed(
                    publication_id,
                    category="http_4xx",
                    http_status=status,
                    claim_token=token,
                )
            if isinstance(status, int) and status >= 500:
                return await self.mark_transport_timeout(
                    publication_id, category="http_5xx", claim_token=token
                )
            external_id = _response_value(
                response, "id", _response_value(response, "external_id")
            )
            url = _response_value(
                response, "html_url", _response_value(response, "url")
            )
            if not isinstance(external_id, str) or not external_id:
                return await self.mark_failed(
                    publication_id, category="invalid_response", claim_token=token
                )
            return await self.mark_succeeded(
                publication_id, external_id, url, claim_token=token
            )
        except asyncio.CancelledError:
            raise
        except TimeoutError, ConnectionError:
            return await self.mark_transport_timeout(
                publication_id, category="timeout", claim_token=token
            )
        except ValueError:
            return await self.mark_failed(
                publication_id,
                category="invalid_response",
                claim_token=token,
            )
        except PublicationConflictError:
            raise
        except Exception as exc:
            status = _status_code(exc)
            if status is not None and 400 <= status < 500:
                return await self.mark_failed(
                    publication_id,
                    category="http_4xx",
                    http_status=status,
                    claim_token=token,
                )
            category = (
                self._error_category(exc)
                if self._error_category
                else _error_category(exc, status=status)
            )
            return await self.mark_transport_timeout(
                publication_id, category=category, claim_token=token
            )


__all__ = [
    "PUBLICATION_KINDS",
    "PUBLICATION_STATUSES",
    "PublicationConflictError",
    "PublicationCoordinator",
    "PublicationLease",
    "PublicationLeaseError",
    "PublicationLimits",
    "PublicationProbe",
    "PublicationSender",
    "PublicationService",
    "WorkUnitResultCoordinator",
    "build_publication_body",
    "coordinate_publication",
    "publication_marker",
    "request_fingerprint",
    "safe_hash",
    "validate_external_key",
]
