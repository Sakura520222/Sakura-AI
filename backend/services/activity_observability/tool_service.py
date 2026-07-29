"""Authoritative tool execution, canonical messages, and native artifacts."""

from __future__ import annotations

import hashlib
import hmac
import json
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Protocol

from loguru import logger
from sqlalchemy import or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from backend.models import database as db_module
from backend.models.activity_observability_models import (
    ActivityArtifactAccessLog,
    ActivityCanonicalContextRevision,
    ActivityContextOperation,
    ActivityInvocation,
    ActivityInvocationWorkUnit,
    ActivityModelAttempt,
    ActivityNativeArtifact,
    ActivityObservabilityMessage,
    ActivityObservabilitySession,
    ActivityResourceIdentity,
    ActivityThread,
    ActivityToolExecution,
)
from backend.models.database import utc_now
from backend.core.config import get_settings
from backend.services.secret_crypto_service import decrypt_secret, encrypt_secret
from backend.services.activity_observability.context_service import (
    ContextService,
    ThreadLeaseToken,
)
from backend.services.activity_observability.event_service import append_lifecycle_event
from backend.services.activity_observability.reasoning import (
    REASONING_ENCRYPTED_OPAQUE,
    REASONING_PROVIDER_EXPOSED,
    ReasoningCapturePolicy,
    VALID_AVAILABILITY,
    build_compatibility_key,
)

TOOL_STATUS_PENDING = "pending"
TOOL_STATUS_RUNNING = "running"
TOOL_STATUS_COMPLETED = "completed"
TOOL_STATUS_FAILED = "failed"
TOOL_STATUS_CANCELLED = "cancelled"
TERMINAL_TOOL_STATUSES = frozenset(
    {TOOL_STATUS_COMPLETED, TOOL_STATUS_FAILED, TOOL_STATUS_CANCELLED}
)
VALID_TOOL_STATUSES = frozenset(
    {TOOL_STATUS_PENDING, TOOL_STATUS_RUNNING, *TERMINAL_TOOL_STATUSES}
)

SENSITIVITY_PUBLIC = "public"
SENSITIVITY_INTERNAL = "internal"
SENSITIVITY_SENSITIVE = "sensitive"
SENSITIVITY_SECRET = "secret"
VALID_SENSITIVITY = frozenset(
    {
        SENSITIVITY_PUBLIC,
        SENSITIVITY_INTERNAL,
        SENSITIVITY_SENSITIVE,
        SENSITIVITY_SECRET,
    }
)


class ConflictError(RuntimeError):
    """Raised when an idempotent operation conflicts with its prior result."""


def _normalize_tool_call(tc: Any) -> dict[str, str]:
    """Normalize an OpenAI-shaped tool_call into ``{id, name, arguments}``."""
    if isinstance(tc, dict):
        fn = tc.get("function") or {}
        return {
            "id": str(tc.get("id", "")),
            "name": str(fn.get("name", "")),
            "arguments": str(fn.get("arguments", "")),
        }
    fn = getattr(tc, "function", None)
    return {
        "id": str(getattr(tc, "id", "")),
        "name": str(getattr(fn, "name", "")),
        "arguments": str(getattr(fn, "arguments", "")),
    }


class ArtifactAccessDeniedError(PermissionError):
    """Raised for both missing and unauthorized artifact reads."""


@dataclass(frozen=True)
class ArtifactAuthorization:
    allowed: bool
    authorization_scope: str | None = None
    can_display: bool = False


class ArtifactAuthorizer(Protocol):
    async def authorize(
        self,
        *,
        reader: str,
        artifact: ActivityNativeArtifact,
        session: ActivityObservabilitySession,
        resource_identity: ActivityResourceIdentity,
        require_trace: bool,
    ) -> bool | ArtifactAuthorization: ...


@dataclass(frozen=True)
class AuthorizedArtifactView:
    """Safe projection; never exposes ORM or ciphertext."""

    artifact_id: int
    artifact_kind: str
    availability: str
    provider_family: str
    protocol_family: str
    compatibility_key: str
    capture_mode: str
    visibility: str
    payload_safe_summary: str | None
    payload: str | None
    payload_unavailable_reason: str | None
    replay_allowed: bool
    retention_expires_at: datetime | None


@dataclass(frozen=True)
class EncryptedPayload:
    ciphertext: str
    nonce: str
    key_id: str


class EncryptionProvider(Protocol):
    def encrypt(self, payload: str) -> EncryptedPayload: ...

    def decrypt(
        self,
        ciphertext: str,
        *,
        nonce: str | None,
        key_id: str | None,
    ) -> str: ...


class DefaultArtifactEncryptionProvider:
    """Application Fernet adapter used by production observability bundles."""

    def encrypt(self, payload: str) -> EncryptedPayload:
        settings = get_settings()
        return EncryptedPayload(
            ciphertext=encrypt_secret(payload),
            nonce="fernet-v1",
            key_id=str(
                getattr(
                    settings,
                    "activity_artifact_encryption_key_id",
                    "app-fernet-v1",
                )
            ),
        )

    def decrypt(
        self,
        ciphertext: str,
        *,
        nonce: str | None,
        key_id: str | None,
    ) -> str:
        del nonce, key_id
        return decrypt_secret(ciphertext)


class ToolService:
    """Service enforcing parent-chain, storage, and access invariants."""

    def __init__(
        self,
        db: AsyncSession | None = None,
        *,
        encryption_provider: EncryptionProvider | None = None,
        artifact_authorizer: ArtifactAuthorizer | None = None,
        artifact_hash_secret: bytes | None = None,
        clock=utc_now,
    ) -> None:
        self._db = db
        self._encryption_provider = encryption_provider
        self._artifact_authorizer = artifact_authorizer
        self._artifact_hash_secret = artifact_hash_secret
        self._clock = clock

    async def _session_scope(self):
        if self._db is not None:
            yield self._db
            return
        async with db_module.async_session() as db:
            yield db

    @staticmethod
    def _serialized(value: Any) -> str:
        if isinstance(value, str):
            return value
        return json.dumps(
            value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        )

    @staticmethod
    def _message_manifest_ids(
        revision: ActivityCanonicalContextRevision,
    ) -> list[int]:
        """Return a validated, ordered message manifest for one immutable revision."""
        try:
            manifest = json.loads(revision.message_manifest_json)
        except (TypeError, json.JSONDecodeError) as exc:
            raise ValueError(
                "context revision has an invalid message manifest"
            ) from exc
        if (
            not isinstance(manifest, list)
            or any(
                not isinstance(message_id, int) or isinstance(message_id, bool)
                for message_id in manifest
            )
            or len(set(manifest)) != len(manifest)
        ):
            raise ValueError("context revision has an invalid message manifest")
        return manifest

    @staticmethod
    def is_failed_tool_result(message: dict[str, Any]) -> bool:
        """Recognize the stable tool-result failure envelope used by reviewers."""
        payload: Any = message.get("content")
        if isinstance(payload, str):
            try:
                payload = json.loads(payload)
            except (TypeError, json.JSONDecodeError):
                return False
        if not isinstance(payload, dict):
            return False
        if payload.get("success") is False:
            return True
        status = str(payload.get("status") or "").strip().lower()
        return status in {"failed", "error"} or (
            bool(payload.get("error")) and payload.get("success") is not True
        )

    def _digest(self, payload: str | None) -> str | None:
        if payload is None or self._artifact_hash_secret is None:
            return None
        return hmac.new(
            self._artifact_hash_secret, payload.encode("utf-8"), hashlib.sha256
        ).hexdigest()

    async def _capture_sensitive_artifact_row(
        self,
        db,
        *,
        artifact_kind: str,
        payload: str,
        attempt: ActivityModelAttempt | None = None,
        context_operation_id: int | None = None,
        endpoint_scope: str,
    ) -> ActivityNativeArtifact:
        """Create an encrypted artifact inside the caller-owned transaction.

        Canonical conversation persistence uses this helper so sensitive
        messages and tool payloads are committed atomically with their public
        metadata row. Encryption failures deliberately produce metadata only;
        they never fall back to plaintext.
        """
        encrypted = None
        capture_error = None
        if self._encryption_provider is None:
            capture_error = "encryption_unavailable"
        else:
            try:
                encrypted = self._encryption_provider.encrypt(payload)
            except Exception:
                capture_error = "encryption_failed"
                logger.exception(
                    "敏感观测数据未保存：加密失败 attempt_id={} kind={}",
                    attempt.id if attempt is not None else None,
                    artifact_kind,
                )
        if capture_error == "encryption_unavailable":
            logger.warning(
                "敏感观测数据未保存：加密服务不可用 attempt_id={} kind={}",
                attempt.id if attempt is not None else None,
                artifact_kind,
            )

        provider_family = (
            str(
                attempt.effective_provider
                or attempt.requested_provider
                or "application"
            )
            if attempt is not None
            else "application"
        )
        protocol_family = (
            str(attempt.protocol_family or "canonical")
            if attempt is not None
            else "canonical"
        )
        model_family = (
            str(attempt.effective_model or attempt.requested_model or "conversation")
            if attempt is not None
            else "conversation"
        )
        retention_days = int(get_settings().activity_artifact_retention_days)
        artifact = ActivityNativeArtifact(
            attempt_id=attempt.id if attempt is not None else None,
            context_operation_id=context_operation_id,
            artifact_kind=artifact_kind,
            availability=REASONING_PROVIDER_EXPOSED,
            provider_family=provider_family,
            protocol_family=protocol_family,
            model_family=model_family,
            compatibility_key=build_compatibility_key(
                provider_family,
                protocol_family,
                model_family,
                endpoint_scope,
            ),
            capture_mode="artifact" if encrypted is not None else "metadata_only",
            visibility="admin_only",
            payload_ciphertext=encrypted.ciphertext if encrypted else None,
            payload_nonce=encrypted.nonce if encrypted else None,
            encryption_key_id=encrypted.key_id if encrypted else None,
            capture_error=capture_error,
            payload_safe_summary=None,
            payload_hash=self._digest(payload),
            retention_expires_at=self._clock() + timedelta(days=retention_days),
            replay_allowed=False,
        )
        db.add(artifact)
        await db.flush()
        return artifact

    def _decrypt_artifact_payload(
        self,
        artifact: ActivityNativeArtifact,
    ) -> str | None:
        decrypt = getattr(self._encryption_provider, "decrypt", None)
        if decrypt is None or not artifact.payload_ciphertext:
            return None
        try:
            return decrypt(
                artifact.payload_ciphertext,
                nonce=artifact.payload_nonce,
                key_id=artifact.encryption_key_id,
            )
        except Exception:
            logger.exception(
                "规范对话 Artifact 解密失败 artifact_id={} kind={}",
                artifact.id,
                artifact.artifact_kind,
            )
            return None

    @staticmethod
    async def _get_parent_chain(db, work_unit_id: int, thread_id: int | None):
        work_unit = await db.get(
            ActivityInvocationWorkUnit, work_unit_id, with_for_update=True
        )
        if work_unit is None:
            raise ValueError("work unit not found")
        if work_unit.thread_id != thread_id:
            raise ValueError("work unit and thread do not match")
        thread = None
        if thread_id is not None:
            thread = await db.get(ActivityThread, thread_id, with_for_update=True)
            if thread is None or thread.session_id != work_unit.session_id:
                raise ValueError("invalid work unit thread parent")
        invocation = await db.get(ActivityInvocation, work_unit.invocation_id)
        if invocation is None or invocation.session_id != work_unit.session_id:
            raise ValueError("invalid work unit invocation parent")
        return work_unit, thread, invocation

    async def create_tool_execution(
        self,
        *,
        work_unit_id: int,
        thread_id: int | None,
        origin_attempt_id: int | None,
        tool_call_id: str,
        name: str,
        arguments: Any,
        sensitivity: str = SENSITIVITY_INTERNAL,
    ) -> ActivityToolExecution:
        if sensitivity not in VALID_SENSITIVITY:
            raise ValueError(f"unknown sensitivity: {sensitivity}")
        if not tool_call_id or not name:
            raise ValueError("tool_call_id and name are required")
        arguments_json = self._serialized(arguments)
        async for db in self._session_scope():
            async with db.begin():
                work_unit, _, _ = await self._get_parent_chain(
                    db, work_unit_id, thread_id
                )
                if origin_attempt_id is not None:
                    attempt = await db.get(ActivityModelAttempt, origin_attempt_id)
                    if attempt is None or attempt.work_unit_id != work_unit.id:
                        raise ValueError("origin attempt is not owned by work unit")
                else:
                    attempt = None
                existing = (
                    await db.execute(
                        select(ActivityToolExecution).where(
                            ActivityToolExecution.work_unit_id == work_unit_id,
                            ActivityToolExecution.tool_call_id == tool_call_id,
                        )
                    )
                ).scalar_one_or_none()
                if existing is not None:
                    same_payload = (
                        existing.arguments_json == arguments_json
                        if sensitivity == SENSITIVITY_PUBLIC
                        else existing.arguments_hash == self._digest(arguments_json)
                    )
                    if (
                        existing.name != name
                        or existing.arguments_sensitivity != sensitivity
                        or not same_payload
                    ):
                        raise ConflictError(
                            "tool call id already has different payload"
                        )
                    return existing
                artifact = None
                if sensitivity != SENSITIVITY_PUBLIC:
                    artifact = await self._capture_sensitive_artifact_row(
                        db,
                        artifact_kind="request_projection",
                        payload=arguments_json,
                        attempt=attempt,
                        endpoint_scope=f"tool:{name}",
                    )
                execution = ActivityToolExecution(
                    work_unit_id=work_unit.id,
                    thread_id=thread_id,
                    origin_attempt_id=origin_attempt_id,
                    tool_call_id=tool_call_id,
                    name=name,
                    arguments_json=(
                        arguments_json if sensitivity == SENSITIVITY_PUBLIC else None
                    ),
                    arguments_sensitivity=sensitivity,
                    arguments_hash=self._digest(arguments_json),
                    arguments_storage_ref=(
                        f"artifact:{artifact.id}" if artifact is not None else None
                    ),
                    status=TOOL_STATUS_PENDING,
                )
                db.add(execution)
                try:
                    await db.flush()
                except IntegrityError as exc:
                    raise ConflictError("tool call id already exists") from exc
                return execution
        raise RuntimeError("unreachable")

    async def start_tool_execution(self, tool_execution_id: int) -> None:
        async for db in self._session_scope():
            async with db.begin():
                execution = await db.get(
                    ActivityToolExecution, tool_execution_id, with_for_update=True
                )
                if execution is None:
                    raise ValueError("tool execution not found")
                if execution.status == TOOL_STATUS_RUNNING:
                    return
                if execution.status != TOOL_STATUS_PENDING:
                    raise ConflictError("tool execution is not pending")
                execution.status = TOOL_STATUS_RUNNING
                execution.started_at = self._clock()
            return
        raise RuntimeError("unreachable")

    async def finish_tool_execution(
        self,
        tool_execution_id: int,
        *,
        status: str,
        result: Any = None,
        error_message: str | None = None,
        result_sensitivity: str = SENSITIVITY_INTERNAL,
    ) -> None:
        if status not in TERMINAL_TOOL_STATUSES:
            raise ValueError(f"unknown terminal status: {status}")
        if result_sensitivity not in VALID_SENSITIVITY:
            raise ValueError(f"unknown result sensitivity: {result_sensitivity}")
        result_json = None if result is None else self._serialized(result)
        async for db in self._session_scope():
            async with db.begin():
                execution = await db.get(
                    ActivityToolExecution, tool_execution_id, with_for_update=True
                )
                if execution is None:
                    raise ValueError("tool execution not found")
                if execution.status in TERMINAL_TOOL_STATUSES:
                    same = (
                        execution.status == status
                        and execution.result_sensitivity == result_sensitivity
                    )
                    same = same and (
                        execution.result_json == result_json
                        if result_sensitivity == SENSITIVITY_PUBLIC
                        else execution.result_hash == self._digest(result_json)
                    )
                    if not same:
                        raise ConflictError(
                            "terminal tool execution conflicts with prior result"
                        )
                    return
                if execution.status != TOOL_STATUS_RUNNING:
                    raise ConflictError("tool execution must be running")
                execution.status = status
                execution.completed_at = self._clock()
                execution.result_sensitivity = result_sensitivity
                execution.error_message = (
                    "tool execution failed" if status == TOOL_STATUS_FAILED else None
                )
                artifact = None
                if result_json is not None and result_sensitivity != SENSITIVITY_PUBLIC:
                    attempt = (
                        await db.get(ActivityModelAttempt, execution.origin_attempt_id)
                        if execution.origin_attempt_id is not None
                        else None
                    )
                    artifact = await self._capture_sensitive_artifact_row(
                        db,
                        artifact_kind="response_projection",
                        payload=result_json,
                        attempt=attempt,
                        endpoint_scope=f"tool:{execution.name}",
                    )
                execution.result_json = (
                    result_json if result_sensitivity == SENSITIVITY_PUBLIC else None
                )
                execution.result_hash = self._digest(result_json)
                execution.result_storage_ref = (
                    f"artifact:{artifact.id}" if artifact is not None else None
                )
            return
        raise RuntimeError("unreachable")

    async def get_tool_execution(
        self, tool_execution_id: int
    ) -> ActivityToolExecution | None:
        async for db in self._session_scope():
            return await db.get(ActivityToolExecution, tool_execution_id)
        raise RuntimeError("unreachable")

    async def append_assistant_message(
        self,
        *,
        thread_id: int,
        work_unit_id: int,
        origin_attempt_id: int | None,
        content: str,
        reasoning_content: str | None = None,
        artifact_id: int | None = None,
        seq: int | None = None,
    ) -> ActivityObservabilityMessage:
        """Append final visible content; reasoning_content is intentionally dropped."""
        async for db in self._session_scope():
            async with db.begin():
                work_unit, thread, invocation = await self._get_parent_chain(
                    db, work_unit_id, thread_id
                )
                if thread is None:
                    raise ValueError("canonical assistant messages require a thread")
                if origin_attempt_id is not None:
                    attempt = await db.get(ActivityModelAttempt, origin_attempt_id)
                    if attempt is None or attempt.work_unit_id != work_unit.id:
                        raise ValueError("origin attempt is not owned by work unit")
                if artifact_id is not None:
                    artifact = await db.get(ActivityNativeArtifact, artifact_id)
                    if artifact is None or artifact.attempt_id != origin_attempt_id:
                        raise ValueError("artifact is not owned by origin attempt")
                locked_thread = await db.get(
                    ActivityThread, thread.id, with_for_update=True
                )
                locked_thread.last_seq += 1
                message_seq = locked_thread.last_seq
                payload = {"content": content, "role": "assistant"}
                if artifact_id is not None:
                    payload["artifact_id"] = artifact_id
                message = ActivityObservabilityMessage(
                    thread_id=thread.id,
                    work_unit_id=work_unit.id,
                    origin_attempt_id=origin_attempt_id,
                    seq=message_seq,
                    role="assistant",
                    content=content,
                    message_json=json.dumps(
                        payload,
                        ensure_ascii=False,
                        sort_keys=True,
                        separators=(",", ":"),
                    ),
                    artifact_id=artifact_id,
                )
                db.add(message)
                await db.flush()
                await append_lifecycle_event(
                    db,
                    session_id=work_unit.session_id,
                    invocation_id=invocation.id,
                    work_unit_id=work_unit.id,
                    event_type="message_appended",
                    payload={
                        "status": "created",
                        "phase": work_unit.current_phase,
                    },
                )
                return message
        raise RuntimeError("unreachable")

    async def append_conversation_message(
        self,
        *,
        thread_id: int,
        work_unit_id: int,
        message: dict[str, Any],
        origin_attempt_id: int | None = None,
        lease: ThreadLeaseToken | None = None,
    ) -> ActivityObservabilityMessage:
        """Append an arbitrary-role conversation message (user/assistant/tool/system).

        Used by review/issue workers to persist the reviewer dialogue onto the
        canonical thread so incremental reviews can resume from the same thread.
        ``tool_calls`` on an assistant message create pending ``ActivityToolExecution``
        rows; ``reasoning_content`` is dropped per the canonical invariant.
        """
        role = str(message.get("role") or "")
        if not role:
            raise ValueError("message role is required")
        async for db in self._session_scope():
            async with db.begin():
                work_unit, thread, invocation = await self._get_parent_chain(
                    db, work_unit_id, thread_id
                )
                if thread is None:
                    raise ValueError("conversation messages require a thread")
                if origin_attempt_id is not None:
                    attempt = await db.get(ActivityModelAttempt, origin_attempt_id)
                    if attempt is None or attempt.work_unit_id != work_unit.id:
                        raise ValueError("origin attempt is not owned by work unit")
                else:
                    attempt = None
                locked_thread = await db.get(
                    ActivityThread, thread.id, with_for_update=True
                )
                locked_thread.last_seq += 1
                message_seq = locked_thread.last_seq
                # Canonical transcript never persists reasoning content.
                safe = {k: v for k, v in message.items() if k != "reasoning_content"}
                sensitive_payload_json = json.dumps(
                    safe,
                    ensure_ascii=False,
                    default=str,
                    sort_keys=True,
                    separators=(",", ":"),
                )
                content = message.get("content")
                if isinstance(content, list):
                    # OpenAI message content can be a list of parts; keep JSON form.
                    content_value: Any = json.dumps(
                        content, ensure_ascii=False, default=str
                    )
                else:
                    content_value = content
                tool_calls = message.get("tool_calls") or []
                needs_sensitive_artifact = role != "assistant" or bool(tool_calls)
                artifact = None
                if needs_sensitive_artifact:
                    artifact = await self._capture_sensitive_artifact_row(
                        db,
                        artifact_kind=(
                            "response_projection"
                            if role == "tool"
                            else "request_projection"
                        ),
                        payload=sensitive_payload_json,
                        attempt=attempt,
                        endpoint_scope=f"canonical-thread:{thread.id}",
                    )
                public_payload: dict[str, Any] = {"role": role}
                if role == "assistant" and isinstance(content_value, str):
                    public_payload["content"] = content_value
                if message.get("name"):
                    public_payload["name"] = str(message["name"])
                if message.get("tool_call_id"):
                    public_payload["tool_call_id"] = str(message["tool_call_id"])
                if tool_calls and isinstance(tool_calls, list) and role == "assistant":
                    public_payload["tool_calls"] = [
                        {
                            "id": parsed["id"],
                            "type": "function",
                            "function": {"name": parsed["name"]},
                        }
                        for parsed in (
                            _normalize_tool_call(item) for item in tool_calls
                        )
                        if parsed["id"] and parsed["name"]
                    ]
                if artifact is not None:
                    public_payload["artifact_id"] = artifact.id
                row = ActivityObservabilityMessage(
                    thread_id=thread.id,
                    work_unit_id=work_unit.id,
                    origin_attempt_id=origin_attempt_id,
                    seq=message_seq,
                    role=role,
                    content=(
                        content_value
                        if role == "assistant" and isinstance(content_value, str)
                        else None
                    ),
                    message_json=json.dumps(
                        public_payload,
                        ensure_ascii=False,
                        sort_keys=True,
                        separators=(",", ":"),
                    ),
                    artifact_id=artifact.id if artifact is not None else None,
                    tool_call_id=message.get("tool_call_id"),
                    context_revision_id=locked_thread.current_revision_id,
                )
                db.add(row)
                await db.flush()
                # Mirror assistant tool_calls as authoritative pending executions so
                # tool status tracking no longer needs the legacy checkpoint tables.
                if isinstance(tool_calls, list) and role == "assistant":
                    for tc in tool_calls:
                        parsed = _normalize_tool_call(tc)
                        if not parsed["id"] or not parsed["name"]:
                            continue
                        existing = (
                            await db.execute(
                                select(ActivityToolExecution).where(
                                    ActivityToolExecution.work_unit_id == work_unit.id,
                                    ActivityToolExecution.tool_call_id == parsed["id"],
                                )
                            )
                        ).scalar_one_or_none()
                        if existing is not None:
                            continue
                        db.add(
                            ActivityToolExecution(
                                work_unit_id=work_unit.id,
                                thread_id=thread.id,
                                origin_attempt_id=origin_attempt_id,
                                tool_call_id=parsed["id"],
                                name=parsed["name"],
                                arguments_json=None,
                                arguments_sensitivity=SENSITIVITY_INTERNAL,
                                arguments_hash=self._digest(parsed["arguments"] or ""),
                                arguments_storage_ref=(
                                    f"artifact:{artifact.id}"
                                    if artifact is not None
                                    else None
                                ),
                                status=TOOL_STATUS_PENDING,
                            )
                        )
                if role == "tool" and message.get("tool_call_id"):
                    execution = (
                        await db.execute(
                            select(ActivityToolExecution).where(
                                ActivityToolExecution.work_unit_id == work_unit.id,
                                ActivityToolExecution.tool_call_id
                                == str(message["tool_call_id"]),
                            )
                        )
                    ).scalar_one_or_none()
                    if execution is not None:
                        execution.result_json = None
                        execution.result_sensitivity = SENSITIVITY_INTERNAL
                        execution.result_hash = self._digest(
                            self._serialized(message.get("content"))
                        )
                        execution.result_storage_ref = (
                            f"artifact:{artifact.id}" if artifact is not None else None
                        )
                await db.flush()
                if lease is not None:
                    parent_revision_id = locked_thread.current_revision_id
                    if parent_revision_id is None:
                        message_ids = list(
                            (
                                await db.execute(
                                    select(ActivityObservabilityMessage.id)
                                    .where(
                                        ActivityObservabilityMessage.thread_id
                                        == thread.id
                                    )
                                    .order_by(ActivityObservabilityMessage.seq)
                                )
                            ).scalars()
                        )
                    else:
                        parent_revision = await db.get(
                            ActivityCanonicalContextRevision,
                            parent_revision_id,
                        )
                        if (
                            parent_revision is None
                            or parent_revision.thread_id != thread.id
                        ):
                            raise ValueError(
                                "thread head does not belong to its canonical thread"
                            )
                        message_ids = self._message_manifest_ids(parent_revision)
                        message_ids.append(int(row.id))
                    revision = await ContextService(db=db).create_revision(
                        thread.id,
                        lease,
                        expected_parent_revision_id=parent_revision_id,
                        message_manifest=message_ids,
                        reason="canonical_message_append",
                        created_invocation_id=work_unit.invocation_id,
                        created_work_unit_id=work_unit.id,
                    )
                    row.revision_id = revision.id
                    await db.flush()
                await append_lifecycle_event(
                    db,
                    session_id=work_unit.session_id,
                    invocation_id=invocation.id,
                    work_unit_id=work_unit.id,
                    event_type="message_appended",
                    payload={
                        "status": "created",
                        "phase": work_unit.current_phase,
                    },
                )
                return row
        raise RuntimeError("unreachable")

    async def load_conversation_messages(self, thread_id: int) -> list[dict[str, Any]]:
        """Load only the thread head revision in its canonical manifest order."""
        async for db in self._session_scope():
            thread = await db.get(ActivityThread, thread_id)
            if thread is None:
                return []
            if thread.current_revision_id is None:
                rows = (
                    (
                        await db.execute(
                            select(ActivityObservabilityMessage)
                            .where(ActivityObservabilityMessage.thread_id == thread_id)
                            .order_by(ActivityObservabilityMessage.seq)
                        )
                    )
                    .scalars()
                    .all()
                )
            else:
                revision = await db.get(
                    ActivityCanonicalContextRevision,
                    thread.current_revision_id,
                )
                if revision is None or revision.thread_id != thread_id:
                    raise ValueError(
                        "thread head does not belong to its canonical thread"
                    )
                manifest = self._message_manifest_ids(revision)
                if not manifest:
                    rows = []
                else:
                    loaded = (
                        (
                            await db.execute(
                                select(ActivityObservabilityMessage).where(
                                    ActivityObservabilityMessage.id.in_(manifest)
                                )
                            )
                        )
                        .scalars()
                        .all()
                    )
                    by_id = {int(row.id): row for row in loaded}
                    if len(by_id) != len(manifest):
                        raise ValueError(
                            "context revision references a missing canonical message"
                        )
                    rows = [by_id[message_id] for message_id in manifest]
            messages: list[dict[str, Any]] = []
            for row in rows:
                payload = None
                if row.artifact_id is not None:
                    artifact = await db.get(ActivityNativeArtifact, row.artifact_id)
                    if artifact is not None:
                        decrypted = self._decrypt_artifact_payload(artifact)
                        if decrypted is not None:
                            try:
                                candidate = json.loads(decrypted)
                            except (TypeError, json.JSONDecodeError):
                                candidate = None
                            if isinstance(candidate, dict):
                                payload = candidate
                try:
                    payload = payload or json.loads(row.message_json)
                except (TypeError, json.JSONDecodeError):
                    payload = {"role": row.role, "content": row.content}
                if isinstance(payload, dict):
                    payload.pop("reasoning_content", None)
                    messages.append(payload)
            return messages
        return []

    async def replace_context_messages(
        self,
        *,
        thread_id: int,
        work_unit_id: int,
        messages: list[dict[str, Any]],
        lease: ThreadLeaseToken,
        trigger_reason: str,
    ):
        """Persist replacement context and atomically advance the thread revision."""
        if not messages:
            raise ValueError("replacement context must contain at least one message")
        async for db in self._session_scope():
            async with db.begin():
                work_unit, thread, invocation = await self._get_parent_chain(
                    db, work_unit_id, thread_id
                )
                if thread is None:
                    raise ValueError("context replacement requires a thread")
                locked_thread = await db.get(
                    ActivityThread, thread.id, with_for_update=True
                )
                before_revision_id = locked_thread.current_revision_id
                context_service = ContextService(db=db)
                operation = await context_service.begin_operation(
                    work_unit.id,
                    "canonical_summary",
                    trigger_reason,
                    before_revision_id,
                )
                replacement_rows: list[ActivityObservabilityMessage] = []
                for message in messages:
                    role = str(message.get("role") or "").strip()
                    if not role:
                        raise ValueError("replacement message role is required")
                    safe = {
                        key: value
                        for key, value in message.items()
                        if key != "reasoning_content"
                    }
                    locked_thread.last_seq += 1
                    content = safe.get("content")
                    tool_calls = safe.get("tool_calls") or []
                    artifact = None
                    if role != "assistant" or bool(tool_calls):
                        artifact = await self._capture_sensitive_artifact_row(
                            db,
                            artifact_kind=(
                                "response_projection"
                                if role == "tool"
                                else "request_projection"
                            ),
                            payload=json.dumps(
                                safe,
                                ensure_ascii=False,
                                default=str,
                                sort_keys=True,
                                separators=(",", ":"),
                            ),
                            context_operation_id=operation.id,
                            endpoint_scope=f"canonical-thread:{thread.id}",
                        )
                    public_payload: dict[str, Any] = {"role": role}
                    if role == "assistant" and isinstance(content, str):
                        public_payload["content"] = content
                    if safe.get("name"):
                        public_payload["name"] = str(safe["name"])
                    if safe.get("tool_call_id"):
                        public_payload["tool_call_id"] = str(safe["tool_call_id"])
                    if artifact is not None:
                        public_payload["artifact_id"] = artifact.id
                    row = ActivityObservabilityMessage(
                        thread_id=thread.id,
                        work_unit_id=work_unit.id,
                        seq=locked_thread.last_seq,
                        role=role,
                        content=(
                            content
                            if role == "assistant" and isinstance(content, str)
                            else None
                        ),
                        message_json=json.dumps(
                            public_payload,
                            ensure_ascii=False,
                            sort_keys=True,
                            separators=(",", ":"),
                        ),
                        artifact_id=artifact.id if artifact is not None else None,
                        tool_call_id=safe.get("tool_call_id"),
                        context_revision_id=before_revision_id,
                    )
                    db.add(row)
                    replacement_rows.append(row)
                await db.flush()
                revision = await context_service.create_revision(
                    thread.id,
                    lease,
                    expected_parent_revision_id=before_revision_id,
                    message_manifest=[int(row.id) for row in replacement_rows],
                    reason="compaction",
                    context_operation_id=operation.id,
                    created_invocation_id=invocation.id,
                    created_work_unit_id=work_unit.id,
                )
                for row in replacement_rows:
                    row.revision_id = revision.id
                work_unit.current_phase = "context_compaction"
                invocation.current_phase = work_unit.current_phase
                session = await db.get(
                    ActivityObservabilitySession, work_unit.session_id
                )
                if session is not None:
                    session.last_active_at = self._clock()
                await context_service.complete_operation(
                    operation.id,
                    revision.id,
                    token=lease,
                )
                await db.flush()
                await append_lifecycle_event(
                    db,
                    session_id=work_unit.session_id,
                    invocation_id=invocation.id,
                    work_unit_id=work_unit.id,
                    event_type="context_compacted",
                    payload={
                        "status": "completed",
                        "phase": "context_compaction",
                    },
                )
                return revision
        raise RuntimeError("unreachable")

    async def mark_tool_execution_running(
        self, work_unit_id: int, tool_call_id: str
    ) -> None:
        async for db in self._session_scope():
            async with db.begin():
                execution = (
                    await db.execute(
                        select(ActivityToolExecution).where(
                            ActivityToolExecution.work_unit_id == work_unit_id,
                            ActivityToolExecution.tool_call_id == tool_call_id,
                        )
                    )
                ).scalar_one_or_none()
                if execution is None or execution.status == TOOL_STATUS_RUNNING:
                    return
                if execution.status != TOOL_STATUS_PENDING:
                    return
                execution.status = TOOL_STATUS_RUNNING
                execution.started_at = self._clock()
                work_unit, _, invocation = await self._get_parent_chain(
                    db, work_unit_id, execution.thread_id
                )
                work_unit.current_phase = f"tool:{execution.name}"
                invocation.current_phase = work_unit.current_phase
                session = await db.get(
                    ActivityObservabilitySession, work_unit.session_id
                )
                if session is not None:
                    session.last_active_at = self._clock()
                await append_lifecycle_event(
                    db,
                    session_id=work_unit.session_id,
                    invocation_id=invocation.id,
                    work_unit_id=work_unit.id,
                    event_type="tool_started",
                    payload={
                        "status": execution.status,
                        "phase": work_unit.current_phase,
                    },
                )
        return

    async def mark_tool_execution_completed(
        self, work_unit_id: int, tool_call_id: str
    ) -> None:
        async for db in self._session_scope():
            async with db.begin():
                execution = (
                    await db.execute(
                        select(ActivityToolExecution).where(
                            ActivityToolExecution.work_unit_id == work_unit_id,
                            ActivityToolExecution.tool_call_id == tool_call_id,
                        )
                    )
                ).scalar_one_or_none()
                if execution is None or execution.status == TOOL_STATUS_COMPLETED:
                    return
                execution.status = TOOL_STATUS_COMPLETED
                execution.completed_at = self._clock()
                work_unit, _, invocation = await self._get_parent_chain(
                    db, work_unit_id, execution.thread_id
                )
                work_unit.current_phase = "model_followup"
                invocation.current_phase = work_unit.current_phase
                session = await db.get(
                    ActivityObservabilitySession, work_unit.session_id
                )
                if session is not None:
                    session.last_active_at = self._clock()
                await append_lifecycle_event(
                    db,
                    session_id=work_unit.session_id,
                    invocation_id=invocation.id,
                    work_unit_id=work_unit.id,
                    event_type="tool_completed",
                    payload={
                        "status": execution.status,
                        "phase": work_unit.current_phase,
                    },
                )
        return

    async def mark_tool_execution_failed(
        self,
        work_unit_id: int,
        tool_call_id: str,
        *,
        error_message: str = "tool execution failed",
    ) -> None:
        """Mark an authoritative tool execution failed without storing raw errors."""
        async for db in self._session_scope():
            async with db.begin():
                execution = (
                    await db.execute(
                        select(ActivityToolExecution).where(
                            ActivityToolExecution.work_unit_id == work_unit_id,
                            ActivityToolExecution.tool_call_id == tool_call_id,
                        )
                    )
                ).scalar_one_or_none()
                if execution is None or execution.status == TOOL_STATUS_FAILED:
                    return
                if execution.status not in {TOOL_STATUS_PENDING, TOOL_STATUS_RUNNING}:
                    return
                execution.status = TOOL_STATUS_FAILED
                execution.completed_at = self._clock()
                execution.error_message = "tool execution failed"
                work_unit, _, invocation = await self._get_parent_chain(
                    db, work_unit_id, execution.thread_id
                )
                work_unit.current_phase = "tool_failed"
                invocation.current_phase = work_unit.current_phase
                session = await db.get(
                    ActivityObservabilitySession, work_unit.session_id
                )
                if session is not None:
                    session.last_active_at = self._clock()
                await append_lifecycle_event(
                    db,
                    session_id=work_unit.session_id,
                    invocation_id=invocation.id,
                    work_unit_id=work_unit.id,
                    event_type="tool_failed",
                    payload={
                        "status": execution.status,
                        "phase": work_unit.current_phase,
                    },
                )
        return

    async def capture_reasoning_artifact(
        self,
        *,
        attempt_id: int,
        context_operation_id: int | None = None,
        artifact_kind: str = "reasoning",
        availability: str,
        payload: str | None,
        provider_family: str,
        protocol_family: str,
        model_family: str,
        endpoint_scope: str,
        policy: ReasoningCapturePolicy,
        response_item_id: str | None = None,
        recovery_cursor: str | None = None,
    ) -> ActivityNativeArtifact | None:
        if availability not in VALID_AVAILABILITY:
            raise ValueError(f"unknown availability: {availability}")
        key = build_compatibility_key(
            provider_family, protocol_family, model_family, endpoint_scope
        )
        persist = policy.should_persist_payload(
            availability, provider_family, protocol_family
        )
        capture_error = None
        encrypted = None
        if persist and payload is not None:
            if self._encryption_provider is None:
                capture_error = "encryption_unavailable"
                logger.warning(
                    "敏感观测数据未保存：加密服务不可用 attempt_id={} kind={}",
                    attempt_id,
                    artifact_kind,
                )
            else:
                try:
                    encrypted = self._encryption_provider.encrypt(payload)
                except Exception:
                    capture_error = "encryption_failed"
                    logger.exception(
                        "敏感观测数据未保存：加密失败 attempt_id={} kind={}",
                        attempt_id,
                        artifact_kind,
                    )
        normalized_kind = artifact_kind
        if artifact_kind == "reasoning":
            normalized_kind = {
                "summarized": "reasoning_summary",
                "provider_exposed": "reasoning_content",
                "encrypted_opaque": "encrypted_opaque",
            }.get(availability, "reasoning_content")
        retention = (
            self._clock() + timedelta(days=policy.retention_days)
            if policy.retention_days is not None
            else None
        )
        async for db in self._session_scope():
            async with db.begin():
                attempt = await db.get(ActivityModelAttempt, attempt_id)
                if attempt is None:
                    raise ValueError("attempt not found")
                artifact = ActivityNativeArtifact(
                    attempt_id=attempt.id,
                    context_operation_id=context_operation_id,
                    artifact_kind=normalized_kind,
                    availability=availability,
                    provider_family=provider_family,
                    protocol_family=protocol_family,
                    model_family=model_family,
                    compatibility_key=key,
                    response_item_id=response_item_id,
                    recovery_cursor=recovery_cursor,
                    capture_mode=(
                        "artifact" if encrypted is not None else "metadata_only"
                    ),
                    visibility=policy.artifact_visibility,
                    payload_ciphertext=encrypted.ciphertext if encrypted else None,
                    payload_nonce=encrypted.nonce if encrypted else None,
                    encryption_key_id=encrypted.key_id if encrypted else None,
                    capture_error=capture_error,
                    payload_safe_summary=None,
                    payload_hash=self._digest(payload),
                    retention_expires_at=retention,
                    replay_allowed=policy.capture_mode == "artifact"
                    and availability
                    in {REASONING_ENCRYPTED_OPAQUE, REASONING_PROVIDER_EXPOSED},
                )
                db.add(artifact)
                await db.flush()
                return artifact
        raise RuntimeError("unreachable")

    async def capture_sensitive_artifact(
        self,
        *,
        artifact_kind: str,
        payload: str,
        attempt_id: int | None = None,
        context_operation_id: int | None = None,
        provider_family: str,
        protocol_family: str,
        model_family: str,
        endpoint_scope: str,
        availability: str = REASONING_PROVIDER_EXPOSED,
        visibility: str = "admin_only",
        retention_days: int | None = None,
    ) -> ActivityNativeArtifact:
        """Encrypt a sanitized request/response/conversation projection."""
        if artifact_kind not in {
            "reasoning_content",
            "reasoning_summary",
            "request_projection",
            "response_projection",
            "encrypted_opaque",
        }:
            raise ValueError("unsupported sensitive artifact kind")
        if availability not in VALID_AVAILABILITY:
            raise ValueError(f"unknown availability: {availability}")
        encrypted = None
        capture_error = None
        if self._encryption_provider is None:
            capture_error = "encryption_unavailable"
            logger.warning(
                "敏感观测数据未保存：加密服务不可用 attempt_id={} kind={}",
                attempt_id,
                artifact_kind,
            )
        else:
            try:
                encrypted = self._encryption_provider.encrypt(payload)
            except Exception:
                capture_error = "encryption_failed"
                logger.exception(
                    "敏感观测数据未保存：加密失败 attempt_id={} kind={}",
                    attempt_id,
                    artifact_kind,
                )
        retention = (
            self._clock() + timedelta(days=retention_days)
            if retention_days is not None
            else None
        )
        key = build_compatibility_key(
            provider_family,
            protocol_family,
            model_family,
            endpoint_scope,
        )
        async for db in self._session_scope():
            async with db.begin():
                if attempt_id is not None:
                    attempt = await db.get(ActivityModelAttempt, attempt_id)
                    if attempt is None:
                        raise ValueError("attempt not found")
                artifact = ActivityNativeArtifact(
                    attempt_id=attempt_id,
                    context_operation_id=context_operation_id,
                    artifact_kind=artifact_kind,
                    availability=availability,
                    provider_family=provider_family,
                    protocol_family=protocol_family,
                    model_family=model_family,
                    compatibility_key=key,
                    capture_mode=(
                        "artifact" if encrypted is not None else "metadata_only"
                    ),
                    visibility=visibility,
                    payload_ciphertext=encrypted.ciphertext if encrypted else None,
                    payload_nonce=encrypted.nonce if encrypted else None,
                    encryption_key_id=encrypted.key_id if encrypted else None,
                    capture_error=capture_error,
                    payload_safe_summary=None,
                    payload_hash=self._digest(payload),
                    retention_expires_at=retention,
                    replay_allowed=artifact_kind == "encrypted_opaque",
                )
                db.add(artifact)
                await db.flush()
                return artifact
        raise RuntimeError("unreachable")

    async def _audit(
        self, db, artifact_id: int | None, actor: str, scope: str | None, outcome: str
    ) -> None:
        db.add(
            ActivityArtifactAccessLog(
                artifact_id=artifact_id,
                actor_external_id=actor,
                action="read",
                authorization_scope=scope,
                outcome=outcome,
                metadata_json=None,
                created_at=self._clock(),
            )
        )
        await db.flush()

    async def read_artifact_with_audit(
        self,
        artifact_id: int,
        *,
        reader: str | None = None,
        reader_user_id: str | None = None,
        is_admin: bool | None = None,
        require_trace: bool = True,
    ) -> AuthorizedArtifactView | None:
        """Read through an injected authorizer; default is fail closed."""
        actor = reader or reader_user_id or "anonymous"
        async for db in self._session_scope():
            async with db.begin():
                artifact = await db.get(ActivityNativeArtifact, artifact_id)
                if artifact is None:
                    await self._audit(db, None, actor, None, "denied_not_found")
                    return None
                attempt = (
                    await db.get(ActivityModelAttempt, artifact.attempt_id)
                    if artifact.attempt_id is not None
                    else None
                )
                work_unit = await db.get(
                    ActivityInvocationWorkUnit,
                    attempt.work_unit_id if attempt else -1,
                )
                if work_unit is None and artifact.context_operation_id is not None:
                    operation = await db.get(
                        ActivityContextOperation,
                        artifact.context_operation_id,
                    )
                    work_unit = await db.get(
                        ActivityInvocationWorkUnit,
                        operation.work_unit_id if operation else -1,
                    )
                if work_unit is None:
                    message = (
                        await db.execute(
                            select(ActivityObservabilityMessage).where(
                                ActivityObservabilityMessage.artifact_id == artifact.id
                            )
                        )
                    ).scalar_one_or_none()
                    work_unit = await db.get(
                        ActivityInvocationWorkUnit,
                        message.work_unit_id if message else -1,
                    )
                if work_unit is None:
                    storage_ref = f"artifact:{artifact.id}"
                    execution = (
                        await db.execute(
                            select(ActivityToolExecution).where(
                                or_(
                                    ActivityToolExecution.arguments_storage_ref
                                    == storage_ref,
                                    ActivityToolExecution.result_storage_ref
                                    == storage_ref,
                                )
                            )
                        )
                    ).scalar_one_or_none()
                    work_unit = await db.get(
                        ActivityInvocationWorkUnit,
                        execution.work_unit_id if execution else -1,
                    )
                invocation = await db.get(
                    ActivityInvocation, work_unit.invocation_id if work_unit else -1
                )
                session = await db.get(
                    ActivityObservabilitySession,
                    invocation.session_id if invocation else -1,
                )
                identity = await db.get(
                    ActivityResourceIdentity,
                    session.resource_identity_id if session else -1,
                )
                if self._artifact_authorizer is None or any(
                    item is None for item in (work_unit, invocation, session, identity)
                ):
                    await self._audit(db, artifact.id, actor, None, "denied")
                    return None
                decision = await self._artifact_authorizer.authorize(
                    reader=actor,
                    artifact=artifact,
                    session=session,
                    resource_identity=identity,
                    require_trace=require_trace,
                )
                decision = (
                    decision
                    if isinstance(decision, ArtifactAuthorization)
                    else ArtifactAuthorization(bool(decision))
                )
                if not decision.allowed:
                    await self._audit(
                        db, artifact.id, actor, decision.authorization_scope, "denied"
                    )
                    return None
                await self._audit(
                    db, artifact.id, actor, decision.authorization_scope, "allowed"
                )
                payload = None
                unavailable_reason = artifact.capture_error
                if (
                    decision.can_display
                    and artifact.payload_ciphertext
                    and artifact.availability != REASONING_ENCRYPTED_OPAQUE
                ):
                    decrypt = getattr(self._encryption_provider, "decrypt", None)
                    if decrypt is None:
                        unavailable_reason = "decryption_unavailable"
                    else:
                        try:
                            payload = decrypt(
                                artifact.payload_ciphertext,
                                nonce=artifact.payload_nonce,
                                key_id=artifact.encryption_key_id,
                            )
                        except Exception:
                            unavailable_reason = "decryption_failed"
                            await self._audit(
                                db,
                                artifact.id,
                                actor,
                                decision.authorization_scope,
                                "decrypt_failed",
                            )
                return AuthorizedArtifactView(
                    artifact_id=artifact.id,
                    artifact_kind=artifact.artifact_kind,
                    availability=artifact.availability,
                    provider_family=artifact.provider_family,
                    protocol_family=artifact.protocol_family,
                    compatibility_key=artifact.compatibility_key,
                    capture_mode=artifact.capture_mode,
                    visibility=artifact.visibility,
                    payload_safe_summary=artifact.payload_safe_summary
                    if decision.can_display
                    and artifact.availability != REASONING_ENCRYPTED_OPAQUE
                    else None,
                    payload=payload,
                    payload_unavailable_reason=unavailable_reason,
                    replay_allowed=artifact.replay_allowed,
                    retention_expires_at=artifact.retention_expires_at,
                )
        raise RuntimeError("unreachable")

    @staticmethod
    def can_replay_artifact(
        artifact: ActivityNativeArtifact, *, compatibility_key: str, policy_allows: bool
    ) -> bool:
        return bool(
            artifact.replay_allowed
            and policy_allows
            and artifact.compatibility_key == compatibility_key
        )
