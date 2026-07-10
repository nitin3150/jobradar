# Standard Alembic mako template — JobRadar customization is in env.py.
"""merge divergent 0003 heads (job description + paused status)

Revision ID: b1a536fd056f
Revises: 0003_add_job_description, 0003_add_paused_status
Create Date: 2026-07-10 04:41:24.218121+00:00

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'b1a536fd056f'
down_revision: Union[str, None] = ('0003_add_job_description', '0003_add_paused_status')
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
