"""Add ats_type and ats_slug to companies table."""
from alembic import op
import sqlalchemy as sa

revision = "003"
down_revision = "002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("companies", sa.Column("ats_type", sa.String(32), nullable=True))
    op.add_column("companies", sa.Column("ats_slug", sa.String(256), nullable=True))


def downgrade() -> None:
    op.drop_column("companies", "ats_slug")
    op.drop_column("companies", "ats_type")
