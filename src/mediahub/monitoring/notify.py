"""Decides when a problem deserves a notification, and when it's over.

Needs Attention can afford to flicker, a phone can't. So a problem must last before it alerts
and be verifiably gone before it clears, and each incident alerts once. Issue fingerprints are
too fine for that (a health check's text is part of one), so ``incident_key`` is used instead.

An issue missing from a view doesn't mean it's over: that's only true if the service was seen
working just now (``Sight``). A peer that's down or a poll that failed says nothing, and a
service someone removed never recovers, so it's forgotten without a word.

``NotifyTracker`` is the state machine, free of I/O. Nothing is sent from here: ``NotifyTarget``
hands messages to the push dispatcher, because a poll slot has a timeout and a push service
can be slow.
"""

from __future__ import annotations

import asyncio
import hashlib
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import Any

from sqlalchemy.orm import Session, sessionmaker

from mediahub.aggregation.dashboard import SCOPE_ALL, DashboardView, build_view
from mediahub.db.models import NotifyIncident
from mediahub.db.session import session_scope
from mediahub.logging import get_logger
from mediahub.models.common import ServiceStatus
from mediahub.models.issues import Issue, IssueCategory, Severity
from mediahub.monitoring.poller import PollTarget
from mediahub.monitoring.store import SnapshotStore
from mediahub.push import repository
from mediahub.push.dispatcher import PushDispatcher
from mediahub.push.messages import (
    Alert,
    PushMessage,
    clear_message,
    group_tag,
    raise_message,
    safe_url,
)

log = get_logger(__name__)

TARGET_KEY = "__notify__"
TICK_S = 15.0
RAISE_AFTER = timedelta(seconds=120)  # about two failed polls with nobody viewing
CLEAR_AFTER = timedelta(seconds=300)
COALESCE = timedelta(seconds=60)  # problems starting this close together share one alert
STARTUP_HOLD = timedelta(seconds=300)  # hosts booting together shouldn't alert on each other
FORGET_BLIND_AFTER = timedelta(days=7)
HEARTBEAT = timedelta(hours=1)  # how stale a stored last_seen_at may get

_STATUS_CATEGORIES = (IssueCategory.UNREACHABLE, IssueCategory.AUTH, IssueCategory.PROTOCOL)
_PEER_CATEGORIES = (IssueCategory.PEER_DOWN, IssueCategory.PEER_INCOMPATIBLE)


def incident_key(issue: Issue) -> str:
    if issue.category in _STATUS_CATEGORIES:
        raw = f"status:{issue.source.key}"
    elif issue.category in _PEER_CATEGORIES:
        raw = f"peer:{issue.source.instance_id}"
    elif issue.category is IssueCategory.HEALTH_CHECK:
        raw = f"health:{issue.source.key}:{issue.detail or issue.title}"
    else:
        raw = issue.fingerprint
    return hashlib.sha256(raw.encode()).hexdigest()[:32]


class Sight(StrEnum):
    FRESH = "fresh"  # seen working just now: if the issue is missing, it's gone
    BLIND = "blind"  # nothing known: keep waiting
    GONE = "gone"  # disabled or removed: it will never recover, so forget it


@dataclass(frozen=True, slots=True)
class Seen:
    key: str
    owner: str  # local:<instance id> or peer:<peer id>
    source_key: str | None  # None: the peer link itself
    title: str
    detail: str | None
    url: str
    hidden: bool


@dataclass(frozen=True, slots=True)
class Observation:
    seen: list[Seen]
    sight: Callable[[str, str | None], Sight]


def _sight(
    store: SnapshotStore, polled: set[str], owner: str, source_key: str | None, now: datetime
) -> Sight:
    kind, _, owner_id = owner.partition(":")
    if kind == "local":
        snap = store.get_local(owner_id)
        if snap is None:  # not polled yet, or removed
            return Sight.BLIND if owner_id in polled else Sight.GONE
        if snap.status is ServiceStatus.DISABLED:
            return Sight.GONE
        return Sight.FRESH if snap.status.is_up and not snap.is_stale(now) else Sight.BLIND

    peer = store.get_peer(owner_id)
    if peer is None:
        return Sight.GONE
    if peer.status is not ServiceStatus.OK:
        return Sight.BLIND
    if source_key is None:
        return Sight.FRESH
    # is_stale would compare two machines' clocks, and peer.status already says it's current.
    services = peer.snapshot.services if peer.snapshot else []
    service = next((s for s in services if s.source.key == source_key), None)
    if service is None:  # a restarted peer lists services only after polling them
        return Sight.BLIND
    if service.status is ServiceStatus.DISABLED:
        return Sight.GONE
    return Sight.FRESH if service.status.is_up else Sight.BLIND


def observe(
    view: DashboardView, store: SnapshotStore, polled: set[str], now: datetime
) -> Observation:
    """What the tracker needs from a view: its errors, and how to read the ones it lacks."""
    peers = {p.snapshot.server_id: p.peer_id for p in store.peers() if p.snapshot}
    unverified = {s.source.key for s in view.snapshots if s.peer_down}
    seen: dict[str, Seen] = {}
    for issue in [*view.issues, *view.hidden_issues]:
        src = issue.source
        if issue.severity is not Severity.ERROR or src.key in unverified:
            continue
        title = issue.title
        source_key: str | None = src.key
        if not src.is_remote:
            owner = f"local:{src.instance_id}"
        elif src.service_type is None:
            owner, source_key = f"peer:{src.instance_id}", None
        elif src.server_id in peers:
            owner = f"peer:{peers[src.server_id]}"
            title = f"{src.server_name}: {title}"
        else:
            continue
        seen.setdefault(  # a season pack is several issues with the same key
            (key := incident_key(issue)),
            Seen(key, owner, source_key, title, issue.detail, safe_url(issue.link), issue.hidden),
        )

    def sight(owner: str, source_key: str | None) -> Sight:
        return _sight(store, polled, owner, source_key, now)

    return Observation(list(seen.values()), sight)


class Phase(StrEnum):
    PENDING = "pending"  # memory only: not lasted long enough to mean anything
    NOTIFIED = "notified"
    SILENT = "silent"  # settled with nobody subscribed, so it's owed no follow-up


@dataclass
class Incident:
    key: str
    owner: str
    source_key: str | None
    title: str
    detail: str | None
    url: str
    first_seen: datetime
    last_seen: datetime
    phase: Phase = Phase.PENDING
    hidden: bool = False
    tag: str | None = None
    notified_at: datetime | None = None
    missing_since: datetime | None = None
    written_seen: datetime | None = None

    def row(self) -> dict[str, Any]:
        return {
            "key": self.key,
            "owner": self.owner,
            "source_key": self.source_key,
            "notified": self.phase is Phase.NOTIFIED,
            "title": self.title[:255],
            "url": self.url,
            "tag": self.tag,
            "first_seen_at": self.first_seen,
            "notified_at": self.notified_at,
            "last_seen_at": self.last_seen,
        }


class NotifyTracker:
    """The state machine, free of I/O: feed it observations, write out what ``pending`` returns."""

    def __init__(self, started_at: datetime) -> None:
        self._started_at = started_at
        self._incidents: dict[str, Incident] = {}
        self._dirty: set[str] = set()
        self._removed: set[str] = set()

    def restore(self, rows: Iterable[NotifyIncident]) -> None:
        for row in rows:
            self._incidents[row.key] = Incident(
                key=row.key,
                owner=row.owner,
                source_key=row.source_key,
                title=row.title,
                detail=None,
                url=row.url,
                first_seen=row.first_seen_at,
                last_seen=row.last_seen_at,
                phase=Phase.NOTIFIED if row.notified else Phase.SILENT,
                tag=row.tag,
                notified_at=row.notified_at,
                written_seen=row.last_seen_at,
            )

    def incident(self, key: str) -> Incident | None:
        return self._incidents.get(key)

    def update(self, obs: Observation, now: datetime, subscribers: bool) -> list[PushMessage]:
        present = {seen.key for seen in obs.seen}
        for seen in obs.seen:
            self._note(seen, now)
        messages = self._raise(now, subscribers)
        messages.extend(self._settle_missing(present, obs, now))
        return messages

    def _note(self, seen: Seen, now: datetime) -> None:
        incident = self._incidents.get(seen.key)
        if incident is None:
            self._incidents[seen.key] = Incident(
                key=seen.key,
                owner=seen.owner,
                source_key=seen.source_key,
                title=seen.title,
                detail=seen.detail,
                url=seen.url,
                first_seen=now,
                last_seen=now,
                hidden=seen.hidden,
            )
            return
        settled = incident.phase is not Phase.PENDING
        if settled and (incident.title, incident.url) != (seen.title, seen.url):
            self._dirty.add(seen.key)
        if settled and now - (incident.written_seen or incident.first_seen) >= HEARTBEAT:
            self._dirty.add(seen.key)
        incident.title, incident.detail, incident.url = seen.title, seen.detail, seen.url
        incident.hidden, incident.last_seen, incident.missing_since = seen.hidden, now, None

    def _raise(self, now: datetime, subscribers: bool) -> list[PushMessage]:
        if now - self._started_at < STARTUP_HOLD:
            return []
        candidates = [
            i for i in self._incidents.values() if i.phase is Phase.PENDING and not i.hidden
        ]
        ready = [i for i in candidates if now - i.first_seen >= RAISE_AFTER]
        if not ready:
            return []
        if not subscribers:
            for incident in ready:
                incident.phase = Phase.SILENT
                self._settled(incident)
            return []
        batch = [i for i in candidates if now - i.first_seen >= RAISE_AFTER - COALESCE]
        tag = batch[0].key if len(batch) == 1 else group_tag([i.key for i in batch])
        for incident in batch:
            incident.phase, incident.tag, incident.notified_at = Phase.NOTIFIED, tag, now
            self._settled(incident)
        return [raise_message(tag, [Alert(i.title, i.detail, i.url) for i in batch])]

    def _settle_missing(
        self, present: set[str], obs: Observation, now: datetime
    ) -> list[PushMessage]:
        cleared: dict[str, list[Incident]] = {}
        for incident in list(self._incidents.values()):
            if incident.key in present:
                continue
            if incident.phase is Phase.PENDING:
                del self._incidents[incident.key]
                continue
            sight = obs.sight(incident.owner, incident.source_key)
            if sight is Sight.GONE:
                self._forget(incident)
            elif sight is Sight.BLIND:
                incident.missing_since = None
                if now - incident.last_seen >= FORGET_BLIND_AFTER:
                    self._forget(incident)
            else:
                incident.missing_since = incident.missing_since or now
                if now - incident.missing_since >= CLEAR_AFTER:
                    if incident.phase is Phase.NOTIFIED:
                        cleared.setdefault(incident.tag or incident.key, []).append(incident)
                    self._forget(incident)
        return [self._clear(tag, done) for tag, done in cleared.items()]

    def _clear(self, tag: str, done: list[Incident]) -> PushMessage:
        active = [
            i.title for i in self._incidents.values() if i.tag == tag and i.phase is Phase.NOTIFIED
        ]
        return clear_message(tag, [i.title for i in done], active)

    def _settled(self, incident: Incident) -> None:
        self._dirty.add(incident.key)
        self._removed.discard(incident.key)

    def _forget(self, incident: Incident) -> None:
        del self._incidents[incident.key]
        self._dirty.discard(incident.key)
        self._removed.add(incident.key)

    def pending(self) -> tuple[list[Incident], set[str]]:
        """What the database lacks: rows to write and keys to delete."""
        return [self._incidents[k] for k in self._dirty], set(self._removed)

    def written(self, incidents: list[Incident], removed: set[str]) -> None:
        for incident in incidents:
            incident.written_seen = incident.last_seen
            self._dirty.discard(incident.key)
        self._removed -= removed


class NotifyTarget(PollTarget):
    key = TARGET_KEY

    def __init__(
        self,
        store: SnapshotStore,
        sessions: sessionmaker[Session],
        dispatcher: PushDispatcher,
        polled: Callable[[], set[str]],
        wall: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        super().__init__()
        self._store = store
        self._sessions = sessions
        self._dispatcher = dispatcher
        self._polled = polled
        self._wall = wall
        self.tracker = NotifyTracker(wall())
        self._restored = False

    def interval(self, active: bool) -> float:
        return TICK_S

    async def run_once(self, active: bool) -> bool:
        now = self._wall()
        try:
            if not self._restored:
                self.tracker.restore(await asyncio.to_thread(self._read))
                self._restored = True
            subscribers = await asyncio.to_thread(self._has_subscribers)
            view = build_view(self._store, SCOPE_ALL)
            observation = observe(view, self._store, self._polled(), now)
            # No await until these are queued: a poll timeout mustn't lose what was decided.
            for message in self.tracker.update(observation, now, subscribers):
                self._dispatcher.enqueue(message)
        except Exception:
            log.exception("notify_tick_failed")
            return False
        return await self._flush()

    def _read(self) -> list[NotifyIncident]:
        with session_scope(self._sessions) as db:
            return repository.load_incidents(db)

    def _has_subscribers(self) -> bool:
        with session_scope(self._sessions) as db:
            return repository.has_subscriptions(db)

    def _write(self, rows: list[dict[str, Any]], removed: set[str]) -> None:
        with session_scope(self._sessions) as db:
            repository.save_incidents(db, rows, removed)

    async def _flush(self) -> bool:
        incidents, removed = self.tracker.pending()
        if not incidents and not removed:
            return True
        try:
            await asyncio.to_thread(self._write, [i.row() for i in incidents], removed)
        except Exception:
            log.exception("notify_write_failed", incidents=len(incidents))
            return False  # still pending, so the next tick writes them again
        self.tracker.written(incidents, removed)
        return True

    async def aclose(self) -> None:
        if self._restored:
            await self._flush()
