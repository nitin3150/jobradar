"""Create resumes table for multi-resume uploads with per-resume tagging."""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import ARRAY, UUID

revision = "005"
down_revision = "004"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "resumes",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column("name", sa.String(512), nullable=False),
        sa.Column("storage_path", sa.String(512), nullable=False, unique=True),
        sa.Column("content_type", sa.String(128), nullable=False),
        sa.Column("size_bytes", sa.Integer, nullable=False),
        sa.Column("tags", ARRAY(sa.String(64)), nullable=False, server_default="{}"),
        sa.Column("is_default", sa.Boolean, nullable=False, server_default=sa.false()),
        sa.Column(
            "uploaded_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
    )
    op.create_index("ix_resumes_is_default", "resumes", ["is_default"])
    op.create_index("ix_resumes_uploaded_at", "resumes", ["uploaded_at"])


def downgrade() -> None:
    op.drop_index("ix_resumes_uploaded_at", table_name="resumes")
    op.drop_index("ix_resumes_is_default", table_name="resumes")
    op.drop_table("resumes")
