"""multi-user pilot: users, invites, ownership columns

Revision ID: 0002
Revises: 0001
Create Date: 2026-08-27

"""

import os
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0002"
down_revision: str | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "users",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=False),
        sa.Column("username", sa.String(), nullable=True),
        sa.Column("first_name", sa.String(), nullable=True),
        sa.Column("chat_id", sa.BigInteger(), nullable=True),
        sa.Column("level", sa.String(), nullable=False, server_default="B1"),
        sa.Column("invite_code", sa.String(), nullable=True),
        sa.Column("is_admin", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("active", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("reminder_time", sa.String(), nullable=True),
        sa.Column("writing_time", sa.String(), nullable=True),
        sa.Column("daily_new_limit", sa.Integer(), nullable=True),
        sa.Column("session_max", sa.Integer(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
    )
    op.create_table(
        "invites",
        sa.Column("code", sa.String(), primary_key=True),
        sa.Column("created_by", sa.BigInteger(), nullable=False),
        sa.Column("max_uses", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("used_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("created_at", sa.DateTime(), nullable=False),
    )
    op.add_column("cards", sa.Column("user_id", sa.BigInteger(), nullable=True))
    op.create_index("ix_cards_user_id", "cards", ["user_id"])
    op.add_column("reviews", sa.Column("user_id", sa.BigInteger(), nullable=True))
    op.create_index("ix_reviews_user_id", "reviews", ["user_id"])
    op.add_column("writings", sa.Column("user_id", sa.BigInteger(), nullable=True))
    op.create_index("ix_writings_user_id", "writings", ["user_id"])

    # Migrating an existing single-user database: everything belongs to the
    # (previously sole) allowed user, if configured.
    owner = os.getenv("ADMIN_USER_ID") or os.getenv("ALLOWED_USER_ID")
    if owner and owner.strip().lstrip("-").isdigit():
        owner_id = int(owner)
        op.execute(sa.text(f"UPDATE cards SET user_id = {owner_id}"))
        op.execute(
            sa.text(
                f"UPDATE reviews SET user_id = {owner_id}"
            )
        )
        op.execute(sa.text(f"UPDATE writings SET user_id = {owner_id}"))


def downgrade() -> None:
    op.drop_index("ix_writings_user_id", table_name="writings")
    op.drop_column("writings", "user_id")
    op.drop_index("ix_reviews_user_id", table_name="reviews")
    op.drop_column("reviews", "user_id")
    op.drop_index("ix_cards_user_id", table_name="cards")
    op.drop_column("cards", "user_id")
    op.drop_table("invites")
    op.drop_table("users")
