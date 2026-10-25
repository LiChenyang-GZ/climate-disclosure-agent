"""Immutable document reading order."""

import sqlalchemy as sa
from alembic import op

revision = "0004_unit_seq"
down_revision = "0003_document_parts"
branch_labels = None
depends_on = None

_PREVIOUS_FUNCTION = """
        CREATE OR REPLACE FUNCTION reject_unit_mutation() RETURNS trigger
        LANGUAGE plpgsql AS $$
        BEGIN
            IF OLD.document_id IS DISTINCT FROM NEW.document_id OR
            OLD.parent_id IS DISTINCT FROM NEW.parent_id OR
            OLD.kind IS DISTINCT FROM NEW.kind OR
            OLD.pdf_page IS DISTINCT FROM NEW.pdf_page OR
            OLD.page_half IS DISTINCT FROM NEW.page_half OR
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
    op.execute("""DO $$ BEGIN
        IF EXISTS (SELECT 1 FROM units) THEN
            RAISE EXCEPTION 're-parse required: units has rows without seq';
        END IF;
    END $$""")
    op.add_column("units", sa.Column("seq", sa.Integer(), nullable=False))
    op.create_check_constraint("ck_units_seq", "units", "seq >= 0")
    op.create_unique_constraint("uq_units_document_id_seq", "units", ["document_id", "seq"])
    op.execute(
        _PREVIOUS_FUNCTION.replace(
            "            OLD.pdf_page",
            "            OLD.seq IS DISTINCT FROM NEW.seq OR\n            OLD.pdf_page",
        )
    )


def downgrade() -> None:
    op.execute(_PREVIOUS_FUNCTION)
    op.drop_constraint("uq_units_document_id_seq", "units", type_="unique")
    op.drop_constraint("ck_units_seq", "units", type_="check")
    op.drop_column("units", "seq")
