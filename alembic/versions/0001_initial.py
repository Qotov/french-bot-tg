"""initial schema

Revision ID: 0001
Revises:
Create Date: 2026-08-25

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "cards",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("text", sa.String(), nullable=False),
        sa.Column("lemma", sa.String(), nullable=False),
        sa.Column("kind", sa.String(), nullable=False),
        sa.Column("enrichment", sa.JSON(), nullable=True),
        sa.Column("error_meta", sa.JSON(), nullable=True),
        sa.Column("fsrs", sa.JSON(), nullable=False),
        sa.Column("due", sa.DateTime(), nullable=False),
        sa.Column("state", sa.String(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("suspended", sa.Boolean(), nullable=False, server_default=sa.false()),
    )
    op.create_index("ix_cards_lemma", "cards", ["lemma"])
    op.create_index("ix_cards_due", "cards", ["due"])

    op.create_table(
        "reviews",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "card_id",
            sa.Integer(),
            sa.ForeignKey("cards.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("rating", sa.Integer(), nullable=False),
        sa.Column("reviewed_at", sa.DateTime(), nullable=False),
        sa.Column("elapsed_days", sa.Float(), nullable=False),
    )
    op.create_index("ix_reviews_card_id", "reviews", ["card_id"])
    op.create_index("ix_reviews_reviewed_at", "reviews", ["reviewed_at"])

    op.create_table(
        "writings",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("prompt", sa.String(), nullable=False),
        sa.Column("answer", sa.String(), nullable=True),
        sa.Column("corrections", sa.JSON(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
    )

    op.create_table(
        "drill_topics",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("slug", sa.String(), nullable=False, unique=True),
        sa.Column("title_fr", sa.String(), nullable=False),
        sa.Column("position", sa.Integer(), nullable=False),
        sa.Column("active_week", sa.Date(), nullable=True),
    )

    op.create_table(
        "app_settings",
        sa.Column("key", sa.String(), primary_key=True),
        sa.Column("value", sa.String(), nullable=False),
    )


def downgrade() -> None:
    op.drop_table("app_settings")
    op.drop_table("drill_topics")
    op.drop_table("writings")
    op.drop_index("ix_reviews_reviewed_at", table_name="reviews")
    op.drop_index("ix_reviews_card_id", table_name="reviews")
    op.drop_table("reviews")
    op.drop_index("ix_cards_due", table_name="cards")
    op.drop_index("ix_cards_lemma", table_name="cards")
    op.drop_table("cards")
