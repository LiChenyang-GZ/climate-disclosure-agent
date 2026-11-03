"""Record a run's selected checks and the documents it skipped (D83)."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import ARRAY, JSONB

revision = "0007_run_scope"
down_revision = "0006_llm_calls_temperature"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("runs", sa.Column("check_ids", ARRAY(sa.Text()), nullable=True))
    op.add_column(
        "runs",
        sa.Column(
            "skipped_documents", JSONB(), nullable=False, server_default=sa.text("'[]'::jsonb")
        ),
    )


def downgrade() -> None:
    op.drop_column("runs", "skipped_documents")
    op.drop_column("runs", "check_ids")
