"""Create the disclosure schema and enforce immutable parsed units.

Revision ID: 0002_schema
Revises: 0001_enable_pgvector
"""

from alembic import op
from pgvector.sqlalchemy import Vector
from sqlalchemy import (
    CHAR,
    REAL,
    BigInteger,
    Boolean,
    CheckConstraint,
    Column,
    Computed,
    DateTime,
    ForeignKey,
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

revision = "0002_schema"
down_revision = "0001_enable_pgvector"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "users",
        Column(
            "id",
            PGUUID(as_uuid=True),
            nullable=False,
            primary_key=True,
            server_default=sql_text("gen_random_uuid()"),
        ),
        Column("handle", Text(), nullable=False),
        Column("role", Text(), nullable=False),
        Column("display_name", Text(), nullable=False),
        CheckConstraint("role IN ('lead', 'analyst')", name="ck_users_role"),
        UniqueConstraint("handle", name="uq_users_handle"),
    )
    op.create_table(
        "teams",
        Column(
            "id",
            PGUUID(as_uuid=True),
            nullable=False,
            primary_key=True,
            server_default=sql_text("gen_random_uuid()"),
        ),
        Column("name", Text(), nullable=False),
        UniqueConstraint("name", name="uq_teams_name"),
    )
    op.create_table(
        "team_members",
        Column(
            "team_id",
            PGUUID(as_uuid=True),
            ForeignKey("teams.id", ondelete="CASCADE"),
            nullable=False,
            primary_key=True,
        ),
        Column(
            "user_id",
            PGUUID(as_uuid=True),
            ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
            primary_key=True,
        ),
    )
    op.create_index("ix_team_members_team_id", "team_members", ["team_id"])
    op.create_index("ix_team_members_user_id", "team_members", ["user_id"])
    op.create_table(
        "packs",
        Column(
            "id",
            PGUUID(as_uuid=True),
            nullable=False,
            primary_key=True,
            server_default=sql_text("gen_random_uuid()"),
        ),
        Column("company", Text(), nullable=False),
        Column("reporting_year", Integer(), nullable=False),
        Column(
            "team_id",
            PGUUID(as_uuid=True),
            ForeignKey("teams.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        Column(
            "created_by",
            PGUUID(as_uuid=True),
            ForeignKey("users.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        Column(
            "created_at", DateTime(timezone=True), nullable=False, server_default=sql_text("now()")
        ),
        CheckConstraint("reporting_year BETWEEN 2000 AND 2100", name="ck_packs_reporting_year"),
    )
    op.create_index("ix_packs_team_id", "packs", ["team_id"])
    op.create_index("ix_packs_created_by", "packs", ["created_by"])
    op.create_table(
        "documents",
        Column(
            "id",
            PGUUID(as_uuid=True),
            nullable=False,
            primary_key=True,
            server_default=sql_text("gen_random_uuid()"),
        ),
        Column(
            "pack_id",
            PGUUID(as_uuid=True),
            ForeignKey("packs.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        Column("kind", Text(), nullable=False),
        Column("filename", Text(), nullable=False),
        Column("sha256", CHAR(64), nullable=False),
        Column("page_count", Integer(), nullable=False),
        Column("parser_version", Text(), nullable=False),
        Column("deleted_at", DateTime(timezone=True), nullable=True),
        CheckConstraint("kind IN ('sustainability', 'annual', 'other')", name="ck_documents_kind"),
        CheckConstraint("page_count >= 1", name="ck_documents_page_count"),
    )
    op.create_index("ix_documents_pack_id", "documents", ["pack_id"])
    op.create_index(
        "uq_documents_pack_id_sha256_active",
        "documents",
        ["pack_id", "sha256"],
        unique=True,
        postgresql_where=sql_text("deleted_at IS NULL"),
    )
    op.create_table(
        "units",
        Column(
            "id",
            PGUUID(as_uuid=True),
            nullable=False,
            primary_key=True,
            server_default=sql_text("gen_random_uuid()"),
        ),
        Column(
            "document_id",
            PGUUID(as_uuid=True),
            ForeignKey("documents.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        Column(
            "parent_id",
            PGUUID(as_uuid=True),
            ForeignKey("units.id", ondelete="RESTRICT"),
            nullable=True,
        ),
        Column("kind", Text(), nullable=False),
        Column("pdf_page", Integer(), nullable=False),
        Column("printed_page", Text(), nullable=True),
        Column("section_path", Text(), nullable=False),
        Column("text", Text(), nullable=False),
        Column("context_header", Text(), nullable=True),
        Column("token_count", Integer(), nullable=False, server_default=sql_text("0")),
        Column("parser_version", Text(), nullable=False),
        Column("embedding", Vector(384), nullable=True),
        Column(
            "tsv",
            TSVECTOR(),
            Computed(
                "to_tsvector('english', coalesce(context_header, '') || ' ' || text)",
                persisted=True,
            ),
            nullable=False,
        ),
        CheckConstraint(
            "kind IN ('paragraph', 'table_row', 'section', 'table')", name="ck_units_kind"
        ),
        CheckConstraint("pdf_page >= 1", name="ck_units_pdf_page"),
        CheckConstraint("char_length(text) > 0", name="ck_units_text"),
        CheckConstraint("token_count >= 0", name="ck_units_token_count"),
    )
    op.create_index("ix_units_document_id", "units", ["document_id"])
    op.create_index("ix_units_parent_id", "units", ["parent_id"])
    op.create_index("ix_units_tsv", "units", ["tsv"], postgresql_using="gin")
    op.create_index(
        "ix_units_embedding",
        "units",
        ["embedding"],
        postgresql_using="hnsw",
        postgresql_ops={"embedding": "vector_cosine_ops"},
    )
    op.create_index("ix_units_document_id_pdf_page", "units", ["document_id", "pdf_page"])
    op.create_table(
        "check_sets",
        Column("version", Text(), nullable=False, primary_key=True),
        Column("yaml_sha256", CHAR(64), nullable=False),
        Column(
            "created_at", DateTime(timezone=True), nullable=False, server_default=sql_text("now()")
        ),
    )
    op.create_table(
        "runs",
        Column(
            "id",
            PGUUID(as_uuid=True),
            nullable=False,
            primary_key=True,
            server_default=sql_text("gen_random_uuid()"),
        ),
        Column(
            "pack_id",
            PGUUID(as_uuid=True),
            ForeignKey("packs.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        Column("method", Text(), nullable=False),
        Column("model_key", Text(), nullable=False),
        Column(
            "check_set_version",
            Text(),
            ForeignKey("check_sets.version", ondelete="RESTRICT"),
            nullable=False,
        ),
        Column("prompt_sha256", CHAR(64), nullable=False),
        Column("budget_usd", Numeric(12, 6), nullable=False),
        Column("spent_usd", Numeric(12, 6), nullable=False, server_default=sql_text("0")),
        Column("status", Text(), nullable=False),
        Column("lease_owner", Text(), nullable=True),
        Column("lease_expires_at", DateTime(timezone=True), nullable=True),
        Column("idempotency_key", Text(), nullable=False),
        Column(
            "created_by",
            PGUUID(as_uuid=True),
            ForeignKey("users.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        Column(
            "created_at", DateTime(timezone=True), nullable=False, server_default=sql_text("now()")
        ),
        Column("started_at", DateTime(timezone=True), nullable=True),
        Column("finished_at", DateTime(timezone=True), nullable=True),
        Column("error", Text(), nullable=True),
        CheckConstraint("method IN ('A', 'B', 'C', 'LC')", name="ck_runs_method"),
        CheckConstraint("budget_usd > 0", name="ck_runs_budget_usd"),
        CheckConstraint("spent_usd >= 0", name="ck_runs_spent_usd"),
        CheckConstraint(
            "status IN ('queued', 'running', 'waiting_review', 'final', 'cancelled', 'failed')",
            name="ck_runs_status",
        ),
        UniqueConstraint("idempotency_key", name="uq_runs_idempotency_key"),
    )
    op.create_index("ix_runs_pack_id", "runs", ["pack_id"])
    op.create_index("ix_runs_check_set_version", "runs", ["check_set_version"])
    op.create_index("ix_runs_created_by", "runs", ["created_by"])
    op.create_index("ix_runs_status_created_at", "runs", ["status", "created_at"])
    op.create_table(
        "run_events",
        Column("id", BigInteger(), nullable=False, primary_key=True),
        Column(
            "run_id",
            PGUUID(as_uuid=True),
            ForeignKey("runs.id", ondelete="CASCADE"),
            nullable=False,
        ),
        Column("check_id", Text(), nullable=True),
        Column("type", Text(), nullable=False),
        Column("payload", JSONB(), nullable=False, server_default=sql_text("'{}'::jsonb")),
        Column(
            "created_at", DateTime(timezone=True), nullable=False, server_default=sql_text("now()")
        ),
        CheckConstraint("char_length(type) > 0", name="ck_run_events_type"),
    )
    op.create_index("ix_run_events_run_id", "run_events", ["run_id"])
    op.create_index("ix_run_events_run_id_id", "run_events", ["run_id", "id"])
    op.create_table(
        "findings",
        Column(
            "id",
            PGUUID(as_uuid=True),
            nullable=False,
            primary_key=True,
            server_default=sql_text("gen_random_uuid()"),
        ),
        Column(
            "run_id",
            PGUUID(as_uuid=True),
            ForeignKey("runs.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        Column("check_id", Text(), nullable=False),
        Column("status", Text(), nullable=False),
        Column("confidence", Text(), nullable=True),
        Column("rationale", Text(), nullable=False),
        Column("missing_aspects", JSONB(), nullable=False, server_default=sql_text("'[]'::jsonb")),
        Column("relief_applied", Boolean(), nullable=False, server_default=sql_text("false")),
        Column("needs_review_reason", Text(), nullable=True),
        Column("steps", Integer(), nullable=False),
        Column("tokens_in", Integer(), nullable=False, server_default=sql_text("0")),
        Column("tokens_out", Integer(), nullable=False, server_default=sql_text("0")),
        Column("cost_usd", Numeric(12, 6), nullable=False),
        Column("version", Integer(), nullable=False, server_default=sql_text("1")),
        Column(
            "created_at", DateTime(timezone=True), nullable=False, server_default=sql_text("now()")
        ),
        Column(
            "updated_at", DateTime(timezone=True), nullable=False, server_default=sql_text("now()")
        ),
        CheckConstraint(
            "status IN ('evidence_found', 'partial_evidence', 'no_evidence_found', 'inconsistent', 'needs_review', 'not_checked', 'error')",
            name="ck_findings_status",
        ),
        CheckConstraint("confidence IN ('high', 'low')", name="ck_findings_confidence"),
        CheckConstraint("steps >= 0", name="ck_findings_steps"),
        UniqueConstraint("run_id", "check_id", name="uq_findings_run_id_check_id"),
    )
    op.create_index("ix_findings_run_id", "findings", ["run_id"])
    op.create_table(
        "evidence",
        Column(
            "id",
            PGUUID(as_uuid=True),
            nullable=False,
            primary_key=True,
            server_default=sql_text("gen_random_uuid()"),
        ),
        Column(
            "finding_id",
            PGUUID(as_uuid=True),
            ForeignKey("findings.id", ondelete="CASCADE"),
            nullable=False,
        ),
        Column(
            "unit_id",
            PGUUID(as_uuid=True),
            ForeignKey("units.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        Column("quote", Text(), nullable=False),
        Column("role", Text(), nullable=False),
        Column("verified", Boolean(), nullable=False, server_default=sql_text("false")),
        CheckConstraint("char_length(quote) > 0", name="ck_evidence_quote"),
        CheckConstraint("role IN ('supports', 'side_a', 'side_b')", name="ck_evidence_role"),
    )
    op.create_index("ix_evidence_finding_id", "evidence", ["finding_id"])
    op.create_index("ix_evidence_unit_id", "evidence", ["unit_id"])
    op.create_table(
        "facts",
        Column(
            "id",
            PGUUID(as_uuid=True),
            nullable=False,
            primary_key=True,
            server_default=sql_text("gen_random_uuid()"),
        ),
        Column(
            "finding_id",
            PGUUID(as_uuid=True),
            ForeignKey("findings.id", ondelete="CASCADE"),
            nullable=False,
        ),
        Column(
            "unit_id",
            PGUUID(as_uuid=True),
            ForeignKey("units.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        Column("name", Text(), nullable=False),
        Column("value_num", Numeric(), nullable=True),
        Column("unit", Text(), nullable=False),
        Column("scope", Text(), nullable=False),
        Column("raw", Text(), nullable=False),
        Column("year", Integer(), nullable=True),
    )
    op.create_index("ix_facts_finding_id", "facts", ["finding_id"])
    op.create_index("ix_facts_unit_id", "facts", ["unit_id"])
    op.create_table(
        "inconsistencies",
        Column(
            "id",
            PGUUID(as_uuid=True),
            nullable=False,
            primary_key=True,
            server_default=sql_text("gen_random_uuid()"),
        ),
        Column(
            "run_id",
            PGUUID(as_uuid=True),
            ForeignKey("runs.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        Column("type", Text(), nullable=False),
        Column(
            "finding_id",
            PGUUID(as_uuid=True),
            ForeignKey("findings.id", ondelete="RESTRICT"),
            nullable=True,
        ),
        Column("detail", JSONB(), nullable=False),
        CheckConstraint(
            "type IN ('T1', 'T2', 'T3', 'T4', 'T5', 'T6')", name="ck_inconsistencies_type"
        ),
    )
    op.create_index("ix_inconsistencies_run_id", "inconsistencies", ["run_id"])
    op.create_index("ix_inconsistencies_finding_id", "inconsistencies", ["finding_id"])
    op.create_table(
        "reviews",
        Column(
            "id",
            PGUUID(as_uuid=True),
            nullable=False,
            primary_key=True,
            server_default=sql_text("gen_random_uuid()"),
        ),
        Column(
            "finding_id",
            PGUUID(as_uuid=True),
            ForeignKey("findings.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        Column(
            "reviewer_id",
            PGUUID(as_uuid=True),
            ForeignKey("users.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        Column("decision", Text(), nullable=False),
        Column("new_status", Text(), nullable=True),
        Column("note", Text(), nullable=False),
        Column(
            "created_at", DateTime(timezone=True), nullable=False, server_default=sql_text("now()")
        ),
        CheckConstraint("decision IN ('confirm', 'change_status')", name="ck_reviews_decision"),
        CheckConstraint(
            "new_status IN ('evidence_found', 'partial_evidence', 'no_evidence_found', 'inconsistent', 'needs_review', 'not_checked', 'error')",
            name="ck_reviews_new_status",
        ),
        CheckConstraint(
            "(decision = 'confirm') = (new_status IS NULL)", name="ck_reviews_decision_new_status"
        ),
        CheckConstraint(
            "new_status IS NULL OR char_length(btrim(note)) >= 10", name="ck_reviews_note"
        ),
    )
    op.create_index("ix_reviews_finding_id", "reviews", ["finding_id"])
    op.create_index("ix_reviews_reviewer_id", "reviews", ["reviewer_id"])
    op.create_table(
        "llm_calls",
        Column(
            "id",
            PGUUID(as_uuid=True),
            nullable=False,
            primary_key=True,
            server_default=sql_text("gen_random_uuid()"),
        ),
        Column(
            "run_id",
            PGUUID(as_uuid=True),
            ForeignKey("runs.id", ondelete="CASCADE"),
            nullable=False,
        ),
        Column("check_id", Text(), nullable=True),
        Column("step", Integer(), nullable=False),
        Column("model", Text(), nullable=False),
        Column("prompt_sha256", CHAR(64), nullable=False),
        Column("tools_sha256", CHAR(64), nullable=False),
        Column("temperature", REAL(), nullable=False),
        Column("seed", Integer(), nullable=True),
        Column("system_fingerprint", Text(), nullable=True),
        Column("tokens_in", Integer(), nullable=False, server_default=sql_text("0")),
        Column("tokens_out", Integer(), nullable=False, server_default=sql_text("0")),
        Column("tokens_cached", Integer(), nullable=False, server_default=sql_text("0")),
        Column("cost_usd", Numeric(12, 6), nullable=False),
        Column("latency_ms", Integer(), nullable=True),
        Column(
            "created_at", DateTime(timezone=True), nullable=False, server_default=sql_text("now()")
        ),
    )
    op.create_index("ix_llm_calls_run_id", "llm_calls", ["run_id"])
    op.create_table(
        "audit_log",
        Column("id", BigInteger(), nullable=False, primary_key=True),
        Column("actor", Text(), nullable=False),
        Column("action", Text(), nullable=False),
        Column("target", Text(), nullable=False),
        Column("detail", JSONB(), nullable=False, server_default=sql_text("'{}'::jsonb")),
        Column(
            "created_at", DateTime(timezone=True), nullable=False, server_default=sql_text("now()")
        ),
    )
    op.execute("""
        CREATE FUNCTION reject_unit_mutation() RETURNS trigger
        LANGUAGE plpgsql AS $$
        BEGIN
            IF OLD.document_id IS DISTINCT FROM NEW.document_id OR
            OLD.parent_id IS DISTINCT FROM NEW.parent_id OR
            OLD.kind IS DISTINCT FROM NEW.kind OR
            OLD.pdf_page IS DISTINCT FROM NEW.pdf_page OR
            OLD.printed_page IS DISTINCT FROM NEW.printed_page OR
            OLD.section_path IS DISTINCT FROM NEW.section_path OR
            OLD.text IS DISTINCT FROM NEW.text OR
            OLD.token_count IS DISTINCT FROM NEW.token_count OR
            OLD.parser_version IS DISTINCT FROM NEW.parser_version THEN
                RAISE EXCEPTION 'units are immutable';
            END IF;
            RETURN NEW;
        END;
        $$
    """)
    op.execute("""
        CREATE TRIGGER units_immutable BEFORE UPDATE ON units
        FOR EACH ROW EXECUTE FUNCTION reject_unit_mutation()
    """)


def downgrade() -> None:
    op.execute("DROP TRIGGER units_immutable ON units")
    op.execute("DROP FUNCTION reject_unit_mutation()")
    op.drop_table("audit_log")
    op.drop_table("llm_calls")
    op.drop_table("reviews")
    op.drop_table("inconsistencies")
    op.drop_table("facts")
    op.drop_table("evidence")
    op.drop_table("findings")
    op.drop_table("run_events")
    op.drop_table("runs")
    op.drop_table("check_sets")
    op.drop_table("units")
    op.drop_table("documents")
    op.drop_table("packs")
    op.drop_table("team_members")
    op.drop_table("teams")
    op.drop_table("users")
