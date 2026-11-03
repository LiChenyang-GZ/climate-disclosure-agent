"""Record where an imported document came from (X2, D84)."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision = "0008_document_source"
down_revision = "0007_run_scope"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("documents", sa.Column("source", JSONB(), nullable=True))


def downgrade() -> None:
    op.drop_column("documents", "source")
