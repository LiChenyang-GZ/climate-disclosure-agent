"""Salesforce: a pack's linked Account and the Cases created per finding (D85)."""

from alembic import op
from sqlalchemy import CheckConstraint, Column, DateTime, ForeignKey, Text, UniqueConstraint
from sqlalchemy import text as sql_text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PGUUID

revision = "0009_salesforce"
down_revision = "0008_document_source"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("packs", Column("salesforce_account", JSONB(), nullable=True))
    op.create_table(
        "salesforce_cases",
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
        Column("case_id", Text(), nullable=False),
        Column("case_number", Text(), nullable=True),
        Column(
            "created_at",
            DateTime(timezone=True),
            nullable=False,
            server_default=sql_text("now()"),
        ),
        CheckConstraint("char_length(case_id) > 0", name="ck_salesforce_cases_case_id"),
        UniqueConstraint("finding_id", name="uq_salesforce_cases_finding_id"),
    )


def downgrade() -> None:
    op.drop_table("salesforce_cases")
    op.drop_column("packs", "salesforce_account")
