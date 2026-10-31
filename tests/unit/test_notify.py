"""When a problem notifies, when it clears, and when it's quietly forgotten."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy.orm import Session, sessionmaker

from mediahub.aggregation.dashboard import SCOPE_ALL, build_view
from mediahub.db.app_settings import ServerIdentity
from mediahub.db.models import NotifyIncident
from mediahub.db.session import make_engine, make_sessionmaker, run_migrations, session_scope
from mediahub.models.common import ServiceStatus, ServiceType, SourceRef
from mediahub.models.downloads import DownloadItem, DownloadState
from mediahub.models.issues import Issue, IssueCategory, Severity
from mediahub.models.snapshot import HealthCheck, ServerSnapshot, ServiceSnapshot
from mediahub.monitoring import notify
from mediahub.monitoring.notify import (
    CLEAR_AFTER,
    COALESCE,
    FORGET_BLIND_AFTER,
    RAISE_AFTER,
    STARTUP_HOLD,
    NotifyTarget,
    NotifyTracker,
    Observation,
    Phase,
    Seen,
    Sight,
    incident_key,
    observe,
)
from mediahub.monitoring.store import SnapshotStore
from mediahub.push import repository
from mediahub.push.messages import PushMessage

START = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)
AFTER_HOLD = START + STARTUP_HOLD
IDENTITY = ServerIdentity(server_id="srv-1", server_name="Skynet")
SRC = SourceRef(
    server_id="srv-1",
    server_name="Skynet",
    instance_id="i1",
    instance_name="Sonarr",
    service_type=ServiceType.SONARR,
)
PEER_SRC = SourceRef(
    server_id="srv-2",
    server_name="Brother Server",
    instance_id="r1",
    instance_name="Radarr",
    service_type=ServiceType.RADARR,
    is_remote=True,
)


def seen(key: str = "a", *, hidden: bool = False, title: str | None = None) -> Seen:
    return Seen(key, "local:i1", "srv-1:i1", title or f"{key} broke", None, "/attention", hidden)


class Harness:
    """Steps the tracker through time, one call per tick."""

    def __init__(self, now: datetime = AFTER_HOLD, subscribers: bool = True) -> None:
        self.tracker = NotifyTracker(START)
        self.now = now
        self.subscribers = subscribers
        self.sight = Sight.FRESH

    def tick(self, *issues: Seen, after: timedelta = timedelta(0)) -> list[PushMessage]:
        self.now += after
        obs = Observation(list(issues), lambda _owner, _source: self.sight)
        return self.tracker.update(obs, self.now, self.subscribers)

    def phase(self, key: str = "a") -> Phase | None:
        incident = self.tracker.incident(key)
        return incident.phase if incident else None

    def raised(self, *issues: Seen) -> list[PushMessage]:
        self.tick(*issues)
        return self.tick(*issues, after=RAISE_AFTER)


# ----- incident keys ---------------------------------------------------------------------------
def issue(category: IssueCategory, *, fp: str = "fp", title: str = "t", detail: str | None = None):
    return Issue(
        fingerprint=fp,
        severity=Severity.ERROR,
        category=category,
        source=SRC,
        title=title,
        detail=detail,
    )


def test_a_service_failing_differently_is_one_incident() -> None:
    assert incident_key(issue(IssueCategory.UNREACHABLE, fp="x")) == incident_key(
        issue(IssueCategory.AUTH, fp="y")
    )


def test_a_peer_going_incompatible_is_the_same_incident() -> None:
    assert incident_key(issue(IssueCategory.PEER_DOWN, fp="x")) == incident_key(
        issue(IssueCategory.PEER_INCOMPATIBLE, fp="y")
    )


def test_health_check_key_survives_its_text_changing() -> None:
    assert incident_key(
        issue(IssueCategory.HEALTH_CHECK, fp="x", title="Indexers a, b", detail="IndexerCheck")
    ) == incident_key(
        issue(IssueCategory.HEALTH_CHECK, fp="y", title="Indexers a", detail="IndexerCheck")
    )


def test_downloads_keep_their_own_fingerprint() -> None:
    assert incident_key(issue(IssueCategory.DOWNLOAD_FAILED, fp="x")) != incident_key(
        issue(IssueCategory.DOWNLOAD_FAILED, fp="y")
    )


def test_incident_key_fits_a_topic_header() -> None:
    assert len(incident_key(issue(IssueCategory.AUTH))) == 32


# ----- raising ---------------------------------------------------------------------------------
def test_a_blip_never_alerts() -> None:
    h = Harness()
    h.tick(seen())
    assert h.tick(after=RAISE_AFTER - timedelta(seconds=1)) == []
    assert h.phase() is None


def test_a_lasting_problem_alerts_once() -> None:
    h = Harness()
    assert h.tick(seen()) == []
    assert h.tick(seen(), after=RAISE_AFTER - timedelta(seconds=1)) == []
    [message] = h.tick(seen(), after=timedelta(seconds=1))
    assert message.title == "a broke"
    assert message.urgent
    assert h.tick(seen(), after=timedelta(hours=1)) == []


def test_nothing_alerts_right_after_startup() -> None:
    h = Harness(now=START)
    h.tick(seen())
    assert h.tick(seen(), after=STARTUP_HOLD - timedelta(seconds=1)) == []
    assert len(h.tick(seen(), after=timedelta(seconds=1))) == 1


def test_problems_starting_together_share_one_alert() -> None:
    h = Harness()
    h.tick(seen("a"))
    h.tick(seen("a"), seen("b"), after=COALESCE)
    [message] = h.tick(seen("a"), seen("b"), after=RAISE_AFTER - COALESCE)
    assert message.title == "2 problems need attention"
    assert h.phase("a") is h.phase("b") is Phase.NOTIFIED


def test_a_later_problem_gets_its_own_alert() -> None:
    h = Harness()
    h.tick(seen("a"))
    h.tick(seen("a"), seen("b"), after=RAISE_AFTER - COALESCE + timedelta(seconds=1))
    [first] = h.tick(seen("a"), seen("b"), after=COALESCE - timedelta(seconds=1))
    assert first.title == "a broke"
    assert h.phase("b") is Phase.PENDING


# ----- nobody subscribed -----------------------------------------------------------------------
def test_problems_before_anyone_subscribed_never_notify() -> None:
    h = Harness(subscribers=False)
    assert h.raised(seen()) == []
    assert h.phase() is Phase.SILENT
    h.subscribers = True
    assert h.tick(seen(), after=timedelta(hours=1)) == []


def test_silent_problems_clear_silently() -> None:
    h = Harness(subscribers=False)
    h.raised(seen())
    h.subscribers = True
    h.tick(after=timedelta(seconds=15))
    assert h.tick(after=CLEAR_AFTER) == []
    assert h.phase() is None


# ----- clearing --------------------------------------------------------------------------------
def test_clears_after_staying_gone() -> None:
    h = Harness()
    h.raised(seen())
    assert h.tick(after=timedelta(seconds=15)) == []
    assert h.tick(after=CLEAR_AFTER - timedelta(seconds=1)) == []
    [message] = h.tick(after=timedelta(seconds=1))
    assert message.title == "Resolved: a broke"
    assert not message.urgent
    assert h.phase() is None


def test_reappearing_restarts_the_wait() -> None:
    h = Harness()
    h.raised(seen())
    h.tick(after=timedelta(seconds=15))
    h.tick(seen(), after=CLEAR_AFTER - timedelta(seconds=1))
    assert h.tick(after=timedelta(seconds=15)) == []
    assert h.tick(after=CLEAR_AFTER - timedelta(seconds=1)) == []


def test_flapping_stays_one_incident() -> None:
    h = Harness()
    sent = h.raised(seen())
    for _ in range(20):
        sent += h.tick(after=timedelta(seconds=60))
        sent += h.tick(seen(), after=timedelta(seconds=60))
    assert len(sent) == 1


def test_clear_replaces_the_alert_it_answers() -> None:
    h = Harness()
    [alert] = h.raised(seen())
    h.tick(after=timedelta(seconds=15))
    [clear] = h.tick(after=CLEAR_AFTER)
    assert clear.tag == alert.tag


def test_part_of_a_group_clearing_says_what_remains() -> None:
    h = Harness()
    [alert] = h.raised(seen("a"), seen("b"))
    h.tick(seen("b"), after=timedelta(seconds=15))
    [clear] = h.tick(seen("b"), after=CLEAR_AFTER)
    assert clear.tag == alert.tag
    assert clear.title == "Resolved: a broke"
    assert "Still active:\nb broke" in clear.body


def test_a_group_clearing_together_is_one_message() -> None:
    h = Harness()
    h.raised(seen("a"), seen("b"))
    h.tick(after=timedelta(seconds=15))
    [clear] = h.tick(after=CLEAR_AFTER)
    assert clear.title == "2 problems resolved"


def test_error_downgraded_to_warning_clears() -> None:
    store = SnapshotStore(identity=IDENTITY)
    now = datetime.now(UTC)
    check = HealthCheck(source="Disk", level=Severity.ERROR, message="Disk failing")
    store.put_local(
        ServiceSnapshot(source=SRC, status=ServiceStatus.OK, fetched_at=now, health_checks=[check])
    )
    assert len(observe(build_view(store, SCOPE_ALL), store, {"i1"}, now).seen) == 1
    warned = check.model_copy(update={"level": Severity.WARNING})
    store.put_local(
        ServiceSnapshot(source=SRC, status=ServiceStatus.OK, fetched_at=now, health_checks=[warned])
    )
    assert observe(build_view(store, SCOPE_ALL), store, {"i1"}, now).seen == []


# ----- what can't be seen ----------------------------------------------------------------------
def test_blind_absence_never_clears() -> None:
    h = Harness()
    h.raised(seen())
    h.sight = Sight.BLIND
    assert h.tick(after=timedelta(seconds=15)) == []
    assert h.tick(after=timedelta(days=1)) == []
    assert h.phase() is Phase.NOTIFIED


def test_going_blind_restarts_the_wait() -> None:
    h = Harness()
    h.raised(seen())
    h.tick(after=timedelta(seconds=15))
    h.sight = Sight.BLIND
    h.tick(after=CLEAR_AFTER - timedelta(seconds=1))
    h.sight = Sight.FRESH
    assert h.tick(after=timedelta(seconds=15)) == []


def test_blind_for_a_week_is_forgotten_without_a_message() -> None:
    h = Harness()
    h.raised(seen())
    h.sight = Sight.BLIND
    assert h.tick(after=FORGET_BLIND_AFTER) == []
    assert h.phase() is None


def test_removed_sources_are_forgotten_without_a_message() -> None:
    h = Harness()
    h.raised(seen())
    h.sight = Sight.GONE
    assert h.tick(after=timedelta(seconds=15)) == []
    assert h.phase() is None


# ----- acknowledged and muted ------------------------------------------------------------------
def test_acknowledged_before_alerting_never_alerts() -> None:
    h = Harness()
    h.tick(seen(hidden=True))
    assert h.tick(seen(hidden=True), after=timedelta(hours=1)) == []
    assert h.phase() is Phase.PENDING


def test_mute_running_out_alerts_then() -> None:
    h = Harness()
    h.tick(seen(hidden=True))
    h.tick(seen(hidden=True), after=timedelta(hours=1))
    assert len(h.tick(seen(), after=timedelta(seconds=15))) == 1


def test_acknowledged_after_alerting_still_gets_its_follow_up() -> None:
    h = Harness()
    h.raised(seen())
    h.tick(seen(hidden=True), after=timedelta(seconds=15))
    h.tick(after=timedelta(seconds=15))
    assert len(h.tick(after=CLEAR_AFTER)) == 1


# ----- restart ---------------------------------------------------------------------------------
def rows(h: Harness) -> list[NotifyIncident]:
    incidents, _ = h.tracker.pending()
    return [NotifyIncident(**i.row()) for i in incidents]


def test_restart_does_not_repeat_an_alert() -> None:
    h = Harness()
    h.raised(seen())
    after = Harness(now=h.now + STARTUP_HOLD)
    after.tracker.restore(rows(h))
    assert after.tick(seen()) == []
    assert after.phase() is Phase.NOTIFIED


def test_recovery_during_a_restart_still_sends_the_follow_up() -> None:
    h = Harness()
    h.raised(seen())
    after = Harness(now=h.now + STARTUP_HOLD)
    after.tracker.restore(rows(h))
    after.tick()
    [clear] = after.tick(after=CLEAR_AFTER)
    assert clear.title == "Resolved: a broke"


def test_failed_write_is_kept_for_the_next_one() -> None:
    h = Harness()
    h.raised(seen())
    assert len(h.tracker.pending()[0]) == 1
    h.tick(seen(), after=timedelta(seconds=15))
    incidents, removed = h.tracker.pending()
    assert len(incidents) == 1
    h.tracker.written(incidents, removed)
    assert h.tracker.pending() == ([], set())


def test_forgotten_incidents_are_deleted_from_storage() -> None:
    h = Harness()
    h.raised(seen())
    h.sight = Sight.GONE
    h.tick()
    assert h.tracker.pending() == ([], {"a"})


# ----- reading the store -----------------------------------------------------------------------
def snapshot(status: ServiceStatus = ServiceStatus.OK, **extra: object) -> ServiceSnapshot:
    return ServiceSnapshot(source=SRC, status=status, fetched_at=datetime.now(UTC), **extra)


def local_sight(store: SnapshotStore, polled: set[str] | None = None) -> Sight:
    now = datetime.now(UTC)
    obs = observe(build_view(store, SCOPE_ALL), store, polled or set(), now)
    return obs.sight("local:i1", SRC.key)


def test_only_errors_are_watched() -> None:
    store = SnapshotStore(identity=IDENTITY)
    warning = HealthCheck(source="x", level=Severity.WARNING, message="careful")
    store.put_local(snapshot(health_checks=[warning]))
    now = datetime.now(UTC)
    assert observe(build_view(store, SCOPE_ALL), store, set(), now).seen == []


def test_season_pack_is_one_incident() -> None:
    store = SnapshotStore(identity=IDENTITY)
    pack = [
        DownloadItem(
            source=SRC,
            item_id=str(n),
            download_id="pack",
            title=f"Episode {n}",
            state=DownloadState.FAILED,
        )
        for n in range(3)
    ]
    store.put_local(snapshot(downloads=pack))
    now = datetime.now(UTC)
    assert len(observe(build_view(store, SCOPE_ALL), store, set(), now).seen) == 1


def test_working_service_is_fresh() -> None:
    store = SnapshotStore(identity=IDENTITY)
    store.put_local(snapshot())
    assert local_sight(store) is Sight.FRESH


def test_failing_service_is_blind() -> None:
    store = SnapshotStore(identity=IDENTITY)
    store.put_local(snapshot(ServiceStatus.UNREACHABLE))
    assert local_sight(store) is Sight.BLIND


def test_stale_snapshot_is_blind() -> None:
    store = SnapshotStore(identity=IDENTITY)
    old = datetime.now(UTC) - timedelta(hours=1)
    store.put_local(ServiceSnapshot(source=SRC, status=ServiceStatus.OK, fetched_at=old))
    assert local_sight(store) is Sight.BLIND


def test_not_yet_polled_service_is_blind() -> None:
    assert local_sight(SnapshotStore(identity=IDENTITY), {"i1"}) is Sight.BLIND


def test_deleted_service_is_gone() -> None:
    assert local_sight(SnapshotStore(identity=IDENTITY)) is Sight.GONE


def test_disabled_service_is_gone() -> None:
    store = SnapshotStore(identity=IDENTITY)
    store.put_local(ServiceSnapshot(source=SRC, status=ServiceStatus.DISABLED))
    assert local_sight(store) is Sight.GONE


def peer_store(*services: ServiceSnapshot) -> SnapshotStore:
    store = SnapshotStore(identity=IDENTITY)
    state = store.peer("p1", "Brother Server")
    remote = ServerSnapshot(
        server_id="srv-2",
        server_name="Brother Server",
        is_local=False,
        generated_at=datetime.now(UTC),
        services=list(services),
    )
    store.put_peer(state, remote, datetime.now(UTC))
    return store


def peer_issue_snapshot(**update: object) -> ServiceSnapshot:
    failing = Issue(
        fingerprint="f1",
        severity=Severity.ERROR,
        category=IssueCategory.UNREACHABLE,
        source=PEER_SRC,
        title="Radarr is unreachable",
    )
    return ServiceSnapshot(
        source=PEER_SRC,
        status=ServiceStatus.UNREACHABLE,
        issues=[failing],
        fetched_at=datetime.now(UTC),
    ).model_copy(update=update)


def peer_observation(store: SnapshotStore) -> Observation:
    return observe(build_view(store, SCOPE_ALL), store, set(), datetime.now(UTC))


def test_peer_service_issue_is_watched_and_named() -> None:
    store = peer_store(peer_issue_snapshot())
    [item] = peer_observation(store).seen
    assert item.owner == "peer:p1"
    assert item.title == "Brother Server: Radarr is unreachable"


def test_peer_going_down_is_watched_as_itself() -> None:
    store = peer_store()
    state = store.get_peer("p1")
    assert state is not None
    state.status = ServiceStatus.UNREACHABLE
    [item] = peer_observation(store).seen
    assert (item.owner, item.source_key) == ("peer:p1", None)
    assert item.title == "Brother Server is not responding"


def test_stale_issues_from_a_down_peer_are_not_new_problems() -> None:
    store = peer_store(peer_issue_snapshot())
    state = store.get_peer("p1")
    assert state is not None
    state.status = ServiceStatus.UNREACHABLE
    assert [i.source_key for i in peer_observation(store).seen] == [None]


def test_a_peer_aged_out_says_nothing_about_its_issues() -> None:
    store = peer_store(peer_issue_snapshot())
    state = store.get_peer("p1")
    assert state is not None
    state.status = ServiceStatus.UNREACHABLE
    state.last_success_at = datetime.now(UTC) - timedelta(hours=1)
    obs = peer_observation(store)
    assert obs.sight("peer:p1", PEER_SRC.key) is Sight.BLIND


def test_restarted_peer_listing_no_services_yet_says_nothing() -> None:
    obs = peer_observation(peer_store())
    assert obs.sight("peer:p1", PEER_SRC.key) is Sight.BLIND


def test_peer_back_with_the_service_working_is_fresh() -> None:
    working = ServiceSnapshot(source=PEER_SRC, status=ServiceStatus.OK)
    assert peer_observation(peer_store(working)).sight("peer:p1", PEER_SRC.key) is Sight.FRESH


def test_peer_responding_clears_its_own_incident() -> None:
    assert peer_observation(peer_store()).sight("peer:p1", None) is Sight.FRESH


def test_unlinked_peer_is_gone() -> None:
    assert peer_observation(peer_store()).sight("peer:other", None) is Sight.GONE


def test_disabled_service_on_a_peer_is_gone() -> None:
    disabled = ServiceSnapshot(source=PEER_SRC, status=ServiceStatus.DISABLED)
    assert peer_observation(peer_store(disabled)).sight("peer:p1", PEER_SRC.key) is Sight.GONE


# ----- the target ------------------------------------------------------------------------------
class FakeDispatcher:
    def __init__(self) -> None:
        self.sent: list[PushMessage] = []

    def enqueue(self, message: PushMessage) -> None:
        self.sent.append(message)


@pytest.fixture
def sessions(tmp_path: Path) -> sessionmaker[Session]:
    engine = make_engine(f"sqlite:///{tmp_path / 'test.db'}")
    run_migrations(engine)
    return make_sessionmaker(engine)


def subscribe(sessions: sessionmaker[Session]) -> None:
    with session_scope(sessions) as db:
        repository.upsert_subscription(
            db,
            endpoint="https://fcm.googleapis.com/x",
            p256dh="k",
            auth="a",
            label="Phone",
            session_hash=None,
        )


def target(
    store: SnapshotStore,
    sessions: sessionmaker[Session],
    clock: list[datetime],
    dispatcher: FakeDispatcher,
) -> NotifyTarget:
    return NotifyTarget(
        store,
        sessions,
        dispatcher,  # type: ignore[arg-type]
        lambda: {"i1"},
        wall=lambda: clock[0],
    )


def failing_store() -> SnapshotStore:
    store = SnapshotStore(identity=IDENTITY)
    store.put_local(snapshot(ServiceStatus.UNREACHABLE, error="Connection refused"))
    return store


async def test_target_alerts_and_survives_a_restart(sessions: sessionmaker[Session]) -> None:
    subscribe(sessions)
    store = failing_store()
    clock, dispatcher = [START], FakeDispatcher()
    first = target(store, sessions, clock, dispatcher)
    assert await first.run_once(False)
    clock[0] += STARTUP_HOLD + RAISE_AFTER
    assert await first.run_once(False)
    assert [m.title for m in dispatcher.sent] == ["Sonarr is unreachable"]
    assert dispatcher.sent[0].body == "Connection refused"

    again = FakeDispatcher()
    clock[0] += STARTUP_HOLD
    second = target(store, sessions, clock, again)
    second.tracker = NotifyTracker(START)
    assert await second.run_once(False)
    assert again.sent == []


async def test_target_keeps_rows_when_a_write_fails(
    sessions: sessionmaker[Session], monkeypatch: pytest.MonkeyPatch
) -> None:
    subscribe(sessions)
    clock, dispatcher = [START], FakeDispatcher()
    t = target(failing_store(), sessions, clock, dispatcher)
    await t.run_once(False)
    clock[0] += STARTUP_HOLD + RAISE_AFTER

    def fail(*_args: object) -> None:
        raise RuntimeError("database is locked")

    with monkeypatch.context() as patch:
        patch.setattr(repository, "save_incidents", fail)
        assert not await t.run_once(False)
    assert len(dispatcher.sent) == 1  # decided already, so it isn't lost with the write

    clock[0] += timedelta(seconds=15)
    assert await t.run_once(False)
    with session_scope(sessions) as db:
        assert [r.notified for r in repository.load_incidents(db)] == [True]
    assert len(dispatcher.sent) == 1


async def test_target_writes_rows_on_close(sessions: sessionmaker[Session]) -> None:
    clock, dispatcher = [START], FakeDispatcher()
    t = target(failing_store(), sessions, clock, dispatcher)
    await t.run_once(False)
    clock[0] += STARTUP_HOLD + RAISE_AFTER
    await t.run_once(False)
    await t.aclose()
    with session_scope(sessions) as db:
        assert [r.notified for r in repository.load_incidents(db)] == [False]


async def test_a_broken_view_changes_nothing(
    sessions: sessionmaker[Session], monkeypatch: pytest.MonkeyPatch
) -> None:
    subscribe(sessions)
    clock, dispatcher = [START], FakeDispatcher()
    t = target(failing_store(), sessions, clock, dispatcher)
    await t.run_once(False)
    clock[0] += STARTUP_HOLD + RAISE_AFTER

    def fail(*_args: object) -> None:
        raise RuntimeError("bad snapshot")

    monkeypatch.setattr(notify, "build_view", fail)
    assert not await t.run_once(False)
    assert dispatcher.sent == []


async def test_viewer_only_error_does_not_outlive_the_viewer(
    sessions: sessionmaker[Session],
) -> None:
    """InfiniDysk's overview only loads on full polls, so an error there comes and goes."""
    subscribe(sessions)
    store = failing_store()
    clock, dispatcher = [START + STARTUP_HOLD], FakeDispatcher()
    t = target(store, sessions, clock, dispatcher)
    t.tracker = NotifyTracker(START)
    await t.run_once(True)
    clock[0] += RAISE_AFTER
    await t.run_once(True)
    assert len(dispatcher.sent) == 1

    store.put_local(snapshot())  # the viewer left, so idle polls succeed
    clock[0] += timedelta(seconds=15)
    await t.run_once(False)
    clock[0] += CLEAR_AFTER
    await t.run_once(False)
    assert dispatcher.sent[-1].title == "Resolved: Sonarr is unreachable"
