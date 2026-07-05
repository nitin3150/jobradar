"""Create singleton `settings` table for user preferences (DDL only — GET seeds defaults)."""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import ARRAY

revision = "006"
down_revision = "005"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "settings",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "target_roles", ARRAY(sa.String(64)), nullable=False, server_default="{}"
        ),
        sa.Column(
            "review_window_hours",
            sa.Float(),
            nullable=False,
            server_default="2.0",
        ),
        sa.Column(
            "job_fit_threshold",
            sa.Float(),
            nullable=False,
            server_default="0.6",
        ),
        sa.Column(
            "send_followup_emails",
            sa.Boolean(),
            nullable=False,
            server_default=sa.true(),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
    )


def downgrade() -> None:
    op.drop_table("settings")
