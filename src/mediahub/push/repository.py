"""Stored push state: the signing key, subscribed devices and what's been notified."""

from __future__ import annotations

from collections.abc import Iterable
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import delete, exists, func, select
from sqlalchemy.orm import Session

from mediahub.db.models import NotifyIncident, PushSubscription, PushVapid
from mediahub.db.types import UNDECRYPTABLE
from mediahub.logging import get_logger
from mediahub.push.webpush import (
    Device,
    Outcome,
    Result,
    VapidKeys,
    endpoint_hash,
    endpoint_host,
    generate_vapid,
)

log = get_logger(__name__)

MAX_DEVICES = 20
FAILING_TOO_LONG = timedelta(days=30)


class TooManyDevices(Exception):
    pass


def ensure_vapid(db: Session) -> VapidKeys:
    """This install's signing key, created on first start.

    If it can't be decrypted (the secret key changed) a new one is made. Browsers tie a
    subscription to the key that created it, so the stored ones are useless and go too.
    """
    row = db.get(PushVapid, 1)
    if row is not None and not row.private_key.get(UNDECRYPTABLE):
        return VapidKeys(private=row.private_key["d"], public=row.public_key)
    keys = generate_vapid()
    if row is None:
        db.add(PushVapid(id=1, private_key={"d": keys.private}, public_key=keys.public))
    else:
        db.execute(delete(PushSubscription))
        row.private_key = {"d": keys.private}
        row.public_key = keys.public
        log.warning("vapid_key_reset")
    return keys


# ----- subscriptions ---------------------------------------------------------------------------
def _by_endpoint(db: Session, endpoint: str) -> PushSubscription | None:
    return db.scalar(
        select(PushSubscription).where(PushSubscription.endpoint_hash == endpoint_hash(endpoint))
    )


def upsert_subscription(
    db: Session,
    *,
    endpoint: str,
    p256dh: str,
    auth: str,
    label: str,
    session_hash: str | None,
    old_endpoint: str | None = None,
) -> PushSubscription:
    row = _by_endpoint(db, endpoint)
    if row is None and old_endpoint:
        row = _by_endpoint(db, old_endpoint)  # the browser replaced this device's subscription
    if row is None:
        count = db.scalar(select(func.count()).select_from(PushSubscription)) or 0
        if count >= MAX_DEVICES:
            raise TooManyDevices
        row = PushSubscription()
        db.add(row)
    row.endpoint_hash = endpoint_hash(endpoint)
    row.secrets = {"endpoint": endpoint, "p256dh": p256dh, "auth": auth}
    row.host = endpoint_host(endpoint)
    row.label = label[:128]
    row.session_hash = session_hash
    db.flush()
    return row


def list_subscriptions(db: Session) -> list[PushSubscription]:
    return list(db.scalars(select(PushSubscription).order_by(PushSubscription.created_at)))


def has_subscriptions(db: Session) -> bool:
    return bool(db.scalar(select(exists().where(PushSubscription.id.is_not(None)))))


def devices(db: Session) -> list[Device]:
    """Everything to send to. A row that can't be decrypted can never be used, so it's dropped."""
    out: list[Device] = []
    for row in list_subscriptions(db):
        if row.secrets.get(UNDECRYPTABLE):
            db.delete(row)
            continue
        out.append(
            Device(
                id=row.id,
                endpoint=row.secrets["endpoint"],
                p256dh=row.secrets["p256dh"],
                auth=row.secrets["auth"],
                host=row.host,
            )
        )
    return out


def device_for_endpoint(db: Session, endpoint: str) -> Device | None:
    row = _by_endpoint(db, endpoint)
    return next((d for d in devices(db) if row is not None and d.id == row.id), None)


def touch_subscription(db: Session, endpoint: str, session_hash: str) -> bool:
    """Link a device to the current login. False if it's no longer subscribed."""
    row = _by_endpoint(db, endpoint)
    if row is None:
        return False
    row.session_hash = session_hash
    return True


def delete_by_endpoint(db: Session, endpoint: str) -> None:
    db.execute(
        delete(PushSubscription).where(PushSubscription.endpoint_hash == endpoint_hash(endpoint))
    )


def delete_by_id(db: Session, subscription_id: str) -> None:
    db.execute(delete(PushSubscription).where(PushSubscription.id == subscription_id))


def delete_for_session(db: Session, session_hash: str) -> None:
    db.execute(delete(PushSubscription).where(PushSubscription.session_hash == session_hash))


def delete_other_sessions(db: Session, session_hash: str) -> None:
    db.execute(
        delete(PushSubscription).where(
            PushSubscription.session_hash.is_(None)
            | (PushSubscription.session_hash != session_hash)
        )
    )


def record_result(db: Session, device_id: str, result: Result, now: datetime) -> None:
    row = db.get(PushSubscription, device_id)
    if row is None:
        return
    if result.outcome is Outcome.GONE:
        db.delete(row)
        return
    if result.outcome is Outcome.SENT:
        row.last_success_at = now
        row.last_error = row.last_error_at = None
        return
    row.last_error = (result.error or result.outcome.value)[:64]
    row.last_error_at = now
    if now - (row.last_success_at or row.created_at) > FAILING_TOO_LONG:
        db.delete(row)


# ----- incidents -------------------------------------------------------------------------------
def load_incidents(db: Session) -> list[NotifyIncident]:
    return list(db.scalars(select(NotifyIncident)))


def save_incidents(db: Session, rows: Iterable[dict[str, Any]], removed: Iterable[str]) -> None:
    for row in rows:
        db.merge(NotifyIncident(**row))
    if keys := list(removed):
        db.execute(delete(NotifyIncident).where(NotifyIncident.key.in_(keys)))
