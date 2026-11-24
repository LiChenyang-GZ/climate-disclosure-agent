"""Keep the model's status when code overrides it to needs_review (D88)."""

import sqlalchemy as sa
from alembic import op

revision = "0011_proposed_status"
down_revision = "0010_run_settings"
branch_labels = None
depends_on = None

PROPOSED = ("evidence_found", "partial_evidence", "no_evidence_found", "inconsistent")


def upgrade() -> None:
    op.add_column("findings", sa.Column("proposed_status", sa.Text(), nullable=True))
    op.create_check_constraint(
        "ck_findings_proposed_status",
        "findings",
        "proposed_status IN (" + ", ".join(repr(v) for v in PROPOSED) + ")",
    )


def downgrade() -> None:
    op.drop_constraint("ck_findings_proposed_status", "findings", type_="check")
    op.drop_column("findings", "proposed_status")
