"""Persistence for linked peers (other MediaHub servers this one reads from)."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from mediahub.db.models import Peer
from mediahub.db.types import UNDECRYPTABLE


@dataclass(frozen=True)
class PeerConfig:
    """Decrypted peer configuration handed to a peer client. Never rendered."""

    id: str
    name: str
    base_url: str
    token: str = field(repr=False)
    enabled: bool = True
    remote_server_id: str | None = None


@dataclass(frozen=True)
class ManagedPeer:
    """What the Servers page needs of a row: no token, so nothing is decrypted to poll it."""

    id: str
    name: str
    enabled: bool


def list_peers(db: Session) -> list[Peer]:
    return list(db.scalars(select(Peer).order_by(Peer.name)))


def list_managed(db: Session) -> list[ManagedPeer]:
    rows = db.execute(select(Peer.id, Peer.name, Peer.enabled).order_by(Peer.name))
    return [ManagedPeer(r.id, r.name, r.enabled) for r in rows]


def get_peer(db: Session, peer_id: str) -> Peer | None:
    return db.get(Peer, peer_id)


def token_readable(row: Peer) -> bool:
    return bool((row.token or {}).get("token")) and not (row.token or {}).get(UNDECRYPTABLE)


def safe_to_config(row: Peer) -> PeerConfig | None:
    if not token_readable(row):
        return None
    return PeerConfig(
        id=row.id,
        name=row.name,
        base_url=row.base_url,
        token=str(row.token["token"]),
        enabled=row.enabled,
        remote_server_id=row.remote_server_id,
    )


def record_status(db: Session, peer_id: str, ok: bool, error: str | None, at: datetime) -> None:
    row = db.get(Peer, peer_id)
    if row is None:
        return
    if ok:
        row.last_success_at = at
        row.last_error = None
    else:
        row.last_error = error
        row.last_error_at = at


def remember_server_id(db: Session, peer_id: str, server_id: str) -> None:
    row = db.get(Peer, peer_id)
    if row is not None and row.remote_server_id is None:
        row.remote_server_id = server_id
