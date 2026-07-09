"""add description column to jobs — JobRadar v1 evolution.

Why a new ``description`` column
================================

The React ``JobCard`` previously surfaced only ``ai_fit_reasoning``
(the model's explanation of *why* it gave the score it did), which
left the operator blind to the actual posting body until they
clicked through to the source URL. v0.5 added a per-card "Read more"
modal that wants the actual board-published description, so the
``jobs`` table now persists that field directly.

Nullable
========

Some boards (notably Ashby on a ``GET posting-api/job-board/<slug>``
response) sometimes omit the description field. Nullable keeps the
scoring pipeline tolerant of that absence; the React card treats
``description=None`` as "no Read more affordance, show the
ai_fit_reasoning as the only body text" (the existing v0.4 fallback
behavior, which is why we don't break the operator's mental model
when the field is missing).

No backfill
===========

We do not backfill existing rows — pre-migration jobs simply render
without a description, which is the same UX as a fresh ``null``
on insert. A backfill would require re-fetching every job's source
URL, which is out of scope for a v0.5 frontend polish PR.
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# Revision identifiers, used by Alembic.
revision: str = "0003_add_job_description"
down_revision: Union[str, None] = "0002_status_history_and_research_reports"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # No default — backfilling historical rows is deliberately out of
    # scope. ``nullable=True`` because Ashby sometimes omits the
    # field on its public scraper endpoints.
    op.add_column(
        "jobs",
        sa.Column("description", sa.Text(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("jobs", "description")
