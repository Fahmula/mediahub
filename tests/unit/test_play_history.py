from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import inspect
from sqlalchemy.orm import Session, sessionmaker

from mediahub.db.app_settings import ServerIdentity
from mediahub.db.models import PlayRecord
from mediahub.db.session import make_engine, make_sessionmaker, run_migrations, session_scope
from mediahub.history import repository
from mediahub.integrations.base import InstanceConfig
from mediahub.integrations.demo import DemoIntegration
from mediahub.integrations.infinidysk.ws import LiveFeed
from mediahub.models.common import ServiceStatus, ServiceType, SourceRef
from mediahub.models.infinidysk import (
    ActiveRead,
    InfiniDyskStats,
    ProblemOutcome,
    ReadProblem,
    StreamHealth,
)
from mediahub.models.playback import MediaInfo, MediaKind, PlaybackSession, PlaybackState
from mediahub.models.snapshot import ServiceSnapshot
from mediahub.monitoring.play_history import (
    EMBY_LOST_S,
    END_GRACE_S,
    FEED_LOST_S,
    HEARTBEAT_S,
    KEEP_FIRST,
    KEEP_NEWEST,
    RESTART_GAP_S,
    Play,
    PlayHistoryTarget,
    PlayTracker,
)
from mediahub.monitoring.poller import IDLE_INTERVAL_S
from mediahub.monitoring.store import SnapshotStore
from mediahub.monitoring.targets import InstanceTarget

T0 = datetime(2026, 10, 1, 20, 0, tzinfo=UTC)
EMBY_ID = "emby-1"


def _source(instance_id: str, service_type: ServiceType) -> SourceRef:
    return SourceRef(
        server_id="s",
        server_name="Home",
        instance_id=instance_id,
        instance_name=service_type.label,
        service_type=service_type,
    )


def _session(item: str = "movie-1", session_id: str = "sess", name: str = "movie.mkv") -> Any:
    return PlaybackSession(
        source=_source(EMBY_ID, ServiceType.EMBY),
        session_id=session_id,
        item_id=item,
        user="Sam",
        device="TV",
        file_name=name,
        state=PlaybackState.PLAYING,
        position_s=10.0,
        duration_s=100.0,
        media=MediaInfo(title=item.title(), kind=MediaKind.MOVIE),
    )


def _emby(at: datetime | None, *sessions: PlaybackSession) -> ServiceSnapshot:
    return ServiceSnapshot(
        source=_source(EMBY_ID, ServiceType.EMBY),
        status=ServiceStatus.OK,
        fetched_at=at,
        sessions=list(sessions),
    )


def _problem(at: datetime, message: str = "gap", outcome: Any = None) -> ReadProblem:
    return ReadProblem(at=at, outcome=outcome or ProblemOutcome.RECOVERED, message=message)


def _feed(*problems: ReadProblem, live: bool = True, name: str = "movie.mkv") -> ServiceSnapshot:
    reads = [ActiveRead(read_id="r", file_name=name, problems=list(problems))] if live else []
    return ServiceSnapshot(
        source=_source("idk-1", ServiceType.INFINIDYSK),
        status=ServiceStatus.OK,
        infinidysk=InfiniDyskStats(live=live, reads=reads),
    )


def _at(seconds: float) -> datetime:
    return T0 + timedelta(seconds=seconds)


class Harness:
    def __init__(self) -> None:
        self.tracker = PlayTracker()

    def tick(self, seconds: float, *snapshots: ServiceSnapshot) -> list[Play]:
        now = _at(seconds)
        self.tracker.update(list(snapshots), {EMBY_ID}, now)
        pending = self.tracker.pending(now)
        self.tracker.written(pending, now)
        return pending


@pytest.fixture
def h() -> Harness:
    return Harness()


def test_a_session_that_never_matches_leaves_no_row(h: Harness) -> None:
    assert h.tick(0, _emby(_at(0), _session()), _feed(name="other.mkv")) == []
    assert h.tick(200, _emby(_at(200)), _feed(name="other.mkv")) == []
    assert h.tracker.pending(_at(200)) == []


def test_a_match_that_arrives_after_the_session_left_still_counts(h: Harness) -> None:
    h.tick(0, _emby(_at(0), _session()), _feed(name="other.mkv"))
    written = h.tick(60, _emby(_at(60)), _feed())
    assert [p.title for p in written] == ["Movie-1"]


def test_a_failed_emby_poll_ends_nothing(h: Harness) -> None:
    h.tick(0, _emby(_at(0), _session()), _feed())
    # fetched_at doesn't advance on a failed poll, and the session list may be empty or old.
    h.tick(END_GRACE_S * 3, _emby(_at(0)), _feed())
    h.tick(EMBY_LOST_S - 5, _emby(_at(0)), _feed())
    play = h.tracker._plays[(EMBY_ID, "sess", "movie-1")]
    assert play.ended_at is None and not play.incomplete


def test_a_long_emby_outage_closes_the_play_as_incomplete(h: Harness) -> None:
    h.tick(0, _emby(_at(0), _session()), _feed())
    written = h.tick(EMBY_LOST_S, _emby(_at(0)), _feed())
    assert written[0].ended_at == _at(0) and written[0].incomplete


def test_a_play_ends_only_after_the_grace_period(h: Harness) -> None:
    h.tick(0, _emby(_at(0), _session()), _feed())
    h.tick(10, _emby(_at(10), _session()), _feed())
    h.tick(END_GRACE_S + 9, _emby(_at(END_GRACE_S + 9)), _feed())
    assert (EMBY_ID, "sess", "movie-1") in h.tracker._plays
    written = h.tick(END_GRACE_S + 10, _emby(_at(END_GRACE_S + 10)), _feed())
    assert written[0].ended_at == _at(10) and not written[0].incomplete


def test_reopening_within_grace_is_the_same_play(h: Harness) -> None:
    first = h.tick(0, _emby(_at(0), _session()), _feed())[0]
    h.tick(60, _emby(_at(60)), _feed())
    h.tick(100, _emby(_at(100), _session()), _feed())
    assert h.tracker._plays[(EMBY_ID, "sess", "movie-1")].id == first.id


def test_reopening_after_grace_is_a_new_play_without_the_old_problems(h: Harness) -> None:
    old = _problem(_at(5))
    first = h.tick(0, _emby(_at(0), _session()), _feed(old))[0]
    h.tick(300, _emby(_at(300)), _feed(old))
    second = h.tick(310, _emby(_at(310), _session()), _feed(old))[0]
    assert second.id != first.id
    assert second.problems == [] and second.outcome is StreamHealth.OK


def test_autoplay_closes_the_previous_item_at_once(h: Harness) -> None:
    h.tick(0, _emby(_at(0), _session("ep-1", name="a.mkv")), _feed(name="a.mkv"))
    written = h.tick(30, _emby(_at(30), _session("ep-2", name="b.mkv")), _feed(name="a.mkv"))
    assert [p.key[2] for p in written] == ["ep-1"] and written[0].ended_at == _at(0)


def test_a_removed_instance_closes_at_once_as_incomplete(h: Harness) -> None:
    h.tick(0, _emby(_at(0), _session()), _feed())
    h.tracker.update([], set(), _at(5))
    written = h.tracker.pending(_at(5))
    assert written[0].incomplete and written[0].ended_at == _at(0)


def test_problems_are_deduped_and_grow_past_the_feed_window(h: Harness) -> None:
    a, b = _problem(_at(1), "a"), _problem(_at(2), "b")
    h.tick(0, _emby(_at(0), _session()), _feed(a))
    h.tick(5, _emby(_at(5), _session()), _feed(a, b))
    h.tick(10, _emby(_at(10), _session()), _feed(b))
    play = h.tracker._plays[(EMBY_ID, "sess", "movie-1")]
    assert [p.message for p in play.problems] == ["a", "b"] and play.problem_count == 2


def test_problems_before_the_play_started_are_ignored_with_clock_slack(h: Harness) -> None:
    h.tick(-5, _emby(_at(-5)), _feed())
    old = _problem(_at(-120), "old")
    slack = _problem(_at(-30), "within clock slack")
    h.tick(0, _emby(_at(0), _session()), _feed(old, slack))
    play = h.tracker._plays[(EMBY_ID, "sess", "movie-1")]
    assert [p.message for p in play.problems] == ["within clock slack"]


def test_the_stored_list_is_capped_but_keeps_the_count_and_worst_outcome(h: Harness) -> None:
    failed = _problem(_at(20), "failed", ProblemOutcome.FAILED)
    h.tick(0, _emby(_at(0), _session()), _feed(failed))
    total = KEEP_FIRST + KEEP_NEWEST + 30
    for i in range(total):
        h.tick(5 + i, _emby(_at(5 + i), _session()), _feed(_problem(_at(1000 + i), f"p{i}")))
    play = h.tracker._plays[(EMBY_ID, "sess", "movie-1")]
    assert len(play.problems) == KEEP_FIRST + KEEP_NEWEST
    assert play.problem_count == total + 1
    assert play.problems[-1].message == f"p{total - 1}"
    assert play.outcome is StreamHealth.FAILED


def test_a_feed_drop_keeps_problems_and_flags_only_a_long_gap(h: Harness) -> None:
    h.tick(0, _emby(_at(0), _session()), _feed(_problem(_at(1))))
    h.tick(5, _emby(_at(5), _session()), _feed(live=False))
    play = h.tracker._plays[(EMBY_ID, "sess", "movie-1")]
    h.tick(FEED_LOST_S, _emby(_at(FEED_LOST_S), _session()), _feed(live=False))
    assert play.incomplete is False and play.problem_count == 1
    written = h.tick(FEED_LOST_S + 5, _emby(_at(FEED_LOST_S + 5), _session()), _feed(live=False))
    assert written[0].incomplete and written[0].problem_count == 1


def test_heartbeat_rewrites_an_open_play(h: Harness) -> None:
    h.tick(0, _emby(_at(0), _session()), _feed())
    assert h.tick(HEARTBEAT_S - 1, _emby(_at(HEARTBEAT_S - 1), _session()), _feed()) == []
    written = h.tick(HEARTBEAT_S, _emby(_at(HEARTBEAT_S), _session()), _feed())
    assert written[0].last_seen_at == _at(HEARTBEAT_S)


def _record(**overrides: Any) -> PlayRecord:
    row = Play.start(_session(), T0, None)
    row.seen(_session(), T0)
    row.matched = True
    return PlayRecord(**{**row.row(), **overrides})


def test_restart_reattaches_a_live_session(h: Harness) -> None:
    record = _record(problems=[_problem(_at(1)).model_dump(mode="json")], problem_count=1)
    h.tracker.restore([record], _at(30))
    h.tick(35, _emby(_at(35), _session()), _feed(_problem(_at(1)), _problem(_at(32), "later")))
    play = h.tracker._plays[(EMBY_ID, "sess", "movie-1")]
    assert play.id == record.id and play.problem_count == 2 and not play.incomplete


def test_restart_closes_a_play_that_ended_meanwhile(h: Harness) -> None:
    record = _record()
    h.tracker.restore([record], _at(RESTART_GAP_S + 1))
    written = h.tick(RESTART_GAP_S + 5, _emby(_at(RESTART_GAP_S + 5)), _feed())
    assert written[0].id == record.id and written[0].ended_at == T0 and written[0].incomplete


@pytest.fixture
def sessions(tmp_path: Path) -> sessionmaker[Session]:
    engine = make_engine(f"sqlite:///{tmp_path / 'test.db'}")
    run_migrations(engine)
    return make_sessionmaker(engine)


def _target(
    store: SnapshotStore, sessions: sessionmaker[Session], clock: list[datetime]
) -> PlayHistoryTarget:
    return PlayHistoryTarget(store, sessions, lambda: {EMBY_ID}, wall=lambda: clock[0])


async def test_rows_survive_a_restart_and_a_failed_write_is_retried(
    sessions: sessionmaker[Session], monkeypatch: pytest.MonkeyPatch
) -> None:
    store = SnapshotStore(identity=ServerIdentity(server_id="s", server_name="Home"))
    store.put_local(_emby(T0, _session()))
    store.put_local(_feed(_problem(_at(1))))
    clock = [T0]
    target = _target(store, sessions, clock)

    real = repository.upsert_plays

    def fail(*_: Any) -> None:
        raise OSError("disk full")

    monkeypatch.setattr(repository, "upsert_plays", fail)
    assert await target.run_once(True) is False
    monkeypatch.setattr(repository, "upsert_plays", real)
    assert await target.run_once(True) is True
    with session_scope(sessions) as db:
        rows = repository.open_plays(db)
    assert [r.title for r in rows] == ["Movie-1"]
    assert rows[0].outcome == "recovered" and rows[0].problem_count == 1
    assert ReadProblem.model_validate(rows[0].problems[0]).message == "gap"

    clock[0] = _at(30)
    restarted = _target(store, sessions, clock)
    store.put_local(_emby(_at(30), _session()))
    assert await restarted.run_once(True) is True
    with session_scope(sessions) as db:
        assert [r.id for r in repository.open_plays(db)] == [rows[0].id]


def test_only_a_new_read_id_counts_as_a_read_starting() -> None:
    fired: list[int] = []
    feed = LiveFeed("ws://x", "k")
    feed.on_read_started = lambda: fired.append(1)
    feed._set_reads([{"id": "a", "path": "/f/a"}])
    feed._set_reads([{"id": "a", "path": "/f/a"}, {"id": "b", "path": "/f/b"}])
    feed._set_reads([{"id": "a", "path": "/f/a"}, {"id": "b", "path": "/f/b"}])
    feed._set_reads([{"id": "b", "path": "/f/b"}])
    assert len(fired) == 2


async def test_boost_polls_now_without_touching_the_reported_interval() -> None:
    config = InstanceConfig(
        id="demo-emby", service_type=ServiceType.EMBY, name="Emby", base_url="demo://"
    )
    store = SnapshotStore(identity=ServerIdentity(server_id="s", server_name="Home"))
    integration = DemoIntegration(config, _source("demo-emby", ServiceType.EMBY))
    target = InstanceTarget(integration, store)
    assert target.interval(False) == IDLE_INTERVAL_S

    target.boost(30)
    assert target.wake.is_set() and target.interval(False) < IDLE_INTERVAL_S
    await target.run_once(False)
    snapshot = store.get_local("demo-emby")
    assert snapshot is not None and snapshot.poll_interval_s == IDLE_INTERVAL_S


def test_prune_keeps_recent_and_open_plays(sessions: sessionmaker[Session]) -> None:
    old = T0 - timedelta(days=40)
    with session_scope(sessions) as db:
        db.add(_record(id="old", started_at=old, ended_at=old))
        db.add(_record(id="old-open", started_at=old, ended_at=None))
        db.add(_record(id="new", started_at=T0, ended_at=T0))
    with session_scope(sessions) as db:
        repository.prune(db, T0 - timedelta(days=30))
    with session_scope(sessions) as db:
        left = {r.id for r in db.query(PlayRecord)}
    assert left == {"old-open", "new"}


def test_history_stores_no_addresses_or_read_paths(sessions: sessionmaker[Session]) -> None:
    engine = sessions.kw["bind"]
    columns = {c["name"] for c in inspect(engine).get_columns("play_history")}
    assert not {"client_ip", "remote_address", "path", "strm_target"} & columns
