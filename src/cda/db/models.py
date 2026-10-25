"""PostgreSQL schema for disclosure packs, runs, and their evidence."""

from datetime import datetime
from decimal import Decimal
from typing import Any
from uuid import UUID

from pgvector.sqlalchemy import Vector
from sqlalchemy import (
    CHAR,
    REAL,
    BigInteger,
    Boolean,
    CheckConstraint,
    Computed,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    Text,
    UniqueConstraint,
)
from sqlalchemy import (
    text as sql_text,
)
from sqlalchemy.dialects.postgresql import JSONB, TSVECTOR
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import DeclarativeBase, Mapped, declared_attr, mapped_column

from cda.db.enums import (
    CONFIDENCES,
    DOCUMENT_KINDS,
    EVIDENCE_ROLES,
    FINDING_STATUSES,
    INCONSISTENCY_TYPES,
    REVIEW_DECISIONS,
    RUN_METHODS,
    RUN_STATUSES,
    UNIT_KINDS,
    USER_ROLES,
    Confidence,
    DocumentKind,
    EvidenceRole,
    FindingStatus,
    InconsistencyType,
    ReviewDecision,
    RunMethod,
    RunStatus,
    UnitKind,
    UserRole,
)


def _allowed_values(column: str, values: tuple[str, ...]) -> str:
    return f"{column} IN ({', '.join(repr(value) for value in values)})"


class Base(DeclarativeBase):
    pass


class User(Base):
    __tablename__ = "users"

    id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True),
        nullable=False,
        primary_key=True,
        server_default=sql_text("gen_random_uuid()"),
    )
    handle: Mapped[str] = mapped_column(Text(), nullable=False)
    role: Mapped[UserRole] = mapped_column(Text(), nullable=False)
    display_name: Mapped[str] = mapped_column(Text(), nullable=False)

    __table_args__ = (
        CheckConstraint(_allowed_values("role", USER_ROLES), name="ck_users_role"),
        UniqueConstraint("handle", name="uq_users_handle"),
    )


class Team(Base):
    __tablename__ = "teams"

    id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True),
        nullable=False,
        primary_key=True,
        server_default=sql_text("gen_random_uuid()"),
    )
    name: Mapped[str] = mapped_column(Text(), nullable=False)

    __table_args__ = (UniqueConstraint("name", name="uq_teams_name"),)


class TeamMember(Base):
    __tablename__ = "team_members"

    team_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("teams.id", ondelete="CASCADE"),
        nullable=False,
        primary_key=True,
    )
    user_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        primary_key=True,
    )

    __table_args__ = (
        Index("ix_team_members_team_id", "team_id"),
        Index("ix_team_members_user_id", "user_id"),
    )


class Pack(Base):
    __tablename__ = "packs"

    id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True),
        nullable=False,
        primary_key=True,
        server_default=sql_text("gen_random_uuid()"),
    )
    company: Mapped[str] = mapped_column(Text(), nullable=False)
    reporting_year: Mapped[int] = mapped_column(Integer(), nullable=False)
    team_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("teams.id", ondelete="RESTRICT"), nullable=False
    )
    created_by: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("users.id", ondelete="RESTRICT"), nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=sql_text("now()")
    )

    __table_args__ = (
        CheckConstraint("reporting_year BETWEEN 2000 AND 2100", name="ck_packs_reporting_year"),
        Index("ix_packs_team_id", "team_id"),
        Index("ix_packs_created_by", "created_by"),
    )


class Document(Base):
    __tablename__ = "documents"

    id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True),
        nullable=False,
        primary_key=True,
        server_default=sql_text("gen_random_uuid()"),
    )
    pack_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("packs.id", ondelete="RESTRICT"), nullable=False
    )
    kind: Mapped[DocumentKind] = mapped_column(Text(), nullable=False)
    filename: Mapped[str] = mapped_column(Text(), nullable=False)
    sha256: Mapped[str] = mapped_column(CHAR(64), nullable=False)
    page_count: Mapped[int] = mapped_column(Integer(), nullable=False)
    parser_version: Mapped[str] = mapped_column(Text(), nullable=False)
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    __table_args__ = (
        CheckConstraint(_allowed_values("kind", DOCUMENT_KINDS), name="ck_documents_kind"),
        CheckConstraint("page_count >= 1", name="ck_documents_page_count"),
        Index("ix_documents_pack_id", "pack_id"),
        Index(
            "uq_documents_pack_id_sha256_active",
            "pack_id",
            "sha256",
            unique=True,
            postgresql_where=sql_text("deleted_at IS NULL"),
        ),
    )


class Unit(Base):
    __tablename__ = "units"

    id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True),
        nullable=False,
        primary_key=True,
        server_default=sql_text("gen_random_uuid()"),
    )
    document_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("documents.id", ondelete="RESTRICT"), nullable=False
    )
    parent_id: Mapped[UUID | None] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("units.id", ondelete="RESTRICT"), nullable=True
    )
    kind: Mapped[UnitKind] = mapped_column(Text(), nullable=False)
    pdf_page: Mapped[int] = mapped_column(Integer(), nullable=False)
    printed_page: Mapped[str | None] = mapped_column(Text(), nullable=True)
    section_path: Mapped[str] = mapped_column(Text(), nullable=False)
    text: Mapped[str] = mapped_column(Text(), nullable=False)
    context_header: Mapped[str | None] = mapped_column(Text(), nullable=True)
    token_count: Mapped[int] = mapped_column(
        Integer(), nullable=False, server_default=sql_text("0")
    )
    parser_version: Mapped[str] = mapped_column(Text(), nullable=False)
    embedding: Mapped[list[float] | None] = mapped_column(Vector(384), nullable=True)
    tsv: Mapped[str] = mapped_column(
        TSVECTOR(),
        Computed(
            "to_tsvector('english', coalesce(context_header, '') || ' ' || text)", persisted=True
        ),
        nullable=False,
    )

    __table_args__ = (
        CheckConstraint(_allowed_values("kind", UNIT_KINDS), name="ck_units_kind"),
        CheckConstraint("pdf_page >= 1", name="ck_units_pdf_page"),
        CheckConstraint("char_length(text) > 0", name="ck_units_text"),
        CheckConstraint("token_count >= 0", name="ck_units_token_count"),
        Index("ix_units_document_id", "document_id"),
        Index("ix_units_parent_id", "parent_id"),
        Index("ix_units_tsv", "tsv", postgresql_using="gin"),
        Index(
            "ix_units_embedding",
            "embedding",
            postgresql_using="hnsw",
            postgresql_ops={"embedding": "vector_cosine_ops"},
        ),
        Index("ix_units_document_id_pdf_page", "document_id", "pdf_page"),
    )


class CheckSet(Base):
    __tablename__ = "check_sets"

    version: Mapped[str] = mapped_column(Text(), nullable=False, primary_key=True)
    yaml_sha256: Mapped[str] = mapped_column(CHAR(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=sql_text("now()")
    )


class Run(Base):
    __tablename__ = "runs"

    id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True),
        nullable=False,
        primary_key=True,
        server_default=sql_text("gen_random_uuid()"),
    )
    pack_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("packs.id", ondelete="RESTRICT"), nullable=False
    )
    method: Mapped[RunMethod] = mapped_column(Text(), nullable=False)
    model_key: Mapped[str] = mapped_column(Text(), nullable=False)
    check_set_version: Mapped[str] = mapped_column(
        Text(), ForeignKey("check_sets.version", ondelete="RESTRICT"), nullable=False
    )
    prompt_sha256: Mapped[str] = mapped_column(CHAR(64), nullable=False)
    budget_usd: Mapped[Decimal] = mapped_column(Numeric(12, 6), nullable=False)
    spent_usd: Mapped[Decimal] = mapped_column(
        Numeric(12, 6), nullable=False, server_default=sql_text("0")
    )
    status: Mapped[RunStatus] = mapped_column(Text(), nullable=False)
    lease_owner: Mapped[str | None] = mapped_column(Text(), nullable=True)
    lease_expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    idempotency_key: Mapped[str] = mapped_column(Text(), nullable=False)
    created_by: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("users.id", ondelete="RESTRICT"), nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=sql_text("now()")
    )
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    error: Mapped[str | None] = mapped_column(Text(), nullable=True)

    __table_args__ = (
        CheckConstraint(_allowed_values("method", RUN_METHODS), name="ck_runs_method"),
        CheckConstraint("budget_usd > 0", name="ck_runs_budget_usd"),
        CheckConstraint("spent_usd >= 0", name="ck_runs_spent_usd"),
        CheckConstraint(
            _allowed_values("status", RUN_STATUSES),
            name="ck_runs_status",
        ),
        UniqueConstraint("idempotency_key", name="uq_runs_idempotency_key"),
        Index("ix_runs_pack_id", "pack_id"),
        Index("ix_runs_check_set_version", "check_set_version"),
        Index("ix_runs_created_by", "created_by"),
        Index("ix_runs_status_created_at", "status", "created_at"),
    )


class RunEvent(Base):
    __tablename__ = "run_events"

    id: Mapped[int] = mapped_column(BigInteger(), nullable=False, primary_key=True)
    run_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("runs.id", ondelete="CASCADE"), nullable=False
    )
    check_id: Mapped[str | None] = mapped_column(Text(), nullable=True)
    type: Mapped[str] = mapped_column(Text(), nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(
        JSONB(), nullable=False, server_default=sql_text("'{}'::jsonb")
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=sql_text("now()")
    )

    __table_args__ = (
        CheckConstraint("char_length(type) > 0", name="ck_run_events_type"),
        Index("ix_run_events_run_id", "run_id"),
        Index("ix_run_events_run_id_id", "run_id", "id"),
    )


class Finding(Base):
    __tablename__ = "findings"

    id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True),
        nullable=False,
        primary_key=True,
        server_default=sql_text("gen_random_uuid()"),
    )
    run_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("runs.id", ondelete="RESTRICT"), nullable=False
    )
    check_id: Mapped[str] = mapped_column(Text(), nullable=False)
    status: Mapped[FindingStatus] = mapped_column(Text(), nullable=False)
    confidence: Mapped[Confidence | None] = mapped_column(Text(), nullable=True)
    rationale: Mapped[str] = mapped_column(Text(), nullable=False)
    missing_aspects: Mapped[list[str]] = mapped_column(
        JSONB(), nullable=False, server_default=sql_text("'[]'::jsonb")
    )
    relief_applied: Mapped[bool] = mapped_column(
        Boolean(), nullable=False, server_default=sql_text("false")
    )
    needs_review_reason: Mapped[str | None] = mapped_column(Text(), nullable=True)
    steps: Mapped[int] = mapped_column(Integer(), nullable=False)
    tokens_in: Mapped[int] = mapped_column(Integer(), nullable=False, server_default=sql_text("0"))
    tokens_out: Mapped[int] = mapped_column(Integer(), nullable=False, server_default=sql_text("0"))
    cost_usd: Mapped[Decimal] = mapped_column(Numeric(12, 6), nullable=False)
    version: Mapped[int] = mapped_column(Integer(), nullable=False, server_default=sql_text("1"))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=sql_text("now()")
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=sql_text("now()")
    )

    __table_args__ = (
        CheckConstraint(
            _allowed_values("status", FINDING_STATUSES),
            name="ck_findings_status",
        ),
        CheckConstraint(_allowed_values("confidence", CONFIDENCES), name="ck_findings_confidence"),
        CheckConstraint("steps >= 0", name="ck_findings_steps"),
        UniqueConstraint("run_id", "check_id", name="uq_findings_run_id_check_id"),
        Index("ix_findings_run_id", "run_id"),
    )

    @declared_attr.directive
    def __mapper_args__(cls) -> dict[str, Any]:
        return {"version_id_col": cls.version}


class Evidence(Base):
    __tablename__ = "evidence"

    id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True),
        nullable=False,
        primary_key=True,
        server_default=sql_text("gen_random_uuid()"),
    )
    finding_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("findings.id", ondelete="CASCADE"), nullable=False
    )
    unit_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("units.id", ondelete="RESTRICT"), nullable=False
    )
    quote: Mapped[str] = mapped_column(Text(), nullable=False)
    role: Mapped[EvidenceRole] = mapped_column(Text(), nullable=False)
    verified: Mapped[bool] = mapped_column(
        Boolean(), nullable=False, server_default=sql_text("false")
    )

    __table_args__ = (
        CheckConstraint("char_length(quote) > 0", name="ck_evidence_quote"),
        CheckConstraint(_allowed_values("role", EVIDENCE_ROLES), name="ck_evidence_role"),
        Index("ix_evidence_finding_id", "finding_id"),
        Index("ix_evidence_unit_id", "unit_id"),
    )


class Fact(Base):
    __tablename__ = "facts"

    id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True),
        nullable=False,
        primary_key=True,
        server_default=sql_text("gen_random_uuid()"),
    )
    finding_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("findings.id", ondelete="CASCADE"), nullable=False
    )
    unit_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("units.id", ondelete="RESTRICT"), nullable=False
    )
    name: Mapped[str] = mapped_column(Text(), nullable=False)
    value_num: Mapped[Decimal | None] = mapped_column(Numeric(), nullable=True)
    unit: Mapped[str] = mapped_column(Text(), nullable=False)
    scope: Mapped[str] = mapped_column(Text(), nullable=False)
    raw: Mapped[str] = mapped_column(Text(), nullable=False)
    year: Mapped[int | None] = mapped_column(Integer(), nullable=True)

    __table_args__ = (
        Index("ix_facts_finding_id", "finding_id"),
        Index("ix_facts_unit_id", "unit_id"),
    )


class Inconsistency(Base):
    __tablename__ = "inconsistencies"

    id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True),
        nullable=False,
        primary_key=True,
        server_default=sql_text("gen_random_uuid()"),
    )
    run_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("runs.id", ondelete="RESTRICT"), nullable=False
    )
    type: Mapped[InconsistencyType] = mapped_column(Text(), nullable=False)
    finding_id: Mapped[UUID | None] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("findings.id", ondelete="RESTRICT"), nullable=True
    )
    detail: Mapped[dict[str, Any]] = mapped_column(JSONB(), nullable=False)

    __table_args__ = (
        CheckConstraint(
            _allowed_values("type", INCONSISTENCY_TYPES), name="ck_inconsistencies_type"
        ),
        Index("ix_inconsistencies_run_id", "run_id"),
        Index("ix_inconsistencies_finding_id", "finding_id"),
    )


class Review(Base):
    __tablename__ = "reviews"

    id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True),
        nullable=False,
        primary_key=True,
        server_default=sql_text("gen_random_uuid()"),
    )
    finding_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("findings.id", ondelete="RESTRICT"), nullable=False
    )
    reviewer_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("users.id", ondelete="RESTRICT"), nullable=False
    )
    decision: Mapped[ReviewDecision] = mapped_column(Text(), nullable=False)
    new_status: Mapped[FindingStatus | None] = mapped_column(Text(), nullable=True)
    note: Mapped[str] = mapped_column(Text(), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=sql_text("now()")
    )

    __table_args__ = (
        CheckConstraint(_allowed_values("decision", REVIEW_DECISIONS), name="ck_reviews_decision"),
        CheckConstraint(
            _allowed_values("new_status", FINDING_STATUSES),
            name="ck_reviews_new_status",
        ),
        CheckConstraint(
            "(decision = 'confirm') = (new_status IS NULL)", name="ck_reviews_decision_new_status"
        ),
        CheckConstraint(
            "new_status IS NULL OR char_length(btrim(note)) >= 10", name="ck_reviews_note"
        ),
        Index("ix_reviews_finding_id", "finding_id"),
        Index("ix_reviews_reviewer_id", "reviewer_id"),
    )


class LLMCall(Base):
    __tablename__ = "llm_calls"

    id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True),
        nullable=False,
        primary_key=True,
        server_default=sql_text("gen_random_uuid()"),
    )
    run_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("runs.id", ondelete="CASCADE"), nullable=False
    )
    check_id: Mapped[str | None] = mapped_column(Text(), nullable=True)
    step: Mapped[int] = mapped_column(Integer(), nullable=False)
    model: Mapped[str] = mapped_column(Text(), nullable=False)
    prompt_sha256: Mapped[str] = mapped_column(CHAR(64), nullable=False)
    tools_sha256: Mapped[str] = mapped_column(CHAR(64), nullable=False)
    temperature: Mapped[float] = mapped_column(REAL(), nullable=False)
    seed: Mapped[int | None] = mapped_column(Integer(), nullable=True)
    system_fingerprint: Mapped[str | None] = mapped_column(Text(), nullable=True)
    tokens_in: Mapped[int] = mapped_column(Integer(), nullable=False, server_default=sql_text("0"))
    tokens_out: Mapped[int] = mapped_column(Integer(), nullable=False, server_default=sql_text("0"))
    tokens_cached: Mapped[int] = mapped_column(
        Integer(), nullable=False, server_default=sql_text("0")
    )
    cost_usd: Mapped[Decimal] = mapped_column(Numeric(12, 6), nullable=False)
    latency_ms: Mapped[int | None] = mapped_column(Integer(), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=sql_text("now()")
    )

    __table_args__ = (Index("ix_llm_calls_run_id", "run_id"),)


class AuditLog(Base):
    __tablename__ = "audit_log"

    id: Mapped[int] = mapped_column(BigInteger(), nullable=False, primary_key=True)
    actor: Mapped[str] = mapped_column(Text(), nullable=False)
    action: Mapped[str] = mapped_column(Text(), nullable=False)
    target: Mapped[str] = mapped_column(Text(), nullable=False)
    detail: Mapped[dict[str, Any]] = mapped_column(
        JSONB(), nullable=False, server_default=sql_text("'{}'::jsonb")
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=sql_text("now()")
    )
