"""play history

Revision ID: 0003
Revises: 0002
Create Date: 2026-10-31 18:00:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0003"
down_revision: str | None = "0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "play_history",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("instance_id", sa.String(length=36), nullable=False),
        sa.Column("instance_name", sa.String(length=64), nullable=False),
        sa.Column("session_id", sa.String(length=128), nullable=False),
        sa.Column("item_key", sa.String(length=255), nullable=False),
        sa.Column("title", sa.String(length=255), nullable=False),
        sa.Column("subtitle", sa.String(length=255), nullable=True),
        sa.Column("year", sa.Integer(), nullable=True),
        sa.Column("kind", sa.String(length=16), nullable=False),
        sa.Column("user", sa.String(length=128), nullable=True),
        sa.Column("device", sa.String(length=128), nullable=True),
        sa.Column("file_name", sa.Text(), nullable=True),
        sa.Column("started_at", sa.DateTime(), nullable=False),
        sa.Column("last_seen_at", sa.DateTime(), nullable=False),
        sa.Column("ended_at", sa.DateTime(), nullable=True),
        sa.Column("position_s", sa.Float(), nullable=True),
        sa.Column("duration_s", sa.Float(), nullable=True),
        sa.Column("outcome", sa.String(length=16), nullable=False),
        sa.Column("problem_count", sa.Integer(), nullable=False),
        sa.Column("problems", sa.JSON(), nullable=False),
        sa.Column("incomplete", sa.Boolean(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    with op.batch_alter_table("play_history", schema=None) as batch_op:
        batch_op.create_index(
            batch_op.f("ix_play_history_started_at"), ["started_at"], unique=False
        )
        batch_op.create_index(
            "ix_play_history_outcome_started_at", ["outcome", "started_at"], unique=False
        )


def downgrade() -> None:
    with op.batch_alter_table("play_history", schema=None) as batch_op:
        batch_op.drop_index("ix_play_history_outcome_started_at")
        batch_op.drop_index(batch_op.f("ix_play_history_started_at"))

    op.drop_table("play_history")
