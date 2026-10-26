"""Allow omitted temperature for Responses API calls."""

import sqlalchemy as sa
from alembic import op

revision = "0006_llm_calls_temperature"
down_revision = "0005_rls"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.alter_column("llm_calls", "temperature", existing_type=sa.REAL(), nullable=True)


def downgrade() -> None:
    op.execute("UPDATE llm_calls SET temperature = 0 WHERE temperature IS NULL")
    op.alter_column("llm_calls", "temperature", existing_type=sa.REAL(), nullable=False)
