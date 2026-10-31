"""Normalized InfiniDysk streaming/cache statistics."""

from __future__ import annotations

from collections.abc import Iterable
from datetime import datetime
from enum import StrEnum

from pydantic import computed_field

from mediahub.models.common import Model


class CircuitState(StrEnum):
    CLOSED = "closed"
    OPEN = "open"
    HALF_OPEN = "half_open"
    UNKNOWN = "unknown"


class StreamHealth(StrEnum):
    OK = "ok"
    WARNING = "warning"
    RECOVERED = "recovered"
    FAILED = "failed"

    @property
    def label(self) -> str:
        return {
            "ok": "No problems reported",
            "warning": "Warning",
            "recovered": "Recovered",
            "failed": "Playback problem",
        }[self.value]

    @property
    def tone(self) -> str:
        return {"ok": "success", "failed": "error"}.get(self.value, "warning")

    @property
    def icon(self) -> str:
        return {"ok": "circle-check", "failed": "circle-x"}.get(self.value, "triangle-alert")


class ProblemOutcome(StrEnum):
    RECOVERED = "recovered"  # InfiniDysk filled a gap; playback went on
    RETRIED = "retried"  # InfiniDysk answered 503 so the player retries
    REPAIR = "repair"  # InfiniDysk started repairing the file
    WARNING = "warning"  # another warning naming the file
    FAILED = "failed"  # the read was aborted or returned an error to the player

    @property
    def label(self) -> str:
        return {
            "recovered": "Gap filled",
            "retried": "Retried",
            "repair": "Repair",
            "warning": "Warning",
            "failed": "Failed",
        }[self.value]


class ReadProblem(Model):
    """One InfiniDysk log line about a file being read, kept verbatim."""

    at: datetime
    outcome: ProblemOutcome
    message: str


class ProviderStats(Model):
    name: str
    circuit: CircuitState = CircuitState.UNKNOWN
    speed_mbps: float | None = None
    error_rate: float | None = None  # 0..1
    last_failure: str | None = None  # verbatim from InfiniDysk
    connections_active: int | None = None
    connections_max: int | None = None
    over_limit: bool = False  # reached its configured byte limit
    saves: int | None = None  # articles it served after another provider missed them (1 h)


class CacheStats(Model):
    enabled: bool = True
    used_bytes: int | None = None
    max_bytes: int | None = None
    entries: int | None = None
    hit_rate: float | None = None  # 0..1, computed from hit/miss counters

    @computed_field  # type: ignore[prop-decorator]
    @property
    def used_pct(self) -> float | None:
        if not self.max_bytes or self.used_bytes is None:
            return None
        return min(100.0, self.used_bytes / self.max_bytes * 100)


def health_of(problems: Iterable[ReadProblem]) -> StreamHealth:
    outcomes = {p.outcome for p in problems}
    if ProblemOutcome.FAILED in outcomes:
        return StreamHealth.FAILED
    if outcomes - {ProblemOutcome.WARNING}:
        return StreamHealth.RECOVERED
    return StreamHealth.WARNING if outcomes else StreamHealth.OK


class ActiveRead(Model):
    read_id: str
    file_name: str
    path: str | None = None  # stripped before data leaves this server
    client_ip: str | None = None  # stripped before data leaves this server
    client_label: str | None = None  # e.g. "Emby" derived from the user agent
    started_at: datetime | None = None
    last_activity_at: datetime | None = None
    bytes_read: int | None = None
    file_size: int | None = None
    offset_pct: float | None = None
    speed_bps: float | None = None
    # InfiniDysk stopped tracking it (no data for 15 s); the player may still be playing
    # from its buffer. Kept for a while so buffered playback doesn't vanish from the list.
    idle: bool = False
    providers: list[str] = []  # providers that served segments of this read
    problems: list[ReadProblem] = []  # newest last; from InfiniDysk's log (see infinidysk.logs)

    @computed_field  # type: ignore[prop-decorator]
    @property
    def health(self) -> StreamHealth:
        return health_of(self.problems)


class InfiniDyskStats(Model):
    healthy: bool | None = None
    ready: bool | None = None
    active_reads: int = 0
    articles_per_min: float | None = None
    errors_per_min: float | None = None
    bytes_served_per_min: float | None = None
    throughput: list[float] = []  # bytes/s buckets, oldest first, for sparklines
    latency_p50_ms: float | None = None
    latency_p95_ms: float | None = None
    latency_p99_ms: float | None = None
    providers: list[ProviderStats] = []
    cache: CacheStats | None = None
    reads: list[ActiveRead] = []
    # Files read in the last hours that InfiniDysk isn't reading now: a player can play from
    # its buffer for many minutes. Used to link Emby streams to their file; not listed.
    recent_reads: list[ActiveRead] = []
    live: bool = False  # reads come from the live feed (backend WebSocket)
    feed_error: str | None = None  # why the live feed isn't connected, when configured
    queue_count: int | None = None
    articles_recovered: int | None = None  # rescued by another provider, overview window (1 h)

    @computed_field  # type: ignore[prop-decorator]
    @property
    def throughput_bps(self) -> float | None:
        return self.bytes_served_per_min / 60 if self.bytes_served_per_min is not None else None
