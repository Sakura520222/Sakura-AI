"""Activity observability ORM models.

This module intentionally defines a new schema.  It does not reference or
migrate the legacy ``activity_*`` tables used by the existing activity page.
"""

from uuid import uuid4

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    Column,
    Float,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    String,
    TIMESTAMP,
    Text,
    UniqueConstraint,
    event,
    select,
)
from sqlalchemy.dialects.mysql import LONGTEXT
from sqlalchemy.orm import relationship

from backend.models.database import Base, utc_now


OBSERVABILITY_PREFIX = "activity_observability_"
ENDPOINT_FINGERPRINT_LENGTH = 64
ENDPOINT_FINGERPRINT_CHECK = "endpoint_fingerprint REGEXP '^[0-9a-f]{64}$'"
OBSERVABILITY_TEXT = LONGTEXT().with_variant(Text(), "sqlite")
OBSERVABILITY_ASCII_512 = String(512, collation="ascii_bin").with_variant(
    String(512), "sqlite"
)
OBSERVABILITY_ASCII_64 = String(64, collation="ascii_bin").with_variant(
    String(64), "sqlite"
)
OBSERVABILITY_ASCII_128 = String(128, collation="ascii_bin").with_variant(
    String(128), "sqlite"
)


class ActivityResourceIdentity(Base):
    """Stable identity of a repository resource across webhook deliveries."""

    __tablename__ = f"{OBSERVABILITY_PREFIX}resource_identities"
    __table_args__ = (
        UniqueConstraint(
            "source_system_instance",
            "repository_external_id",
            "resource_type",
            "resource_number",
            name="uq_activity_observability_resource_identity",
        ),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    source_system_instance = Column(String(255), nullable=False)
    repository_external_id = Column(String(255), nullable=False)
    resource_type = Column(String(50), nullable=False)
    resource_number = Column(String(100), nullable=False)
    # Mutable repository display name; identity is the external repository ID.
    repo_full_name = Column(String(255), nullable=False)
    created_at = Column(TIMESTAMP, default=utc_now, nullable=False)

    sessions = relationship(
        "backend.models.activity_observability_models.ActivityObservabilitySession",
        back_populates="resource_identity",
        cascade="all, delete-orphan",
    )


class ActivityObservabilitySession(Base):
    """Long-lived session for one stable resource identity."""

    __tablename__ = f"{OBSERVABILITY_PREFIX}sessions"
    __table_args__ = (
        UniqueConstraint(
            "resource_identity_id",
            name="uq_activity_observability_session_resource_identity",
        ),
        ForeignKeyConstraint(
            ["last_invocation_id", "id"],
            [
                f"{OBSERVABILITY_PREFIX}invocations.id",
                f"{OBSERVABILITY_PREFIX}invocations.session_id",
            ],
            name="fk_activity_observability_session_last_invocation",
            use_alter=True,
        ),
        Index("ix_activity_observability_session_status", "status"),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    resource_identity_id = Column(
        Integer,
        ForeignKey(f"{OBSERVABILITY_PREFIX}resource_identities.id", ondelete="CASCADE"),
        nullable=False,
    )
    session_kind = Column(String(50), nullable=False)
    status = Column(String(50), nullable=False, default="open")
    last_invocation_id = Column(
        Integer,
        nullable=True,
    )
    # Event 和 Outbox 的 sequence 由服务层在此计数器上原子分配。
    session_event_sequence = Column(BigInteger, nullable=False, default=0)
    last_active_at = Column(TIMESTAMP, nullable=True)
    created_at = Column(TIMESTAMP, default=utc_now, nullable=False)
    archived_at = Column(TIMESTAMP, nullable=True)

    resource_identity = relationship(
        "ActivityResourceIdentity", back_populates="sessions"
    )
    threads = relationship(
        "backend.models.activity_observability_models.ActivityThread",
        back_populates="session",
        cascade="all, delete-orphan",
    )
    triggers = relationship(
        "ActivityTrigger",
        back_populates="session",
        cascade="all, delete-orphan",
    )
    invocations = relationship(
        "backend.models.activity_observability_models.ActivityInvocation",
        back_populates="session",
        cascade="all, delete-orphan",
        foreign_keys="ActivityInvocation.session_id",
    )


class ActivityThread(Base):
    """A transcript lane inside a long-lived activity session."""

    __tablename__ = f"{OBSERVABILITY_PREFIX}threads"
    __table_args__ = (
        UniqueConstraint(
            "session_id",
            "thread_purpose",
            name="uq_activity_observability_thread_purpose",
        ),
        UniqueConstraint(
            "session_id",
            "id",
            name="uq_activity_observability_thread_session_id",
        ),
        ForeignKeyConstraint(
            ["id", "current_revision_id"],
            [
                f"{OBSERVABILITY_PREFIX}canonical_context_revisions.thread_id",
                f"{OBSERVABILITY_PREFIX}canonical_context_revisions.id",
            ],
            name="fk_activity_observability_thread_current_revision",
            use_alter=True,
        ),
        Index("ix_activity_observability_thread_session", "session_id"),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    session_id = Column(
        Integer,
        ForeignKey(f"{OBSERVABILITY_PREFIX}sessions.id", ondelete="CASCADE"),
        nullable=False,
    )
    thread_purpose = Column(String(100), nullable=False)
    current_revision_id = Column(Integer, nullable=True)
    # Monotonic fencing counter used when an expired lease is taken over.
    lease_fencing_token = Column(BigInteger, nullable=False, default=0)
    last_seq = Column(BigInteger, nullable=False, default=0)
    created_at = Column(TIMESTAMP, default=utc_now, nullable=False)
    last_active_at = Column(TIMESTAMP, nullable=True)
    archived_at = Column(TIMESTAMP, nullable=True)

    session = relationship(
        "backend.models.activity_observability_models.ActivityObservabilitySession",
        back_populates="threads",
    )
    revisions = relationship(
        "backend.models.activity_observability_models.ActivityCanonicalContextRevision",
        back_populates="thread",
        foreign_keys="ActivityCanonicalContextRevision.thread_id",
        # 已加载的 revision 由 ORM 级联删除，未加载的由数据库 ON DELETE CASCADE
        # 处理，避免 ORM 将非空 thread_id 置 NULL。
        cascade="all, delete-orphan",
        passive_deletes=True,
    )
    current_revision = relationship(
        "backend.models.activity_observability_models.ActivityCanonicalContextRevision",
        foreign_keys=[current_revision_id],
        post_update=True,
    )


class ActivityTrigger(Base):
    """An independently auditable external or manual trigger."""

    __tablename__ = f"{OBSERVABILITY_PREFIX}triggers"
    __table_args__ = (
        UniqueConstraint(
            "dedupe_key", name="uq_activity_observability_trigger_dedupe_key"
        ),
        Index(
            "ix_activity_observability_trigger_session_status", "session_id", "status"
        ),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    session_id = Column(
        Integer,
        ForeignKey(f"{OBSERVABILITY_PREFIX}sessions.id", ondelete="CASCADE"),
        nullable=False,
    )
    trigger_kind = Column(String(100), nullable=False)
    status = Column(String(50), nullable=False, default="pending")
    dedupe_key = Column(OBSERVABILITY_ASCII_512, nullable=False)
    source_delivery_id = Column(String(255), nullable=True)
    source_comment_id = Column(String(255), nullable=True)
    actor_external_id = Column(String(255), nullable=True)
    metadata_json = Column(OBSERVABILITY_TEXT, nullable=True)
    base_sha = Column(String(255), nullable=True)
    head_sha = Column(String(255), nullable=True)
    created_at = Column(TIMESTAMP, default=utc_now, nullable=False)
    leased_at = Column(TIMESTAMP, nullable=True)
    consumed_at = Column(TIMESTAMP, nullable=True)
    cancelled_at = Column(TIMESTAMP, nullable=True)

    session = relationship(
        "backend.models.activity_observability_models.ActivityObservabilitySession",
        back_populates="triggers",
    )
    invocation_links = relationship(
        "ActivityInvocationTrigger",
        back_populates="trigger",
        cascade="all, delete-orphan",
    )


class ActivityInvocation(Base):
    """One business execution consuming one or more triggers."""

    __tablename__ = f"{OBSERVABILITY_PREFIX}invocations"
    __table_args__ = (
        ForeignKeyConstraint(
            ["primary_work_unit_id", "id"],
            [
                f"{OBSERVABILITY_PREFIX}invocation_work_units.id",
                f"{OBSERVABILITY_PREFIX}invocation_work_units.invocation_id",
            ],
            name="fk_activity_observability_invocation_primary_work_unit",
            use_alter=True,
        ),
        UniqueConstraint(
            "id",
            "session_id",
            name="uq_activity_observability_invocation_id_session",
        ),
        Index(
            "ix_activity_observability_invocation_session_status",
            "session_id",
            "status",
        ),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    session_id = Column(
        Integer,
        ForeignKey(f"{OBSERVABILITY_PREFIX}sessions.id", ondelete="CASCADE"),
        nullable=False,
    )
    task_type = Column(String(50), nullable=True)
    task_id = Column(Integer, nullable=True)
    primary_work_unit_id = Column(
        Integer,
        nullable=True,
    )
    session_event_sequence = Column(BigInteger, nullable=False, default=0)
    status = Column(String(50), nullable=False, default="queued")
    current_phase = Column(String(100), nullable=True)
    primary_requested_provider = Column(String(100), nullable=True)
    primary_requested_model = Column(String(255), nullable=True)
    primary_requested_thinking_mode = Column(String(100), nullable=True)
    primary_final_provider = Column(String(100), nullable=True)
    primary_final_model = Column(String(255), nullable=True)
    primary_final_thinking_mode = Column(String(100), nullable=True)
    base_sha = Column(String(255), nullable=True)
    initial_head_sha = Column(String(255), nullable=True)
    final_head_sha = Column(String(255), nullable=True)
    error_message = Column(OBSERVABILITY_TEXT, nullable=True)
    result_summary_json = Column(OBSERVABILITY_TEXT, nullable=True)
    created_at = Column(TIMESTAMP, default=utc_now, nullable=False)
    started_at = Column(TIMESTAMP, nullable=True)
    completed_at = Column(TIMESTAMP, nullable=True)
    cancelled_at = Column(TIMESTAMP, nullable=True)

    session = relationship(
        "backend.models.activity_observability_models.ActivityObservabilitySession",
        back_populates="invocations",
        foreign_keys=[session_id],
    )
    trigger_links = relationship(
        "ActivityInvocationTrigger",
        back_populates="invocation",
        cascade="all, delete-orphan",
    )
    work_units = relationship(
        "backend.models.activity_observability_models.ActivityInvocationWorkUnit",
        back_populates="invocation",
        cascade="all, delete-orphan",
        foreign_keys="ActivityInvocationWorkUnit.invocation_id",
    )


class ActivityInvocationTrigger(Base):
    """Many-to-one consumption relation between invocations and triggers."""

    __tablename__ = f"{OBSERVABILITY_PREFIX}invocation_triggers"
    __table_args__ = (
        UniqueConstraint(
            "trigger_id", name="uq_activity_observability_trigger_consumed"
        ),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    invocation_id = Column(
        Integer,
        ForeignKey(f"{OBSERVABILITY_PREFIX}invocations.id", ondelete="CASCADE"),
        nullable=False,
    )
    trigger_id = Column(
        Integer,
        ForeignKey(f"{OBSERVABILITY_PREFIX}triggers.id", ondelete="CASCADE"),
        nullable=False,
    )
    created_at = Column(TIMESTAMP, default=utc_now, nullable=False)

    invocation = relationship(
        "backend.models.activity_observability_models.ActivityInvocation",
        back_populates="trigger_links",
    )
    trigger = relationship(
        "backend.models.activity_observability_models.ActivityTrigger",
        back_populates="invocation_links",
    )


class ActivityObservabilityRoleBindingSnapshot(Base):
    """Immutable role binding configuration captured for one work unit."""

    __tablename__ = f"{OBSERVABILITY_PREFIX}role_binding_snapshots"
    __table_args__ = (
        CheckConstraint(
            ENDPOINT_FINGERPRINT_CHECK,
            name="ck_activity_observability_snapshot_endpoint_fingerprint",
            comment="仅允许保存小写 64 位 SHA-256 十六进制 endpoint 摘要。",
        ),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    role = Column(String(100), nullable=False)
    requested_provider = Column(String(100), nullable=False)
    requested_model = Column(String(255), nullable=False)
    requested_thinking_mode = Column(String(100), nullable=True)
    # 服务层使用稳定 key 顺序和紧凑分隔符序列化候选链，不保存凭据或 endpoint URL。
    candidate_chain_json = Column(OBSERVABILITY_TEXT, nullable=False)
    account_id = Column(String(255), nullable=False)
    protocol_family = Column(String(100), nullable=False)
    endpoint_fingerprint = Column(
        OBSERVABILITY_ASCII_64,
        nullable=False,
        comment="小写 64 位 SHA-256 十六进制 endpoint 摘要，不保存 endpoint URL。",
    )
    config_snapshot_version = Column(Integer, nullable=False)
    captured_at = Column(TIMESTAMP, default=utc_now, nullable=False)

    work_units = relationship(
        "backend.models.activity_observability_models.ActivityInvocationWorkUnit",
        back_populates="role_binding_snapshot",
    )


class ActivityInvocationWorkUnit(Base):
    """An execution unit that owns requested/final model state and attempts.

    服务层必须保证同一 invocation 最多一个 ``is_primary=true`` Work Unit。数据库
    迁移在支持 partial unique index 的目标上应建立 ``(invocation_id) WHERE
    is_primary`` 唯一约束；MySQL 不使用不受支持的表达式唯一索引。
    """

    __tablename__ = f"{OBSERVABILITY_PREFIX}invocation_work_units"
    __table_args__ = (
        UniqueConstraint(
            "id",
            "invocation_id",
            name="uq_activity_observability_work_unit_id_invocation",
        ),
        UniqueConstraint(
            "id",
            "thread_id",
            name="uq_activity_observability_work_unit_id_thread",
        ),
        UniqueConstraint(
            "id",
            "session_id",
            name="uq_activity_observability_work_unit_id_session",
        ),
        ForeignKeyConstraint(
            ["invocation_id", "session_id"],
            [
                f"{OBSERVABILITY_PREFIX}invocations.id",
                f"{OBSERVABILITY_PREFIX}invocations.session_id",
            ],
            name="fk_activity_observability_work_unit_invocation_session",
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            ["session_id", "thread_id"],
            [
                f"{OBSERVABILITY_PREFIX}threads.session_id",
                f"{OBSERVABILITY_PREFIX}threads.id",
            ],
            name="fk_activity_observability_work_unit_thread_session",
        ),
        Index("ix_activity_observability_work_unit_status", "status"),
        Index("ix_activity_observability_work_unit_thread", "thread_id"),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    invocation_id = Column(Integer, nullable=False)
    session_id = Column(Integer, nullable=False)
    thread_id = Column(Integer, nullable=True)
    role_binding_snapshot_id = Column(
        Integer,
        ForeignKey(
            f"{OBSERVABILITY_PREFIX}role_binding_snapshots.id",
            ondelete="RESTRICT",
        ),
        nullable=False,
    )
    purpose = Column(String(100), nullable=False)
    requirement = Column(String(50), nullable=False)
    status = Column(String(50), nullable=False, default="queued")
    current_phase = Column(String(100), nullable=True)
    requested_provider = Column(String(100), nullable=True)
    requested_model = Column(String(255), nullable=True)
    requested_thinking_mode = Column(String(100), nullable=True)
    final_provider = Column(String(100), nullable=True)
    final_model = Column(String(255), nullable=True)
    final_thinking_mode = Column(String(100), nullable=True)
    is_primary = Column(Boolean, nullable=False, default=False)
    error_message = Column(OBSERVABILITY_TEXT, nullable=True)
    created_at = Column(TIMESTAMP, default=utc_now, nullable=False)
    started_at = Column(TIMESTAMP, nullable=True)
    completed_at = Column(TIMESTAMP, nullable=True)
    cancelled_at = Column(TIMESTAMP, nullable=True)

    invocation = relationship(
        "backend.models.activity_observability_models.ActivityInvocation",
        back_populates="work_units",
        foreign_keys=[invocation_id],
    )
    role_binding_snapshot = relationship(
        "backend.models.activity_observability_models.ActivityObservabilityRoleBindingSnapshot",
        back_populates="work_units",
    )
    attempts = relationship(
        "backend.models.activity_observability_models.ActivityModelAttempt",
        back_populates="work_unit",
        cascade="all, delete-orphan",
    )
    results = relationship(
        "backend.models.activity_observability_models.ActivityWorkUnitResult",
        back_populates="work_unit",
        cascade="all, delete-orphan",
        foreign_keys="ActivityWorkUnitResult.work_unit_id",
    )


@event.listens_for(ActivityInvocationWorkUnit, "before_insert")
def _populate_work_unit_session_id(mapper, connection, target) -> None:
    """Derive the denormalized session id when legacy callers omit it."""
    if target.session_id is not None:
        return
    target.session_id = connection.execute(
        select(ActivityInvocation.session_id).where(
            ActivityInvocation.id == target.invocation_id
        )
    ).scalar_one_or_none()


class ActivityWorkUnitResult(Base):
    """Structured result produced by a work unit.

    ``thread_id``、``attempt_id`` 与 ``context_revision_id`` 的父链一致性由服务层
    在同一事务内校验；可表达的 Work Unit/Thread 一致性由复合外键强制。
    """

    __tablename__ = f"{OBSERVABILITY_PREFIX}work_unit_results"
    __table_args__ = (
        ForeignKeyConstraint(
            ["work_unit_id", "thread_id"],
            [
                f"{OBSERVABILITY_PREFIX}invocation_work_units.id",
                f"{OBSERVABILITY_PREFIX}invocation_work_units.thread_id",
            ],
            name="fk_activity_observability_result_work_unit_thread",
        ),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    work_unit_id = Column(
        Integer,
        ForeignKey(
            f"{OBSERVABILITY_PREFIX}invocation_work_units.id", ondelete="CASCADE"
        ),
        nullable=False,
    )
    thread_id = Column(Integer, nullable=True)
    attempt_id = Column(
        Integer,
        ForeignKey(f"{OBSERVABILITY_PREFIX}model_attempts.id", ondelete="SET NULL"),
        nullable=True,
    )
    context_revision_id = Column(
        Integer,
        ForeignKey(
            f"{OBSERVABILITY_PREFIX}canonical_context_revisions.id",
            ondelete="SET NULL",
        ),
        nullable=True,
    )
    result_kind = Column(String(100), nullable=False)
    status = Column(String(50), nullable=False, default="generated")
    payload_json = Column(OBSERVABILITY_TEXT, nullable=True)
    requires_publication = Column(Boolean, nullable=False, default=False)
    created_at = Column(TIMESTAMP, default=utc_now, nullable=False)

    work_unit = relationship(
        "backend.models.activity_observability_models.ActivityInvocationWorkUnit",
        back_populates="results",
        foreign_keys=[work_unit_id],
    )
    publications = relationship(
        "backend.models.activity_observability_models.ActivityPublication",
        back_populates="work_unit_result",
        cascade="all, delete-orphan",
    )


class ActivityModelAttempt(Base):
    """One observed provider request, directly owned only by a work unit."""

    __tablename__ = f"{OBSERVABILITY_PREFIX}model_attempts"
    __table_args__ = (
        UniqueConstraint(
            "work_unit_id",
            "attempt_index",
            name="uq_activity_observability_attempt_index",
        ),
        CheckConstraint(
            "context_revision_id IS NOT NULL OR contextless_reason IS NOT NULL",
            name="ck_activity_observability_attempt_context",
            comment=(
                "有 transcript 的 Work Unit 由服务层强制绑定 context revision；"
                "仅明确无 transcript 的调用可填写 contextless_reason。"
            ),
        ),
        CheckConstraint(
            "contextless_reason IS NULL OR "
            "(context_revision_id IS NULL AND contextless_reason IN "
            "('threadless_embedding', 'transcript_not_applicable'))",
            name="ck_activity_observability_attempt_contextless_reason",
        ),
        CheckConstraint(
            ENDPOINT_FINGERPRINT_CHECK,
            name="ck_activity_observability_attempt_endpoint_fingerprint",
            comment="非空时仅允许小写 64 位 SHA-256 十六进制 endpoint 摘要。",
        ),
        Index("ix_activity_observability_attempt_logical_call", "logical_call_id"),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    work_unit_id = Column(
        Integer,
        ForeignKey(
            f"{OBSERVABILITY_PREFIX}invocation_work_units.id", ondelete="CASCADE"
        ),
        nullable=False,
    )
    attempt_index = Column(Integer, nullable=False)
    logical_call_id = Column(String(255), nullable=False)
    # Authoritative attempt lifecycle.  The service, rather than ActivityEvent,
    # owns transitions so a provider send is never represented as a summary row.
    status = Column(String(50), nullable=False, default="running")
    attempt_kind = Column(String(100), nullable=False)
    purpose = Column(String(100), nullable=False)
    requested_provider = Column(String(100), nullable=True)
    requested_model = Column(String(255), nullable=True)
    requested_thinking_mode = Column(String(100), nullable=True)
    requested_effort = Column(String(50), nullable=True)
    effective_provider = Column(String(100), nullable=True)
    effective_model = Column(String(255), nullable=True)
    effective_thinking_mode = Column(String(100), nullable=True)
    effective_effort = Column(String(50), nullable=True)
    protocol_family = Column(String(100), nullable=True)
    max_output_tokens = Column(Integer, nullable=True)
    temperature = Column(Float, nullable=True)
    top_p = Column(Float, nullable=True)
    top_k = Column(Integer, nullable=True)
    tool_choice = Column(String(100), nullable=True)
    account_id = Column(String(255), nullable=True)
    endpoint_fingerprint = Column(
        OBSERVABILITY_ASCII_64,
        nullable=True,
        comment="小写 64 位 SHA-256 十六进制 endpoint 摘要；未知时为空。",
    )
    provider_request_id = Column(String(255), nullable=True)
    retry_of_attempt_id = Column(
        Integer,
        ForeignKey(f"{OBSERVABILITY_PREFIX}model_attempts.id", ondelete="SET NULL"),
        nullable=True,
    )
    fallback_from_attempt_id = Column(
        Integer,
        ForeignKey(f"{OBSERVABILITY_PREFIX}model_attempts.id", ondelete="SET NULL"),
        nullable=True,
    )
    context_revision_id = Column(
        Integer,
        ForeignKey(f"{OBSERVABILITY_PREFIX}canonical_context_revisions.id"),
        nullable=True,
        comment=(
            "有 transcript 的 Work Unit 必须由服务层设置；仅明确无上下文时可为空。"
        ),
    )
    contextless_reason = Column(
        String(100),
        nullable=True,
        comment="仅用于明确无 transcript 的调用，例如 thread_id=NULL 的 embedding。",
    )
    started_at = Column(TIMESTAMP, nullable=True)
    first_token_at = Column(TIMESTAMP, nullable=True)
    completed_at = Column(TIMESTAMP, nullable=True)
    http_status = Column(Integer, nullable=True)
    stop_reason = Column(String(100), nullable=True)
    error_category = Column(String(100), nullable=True)
    error_message = Column(OBSERVABILITY_TEXT, nullable=True)
    retryable = Column(Boolean, nullable=True)
    provider_usage_json = Column(OBSERVABILITY_TEXT, nullable=True)
    normalized_usage_json = Column(OBSERVABILITY_TEXT, nullable=True)
    input_tokens = Column(BigInteger, nullable=True)
    input_tokens_availability = Column(
        String(50), nullable=False, default="unavailable"
    )
    input_tokens_source = Column(String(100), nullable=False, default="provider")
    output_tokens = Column(BigInteger, nullable=True)
    output_tokens_availability = Column(
        String(50), nullable=False, default="unavailable"
    )
    output_tokens_source = Column(String(100), nullable=False, default="provider")
    reasoning_tokens = Column(BigInteger, nullable=True)
    reasoning_tokens_availability = Column(
        String(50), nullable=False, default="unavailable"
    )
    reasoning_tokens_source = Column(String(100), nullable=False, default="provider")
    reasoning_availability = Column(String(50), nullable=False, default="unavailable")
    reasoning_started_at = Column(TIMESTAMP, nullable=True)
    reasoning_completed_at = Column(TIMESTAMP, nullable=True)
    provider_event_metadata_json = Column(OBSERVABILITY_TEXT, nullable=True)
    cached_input_tokens = Column(BigInteger, nullable=True)
    cached_input_tokens_availability = Column(
        String(50), nullable=False, default="unavailable"
    )
    cached_input_tokens_source = Column(String(100), nullable=False, default="provider")
    created_at = Column(TIMESTAMP, default=utc_now, nullable=False)

    work_unit = relationship(
        "backend.models.activity_observability_models.ActivityInvocationWorkUnit",
        back_populates="attempts",
    )
    retry_of_attempt = relationship(
        "backend.models.activity_observability_models.ActivityModelAttempt",
        foreign_keys=[retry_of_attempt_id],
        remote_side=[id],
    )
    fallback_from_attempt = relationship(
        "backend.models.activity_observability_models.ActivityModelAttempt",
        foreign_keys=[fallback_from_attempt_id],
        remote_side=[id],
    )


class ActivityCanonicalContextRevision(Base):
    """Immutable, reproducible canonical context revision."""

    __tablename__ = f"{OBSERVABILITY_PREFIX}canonical_context_revisions"
    __table_args__ = (
        UniqueConstraint(
            "thread_id",
            "revision_number",
            name="uq_activity_observability_revision_number",
        ),
        UniqueConstraint(
            "thread_id",
            "id",
            name="uq_activity_observability_revision_thread_id",
        ),
        ForeignKeyConstraint(
            ["thread_id", "parent_revision_id"],
            [
                f"{OBSERVABILITY_PREFIX}canonical_context_revisions.thread_id",
                f"{OBSERVABILITY_PREFIX}canonical_context_revisions.id",
            ],
            name="fk_activity_observability_revision_parent_thread",
            use_alter=True,
        ),
        Index("ix_activity_observability_revision_thread", "thread_id"),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    thread_id = Column(
        Integer,
        ForeignKey(f"{OBSERVABILITY_PREFIX}threads.id", ondelete="CASCADE"),
        nullable=False,
    )
    revision_number = Column(Integer, nullable=False)
    parent_revision_id = Column(
        Integer,
        ForeignKey(
            f"{OBSERVABILITY_PREFIX}canonical_context_revisions.id",
            ondelete="SET NULL",
        ),
        nullable=True,
    )
    message_manifest_json = Column(OBSERVABILITY_TEXT, nullable=False)
    summary_artifact_reference = Column(String(255), nullable=True)
    system_manifest_json = Column(OBSERVABILITY_TEXT, nullable=True)
    tools_manifest_json = Column(OBSERVABILITY_TEXT, nullable=True)
    tool_choice_manifest_json = Column(OBSERVABILITY_TEXT, nullable=True)
    content_hash = Column(String(255), nullable=False)
    reason = Column(String(255), nullable=False, default="unspecified")
    created_invocation_id = Column(
        Integer,
        ForeignKey(f"{OBSERVABILITY_PREFIX}invocations.id", ondelete="SET NULL"),
        nullable=True,
    )
    created_work_unit_id = Column(
        Integer,
        ForeignKey(
            f"{OBSERVABILITY_PREFIX}invocation_work_units.id", ondelete="SET NULL"
        ),
        nullable=True,
    )
    created_context_operation_id = Column(
        Integer,
        ForeignKey(f"{OBSERVABILITY_PREFIX}context_operations.id", ondelete="SET NULL"),
        nullable=True,
    )
    status = Column(String(50), nullable=False, default="ready")
    created_at = Column(TIMESTAMP, default=utc_now, nullable=False)

    thread = relationship(
        "backend.models.activity_observability_models.ActivityThread",
        back_populates="revisions",
        foreign_keys=[thread_id],
    )
    parent_revision = relationship(
        "backend.models.activity_observability_models.ActivityCanonicalContextRevision",
        foreign_keys=[parent_revision_id],
        remote_side=[id],
    )


class ActivityContextSnapshot(Base):
    """Field-level context measurements with independent provenance."""

    __tablename__ = f"{OBSERVABILITY_PREFIX}context_snapshots"

    id = Column(Integer, primary_key=True, autoincrement=True)
    attempt_id = Column(
        Integer,
        ForeignKey(f"{OBSERVABILITY_PREFIX}model_attempts.id", ondelete="SET NULL"),
        nullable=True,
    )
    context_operation_id = Column(
        Integer,
        ForeignKey(f"{OBSERVABILITY_PREFIX}context_operations.id", ondelete="SET NULL"),
        nullable=True,
    )
    context_revision_id = Column(
        Integer,
        ForeignKey(
            f"{OBSERVABILITY_PREFIX}canonical_context_revisions.id",
            ondelete="SET NULL",
        ),
        nullable=True,
    )
    snapshot_kind = Column(String(50), nullable=False, default="before_request")
    context_tokens = Column(BigInteger, nullable=True)
    context_tokens_availability = Column(
        String(50), nullable=False, default="unavailable"
    )
    context_tokens_source = Column(String(100), nullable=False, default="provider")
    context_window_tokens = Column(BigInteger, nullable=True)
    context_window_tokens_availability = Column(
        String(50), nullable=False, default="unavailable"
    )
    context_window_tokens_source = Column(
        String(100), nullable=False, default="provider"
    )
    reserved_output_tokens = Column(BigInteger, nullable=True)
    reserved_output_tokens_availability = Column(
        String(50), nullable=False, default="unavailable"
    )
    reserved_output_tokens_source = Column(
        String(100), nullable=False, default="configuration"
    )
    available_context_tokens = Column(BigInteger, nullable=True)
    available_context_tokens_availability = Column(
        String(50), nullable=False, default="unavailable"
    )
    available_context_tokens_source = Column(
        String(100), nullable=False, default="heuristic"
    )
    cache_read_tokens = Column(BigInteger, nullable=True)
    cache_read_tokens_availability = Column(
        String(50), nullable=False, default="unavailable"
    )
    cache_read_tokens_source = Column(String(100), nullable=False, default="provider")
    cache_write_tokens = Column(BigInteger, nullable=True)
    cache_write_tokens_availability = Column(
        String(50), nullable=False, default="unavailable"
    )
    cache_write_tokens_source = Column(String(100), nullable=False, default="provider")
    reasoning_context_tokens = Column(BigInteger, nullable=True)
    reasoning_context_tokens_availability = Column(
        String(50), nullable=False, default="unavailable"
    )
    reasoning_context_tokens_source = Column(
        String(100), nullable=False, default="provider"
    )
    created_at = Column(TIMESTAMP, default=utc_now, nullable=False)


class ActivityContextOperation(Base):
    """A context compression, edit, or model handoff operation."""

    __tablename__ = f"{OBSERVABILITY_PREFIX}context_operations"
    __table_args__ = (
        UniqueConstraint(
            "thread_id",
            "operation_type",
            "before_revision_id",
            name="uq_activity_observability_operation_replay",
        ),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    work_unit_id = Column(
        Integer,
        ForeignKey(
            f"{OBSERVABILITY_PREFIX}invocation_work_units.id", ondelete="CASCADE"
        ),
        nullable=False,
    )
    thread_id = Column(
        Integer,
        ForeignKey(f"{OBSERVABILITY_PREFIX}threads.id", ondelete="CASCADE"),
        nullable=False,
    )
    operation_type = Column(String(100), nullable=False)
    trigger_reason = Column(String(100), nullable=False)
    before_revision_id = Column(
        Integer,
        ForeignKey(
            f"{OBSERVABILITY_PREFIX}canonical_context_revisions.id",
            ondelete="SET NULL",
        ),
        nullable=True,
    )
    after_revision_id = Column(
        Integer,
        ForeignKey(
            f"{OBSERVABILITY_PREFIX}canonical_context_revisions.id",
            ondelete="SET NULL",
        ),
        nullable=True,
    )
    before_snapshot_id = Column(
        Integer,
        ForeignKey(f"{OBSERVABILITY_PREFIX}context_snapshots.id", ondelete="SET NULL"),
        nullable=True,
    )
    after_snapshot_id = Column(
        Integer,
        ForeignKey(f"{OBSERVABILITY_PREFIX}context_snapshots.id", ondelete="SET NULL"),
        nullable=True,
    )
    summary_attempt_id = Column(
        Integer,
        ForeignKey(f"{OBSERVABILITY_PREFIX}model_attempts.id", ondelete="SET NULL"),
        nullable=True,
    )
    summary_artifact_reference = Column(String(255), nullable=True)
    status = Column(String(50), nullable=False, default="pending")
    error_message = Column(OBSERVABILITY_TEXT, nullable=True)
    created_at = Column(TIMESTAMP, default=utc_now, nullable=False)
    completed_at = Column(TIMESTAMP, nullable=True)


class ActivityThreadLease(Base):
    """Single-writer lease for context revisions in a thread."""

    __tablename__ = f"{OBSERVABILITY_PREFIX}thread_leases"
    __table_args__ = (
        UniqueConstraint("thread_id", name="uq_activity_observability_thread_lease"),
        ForeignKeyConstraint(
            ["owner_work_unit_id", "thread_id"],
            [
                f"{OBSERVABILITY_PREFIX}invocation_work_units.id",
                f"{OBSERVABILITY_PREFIX}invocation_work_units.thread_id",
            ],
            name="fk_activity_observability_thread_lease_owner_work_unit",
            ondelete="CASCADE",
        ),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    thread_id = Column(
        Integer,
        ForeignKey(f"{OBSERVABILITY_PREFIX}threads.id", ondelete="CASCADE"),
        nullable=False,
    )
    owner_work_unit_id = Column(Integer, nullable=False)
    fencing_token = Column(BigInteger, nullable=False)
    base_revision_id = Column(
        Integer,
        ForeignKey(
            f"{OBSERVABILITY_PREFIX}canonical_context_revisions.id",
            ondelete="SET NULL",
        ),
        nullable=True,
    )
    heartbeat_at = Column(TIMESTAMP, nullable=False, default=utc_now)
    expires_at = Column(TIMESTAMP, nullable=False)
    created_at = Column(TIMESTAMP, nullable=False, default=utc_now)


class ActivityObservabilityMessage(Base):
    """Canonical transcript message; provider-native reasoning is excluded.

    Work Unit 与 Thread 必须属于同一父链；复合外键阻止跨 Thread 写入。
    """

    __tablename__ = f"{OBSERVABILITY_PREFIX}messages"
    __table_args__ = (
        ForeignKeyConstraint(
            ["work_unit_id", "thread_id"],
            [
                f"{OBSERVABILITY_PREFIX}invocation_work_units.id",
                f"{OBSERVABILITY_PREFIX}invocation_work_units.thread_id",
            ],
            name="fk_activity_observability_message_work_unit_thread",
            ondelete="CASCADE",
        ),
        UniqueConstraint(
            "thread_id", "seq", name="uq_activity_observability_message_seq"
        ),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    thread_id = Column(
        Integer,
        ForeignKey(f"{OBSERVABILITY_PREFIX}threads.id", ondelete="CASCADE"),
        nullable=False,
    )
    work_unit_id = Column(Integer, nullable=False)
    artifact_id = Column(
        Integer,
        ForeignKey(
            f"{OBSERVABILITY_PREFIX}native_artifacts.id",
            ondelete="SET NULL",
            use_alter=True,
            name="fk_activity_observability_message_artifact",
        ),
        nullable=True,
    )
    revision_id = Column(
        Integer,
        ForeignKey(
            f"{OBSERVABILITY_PREFIX}canonical_context_revisions.id",
            ondelete="SET NULL",
        ),
        nullable=True,
    )
    context_revision_id = Column(
        Integer,
        ForeignKey(
            f"{OBSERVABILITY_PREFIX}canonical_context_revisions.id",
            ondelete="SET NULL",
        ),
        nullable=True,
    )
    origin_attempt_id = Column(
        Integer,
        ForeignKey(f"{OBSERVABILITY_PREFIX}model_attempts.id", ondelete="SET NULL"),
        nullable=True,
    )
    seq = Column(BigInteger, nullable=False)
    role = Column(String(50), nullable=False)
    content = Column(OBSERVABILITY_TEXT, nullable=True)
    message_json = Column(OBSERVABILITY_TEXT, nullable=False)
    tool_call_id = Column(String(255), nullable=True)
    created_at = Column(TIMESTAMP, default=utc_now, nullable=False)


class ActivityToolExecution(Base):
    """Authoritative state of one tool execution.

    Work Unit/Thread 的父链一致性由复合外键强制；``origin_attempt_id`` 必须属于
    同一 Work Unit 的语义由服务层在事务内校验。
    """

    __tablename__ = f"{OBSERVABILITY_PREFIX}tool_executions"
    __table_args__ = (
        ForeignKeyConstraint(
            ["work_unit_id", "thread_id"],
            [
                f"{OBSERVABILITY_PREFIX}invocation_work_units.id",
                f"{OBSERVABILITY_PREFIX}invocation_work_units.thread_id",
            ],
            name="fk_activity_observability_tool_work_unit_thread",
        ),
        UniqueConstraint(
            "work_unit_id",
            "tool_call_id",
            name="uq_activity_observability_tool_call",
        ),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    work_unit_id = Column(
        Integer,
        ForeignKey(
            f"{OBSERVABILITY_PREFIX}invocation_work_units.id", ondelete="CASCADE"
        ),
        nullable=False,
    )
    thread_id = Column(Integer, nullable=True)
    origin_attempt_id = Column(
        Integer,
        ForeignKey(f"{OBSERVABILITY_PREFIX}model_attempts.id", ondelete="SET NULL"),
        nullable=True,
    )
    tool_call_id = Column(String(255), nullable=False)
    name = Column(String(255), nullable=False)
    arguments_json = Column(OBSERVABILITY_TEXT, nullable=True)
    arguments_sensitivity = Column(String(50), nullable=True)
    arguments_hash = Column(OBSERVABILITY_ASCII_512, nullable=True)
    arguments_storage_ref = Column(String(255), nullable=True)
    result_json = Column(OBSERVABILITY_TEXT, nullable=True)
    result_sensitivity = Column(String(50), nullable=True)
    result_hash = Column(OBSERVABILITY_ASCII_512, nullable=True)
    result_storage_ref = Column(String(255), nullable=True)
    status = Column(String(50), nullable=False, default="pending")
    started_at = Column(TIMESTAMP, nullable=True)
    completed_at = Column(TIMESTAMP, nullable=True)
    error_message = Column(OBSERVABILITY_TEXT, nullable=True)
    created_at = Column(TIMESTAMP, default=utc_now, nullable=False)


class ActivityNativeArtifact(Base):
    """Provider-native artifact kept separate from canonical transcript data."""

    __tablename__ = f"{OBSERVABILITY_PREFIX}native_artifacts"

    id = Column(Integer, primary_key=True, autoincrement=True)
    attempt_id = Column(
        Integer,
        ForeignKey(f"{OBSERVABILITY_PREFIX}model_attempts.id", ondelete="SET NULL"),
        nullable=True,
    )
    context_operation_id = Column(
        Integer,
        ForeignKey(f"{OBSERVABILITY_PREFIX}context_operations.id", ondelete="SET NULL"),
        nullable=True,
    )
    artifact_kind = Column(String(100), nullable=False)
    provider_family = Column(String(100), nullable=False)
    protocol_family = Column(String(100), nullable=False)
    model_family = Column(String(255), nullable=False)
    compatibility_key = Column(OBSERVABILITY_ASCII_512, nullable=False)
    response_item_id = Column(String(255), nullable=True)
    recovery_cursor = Column(String(255), nullable=True)
    availability = Column(String(50), nullable=False, default="unavailable")
    capture_mode = Column(String(50), nullable=False, default="metadata_only")
    visibility = Column(String(50), nullable=False, default="admin_only")
    payload_ciphertext = Column(OBSERVABILITY_TEXT, nullable=True)
    payload_nonce = Column(String(255), nullable=True)
    encryption_key_id = Column(String(255), nullable=True)
    capture_error = Column(String(100), nullable=True)
    payload_safe_summary = Column(OBSERVABILITY_TEXT, nullable=True)
    payload_hash = Column(OBSERVABILITY_ASCII_512, nullable=True)
    retention_expires_at = Column(TIMESTAMP, nullable=True)
    replay_allowed = Column(Boolean, nullable=False, default=False)
    created_at = Column(TIMESTAMP, default=utc_now, nullable=False)


class ActivityPublication(Base):
    """Reliable state machine for an external publication side effect."""

    __tablename__ = f"{OBSERVABILITY_PREFIX}publications"
    __table_args__ = (
        UniqueConstraint(
            "external_idempotency_key",
            name="uq_activity_observability_publication_idempotency",
        ),
        Index("ix_activity_observability_publication_status", "status"),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    work_unit_result_id = Column(
        Integer,
        ForeignKey(f"{OBSERVABILITY_PREFIX}work_unit_results.id", ondelete="CASCADE"),
        nullable=False,
    )
    publication_kind = Column(String(100), nullable=False)
    external_idempotency_key = Column(OBSERVABILITY_ASCII_512, nullable=False)
    status = Column(String(50), nullable=False, default="pending")
    external_object_id = Column(String(255), nullable=True)
    external_object_url = Column(String(1024), nullable=True)
    request_fingerprint = Column(OBSERVABILITY_ASCII_64, nullable=True)
    marker = Column(OBSERVABILITY_ASCII_128, nullable=False)
    attempt_count = Column(Integer, nullable=False, default=0)
    # Compatibility alias for early Task 7 drafts; lifecycle code uses attempt_count.
    retry_count = Column(Integer, nullable=False, default=0)
    claim_token = Column(OBSERVABILITY_ASCII_64, nullable=True)
    error_category = Column(String(64), nullable=True)
    http_status = Column(Integer, nullable=True)
    reconciliation_json = Column(OBSERVABILITY_TEXT, nullable=True)
    error_message = Column(OBSERVABILITY_TEXT, nullable=True)
    started_at = Column(TIMESTAMP, nullable=True)
    completed_at = Column(TIMESTAMP, nullable=True)
    timed_out_at = Column(TIMESTAMP, nullable=True)
    created_at = Column(TIMESTAMP, default=utc_now, nullable=False)

    work_unit_result = relationship(
        "backend.models.activity_observability_models.ActivityWorkUnitResult",
        back_populates="publications",
    )


class ActivityObservabilityEvent(Base):
    """Auditable event and safe UI projection source for one session.

    服务层在同一事务内校验可选 invocation/work-unit 与 ``session_id`` 的父链，
    并与 Outbox 复制同一个 UUID 和原子分配的 sequence。
    """

    __tablename__ = f"{OBSERVABILITY_PREFIX}events"
    __table_args__ = (
        CheckConstraint(
            "visibility IN ('public', 'admin_only', 'internal', 'hidden')",
            name="ck_activity_observability_event_visibility",
        ),
        ForeignKeyConstraint(
            ["invocation_id", "session_id"],
            [
                f"{OBSERVABILITY_PREFIX}invocations.id",
                f"{OBSERVABILITY_PREFIX}invocations.session_id",
            ],
            name="fk_activity_observability_event_invocation_session",
        ),
        ForeignKeyConstraint(
            ["work_unit_id", "session_id"],
            [
                f"{OBSERVABILITY_PREFIX}invocation_work_units.id",
                f"{OBSERVABILITY_PREFIX}invocation_work_units.session_id",
            ],
            name="fk_activity_observability_event_work_unit_session",
        ),
        UniqueConstraint("event_uuid", name="uq_activity_observability_event_uuid"),
        UniqueConstraint(
            "event_uuid",
            "session_id",
            name="uq_activity_observability_event_uuid_session",
        ),
        UniqueConstraint(
            "session_id",
            "event_sequence",
            name="uq_activity_observability_session_event_sequence",
        ),
        Index(
            "ix_activity_observability_event_session", "session_id", "event_sequence"
        ),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    event_uuid = Column(String(36), nullable=False, default=lambda: str(uuid4()))
    session_id = Column(
        Integer,
        ForeignKey(f"{OBSERVABILITY_PREFIX}sessions.id", ondelete="CASCADE"),
        nullable=False,
    )
    invocation_id = Column(Integer, nullable=True)
    work_unit_id = Column(Integer, nullable=True)
    event_sequence = Column(
        BigInteger,
        nullable=False,
        comment="服务层在 Session.session_event_sequence 上原子分配。",
    )
    event_type = Column(String(100), nullable=False)
    visibility = Column(String(50), nullable=False)
    projection_json = Column(OBSERVABILITY_TEXT, nullable=True)
    created_at = Column(TIMESTAMP, default=utc_now, nullable=False)


def _never_render_compat_constraint(*args, **kwargs):
    """Keep the legacy metadata name without enforcing a false uniqueness rule."""
    del args, kwargs
    return False


class ActivityOutbox(Base):
    """Transactional outbox row for at-least-once event delivery.

    服务层须在写入 Event 的同一事务中创建 Outbox，复制同一个 ``event_uuid``，
    并使用 Session 计数器原子分配相同的 ``event_sequence``。
    """

    __tablename__ = f"{OBSERVABILITY_PREFIX}outbox"
    __table_args__ = (
        # Compatibility marker for schema-introspection clients.  It is not
        # emitted as a database constraint because one event has one row per
        # authorised recipient and therefore legitimately repeats event_uuid.
        UniqueConstraint(
            "event_uuid", name="uq_activity_observability_outbox_event_uuid"
        ).ddl_if(callable_=_never_render_compat_constraint),
        UniqueConstraint(
            "target_user_id",
            "session_id",
            "event_sequence",
            name="uq_activity_observability_outbox_user_event_sequence",
        ),
        ForeignKeyConstraint(
            ["event_uuid", "session_id"],
            [
                f"{OBSERVABILITY_PREFIX}events.event_uuid",
                f"{OBSERVABILITY_PREFIX}events.session_id",
            ],
            name="fk_activity_observability_outbox_event_session",
            ondelete="CASCADE",
        ),
        Index("ix_activity_observability_outbox_dispatch", "status", "next_attempt_at"),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    event_uuid = Column(String(36), nullable=False)
    target_user_id = Column(String(255), nullable=False)
    session_id = Column(Integer, nullable=False)
    event_sequence = Column(
        BigInteger,
        nullable=False,
        comment="服务层在 Session.session_event_sequence 上原子分配。",
    )
    projection_version = Column(Integer, nullable=False)
    status = Column(String(50), nullable=False, default="pending")
    payload_json = Column(OBSERVABILITY_TEXT, nullable=False)
    attempt_count = Column(Integer, nullable=False, default=0)
    next_attempt_at = Column(TIMESTAMP, nullable=True)
    claimed_at = Column(TIMESTAMP, nullable=True)
    claim_token = Column(String(255), nullable=True)
    published_at = Column(TIMESTAMP, nullable=True)
    last_error = Column(OBSERVABILITY_TEXT, nullable=True)
    created_at = Column(TIMESTAMP, default=utc_now, nullable=False)


class ActivityArtifactAccessLog(Base):
    """Audit log for every protected native-artifact access."""

    __tablename__ = f"{OBSERVABILITY_PREFIX}artifact_access_logs"
    __table_args__ = (
        Index("ix_activity_observability_artifact_access_artifact", "artifact_id"),
        Index("ix_activity_observability_artifact_access_actor", "actor_external_id"),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    artifact_id = Column(
        Integer,
        ForeignKey(f"{OBSERVABILITY_PREFIX}native_artifacts.id", ondelete="CASCADE"),
        nullable=True,
    )
    actor_external_id = Column(String(255), nullable=False)
    action = Column(String(100), nullable=False)
    authorization_scope = Column(String(255), nullable=True)
    outcome = Column(String(50), nullable=False)
    metadata_json = Column(OBSERVABILITY_TEXT, nullable=True)
    created_at = Column(TIMESTAMP, default=utc_now, nullable=False)


ActivitySession = ActivityObservabilitySession
ActivityMessage = ActivityObservabilityMessage
ActivityEvent = ActivityObservabilityEvent


__all__ = [
    "ActivityResourceIdentity",
    "ActivityObservabilitySession",
    "ActivitySession",
    "ActivityThread",
    "ActivityTrigger",
    "ActivityInvocation",
    "ActivityInvocationTrigger",
    "ActivityObservabilityRoleBindingSnapshot",
    "ActivityInvocationWorkUnit",
    "ActivityWorkUnitResult",
    "ActivityModelAttempt",
    "ActivityCanonicalContextRevision",
    "ActivityContextSnapshot",
    "ActivityContextOperation",
    "ActivityThreadLease",
    "ActivityObservabilityMessage",
    "ActivityMessage",
    "ActivityToolExecution",
    "ActivityNativeArtifact",
    "ActivityPublication",
    "ActivityObservabilityEvent",
    "ActivityEvent",
    "ActivityOutbox",
    "ActivityArtifactAccessLog",
]
