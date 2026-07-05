"""Create jobs, applications, qa_bank_entries tables."""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID

revision = "004"
down_revision = "003"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "jobs",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column("company_id", UUID(as_uuid=True), sa.ForeignKey("companies.id", ondelete="CASCADE"), nullable=False),
        sa.Column("title", sa.String(512), nullable=False),
        sa.Column("url", sa.Text, nullable=False, unique=True),
        sa.Column("ats_type", sa.String(32), nullable=False),
        sa.Column("jd_text", sa.Text, nullable=True),
        sa.Column("ai_fit_score", sa.Float, nullable=True),
        sa.Column("ai_fit_reasoning", sa.Text, nullable=True),
        sa.Column("status", sa.String(32), nullable=False, server_default="in_review"),
        sa.Column("scraped_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("review_deadline", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index("ix_jobs_company_id", "jobs", ["company_id"])
    op.create_index("ix_jobs_status", "jobs", ["status"])

    op.create_table(
        "applications",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column("job_id", UUID(as_uuid=True), sa.ForeignKey("jobs.id", ondelete="CASCADE"), nullable=False, unique=True),
        sa.Column("submitted_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("submission_screenshot_path", sa.Text, nullable=True),
        sa.Column("status", sa.String(32), nullable=False, server_default="submitted"),
        sa.Column("gmail_thread_id", sa.String(256), nullable=True),
        sa.Column("last_email_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("notes", sa.Text, nullable=True),
    )
    op.create_index("ix_applications_status", "applications", ["status"])

    op.create_table(
        "qa_bank_entries",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column("question_pattern", sa.String(512), nullable=False),
        sa.Column("canonical_question", sa.String(512), nullable=False),
        sa.Column("answer", sa.Text, nullable=True),
        sa.Column("answer_type", sa.String(32), nullable=False, server_default="text"),
        sa.Column("times_used", sa.Integer, nullable=False, server_default="0"),
        sa.Column("last_used_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    )
    op.create_index("ix_qa_bank_entries_question_pattern", "qa_bank_entries", ["question_pattern"])


def downgrade() -> None:
    op.drop_table("qa_bank_entries")
    op.drop_table("applications")
    op.drop_table("jobs")
