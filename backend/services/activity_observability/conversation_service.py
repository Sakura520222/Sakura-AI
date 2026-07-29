"""Conversation-first, role-aware projection over observability storage."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.models.activity_observability_models import (
    ActivityCanonicalContextRevision,
    ActivityContextOperation,
    ActivityInvocation,
    ActivityInvocationWorkUnit,
    ActivityModelAttempt,
    ActivityNativeArtifact,
    ActivityObservabilityMessage,
    ActivityObservabilitySession,
    ActivityThread,
    ActivityToolExecution,
)
from backend.services.activity_observability.access_service import (
    ActivityAccessService,
    CursorResetRequiredError,
    project_attempt,
)

CONVERSATION_PROJECTION_VERSION = 3
_TYPE_RANK = {
    "invocation_boundary": 0,
    "thread_boundary": 1,
    "message": 2,
    "attempt": 3,
    "reasoning": 4,
    "tool_call": 5,
    "tool_result": 6,
    "context_operation": 7,
}
_SENSITIVE_MESSAGE_ROLES = frozenset({"system", "user", "tool"})


def _as_utc(value: datetime | None) -> datetime:
    if value is None:
        return datetime.fromtimestamp(0, tz=timezone.utc)
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _iso(value: datetime | None) -> str | None:
    return _as_utc(value).isoformat() if value is not None else None


def _message_tool_call_ids(message_json: str | None) -> set[str]:
    """Read only public tool-call identifiers from a canonical message."""
    try:
        payload = json.loads(message_json or "{}")
    except (TypeError, json.JSONDecodeError):
        return set()
    if not isinstance(payload, dict):
        return set()
    calls = payload.get("tool_calls")
    if not isinstance(calls, list):
        return set()
    return {
        str(item.get("id"))
        for item in calls
        if isinstance(item, dict) and item.get("id")
    }


def _b64encode(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def _b64decode(value: str) -> bytes:
    return base64.urlsafe_b64decode(value + ("=" * (-len(value) % 4)))


class ConversationProjectionService:
    """Build a stable unified timeline without exposing ORM layout to clients."""

    def __init__(
        self,
        db: AsyncSession,
        *,
        access_service: ActivityAccessService,
    ) -> None:
        self.db = db
        self.access_service = access_service

    @staticmethod
    def _role(user: dict[str, Any]) -> str:
        value = str(user.get("role") or "user")
        return value if value in {"admin", "super_admin"} else "user"

    def _cursor_secret(self) -> bytes:
        return self.access_service._require_cursor_config().secret.encode("utf-8")

    def _history_cursor(
        self,
        *,
        session_id: int,
        before: tuple[int, int, int],
        authorization_version: str,
    ) -> str:
        config = self.access_service._require_cursor_config()
        now = int(_as_utc(self.access_service.now()).timestamp())
        body = {
            "kind": "conversation_history",
            "session_id": session_id,
            "before": list(before),
            "auth_version": authorization_version,
            "projection_version": CONVERSATION_PROJECTION_VERSION,
            "expires_at": now + config.ttl_seconds,
        }
        encoded = _b64encode(
            json.dumps(body, separators=(",", ":"), sort_keys=True).encode("utf-8")
        )
        signature = hmac.new(
            self._cursor_secret(), encoded.encode("ascii"), hashlib.sha256
        ).digest()
        return f"{encoded}.{_b64encode(signature)}"

    def _decode_history_cursor(
        self,
        cursor: str,
        *,
        session_id: int,
        authorization_version: str,
    ) -> tuple[int, int, int]:
        try:
            encoded, signature = cursor.split(".", 1)
            expected = hmac.new(
                self._cursor_secret(), encoded.encode("ascii"), hashlib.sha256
            ).digest()
            if not hmac.compare_digest(expected, _b64decode(signature)):
                raise ValueError
            body = json.loads(_b64decode(encoded))
            if (
                body.get("kind") != "conversation_history"
                or int(body["session_id"]) != session_id
                or body["auth_version"] != authorization_version
                or int(body["projection_version"]) != CONVERSATION_PROJECTION_VERSION
                or int(body["expires_at"])
                <= int(_as_utc(self.access_service.now()).timestamp())
            ):
                raise CursorResetRequiredError("conversation cursor changed")
            before = body["before"]
            if (
                not isinstance(before, list)
                or len(before) != 3
                or any(not isinstance(value, int) for value in before)
            ):
                raise ValueError
            return int(before[0]), int(before[1]), int(before[2])
        except CursorResetRequiredError:
            raise
        except (ValueError, TypeError, KeyError, json.JSONDecodeError) as exc:
            raise CursorResetRequiredError("invalid conversation cursor") from exc

    @staticmethod
    def _sort_key(
        created_at: datetime | None,
        entry_type: str,
        row_id: int,
    ) -> tuple[int, int, int]:
        micros = int(_as_utc(created_at).timestamp() * 1_000_000)
        return micros, _TYPE_RANK[entry_type], int(row_id)

    @staticmethod
    def _artifact_metadata(
        artifacts: list[ActivityNativeArtifact],
        *,
        readable_artifact_ids: set[int],
    ) -> list[dict[str, Any]]:
        return [
            {
                "artifact_id": item.id,
                "artifact_kind": item.artifact_kind,
                "availability": item.availability,
                "capture_mode": item.capture_mode,
                "visibility": item.visibility,
                "capture_error": item.capture_error,
                "retention_expires_at": _iso(item.retention_expires_at),
                "can_request": int(item.id) in readable_artifact_ids,
            }
            for item in artifacts
        ]

    async def _collect_entries(
        self,
        session: ActivityObservabilitySession,
        user: dict[str, Any],
    ) -> list[dict[str, Any]]:
        session_id = int(session.id)
        role = self._role(user)
        super_admin = role == "super_admin"
        admin = role in {"admin", "super_admin"}

        threads = list(
            (
                await self.db.execute(
                    select(ActivityThread)
                    .where(ActivityThread.session_id == session_id)
                    .order_by(ActivityThread.id)
                )
            ).scalars()
        )
        invocations = list(
            (
                await self.db.execute(
                    select(ActivityInvocation)
                    .where(ActivityInvocation.session_id == session_id)
                    .order_by(ActivityInvocation.id)
                )
            ).scalars()
        )
        invocation_ids = [int(item.id) for item in invocations]
        work_units = (
            list(
                (
                    await self.db.execute(
                        select(ActivityInvocationWorkUnit)
                        .where(
                            ActivityInvocationWorkUnit.invocation_id.in_(invocation_ids)
                        )
                        .order_by(ActivityInvocationWorkUnit.id)
                    )
                ).scalars()
            )
            if invocation_ids
            else []
        )
        work_unit_ids = [int(item.id) for item in work_units]
        messages = (
            list(
                (
                    await self.db.execute(
                        select(ActivityObservabilityMessage)
                        .where(
                            ActivityObservabilityMessage.work_unit_id.in_(work_unit_ids)
                        )
                        .order_by(
                            ActivityObservabilityMessage.created_at,
                            ActivityObservabilityMessage.id,
                        )
                    )
                ).scalars()
            )
            if work_unit_ids
            else []
        )
        attempts = (
            list(
                (
                    await self.db.execute(
                        select(ActivityModelAttempt)
                        .where(ActivityModelAttempt.work_unit_id.in_(work_unit_ids))
                        .order_by(
                            ActivityModelAttempt.started_at,
                            ActivityModelAttempt.id,
                        )
                    )
                ).scalars()
            )
            if work_unit_ids
            else []
        )
        tools = (
            list(
                (
                    await self.db.execute(
                        select(ActivityToolExecution)
                        .where(ActivityToolExecution.work_unit_id.in_(work_unit_ids))
                        .order_by(
                            ActivityToolExecution.created_at,
                            ActivityToolExecution.id,
                        )
                    )
                ).scalars()
            )
            if work_unit_ids
            else []
        )
        operations = (
            list(
                (
                    await self.db.execute(
                        select(ActivityContextOperation)
                        .where(ActivityContextOperation.work_unit_id.in_(work_unit_ids))
                        .order_by(
                            ActivityContextOperation.created_at,
                            ActivityContextOperation.id,
                        )
                    )
                ).scalars()
            )
            if work_unit_ids
            else []
        )
        attempt_ids = [int(item.id) for item in attempts]
        operation_ids = [int(item.id) for item in operations]
        direct_artifact_ids = {
            int(message.artifact_id)
            for message in messages
            if message.artifact_id is not None
        }
        for tool in tools:
            for storage_ref in (
                tool.arguments_storage_ref,
                tool.result_storage_ref,
            ):
                artifact_id = self._storage_artifact_id(storage_ref)
                if artifact_id is not None:
                    direct_artifact_ids.add(artifact_id)
        artifacts = (
            list(
                (
                    await self.db.execute(
                        select(ActivityNativeArtifact)
                        .where(
                            or_(
                                ActivityNativeArtifact.attempt_id.in_(attempt_ids)
                                if attempt_ids
                                else False,
                                ActivityNativeArtifact.context_operation_id.in_(
                                    operation_ids
                                )
                                if operation_ids
                                else False,
                                ActivityNativeArtifact.id.in_(direct_artifact_ids)
                                if direct_artifact_ids
                                else False,
                            )
                        )
                        .order_by(ActivityNativeArtifact.id)
                    )
                ).scalars()
            )
            if attempt_ids or operation_ids or direct_artifact_ids
            else []
        )

        current_message_ids: set[int] = set()
        revision_ids = {
            int(thread.current_revision_id)
            for thread in threads
            if thread.current_revision_id is not None
        }
        revisions = (
            list(
                (
                    await self.db.execute(
                        select(ActivityCanonicalContextRevision).where(
                            ActivityCanonicalContextRevision.id.in_(revision_ids)
                        )
                    )
                ).scalars()
            )
            if revision_ids
            else []
        )
        for revision in revisions:
            try:
                manifest = json.loads(revision.message_manifest_json)
            except (TypeError, json.JSONDecodeError):
                continue
            if isinstance(manifest, list):
                current_message_ids.update(
                    int(item)
                    for item in manifest
                    if isinstance(item, int) and not isinstance(item, bool)
                )

        work_unit_map = {int(item.id): item for item in work_units}
        invocation_map = {int(item.id): item for item in invocations}
        projected_tool_call_ids = {
            str(item.tool_call_id) for item in tools if item.tool_call_id
        }
        artifacts_by_attempt: dict[int, list[ActivityNativeArtifact]] = {}
        artifacts_by_operation: dict[int, list[ActivityNativeArtifact]] = {}
        artifacts_by_id = {int(item.id): item for item in artifacts}
        for artifact in artifacts:
            if artifact.attempt_id is not None:
                artifacts_by_attempt.setdefault(int(artifact.attempt_id), []).append(
                    artifact
                )
            if artifact.context_operation_id is not None:
                artifacts_by_operation.setdefault(
                    int(artifact.context_operation_id), []
                ).append(artifact)
        readable_artifact_ids: set[int] = set()
        if super_admin:
            for artifact in artifacts:
                if await self.access_service.may_view_reasoning_artifact(
                    user,
                    session,
                    artifact,
                    db=self.db,
                ):
                    readable_artifact_ids.add(int(artifact.id))

        entries: list[dict[str, Any]] = []

        def add(
            entry_type: str,
            row_id: int,
            created_at: datetime | None,
            payload: dict[str, Any],
        ) -> None:
            key = self._sort_key(created_at, entry_type, row_id)
            entries.append(
                {
                    "id": f"{entry_type}:{row_id}",
                    "type": entry_type,
                    "created_at": _iso(created_at),
                    "order_key": f"{key[0]}:{key[1]}:{key[2]}",
                    "_sort": key,
                    **payload,
                }
            )

        for invocation in invocations:
            add(
                "invocation_boundary",
                int(invocation.id),
                invocation.created_at,
                {
                    "session_id": session_id,
                    "invocation_id": invocation.id,
                    "status": invocation.status,
                    "phase": invocation.current_phase,
                    "task_type": invocation.task_type,
                    "base_sha": invocation.base_sha if admin else None,
                    "head_sha": invocation.final_head_sha or invocation.initial_head_sha
                    if admin
                    else None,
                },
            )

        for thread in threads:
            add(
                "thread_boundary",
                int(thread.id),
                thread.created_at,
                {
                    "session_id": session_id,
                    "thread_id": thread.id,
                    "role": thread.thread_purpose,
                    "current_revision_id": thread.current_revision_id,
                    "archived": thread.archived_at is not None,
                },
            )

        for message in messages:
            message_tool_call_ids = _message_tool_call_ids(message.message_json)
            is_projected_tool_result = (
                message.role == "tool"
                and bool(message.tool_call_id)
                and str(message.tool_call_id) in projected_tool_call_ids
            )
            is_projected_tool_request = (
                message.role == "assistant"
                and not str(message.content or "").strip()
                and bool(message_tool_call_ids & projected_tool_call_ids)
            )
            if is_projected_tool_result or is_projected_tool_request:
                # Canonical messages retain the exact protocol transcript, while
                # the public conversation timeline represents a tool round once
                # through its authoritative ActivityToolExecution card.
                continue
            work_unit = work_unit_map.get(int(message.work_unit_id))
            content_allowed = message.role == "assistant"
            restricted = message.role in _SENSITIVE_MESSAGE_ROLES
            message_artifact = (
                artifacts_by_id.get(int(message.artifact_id))
                if message.artifact_id is not None
                else None
            )
            add(
                "message",
                int(message.id),
                message.created_at,
                {
                    "session_id": session_id,
                    "invocation_id": work_unit.invocation_id if work_unit else None,
                    "work_unit_id": message.work_unit_id,
                    "thread_id": message.thread_id,
                    "role": message.role,
                    "source_role": work_unit.purpose if work_unit else None,
                    "seq": int(message.seq),
                    "content": message.content if content_allowed else None,
                    "content_visibility": (
                        "visible"
                        if content_allowed
                        else "restricted"
                        if restricted
                        else "unavailable"
                    ),
                    "artifact_id": message.artifact_id,
                    "artifacts": self._artifact_metadata(
                        [message_artifact] if message_artifact is not None else [],
                        readable_artifact_ids=readable_artifact_ids,
                    ),
                    "can_request_sensitive": bool(
                        message_artifact is not None
                        and int(message_artifact.id) in readable_artifact_ids
                    ),
                    "tool_call_id": message.tool_call_id,
                    "origin_attempt_id": message.origin_attempt_id,
                    "context_revision_id": message.context_revision_id,
                    "current_context": (
                        int(message.id) in current_message_ids
                        if current_message_ids
                        else True
                    ),
                },
            )

        for attempt in attempts:
            work_unit = work_unit_map.get(int(attempt.work_unit_id))
            invocation = (
                invocation_map.get(int(work_unit.invocation_id))
                if work_unit is not None
                else None
            )
            attempt_artifacts = artifacts_by_attempt.get(int(attempt.id), [])
            common = {
                "session_id": session_id,
                "invocation_id": invocation.id if invocation else None,
                "work_unit_id": attempt.work_unit_id,
                "thread_id": work_unit.thread_id if work_unit else None,
                "source_role": work_unit.purpose if work_unit else attempt.purpose,
            }
            add(
                "attempt",
                int(attempt.id),
                attempt.started_at or attempt.created_at,
                {
                    **common,
                    "attempt": project_attempt(attempt, user),
                    "artifacts": self._artifact_metadata(
                        attempt_artifacts,
                        readable_artifact_ids=readable_artifact_ids,
                    ),
                },
            )
            add(
                "reasoning",
                int(attempt.id),
                attempt.reasoning_started_at
                or attempt.started_at
                or attempt.created_at,
                {
                    **common,
                    "attempt_id": attempt.id,
                    "status": (
                        "running"
                        if attempt.status == "running"
                        and attempt.reasoning_started_at is not None
                        and attempt.reasoning_completed_at is None
                        else "completed"
                        if attempt.reasoning_completed_at is not None
                        else attempt.status
                    ),
                    "thinking_mode": attempt.effective_thinking_mode or "unavailable",
                    "effort": attempt.effective_effort or "unavailable",
                    "availability": attempt.reasoning_availability,
                    "tokens": attempt.reasoning_tokens,
                    "started_at": _iso(attempt.reasoning_started_at),
                    "completed_at": _iso(attempt.reasoning_completed_at),
                    "artifacts": self._artifact_metadata(
                        [
                            item
                            for item in attempt_artifacts
                            if item.artifact_kind
                            in {
                                "reasoning_content",
                                "reasoning_summary",
                                "encrypted_opaque",
                                "reasoning",
                            }
                        ],
                        readable_artifact_ids=readable_artifact_ids,
                    ),
                },
            )

        for tool in tools:
            work_unit = work_unit_map.get(int(tool.work_unit_id))
            arguments_artifact_id = self._storage_artifact_id(
                tool.arguments_storage_ref
            )
            result_artifact_id = self._storage_artifact_id(tool.result_storage_ref)
            tool_artifacts = [
                artifact
                for artifact_id in (arguments_artifact_id, result_artifact_id)
                if artifact_id is not None
                and (artifact := artifacts_by_id.get(artifact_id)) is not None
            ]
            common = {
                "session_id": session_id,
                "invocation_id": work_unit.invocation_id if work_unit else None,
                "work_unit_id": tool.work_unit_id,
                "thread_id": tool.thread_id,
                "source_role": work_unit.purpose if work_unit else None,
                "tool_execution_id": tool.id,
                "tool_call_id": tool.tool_call_id,
                "name": tool.name,
                "status": tool.status,
                "started_at": _iso(tool.started_at),
                "completed_at": _iso(tool.completed_at),
                "artifacts": self._artifact_metadata(
                    tool_artifacts,
                    readable_artifact_ids=readable_artifact_ids,
                ),
            }
            add("tool_call", int(tool.id), tool.created_at, common)
            if tool.status in {"completed", "failed", "cancelled"}:
                add(
                    "tool_result",
                    int(tool.id),
                    tool.completed_at or tool.created_at,
                    {
                        **common,
                        "content_visibility": "restricted",
                        "arguments_artifact_id": arguments_artifact_id,
                        "result_artifact_id": result_artifact_id,
                        "can_request_sensitive": bool(
                            any(
                                int(artifact.id) in readable_artifact_ids
                                for artifact in tool_artifacts
                            )
                        ),
                    },
                )

        for operation in operations:
            work_unit = work_unit_map.get(int(operation.work_unit_id))
            add(
                "context_operation",
                int(operation.id),
                operation.created_at,
                {
                    "session_id": session_id,
                    "invocation_id": work_unit.invocation_id if work_unit else None,
                    "work_unit_id": operation.work_unit_id,
                    "thread_id": operation.thread_id,
                    "source_role": work_unit.purpose if work_unit else None,
                    "operation_type": operation.operation_type,
                    "trigger_reason": operation.trigger_reason,
                    "status": operation.status,
                    "before_revision_id": operation.before_revision_id,
                    "after_revision_id": operation.after_revision_id,
                    "completed_at": _iso(operation.completed_at),
                    "artifacts": self._artifact_metadata(
                        artifacts_by_operation.get(int(operation.id), []),
                        readable_artifact_ids=readable_artifact_ids,
                    ),
                },
            )

        entries.sort(key=lambda item: item["_sort"])
        return entries

    @staticmethod
    def _storage_artifact_id(reference: str | None) -> int | None:
        if not reference or not reference.startswith("artifact:"):
            return None
        try:
            return int(reference.split(":", 1)[1])
        except (TypeError, ValueError):
            return None

    async def get_conversation(
        self,
        session_id: int,
        user: dict[str, Any],
        *,
        cursor: str | None = None,
        limit: int | None = None,
    ) -> dict[str, Any]:
        session = await self.access_service.require_session_access(
            session_id, user, self.db
        )
        auth_version = await self.access_service.authorization_version(user, self.db)
        config = self.access_service._require_cursor_config()
        page_size = max(1, min(int(limit or config.page_size), 200))
        entries = await self._collect_entries(session, user)
        before = (
            self._decode_history_cursor(
                cursor,
                session_id=session_id,
                authorization_version=auth_version,
            )
            if cursor
            else None
        )
        eligible = [
            item for item in entries if before is None or item["_sort"] < before
        ]
        page = eligible[-page_size:]
        has_more = len(eligible) > len(page)
        before_cursor = (
            self._history_cursor(
                session_id=session_id,
                before=page[0]["_sort"],
                authorization_version=auth_version,
            )
            if page and has_more
            else None
        )
        high_water = int(session.session_event_sequence or 0)
        for item in page:
            item.pop("_sort", None)
        return {
            "projection_version": CONVERSATION_PROJECTION_VERSION,
            "session_id": session_id,
            "entries": page,
            "before_cursor": before_cursor,
            "has_more": has_more,
            "events_cursor": self.access_service.create_cursor(
                session_id=session_id,
                last_scanned_sequence=high_water,
                authorization_version=auth_version,
            ),
            "last_scanned_sequence": high_water,
        }

    async def get_updates(
        self,
        session_id: int,
        user: dict[str, Any],
        *,
        cursor: str | None,
    ) -> dict[str, Any]:
        event_page = await self.access_service.list_events_after(
            session_id,
            user,
            cursor=cursor,
            db=self.db,
        )
        if not event_page["events"]:
            return {
                "projection_version": CONVERSATION_PROJECTION_VERSION,
                "entries": [],
                "cursor": event_page["cursor"],
                "last_scanned_sequence": event_page["last_scanned_sequence"],
            }
        session = await self.access_service.require_session_access(
            session_id, user, self.db
        )
        entries = await self._collect_entries(session, user)
        affected_work_units = {
            int(item["work_unit_id"])
            for item in event_page["events"]
            if item.get("work_unit_id") is not None
        }
        affected_invocations = {
            int(item["invocation_id"])
            for item in event_page["events"]
            if item.get("invocation_id") is not None
        }
        if affected_work_units or affected_invocations:
            entries = [
                item
                for item in entries
                if item.get("work_unit_id") in affected_work_units
                or item.get("invocation_id") in affected_invocations
            ]
        for item in entries:
            item.pop("_sort", None)
        return {
            "projection_version": CONVERSATION_PROJECTION_VERSION,
            "entries": entries,
            "cursor": event_page["cursor"],
            "last_scanned_sequence": event_page["last_scanned_sequence"],
        }


__all__ = [
    "CONVERSATION_PROJECTION_VERSION",
    "ConversationProjectionService",
]
