"""Record the settings each run executes with (D87); older runs stay NULL."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision = "0010_run_settings"
down_revision = "0009_salesforce"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("runs", sa.Column("settings", JSONB(), nullable=True))


def downgrade() -> None:
    op.drop_column("runs", "settings")
