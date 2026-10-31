"""Records each play MediaHub can match to InfiniDysk, with any problems reported around it.

Neither Emby nor InfiniDysk keeps this: InfiniDysk's log knows files, Emby's sessions know
people and titles, and only MediaHub links the two, at render time. So this target watches the
store and keeps a row per play. Rows outlive the snapshots they came from, for 30 days.

* A play is an Emby session on one item. Pausing, seeking and reopening within END_GRACE_S are
  the same play; another item in the same session (autoplay) is a new one.
* A session that never matches an InfiniDysk read leaves no row (it isn't a STRM file).
* Only a successful Emby poll says a session has gone: a failed one advances nothing, so an
  outage doesn't end every play. After EMBY_LOST_S the play closes marked incomplete.
* A feed that drops for FEED_LOST_S marks open plays incomplete: problems in the gap may be
  missing. Shorter drops are covered by the log backfill after each connect.
* Open rows are reloaded after a restart: a session still playing carries on, others close.

Problems come from the read of the same file, which can hold an earlier play's, so anything
before this play started is ignored. The two hosts' clocks can disagree, hence CLOCK_SLACK.
"""

from __future__ import annotations

import asyncio
import time
import uuid
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy.orm import Session, sessionmaker

from mediahub.db.models import PlayRecord
from mediahub.db.session import session_scope
from mediahub.history import repository
from mediahub.logging import get_logger
from mediahub.models.common import ServiceStatus, ServiceType
from mediahub.models.infinidysk import ActiveRead, ReadProblem, StreamHealth, health_of
from mediahub.models.playback import PlaybackSession
from mediahub.models.snapshot import ServiceSnapshot
from mediahub.monitoring.poller import PollTarget
from mediahub.monitoring.store import SnapshotStore
from mediahub.monitoring.stream_match import build_read_index

log = get_logger(__name__)

TARGET_KEY = "play-history"
TICK_S = 5.0
END_GRACE_S = 120.0
EMBY_LOST_S = 1800.0
FEED_LOST_S = 120.0
RESTART_GAP_S = 300.0
HEARTBEAT_S = 60.0
PRUNE_EVERY_S = 3600.0
RETENTION = timedelta(days=30)
CLOCK_SLACK = timedelta(seconds=60)
# A long play can report hundreds of problems: keep how it started and how it ended.
KEEP_FIRST = 10
KEEP_NEWEST = 40

# (emby instance id, session id, item id or title)
PlayKey = tuple[str, str, str]
_SEVERITY = list(StreamHealth)


def play_key(session: PlaybackSession) -> PlayKey:
    return (
        session.source.instance_id,
        session.session_id,
        session.item_id or session.media.title,
    )


@dataclass
class Play:
    key: PlayKey
    id: str
    instance_name: str
    title: str
    kind: str
    started_at: datetime
    last_seen_at: datetime
    not_before: datetime | None
    subtitle: str | None = None
    year: int | None = None
    user: str | None = None
    device: str | None = None
    file_name: str | None = None
    strm_target: str | None = None
    position_s: float | None = None
    duration_s: float | None = None
    ended_at: datetime | None = None
    problems: list[ReadProblem] = field(default_factory=list)
    problem_count: int = 0
    outcome: StreamHealth = StreamHealth.OK
    incomplete: bool = False
    matched: bool = False
    dirty: bool = False
    written_at: datetime | None = None

    @classmethod
    def start(cls, session: PlaybackSession, at: datetime, not_before: datetime | None) -> Play:
        media = session.media
        return cls(
            key=play_key(session),
            id=str(uuid.uuid4()),
            instance_name=session.source.instance_name,
            title=media.title,
            kind=media.kind.value,
            started_at=at,
            last_seen_at=at,
            not_before=not_before,
            subtitle=media.subtitle,
            year=media.year,
        )

    @classmethod
    def from_record(cls, row: PlayRecord) -> Play:
        return cls(
            key=(row.instance_id, row.session_id, row.item_key),
            id=row.id,
            instance_name=row.instance_name,
            title=row.title,
            kind=row.kind,
            started_at=row.started_at,
            last_seen_at=row.last_seen_at,
            not_before=row.started_at - CLOCK_SLACK,
            subtitle=row.subtitle,
            year=row.year,
            user=row.user,
            device=row.device,
            file_name=row.file_name,
            position_s=row.position_s,
            duration_s=row.duration_s,
            problems=[ReadProblem.model_validate(p) for p in row.problems],
            problem_count=row.problem_count,
            outcome=StreamHealth(row.outcome),
            incomplete=row.incomplete,
            matched=True,
        )

    def seen(self, session: PlaybackSession, at: datetime) -> None:
        self.last_seen_at = at
        self.instance_name = session.source.instance_name
        self.user = session.user
        self.device = session.device or session.client
        self.file_name = session.file_name
        self.strm_target = session.strm_target
        self.position_s = session.position_s
        self.duration_s = session.duration_s

    def absorb(self, read: ActiveRead) -> None:
        if not self.matched:
            self.matched = self.dirty = True
        known = {(p.at, p.message) for p in self.problems}
        fresh = [
            p
            for p in read.problems
            if (p.at, p.message) not in known
            and (self.not_before is None or p.at >= self.not_before)
        ]
        if not fresh:
            return
        self.problem_count += len(fresh)
        problems = sorted([*self.problems, *fresh], key=lambda p: p.at)
        health = health_of(problems)  # before trimming, which could drop the worst one
        if _SEVERITY.index(health) > _SEVERITY.index(self.outcome):
            self.outcome = health
        if len(problems) > KEEP_FIRST + KEEP_NEWEST:
            problems = [*problems[:KEEP_FIRST], *problems[-KEEP_NEWEST:]]
        self.problems = problems
        self.dirty = True

    def row(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "instance_id": self.key[0],
            "instance_name": self.instance_name,
            "session_id": self.key[1],
            "item_key": self.key[2],
            "title": self.title,
            "subtitle": self.subtitle,
            "year": self.year,
            "kind": self.kind,
            "user": self.user,
            "device": self.device,
            "file_name": self.file_name,
            "started_at": self.started_at,
            "last_seen_at": self.last_seen_at,
            "ended_at": self.ended_at,
            "position_s": self.position_s,
            "duration_s": self.duration_s,
            "outcome": self.outcome.value,
            "problem_count": self.problem_count,
            "problems": [p.model_dump(mode="json") for p in self.problems],
            "incomplete": self.incomplete,
        }


class PlayTracker:
    """The state machine, free of I/O: feed it snapshots, write out what ``pending`` returns."""

    def __init__(self) -> None:
        self._plays: dict[PlayKey, Play] = {}
        self._closed: list[Play] = []
        self._polled: dict[str, datetime] = {}  # emby instance id -> last successful poll
        self._feed_lost_at: datetime | None = None

    def restore(self, rows: Iterable[PlayRecord], now: datetime) -> None:
        for row in rows:
            play = Play.from_record(row)
            play.written_at = now
            if (now - play.last_seen_at).total_seconds() > RESTART_GAP_S:
                play.incomplete = play.dirty = True
            self._plays[play.key] = play

    def update(self, snapshots: list[ServiceSnapshot], instances: set[str], now: datetime) -> None:
        for snap in snapshots:
            if snap.source.service_type is ServiceType.EMBY:
                self._observe(snap)
        index = build_read_index(snapshots)
        for play in self._plays.values():
            read = index.find(play.strm_target, play.file_name)
            if read is not None:
                play.absorb(read)
        self._track_feed(snapshots, now)
        self._close_finished(instances, now)

    def _observe(self, snap: ServiceSnapshot) -> None:
        instance_id = snap.source.instance_id
        polled, previous = snap.fetched_at, self._polled.get(instance_id)
        if polled is None or snap.status is ServiceStatus.DISABLED:
            return
        if previous is not None and polled <= previous:
            return  # no successful poll since the last look
        self._polled[instance_id] = polled
        for session in snap.sessions:
            key = play_key(session)
            play = self._plays.get(key)
            if play is None:
                since = previous - CLOCK_SLACK if previous else None
                play = self._plays[key] = Play.start(session, polled, since)
            play.seen(session, polled)
        # The same session on another item: autoplay went on, this play is over.
        live = {play_key(s) for s in snap.sessions}
        sessions = {s.session_id for s in snap.sessions}
        for key, play in list(self._plays.items()):
            if key[0] == instance_id and key[1] in sessions and key not in live:
                self._close(play)

    def _track_feed(self, snapshots: list[ServiceSnapshot], now: datetime) -> None:
        if any(s.infinidysk and s.infinidysk.live for s in snapshots):
            self._feed_lost_at = None
            return
        self._feed_lost_at = self._feed_lost_at or now
        if (now - self._feed_lost_at).total_seconds() < FEED_LOST_S:
            return
        for play in self._plays.values():
            if play.matched and not play.incomplete:
                play.incomplete = play.dirty = True

    def _close_finished(self, instances: set[str], now: datetime) -> None:
        for play in list(self._plays.values()):
            instance_id = play.key[0]
            polled = self._polled.get(instance_id)
            lost = (now - play.last_seen_at).total_seconds() >= EMBY_LOST_S
            if lost or instance_id not in instances:
                self._close(play, incomplete=True)
            elif polled and (polled - play.last_seen_at).total_seconds() >= END_GRACE_S:
                self._close(play)

    def _close(self, play: Play, incomplete: bool = False) -> None:
        del self._plays[play.key]
        if play.matched:
            play.ended_at = play.last_seen_at
            play.incomplete = play.incomplete or incomplete
            self._closed.append(play)

    def pending(self, now: datetime) -> list[Play]:
        def due(play: Play) -> bool:
            if not play.matched:
                return False
            if play.dirty or play.written_at is None:
                return True
            return (now - play.written_at).total_seconds() >= HEARTBEAT_S

        return [*self._closed, *filter(due, self._plays.values())]

    def written(self, plays: list[Play], now: datetime) -> None:
        for play in plays:
            play.dirty = False
            play.written_at = now
        done = {id(p) for p in plays}
        self._closed = [p for p in self._closed if id(p) not in done]


class PlayHistoryTarget(PollTarget):
    key = TARGET_KEY

    def __init__(
        self,
        store: SnapshotStore,
        sessions: sessionmaker[Session],
        instances: Callable[[], set[str]],
        clock: Callable[[], float] = time.monotonic,
        wall: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        super().__init__()
        self._store = store
        self._sessions = sessions
        self._instances = instances
        self._clock = clock
        self._wall = wall
        self.tracker = PlayTracker()
        self._restored = False
        self._next_prune = float("-inf")

    def interval(self, active: bool) -> float:
        return TICK_S

    async def run_once(self, active: bool) -> bool:
        now = self._wall()
        if not self._restored:
            rows = await asyncio.to_thread(self._read_open)
            self.tracker.restore(rows, now)
            self._restored = True
        self.tracker.update(self._store.local_snapshots(), self._instances(), now)
        ok = await self._flush(now)
        if self._clock() >= self._next_prune:
            self._next_prune = self._clock() + PRUNE_EVERY_S
            await asyncio.to_thread(self._prune, now)
        return ok

    def _read_open(self) -> list[PlayRecord]:
        with session_scope(self._sessions) as db:
            return repository.open_plays(db)

    def _prune(self, now: datetime) -> None:
        with session_scope(self._sessions) as db:
            repository.prune(db, now - RETENTION)

    def _write(self, rows: list[dict[str, Any]]) -> None:
        with session_scope(self._sessions) as db:
            repository.upsert_plays(db, rows)

    async def _flush(self, now: datetime) -> bool:
        plays = self.tracker.pending(now)
        if not plays:
            return True
        try:
            # Awaited here, unlike other background writes: later states must land last.
            await asyncio.to_thread(self._write, [p.row() for p in plays])
        except Exception:
            log.exception("play_history_write_failed", plays=len(plays))
            return False  # still pending: rows are complete, so the next tick writes them again
        self.tracker.written(plays, now)
        return True

    async def aclose(self) -> None:
        if self._restored:
            await self._flush(self._wall())
