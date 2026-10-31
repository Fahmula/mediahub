"""ORM models: configuration, plus the play history and notification state.

Monitoring data is otherwise never persisted. ``play_history`` is an exception, because
nothing upstream can answer "what went wrong during that play" afterwards. ``notify_incidents``
is the other: a restart must not repeat alerts, or lose the follow-up to one already sent.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import (
    JSON,
    Boolean,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from mediahub.db.types import EncryptedJSON, UTCDateTime


def utcnow() -> datetime:
    return datetime.now(UTC)


def new_uuid() -> str:
    return str(uuid.uuid4())


class Base(DeclarativeBase):
    type_annotation_map = {datetime: UTCDateTime()}


class AppSetting(Base):
    __tablename__ = "app_settings"

    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    value: Mapped[Any] = mapped_column(JSON)


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    username: Mapped[str] = mapped_column(String(64), unique=True)
    password_hash: Mapped[str] = mapped_column(String(255))
    created_at: Mapped[datetime] = mapped_column(default=utcnow)
    last_login_at: Mapped[datetime | None]


class AuthSession(Base):
    __tablename__ = "auth_sessions"

    id_hash: Mapped[str] = mapped_column(String(64), primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    csrf_token: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(default=utcnow)
    expires_at: Mapped[datetime] = mapped_column(index=True)
    last_seen_at: Mapped[datetime] = mapped_column(default=utcnow)
    ip: Mapped[str | None] = mapped_column(String(64))
    user_agent: Mapped[str | None] = mapped_column(String(255))


class ServiceInstance(Base):
    __tablename__ = "service_instances"
    __table_args__ = (UniqueConstraint("service_type", "name"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    service_type: Mapped[str] = mapped_column(String(32))
    name: Mapped[str] = mapped_column(String(64))
    base_url: Mapped[str] = mapped_column(String(512))
    secrets: Mapped[dict[str, Any]] = mapped_column(EncryptedJSON, default=dict)
    options: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    poll_interval_s: Mapped[int | None]
    sort_order: Mapped[int] = mapped_column(Integer, default=0)
    last_success_at: Mapped[datetime | None]
    last_error: Mapped[str | None] = mapped_column(Text)
    last_error_at: Mapped[datetime | None]
    created_at: Mapped[datetime] = mapped_column(default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(default=utcnow, onupdate=utcnow)


class Peer(Base):
    __tablename__ = "peers"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    remote_server_id: Mapped[str | None] = mapped_column(String(36))
    name: Mapped[str] = mapped_column(String(64), unique=True)
    base_url: Mapped[str] = mapped_column(String(512))
    token: Mapped[dict[str, Any]] = mapped_column(EncryptedJSON)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    last_success_at: Mapped[datetime | None]
    last_error: Mapped[str | None] = mapped_column(Text)
    last_error_at: Mapped[datetime | None]
    created_at: Mapped[datetime] = mapped_column(default=utcnow)


class PeerGrant(Base):
    __tablename__ = "peer_grants"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    label: Mapped[str] = mapped_column(String(64))
    token_hash: Mapped[str] = mapped_column(String(64), unique=True)
    token_prefix: Mapped[str] = mapped_column(String(16))
    scopes: Mapped[str] = mapped_column(String(128), default="read")
    created_at: Mapped[datetime] = mapped_column(default=utcnow)
    last_used_at: Mapped[datetime | None]
    last_used_ip: Mapped[str | None] = mapped_column(String(64))
    revoked_at: Mapped[datetime | None]


class IssueStateRow(Base):
    """What the user decided about an issue (acknowledged / muted), keyed by its fingerprint."""

    __tablename__ = "issue_states"

    fingerprint: Mapped[str] = mapped_column(String(128), primary_key=True)
    source_key: Mapped[str] = mapped_column(String(80), index=True)
    first_seen: Mapped[datetime | None]
    acknowledged_at: Mapped[datetime | None]
    muted_until: Mapped[datetime | None]


class PlayRecord(Base):
    """One play and what InfiniDysk reported around it. Local only: never sent to peers.

    No client address, read path or provider is kept. ``problems`` are InfiniDysk's messages
    verbatim, and those can name paths.
    """

    __tablename__ = "play_history"
    __table_args__ = (Index("ix_play_history_outcome_started_at", "outcome", "started_at"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    instance_id: Mapped[str] = mapped_column(String(36))  # no FK: rows outlive the instance
    instance_name: Mapped[str] = mapped_column(String(64))
    session_id: Mapped[str] = mapped_column(String(128))
    item_key: Mapped[str] = mapped_column(String(255))
    title: Mapped[str] = mapped_column(String(255))
    subtitle: Mapped[str | None] = mapped_column(String(255))
    year: Mapped[int | None]
    kind: Mapped[str] = mapped_column(String(16))
    user: Mapped[str | None] = mapped_column(String(128))
    device: Mapped[str | None] = mapped_column(String(128))
    file_name: Mapped[str | None] = mapped_column(Text)
    started_at: Mapped[datetime] = mapped_column(index=True)
    last_seen_at: Mapped[datetime]
    ended_at: Mapped[datetime | None]
    position_s: Mapped[float | None] = mapped_column(Float)
    duration_s: Mapped[float | None] = mapped_column(Float)
    outcome: Mapped[str] = mapped_column(String(16))
    problem_count: Mapped[int] = mapped_column(Integer)
    problems: Mapped[list[dict[str, Any]]] = mapped_column(JSON)
    incomplete: Mapped[bool] = mapped_column(Boolean)


class PushSubscription(Base):
    """A browser that receives notifications. Its endpoint works as a credential."""

    __tablename__ = "push_subscriptions"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    endpoint_hash: Mapped[str] = mapped_column(String(64), unique=True)
    secrets: Mapped[dict[str, Any]] = mapped_column(EncryptedJSON)
    host: Mapped[str] = mapped_column(String(255))
    label: Mapped[str] = mapped_column(String(128))
    # No FK: a subscription has to keep working after the session that made it expires.
    session_hash: Mapped[str | None] = mapped_column(String(64), index=True)
    created_at: Mapped[datetime] = mapped_column(default=utcnow)
    last_success_at: Mapped[datetime | None]
    last_error: Mapped[str | None] = mapped_column(String(64))
    last_error_at: Mapped[datetime | None]


class PushVapid(Base):
    """This install's push signing key (a single row)."""

    __tablename__ = "push_vapid"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, default=1)
    private_key: Mapped[dict[str, Any]] = mapped_column(EncryptedJSON)
    public_key: Mapped[str] = mapped_column(String(128))
    created_at: Mapped[datetime] = mapped_column(default=utcnow)


class NotifyIncident(Base):
    """A problem the notifier has settled on, until it clears."""

    __tablename__ = "notify_incidents"

    key: Mapped[str] = mapped_column(String(32), primary_key=True)
    owner: Mapped[str] = mapped_column(String(48), index=True)  # local:<id> or peer:<id>
    source_key: Mapped[str | None] = mapped_column(String(80))  # None: the peer link itself
    notified: Mapped[bool] = mapped_column(Boolean)
    title: Mapped[str] = mapped_column(String(255))
    url: Mapped[str] = mapped_column(String(512))
    tag: Mapped[str | None] = mapped_column(String(32))
    first_seen_at: Mapped[datetime]
    notified_at: Mapped[datetime | None]
    last_seen_at: Mapped[datetime]
