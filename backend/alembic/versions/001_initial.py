"""Initial migration - companies and outreach_messages tables

Revision ID: 001
Revises:
Create Date: 2026-03-27
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB, UUID

revision = "001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "companies",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column("name", sa.String(512), nullable=False),
        sa.Column("name_slug", sa.String(512), nullable=False, unique=True),
        sa.Column("website", sa.String(1024)),
        sa.Column("funding_amount", sa.Float),
        sa.Column("funding_stage", sa.String(32), server_default="unknown"),
        sa.Column("funding_date", sa.DateTime(timezone=True)),
        sa.Column("source", sa.String(64), nullable=False),
        sa.Column("source_url", sa.Text),
        sa.Column("founder_name", sa.String(256)),
        sa.Column("founder_twitter", sa.String(256)),
        sa.Column("founder_linkedin", sa.String(512)),
        sa.Column("team_size", sa.Integer),
        sa.Column("description", sa.Text),
        sa.Column("hiring_intent_score", sa.Integer, server_default="0"),
        sa.Column("hiring_signals", JSONB, server_default="[]"),
        sa.Column("likely_roles", JSONB, server_default="[]"),
        sa.Column("company_summary", sa.Text),
        sa.Column("status", sa.String(32), server_default="new"),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
        ),
    )
    op.create_index("ix_companies_name", "companies", ["name"])
    op.create_index("ix_companies_name_slug", "companies", ["name_slug"], unique=True)
    op.create_index("ix_companies_funding_date", "companies", ["funding_date"])
    op.create_index("ix_companies_source", "companies", ["source"])
    op.create_index("ix_companies_hiring_intent_score", "companies", ["hiring_intent_score"])
    op.create_index(
        "ix_companies_hiring_signals_gin",
        "companies",
        ["hiring_signals"],
        postgresql_using="gin",
    )
    op.create_index(
        "ix_companies_likely_roles_gin",
        "companies",
        ["likely_roles"],
        postgresql_using="gin",
    )

    op.create_table(
        "outreach_messages",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "company_id",
            UUID(as_uuid=True),
            sa.ForeignKey("companies.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("type", sa.String(32), nullable=False),
        sa.Column("content", sa.Text, nullable=False),
        sa.Column(
            "generated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
        ),
    )
    op.create_index(
        "ix_outreach_messages_company_id", "outreach_messages", ["company_id"]
    )


def downgrade() -> None:
    op.drop_table("outreach_messages")
    op.drop_table("companies")
