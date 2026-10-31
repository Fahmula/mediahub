"""Server-side login sessions.

The browser holds a random 32-byte token; the database stores only its SHA-256, so a leaked
database can't be used to hijack sessions. Expiry slides forward on use, but the row is
written at most once a minute to keep SQLite writes rare.
"""

from __future__ import annotations

import hashlib
import secrets
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from mediahub.db.models import AuthSession, User

COOKIE_NAME = "mh_session"
_TOUCH_INTERVAL = timedelta(minutes=1)


@dataclass(frozen=True, slots=True)
class Authenticated:
    user_id: int
    username: str
    csrf_token: str
    session_hash: str


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def session_hash(token: str) -> str:
    return _hash(token)


def create_session(
    db: Session, user: User, *, lifetime: timedelta, ip: str | None, user_agent: str | None
) -> str:
    token = secrets.token_urlsafe(32)
    now = datetime.now(UTC)
    db.add(
        AuthSession(
            id_hash=_hash(token),
            user_id=user.id,
            csrf_token=secrets.token_urlsafe(32),
            created_at=now,
            last_seen_at=now,
            expires_at=now + lifetime,
            ip=ip,
            user_agent=(user_agent or "")[:255] or None,
        )
    )
    user.last_login_at = now
    return token


def resolve_session(db: Session, token: str, *, lifetime: timedelta) -> Authenticated | None:
    row = db.execute(
        select(AuthSession, User)
        .join(User, User.id == AuthSession.user_id)
        .where(AuthSession.id_hash == _hash(token))
    ).one_or_none()
    if row is None:
        return None
    sess, user = row
    now = datetime.now(UTC)
    if sess.expires_at <= now:
        db.delete(sess)
        return None
    if now - sess.last_seen_at >= _TOUCH_INTERVAL:
        sess.last_seen_at = now
        sess.expires_at = now + lifetime
    return Authenticated(
        user_id=user.id,
        username=user.username,
        csrf_token=sess.csrf_token,
        session_hash=sess.id_hash,
    )


def delete_session(db: Session, token: str) -> None:
    db.execute(delete(AuthSession).where(AuthSession.id_hash == _hash(token)))


def purge_expired(db: Session) -> int:
    result = db.execute(delete(AuthSession).where(AuthSession.expires_at <= datetime.now(UTC)))
    return result.rowcount or 0  # type: ignore[attr-defined]
