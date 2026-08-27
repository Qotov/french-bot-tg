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
        op.execute(sa.text(f"UPDATE reviews SET user_id = {owner_id}"))
        op.execute(sa.text(f"UPDATE writings SET user_id = {owner_id}"))

        # Settings used to live in the global app_settings table; they are now
        # per-user columns. Carry the owner's choices over, creating their row
        # if this database predates the users table, so an upgrade never
        # silently resets someone's reminder times back to the .env defaults.
        conn = op.get_bind()
        overrides = dict(
            conn.execute(
                sa.text(
                    "SELECT key, value FROM app_settings WHERE key IN "
                    "('REMINDER_TIME','WRITING_TIME','DAILY_NEW_LIMIT','SESSION_MAX')"
                )
            ).fetchall()
        )
        if overrides:
            chat_id = conn.execute(
                sa.text("SELECT value FROM app_settings WHERE key = 'chat_id'")
            ).scalar()
            conn.execute(
                sa.text(
                    "INSERT INTO users (id, chat_id, level, is_admin, active, created_at) "
                    "VALUES (:id, :chat_id, 'B1', 1, 1, CURRENT_TIMESTAMP)"
                ),
                {"id": owner_id, "chat_id": int(chat_id) if chat_id else owner_id},
            )
            for column, key in (
                ("reminder_time", "REMINDER_TIME"),
                ("writing_time", "WRITING_TIME"),
                ("daily_new_limit", "DAILY_NEW_LIMIT"),
                ("session_max", "SESSION_MAX"),
            ):
                if key in overrides:
                    value = overrides[key]
                    if column in ("daily_new_limit", "session_max"):
                        try:
                            value = int(value)
                        except (TypeError, ValueError):
                            continue
                    conn.execute(
                        sa.text(f"UPDATE users SET {column} = :v WHERE id = :id"),
                        {"v": value, "id": owner_id},
                    )


def downgrade() -> None:
    op.drop_index("ix_writings_user_id", table_name="writings")
    op.drop_column("writings", "user_id")
    op.drop_index("ix_reviews_user_id", table_name="reviews")
    op.drop_column("reviews", "user_id")
    op.drop_index("ix_cards_user_id", table_name="cards")
    op.drop_column("cards", "user_id")
    op.drop_table("invites")
    op.drop_table("users")
