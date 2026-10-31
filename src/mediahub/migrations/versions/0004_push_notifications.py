"""push notifications

Revision ID: 0004
Revises: 0003
Create Date: 2026-10-31 21:00:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0004"
down_revision: str | None = "0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "push_subscriptions",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("endpoint_hash", sa.String(length=64), nullable=False),
        sa.Column("secrets", sa.Text(), nullable=False),
        sa.Column("host", sa.String(length=255), nullable=False),
        sa.Column("label", sa.String(length=128), nullable=False),
        sa.Column("session_hash", sa.String(length=64), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("last_success_at", sa.DateTime(), nullable=True),
        sa.Column("last_error", sa.String(length=64), nullable=True),
        sa.Column("last_error_at", sa.DateTime(), nullable=True),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("endpoint_hash"),
    )
    with op.batch_alter_table("push_subscriptions", schema=None) as batch_op:
        batch_op.create_index(
            batch_op.f("ix_push_subscriptions_session_hash"), ["session_hash"], unique=False
        )
    op.create_table(
        "push_vapid",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("private_key", sa.Text(), nullable=False),
        sa.Column("public_key", sa.String(length=128), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_table(
        "notify_incidents",
        sa.Column("key", sa.String(length=32), nullable=False),
        sa.Column("owner", sa.String(length=48), nullable=False),
        sa.Column("source_key", sa.String(length=80), nullable=True),
        sa.Column("notified", sa.Boolean(), nullable=False),
        sa.Column("title", sa.String(length=255), nullable=False),
        sa.Column("url", sa.String(length=512), nullable=False),
        sa.Column("tag", sa.String(length=32), nullable=True),
        sa.Column("first_seen_at", sa.DateTime(), nullable=False),
        sa.Column("notified_at", sa.DateTime(), nullable=True),
        sa.Column("last_seen_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("key"),
    )
    with op.batch_alter_table("notify_incidents", schema=None) as batch_op:
        batch_op.create_index(batch_op.f("ix_notify_incidents_owner"), ["owner"], unique=False)


def downgrade() -> None:
    with op.batch_alter_table("notify_incidents", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_notify_incidents_owner"))
    op.drop_table("notify_incidents")
    op.drop_table("push_vapid")
    with op.batch_alter_table("push_subscriptions", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_push_subscriptions_session_hash"))
    op.drop_table("push_subscriptions")
