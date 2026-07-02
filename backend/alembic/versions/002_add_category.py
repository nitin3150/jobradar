"""Add category column to companies table

Revision ID: 002
Revises: 001
Create Date: 2026-03-27
"""

from alembic import op
import sqlalchemy as sa

revision = "002"
down_revision = "001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "companies",
        sa.Column("category", sa.String(32), server_default="startup", nullable=False),
    )
    op.create_index("ix_companies_category", "companies", ["category"])


def downgrade() -> None:
    op.drop_index("ix_companies_category", table_name="companies")
    op.drop_column("companies", "category")
