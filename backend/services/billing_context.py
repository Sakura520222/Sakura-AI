"""Trusted billing ownership propagated through one business execution.

The serialized representation belongs in worker payloads/checkpoints. ContextVar
is only an in-process carrier; it never resolves identity from HTTP input or a
previous login. Sources contain identifiers, never prompts or credentials.
"""

from __future__ import annotations

from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any
from uuid import NAMESPACE_URL, uuid4, uuid5


@dataclass(frozen=True, slots=True)
class BillingContext:
    user_id: int | None
    operation_id: str
    feature: str
    source: Mapping[str, Any] = field(default_factory=dict)
    platform_reason: str | None = None

    def __post_init__(self) -> None:
        if self.user_id is not None and (
            isinstance(self.user_id, bool)
            or not isinstance(self.user_id, int)
            or self.user_id <= 0
        ):
            raise ValueError("Invalid billing owner")
        if not self.operation_id or len(self.operation_id) > 128:
            raise ValueError("Invalid billing operation")
        if not self.feature or len(self.feature) > 64:
            raise ValueError("Invalid billing feature")
        if self.user_id is None and not self.platform_reason:
            raise ValueError("Platform billing requires an explicit reason")
        object.__setattr__(self, "source", MappingProxyType(dict(self.source)))

    def to_payload(self) -> dict[str, Any]:
        return {
            "user_id": self.user_id,
            "operation_id": self.operation_id,
            "feature": self.feature,
            "source": dict(self.source),
            "platform_reason": self.platform_reason,
        }

    @classmethod
    def from_payload(cls, payload: Mapping[str, Any]) -> BillingContext:
        """Restore a trusted worker payload, never a client-supplied request."""
        return cls(**dict(payload))


_current_billing_context: ContextVar[BillingContext | None] = ContextVar(
    "sakura_billing_context", default=None
)


def get_billing_context() -> BillingContext | None:
    return _current_billing_context.get()


@contextmanager
def bind_billing_context(
    context: BillingContext | None,
) -> Iterator[BillingContext | None]:
    token = _current_billing_context.set(context)
    runtime_token = _operation_runtime.set(
        _OperationRuntimeState(operation_id=context.operation_id if context else None)
    )
    outcome_token = _operation_outcome.set(None)
    try:
        yield context
    finally:
        _current_billing_context.reset(token)
        _operation_runtime.reset(runtime_token)
        _operation_outcome.reset(outcome_token)


def context_for_payload(payload: dict[str, Any], feature: str) -> BillingContext:
    """Build from a server-owned execution payload and persist the carrier.

    A verified GitHub delivery is an execution identity; manual requests without
    a delivery obtain a new UUID before queueing. Automatic redelivery therefore
    shares an operation while a user's new run never shares a PR-number key.
    """
    payer_user_id = payload.get("billing_user_id", payload.get("user_id"))
    existing = payload.get("billing_context")
    if isinstance(existing, dict):
        context = BillingContext.from_payload(existing)
        if context.feature != feature or context.user_id != payer_user_id:
            raise ValueError("Billing payload ownership changed")
        return context
    delivery = payload.get("delivery_id")
    operation_id = str(
        uuid5(NAMESPACE_URL, f"sakura:{feature}:delivery:{delivery}")
        if delivery
        else uuid4()
    )
    source = {
        key: payload[key]
        for key in (
            "repo_full_name",
            "pr_number",
            "issue_number",
            "task_id",
            "sender",
            "trigger_user_id",
        )
        if payload.get(key) is not None
    }
    context = BillingContext(
        user_id=payer_user_id,
        operation_id=operation_id,
        feature=feature,
        source=source,
        platform_reason=None
        if payer_user_id
        else payload.get(
            "billing_platform_reason", "unbound_automatic_or_system_event"
        ),
    )
    payload["billing_context"] = context.to_payload()
    return context


@dataclass(slots=True)
class _OperationRuntimeState:
    """Error/registration propagation for inherited child tasks, never money.

    Every bound business operation gets its own state object. Async children
    inherit this object so their durable commits and financial errors remain
    visible to the worker that will finalize the operation after gather().
    """

    operation_id: str | None
    registered: bool = False
    failure: BaseException | None = None


_operation_runtime: ContextVar[_OperationRuntimeState | None] = ContextVar(
    "sakura_billing_runtime", default=None
)


def mark_billing_failure(exc: BaseException) -> None:
    state = _operation_runtime.get()
    if state is not None and state.failure is None:
        state.failure = exc


def get_pending_billing_failure() -> BaseException | None:
    state = _operation_runtime.get()
    return state.failure if state else None


def mark_registered_operation(operation_id: str) -> None:
    state = _operation_runtime.get()
    if state is not None and state.operation_id == operation_id:
        state.registered = True


def has_registered_operation(operation_id: str) -> bool:
    state = _operation_runtime.get()
    return bool(state and state.registered and state.operation_id == operation_id)


_operation_outcome: ContextVar[str | None] = ContextVar(
    "sakura_billing_outcome", default=None
)


def mark_billing_outcome(status: str) -> None:
    _operation_outcome.set(status)


def enrich_billing_source(**identifiers: Any) -> None:
    """Add server-created row identifiers before any provider request is sent."""
    context = get_billing_context()
    if context is None:
        return
    _current_billing_context.set(
        BillingContext(
            user_id=context.user_id,
            operation_id=context.operation_id,
            feature=context.feature,
            source={**context.source, **identifiers},
            platform_reason=context.platform_reason,
        )
    )


def billable_payload(feature: str):
    """Bind a serialized internal payload around the complete worker lifetime."""
    from functools import wraps

    def decorate(function):
        @wraps(function)
        async def wrapped(self, payload, *args, **kwargs):
            from backend.services.ai_usage_service import finish_billing_operation

            context = context_for_payload(payload, feature)
            outcome_token = _operation_outcome.set(None)
            with bind_billing_context(context):
                try:
                    result = await function(self, payload, *args, **kwargs)
                except BaseException as exc:
                    if _operation_outcome.get() is None:
                        import asyncio

                        await finish_billing_operation(
                            "cancelled"
                            if isinstance(exc, asyncio.CancelledError)
                            else "failed"
                        )
                    raise
                else:
                    if _operation_outcome.get() is None:
                        await finish_billing_operation("completed")
                    return result
                finally:
                    _operation_outcome.reset(outcome_token)

        return wrapped

    return decorate


def billable_record(feature: str):
    """Recover persisted Agent/Scan ownership before entering any helper calls."""
    from functools import wraps

    def decorate(function):
        @wraps(function)
        async def wrapped(self, record_id, *args, **kwargs):
            from sqlalchemy import select

            from backend.models import database as database_module
            from backend.models.agent_team_models import AgentTeamTask
            from backend.models.scan_models import RepoScan
            from backend.models.telegram_models import TelegramUser
            from backend.services.ai_usage_service import finish_billing_operation

            factory = database_module.async_session
            if factory is None:
                # Uninitialized development/test runtime cannot issue financial
                # ownership; observations retain their explicit platform marker.
                with bind_billing_context(None):
                    return await function(self, record_id, *args, **kwargs)
            model = AgentTeamTask if feature == "agent" else RepoScan
            async with factory() as db:
                if feature == "agent":
                    from backend.services.agent_team.billing_admission import (
                        claim_agent_delivery_worker,
                    )

                    if not await claim_agent_delivery_worker(db, record_id):
                        # A cancelled/superseded delivery cannot execute a newer
                        # task carrier or finalize another user's operation.
                        return record_id
                record = (
                    await db.execute(
                        select(model).where(model.id == record_id).with_for_update()
                    )
                ).scalar_one()
                previous_operation_id = record.billing_operation_id
                if (
                    feature == "agent"
                    and function.__name__ == "process_human_followup_iteration"
                    and record.current_phase != "human_followup"
                ):
                    # A fresh user-authored continuation is a new execution.
                    record.billing_operation_id = str(uuid4())
                elif not record.billing_operation_id:
                    record.billing_operation_id = str(uuid4())
                user_id = None
                platform_reason = None
                if feature == "repo_scan":
                    trigger = record.triggered_by or ""
                    if record.trigger_type == "manual" and trigger.startswith(
                        ("webui:", "api:")
                    ):
                        try:
                            candidate_id = int(trigger.rsplit(":", 1)[-1])
                        except ValueError:
                            candidate_id = None
                        user_id = (
                            await db.execute(
                                select(TelegramUser.id).where(
                                    TelegramUser.id == candidate_id,
                                    TelegramUser.is_active,
                                )
                            )
                        ).scalar_one_or_none()
                    if user_id is not None:
                        role = (
                            await db.execute(
                                select(TelegramUser.role).where(
                                    TelegramUser.id == user_id
                                )
                            )
                        ).scalar_one()
                        if role in {"admin", "super_admin"}:
                            user_id = None
                            platform_reason = "administrator_repo_scan"
                    if user_id is None and platform_reason is None:
                        platform_reason = "scheduled_or_unbound_repo_scan"
                    source = {"repo_full_name": record.repo_name, "scan_id": record.id}
                else:
                    user_id = record.billing_user_id
                    platform_reason = record.billing_platform_reason
                    if user_id is None and not platform_reason:
                        # Pre-upgrade rows do not prove who authorized payment.
                        # These finite in-flight legacy tasks stay platform-owned.
                        platform_reason = "legacy_agent_task_without_verified_payer"
                    source = {
                        "repo_full_name": record.repo_full_name,
                        "agent_task_id": record.id,
                        "triggered_by": record.started_by,
                    }
                context = BillingContext(
                    user_id,
                    record.billing_operation_id,
                    feature,
                    source,
                    platform_reason,
                )
                if previous_operation_id and (
                    (
                        feature == "agent"
                        and (
                            function.__name__ == "process_external_review_iteration"
                            or kwargs.get("resume")
                            or (args and args[0] is True)
                        )
                    )
                    or (feature == "repo_scan" and kwargs.get("resume"))
                ):
                    from backend.services.billing_service import (
                        BillingError,
                        BillingService,
                    )

                    try:
                        await BillingService(db).resume_operation(previous_operation_id)
                    except BillingError as exc:
                        # Admission happens outside the worker body. Leave a
                        # retryable task instead of a queued row without any
                        # executing worker; the old financial outcome stays
                        # untouched after rolling back the rejected resume.
                        from sqlalchemy import update

                        await db.rollback()
                        values = {"status": "failed"}
                        if hasattr(model, "error_message"):
                            values["error_message"] = (
                                f"Business execution admission failed: {exc.code}"
                            )
                        if feature == "agent":
                            values.update(
                                current_phase="billing_admission",
                                failed_phase="billing_admission",
                            )
                        await db.execute(
                            update(model)
                            .where(
                                model.id == record_id,
                                model.status.notin_(
                                    ("completed", "cancelled", "abandoned")
                                ),
                            )
                            .values(**values)
                        )
                        await db.commit()
                        raise
                await db.commit()
            outcome_token = _operation_outcome.set(None)
            with bind_billing_context(context):
                try:
                    result = await function(self, record_id, *args, **kwargs)
                except BaseException as exc:
                    import asyncio

                    await finish_billing_operation(
                        "cancelled"
                        if isinstance(exc, asyncio.CancelledError)
                        else "failed"
                    )
                    raise
                else:
                    async with factory() as db:
                        status = (
                            await db.execute(
                                select(model.status).where(model.id == record_id)
                            )
                        ).scalar_one()
                    await finish_billing_operation(
                        "cancelled"
                        if status == "cancelled"
                        else "failed"
                        if status in {"failed", "abandoned"}
                        else "completed"
                    )
                    return result
                finally:
                    _operation_outcome.reset(outcome_token)

        return wrapped

    return decorate


def billable_platform(feature: str, reason: str):
    """Independent maintenance jobs must not inherit a user task's payer."""
    from functools import wraps

    def decorate(function):
        @wraps(function)
        async def wrapped(*args, **kwargs):
            from backend.services.ai_usage_service import finish_billing_operation

            context = BillingContext(
                None,
                str(uuid4()),
                feature,
                {"task": function.__name__},
                platform_reason=reason,
            )
            with bind_billing_context(context):
                try:
                    result = await function(*args, **kwargs)
                except BaseException as exc:
                    import asyncio

                    await finish_billing_operation(
                        "cancelled"
                        if isinstance(exc, asyncio.CancelledError)
                        else "failed"
                    )
                    raise
                else:
                    await finish_billing_operation("completed")
                    return result

        return wrapped

    return decorate
