"""Document parts and immutable spread locations."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import UUID

revision = "0003_document_parts"
down_revision = "0002_schema"
branch_labels = None
depends_on = None

# Keep the 0002 body verbatim so downgrade restores its exact semantics.
_BASE_FUNCTION = """
        CREATE OR REPLACE FUNCTION reject_unit_mutation() RETURNS trigger
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
    """


def upgrade() -> None:
    op.create_table(
        "document_parts",
        sa.Column(
            "id",
            UUID(as_uuid=True),
            primary_key=True,
            nullable=False,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column(
            "document_id",
            UUID(as_uuid=True),
            sa.ForeignKey("documents.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("kind", sa.Text(), nullable=False),
        sa.Column("first_pdf_page", sa.Integer(), nullable=False),
        sa.Column("last_pdf_page", sa.Integer(), nullable=False),
        sa.Column("label", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.CheckConstraint(
            "kind IN ('sustainability', 'annual', 'other')", name="ck_document_parts_kind"
        ),
        sa.CheckConstraint("first_pdf_page >= 1", name="ck_document_parts_first_pdf_page"),
        sa.CheckConstraint("last_pdf_page >= first_pdf_page", name="ck_document_parts_range"),
    )
    op.create_index("ix_document_parts_document_id", "document_parts", ["document_id"])
    op.add_column("units", sa.Column("page_half", sa.Text(), nullable=True))
    op.create_check_constraint("ck_units_page_half", "units", "page_half IN ('left', 'right')")
    op.execute(
        _BASE_FUNCTION.replace(
            "            OLD.printed_page",
            "            OLD.page_half IS DISTINCT FROM NEW.page_half OR\n            OLD.printed_page",
        )
    )


def downgrade() -> None:
    op.execute(_BASE_FUNCTION)
    op.drop_constraint("ck_units_page_half", "units", type_="check")
    op.drop_column("units", "page_half")
    op.drop_table("document_parts")
