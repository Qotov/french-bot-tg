"""exam track per user

Revision ID: 0004
Revises: 0003
Create Date: 2026-08-27

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0004"
down_revision: str | None = "0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # NULL means the general track; existing participants keep their behaviour.
    op.add_column("users", sa.Column("track", sa.String(), nullable=True))


def downgrade() -> None:
    op.drop_column("users", "track")
