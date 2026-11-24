"""Store what the model returned on each call, for replay (D89); older rows stay NULL."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision = "0012_llm_call_response"
down_revision = "0011_proposed_status"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("llm_calls", sa.Column("response", JSONB(), nullable=True))


def downgrade() -> None:
    op.drop_column("llm_calls", "response")
