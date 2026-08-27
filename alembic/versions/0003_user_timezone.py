"""per-user timezone

Revision ID: 0003
Revises: 0002
Create Date: 2026-08-27

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0003"
down_revision: str | None = "0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # NULL means "use the server default from .env"; existing participants
    # joined under that assumption, so leaving them NULL preserves behaviour.
    op.add_column("users", sa.Column("tz", sa.String(), nullable=True))


def downgrade() -> None:
    op.drop_column("users", "tz")
