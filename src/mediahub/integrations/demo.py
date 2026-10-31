"""Demo data (``MEDIAHUB_DEMO_MODE=true``).

Generates realistic, slowly changing snapshots through the *real* poller/store/UI path, for
developing the interface and trying MediaHub without any services configured. Also provides
a fake remote peer ("Brother Server") to exercise the multi-server views.
"""

from __future__ import annotations

import hashlib
import math
import random
import time
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from mediahub.integrations.base import (
    Capability,
    ConnectionTestResult,
    InstanceConfig,
    Integration,
    PollResult,
)
from mediahub.models.common import ServiceStatus, ServiceType, SourceRef
from mediahub.models.downloads import (
    ActivityEvent,
    ActivityKind,
    DownloadItem,
    DownloadState,
    StatusMessage,
)
from mediahub.models.infinidysk import (
    ActiveRead,
    CacheStats,
    CircuitState,
    InfiniDyskStats,
    ProblemOutcome,
    ProviderStats,
    ReadProblem,
    health_of,
)
from mediahub.models.issues import Severity
from mediahub.models.playback import (
    HdrType,
    MediaInfo,
    MediaKind,
    NextCheckState,
    NextEpisodeCheck,
    PlaybackSession,
    PlaybackState,
    PlayMethod,
    StreamInfo,
    TranscodeInfo,
)
from mediahub.models.snapshot import HealthCheck, ServerSnapshot, ServiceSnapshot
from mediahub.monitoring.poller import PollTarget
from mediahub.monitoring.store import SnapshotStore
from mediahub.peers.redact import redact_server

_EPOCH = time.time()
GB = 1024**3


def demo_image(title: str) -> str:
    slug = hashlib.sha1(title.encode(), usedforsecurity=False).hexdigest()[:12]
    return f"/img/demo/{slug}.svg?t={title[:40]}"


@dataclass(frozen=True)
class _Play:
    title: str
    subtitle: str | None
    year: int | None
    kind: MediaKind
    user: str
    device: str
    client: str
    duration_s: int
    start_s: int
    method: PlayMethod
    stream: StreamInfo
    transcode: TranscodeInfo | None = None
    paused: bool = False


LOCAL_PLAYS = [
    _Play(
        "The Batman",
        None,
        2022,
        MediaKind.MOVIE,
        "Oshane",
        "NVIDIA Shield",
        "Emby for Android TV",
        10510,
        4355,
        PlayMethod.DIRECT_PLAY,
        StreamInfo(
            container="mkv",
            video_codec="hevc",
            audio_codec="truehd",
            audio_channels=8,
            width=3840,
            height=2160,
            hdr=HdrType.DOLBY_VISION,
            bitrate=58_400_000,
        ),
    ),
    _Play(
        "One Piece",
        "S21E1076 · The Strongest Line of Defense",
        None,
        MediaKind.EPISODE,
        "Kids",
        "iPhone 15",
        "Emby for iOS",
        1410,
        734,
        PlayMethod.TRANSCODE,
        StreamInfo(
            container="mkv",
            video_codec="hevc",
            audio_codec="eac3",
            audio_channels=6,
            width=1920,
            height=1080,
            hdr=HdrType.SDR,
            bitrate=9_800_000,
        ),
        TranscodeInfo(
            container="ts",
            video_codec="h264",
            audio_codec="aac",
            bitrate=6_200_000,
            width=1920,
            height=1080,
            video_direct=False,
            audio_direct=False,
            hw_decode=True,
            hw_encode=True,
            reasons=["VideoCodecNotSupported", "AudioCodecNotSupported"],
        ),
    ),
    _Play(
        "Reacher",
        "S02E06 · New Kids on the Block",
        None,
        MediaKind.EPISODE,
        "Oshane",
        "LG OLED C3",
        "Emby for LG",
        3060,
        1210,
        PlayMethod.DIRECT_STREAM,
        StreamInfo(
            container="mp4",
            video_codec="h264",
            audio_codec="eac3",
            audio_channels=6,
            width=1920,
            height=1080,
            hdr=HdrType.SDR,
            bitrate=7_100_000,
        ),
        paused=True,
    ),
]

REMOTE_PLAYS = [
    _Play(
        "Interstellar",
        None,
        2014,
        MediaKind.MOVIE,
        "Brother",
        "Apple TV 4K",
        "Emby for Apple TV",
        10140,
        2220,
        PlayMethod.DIRECT_PLAY,
        StreamInfo(
            container="mkv",
            video_codec="hevc",
            audio_codec="dts",
            audio_channels=6,
            width=3840,
            height=2160,
            hdr=HdrType.HDR10,
            bitrate=61_200_000,
        ),
    ),
    _Play(
        "The Dark Knight",
        None,
        2008,
        MediaKind.MOVIE,
        "Marcus",
        "Chrome",
        "Emby Web",
        9120,
        5100,
        PlayMethod.TRANSCODE,
        StreamInfo(
            container="mkv",
            video_codec="hevc",
            audio_codec="truehd",
            audio_channels=8,
            width=3840,
            height=2160,
            hdr=HdrType.HDR10,
            bitrate=48_000_000,
        ),
        TranscodeInfo(
            container="ts",
            video_codec="h264",
            audio_codec="aac",
            bitrate=12_000_000,
            width=1920,
            height=1080,
            video_direct=False,
            audio_direct=False,
            hw_decode=True,
            hw_encode=True,
            tonemapping=True,
            reasons=["VideoRangeNotSupported", "ContainerBitrateExceedsLimit"],
        ),
    ),
]


@dataclass(frozen=True)
class _Dl:
    title: str
    subtitle: str | None
    size_gb: float
    start_pct: float
    rate_mbs: float
    state: DownloadState
    raw_status: str
    tracked_state: str | None = None
    tracked_status: str | None = "ok"
    client: str = "SABnzbd"
    protocol: str = "usenet"
    error: str | None = None
    messages: tuple[StatusMessage, ...] = ()


SONARR_LOCAL = [
    _Dl(
        "The Last of Us",
        "S02E03 · The Path",
        4.2,
        52,
        12.4,
        DownloadState.DOWNLOADING,
        "downloading",
        "downloading",
    ),
    _Dl(
        "House of the Dragon",
        "S02E08 · The Queen Who Ever Was",
        6.8,
        100,
        0,
        DownloadState.IMPORT_BLOCKED,
        "completed",
        "importBlocked",
        "warning",
        messages=(
            StatusMessage(
                title="House.of.the.Dragon.S02E08.2160p.WEB-DL.mkv",
                messages=[
                    "Not an upgrade for existing episode file(s). Existing quality: "
                    "WEBDL-2160p. New Quality WEBDL-2160p."
                ],
            ),
        ),
    ),
    _Dl(
        "Shōgun",
        "S01E10 · A Dream of a Dream",
        3.1,
        100,
        0,
        DownloadState.AWAITING_IMPORT,
        "completed",
        "importPending",
    ),
]
RADARR_LOCAL = [
    _Dl(
        "Dune: Part Two",
        "2024",
        43.2,
        31,
        38.6,
        DownloadState.DOWNLOADING,
        "downloading",
        "downloading",
    ),
    _Dl(
        "Godzilla x Kong: The New Empire",
        "2024",
        21.7,
        0,
        0,
        DownloadState.WAITING,
        "queued",
        "downloading",
    ),
    _Dl(
        "Civil War",
        "2024",
        18.9,
        64,
        0,
        DownloadState.STALLED,
        "warning",
        "downloading",
        client="qBittorrent",
        protocol="torrent",
        error="The download is stalled with no connections",
    ),
]
SONARR_REMOTE = [
    _Dl(
        "Fallout",
        "S01E08 · The Beginning",
        5.3,
        80,
        21.0,
        DownloadState.DOWNLOADING,
        "downloading",
        "downloading",
    ),
]
RADARR_REMOTE: list[_Dl] = []

HISTORY_LOCAL = {
    ServiceType.SONARR: [
        ("Fallout", "S01E08", ActivityKind.IMPORTED, 4),
        ("Shōgun", "S01E10", ActivityKind.GRABBED, 6),
        ("3 Body Problem", "S01E05", ActivityKind.IMPORTED, 8),
        ("House of the Dragon", "S02E08", ActivityKind.GRABBED, 9),
    ],
    ServiceType.RADARR: [
        ("The Fall Guy", "2024", ActivityKind.IMPORTED, 3),
        ("Civil War", "2024", ActivityKind.GRABBED, 6),
        ("Bad Boys: Ride or Die", "2024", ActivityKind.FAILED, 11),
    ],
}


def _session(play: _Play, src: SourceRef, idx: int) -> PlaybackSession:
    elapsed = 0 if play.paused else time.time() - _EPOCH
    position = (play.start_s + elapsed) % play.duration_s
    progress = position / play.duration_s * 100
    transcode = play.transcode
    if transcode is not None:
        transcode = transcode.model_copy(update={"progress_pct": min(100.0, progress + 6)})
    return PlaybackSession(
        source=src,
        session_id=f"demo-{src.instance_id}-{idx}",
        user=play.user,
        device=play.device,
        client=play.client,
        client_version="4.9.1",
        remote_address="192.168.1.42",
        file_name=None if play.kind is MediaKind.LIVE_TV else f"{_demo_file(play)}.strm",
        state=PlaybackState.PAUSED if play.paused else PlaybackState.PLAYING,
        position_s=position,
        duration_s=play.duration_s,
        media=MediaInfo(
            title=play.title,
            subtitle=play.subtitle,
            year=play.year,
            kind=play.kind,
            image_url=demo_image(play.title),
        ),
        play_method=play.method,
        source_stream=play.stream,
        transcode=transcode,
    )


def _download(dl: _Dl, src: SourceRef, idx: int) -> DownloadItem:
    size = int(dl.size_gb * GB)
    done_pct = dl.start_pct
    if dl.state is DownloadState.DOWNLOADING and dl.rate_mbs:
        gained = (time.time() - _EPOCH) * dl.rate_mbs * 1024**2 / size * 100
        done_pct = (dl.start_pct + gained) % 100
    remaining = int(size * (1 - done_pct / 100))
    eta = remaining / (dl.rate_mbs * 1024**2) if dl.rate_mbs else None
    return DownloadItem(
        source=src,
        item_id=f"{src.instance_id}-{idx}",
        download_id=f"demo{idx}",
        title=dl.title,
        subtitle=dl.subtitle,
        release_title=f"{dl.title.replace(' ', '.')}.2160p.WEB-DL.DDP5.1.H.265",
        image_url=demo_image(dl.title),
        protocol=dl.protocol,
        client=dl.client,
        indexer="NZBgeek" if dl.protocol == "usenet" else "Torznab",
        size_bytes=size,
        remaining_bytes=remaining,
        eta_s=eta,
        added_at=datetime.now(UTC) - timedelta(minutes=17 * (idx + 1)),
        state=dl.state,
        raw_status=dl.raw_status,
        raw_tracked_state=dl.tracked_state,
        raw_tracked_status=dl.tracked_status,
        messages=list(dl.messages),
        error_message=dl.error,
    )


def _history(service: ServiceType, src: SourceRef) -> list[ActivityEvent]:
    events = []
    for i, (title, sub, kind, hours) in enumerate(HISTORY_LOCAL.get(service, [])):
        events.append(
            ActivityEvent(
                source=src,
                event_id=f"{src.instance_id}-h{i}",
                kind=kind,
                title=title,
                subtitle=sub,
                image_url=demo_image(title),
                at=datetime.now(UTC) - timedelta(hours=hours),
                detail="Download client reported an error" if kind is ActivityKind.FAILED else None,
            )
        )
    return events


def _demo_file(play: _Play) -> str:
    return f"{play.title.replace(' ', '.')}.2160p.mkv"


def _demo_problems(kind: int, file_name: str) -> list[ReadProblem]:
    """0 clean, 1 recovered, 2 failed: the demo shows every health state."""
    # Fixed times: a problem that moved with each poll would count as a new one each poll.
    now = datetime.fromtimestamp(_EPOCH, UTC)
    path = f'"/content/movies/{file_name}"'
    if kind == 1:
        return [
            ReadProblem(
                at=now - timedelta(minutes=4),
                outcome=ProblemOutcome.RECOVERED,
                message=f'Article "a1b2c3@news.example" missing on all providers while reading '
                f'"{file_name}". Filling the 768000-byte gap to preserve later file offsets.',
            )
        ]
    if kind == 2:
        return [
            ReadProblem(
                at=now - timedelta(minutes=6),
                outcome=ProblemOutcome.RECOVERED,
                message=f'Article "d4e5f6@news.example" missing on all providers while reading '
                f'"{file_name}". Filling the 768000-byte gap to preserve later file offsets.',
            ),
            ReadProblem(
                at=now - timedelta(minutes=1),
                outcome=ProblemOutcome.FAILED,
                message=f"File {path} has missing articles: Article with message-id "
                "d4e5f6@news.example not found.",
            ),
        ]
    return []


def demo_play_rows(now: datetime) -> list[dict[str, Any]]:
    """A month of finished plays for the Play history tab; the live ones are recorded as usual."""
    plays = [
        ("Dune: Part Two", 2024, 2, 0.1, 0.92),
        ("Severance", None, 1, 5, 0.31),
        ("Oppenheimer", 2023, 0, 9, 1.0),
        ("The Bear", None, 2, 26, 0.07),
        ("Blade Runner 2049", 2017, 1, 50, 0.64),
        ("Andor", None, 0, 75, 1.0),
        ("Arrival", 2016, 2, 200, 0.45),
        ("Shogun", None, 1, 400, 0.88),
    ]
    rows = []
    for i, (title, year, kind, hours_ago, watched) in enumerate(plays):
        file_name = f"{title.replace(' ', '.').replace(':', '')}.2160p.mkv"
        started = now - timedelta(hours=hours_ago)
        problems = [
            p.model_copy(update={"at": started + timedelta(minutes=10 + 8 * n)})
            for n, p in enumerate(_demo_problems(kind, file_name))
        ]
        ended = started + timedelta(minutes=int(120 * watched))
        rows.append(
            {
                "id": f"00000000-0000-4000-8000-{i:012d}",
                "instance_id": "demo-emby",
                "instance_name": "Emby",
                "session_id": f"demo-history-{i}",
                "item_key": f"demo-history-{i}",
                "title": title,
                "subtitle": None if year else f"S01E0{i + 1}",
                "year": year,
                "kind": "movie" if year else "episode",
                "user": ("Oshane", "Kids", "Marcus")[i % 3],
                "device": ("NVIDIA Shield", "iPhone 15", "LG OLED C3")[i % 3],
                "file_name": file_name,
                "started_at": started,
                "last_seen_at": ended,
                "ended_at": ended,
                "position_s": 7200.0 * watched,
                "duration_s": 7200.0,
                "outcome": health_of(problems).value,
                "problem_count": len(problems),
                "problems": [p.model_dump(mode="json") for p in problems],
                "incomplete": False,
            }
        )
    return rows


def _infinidysk(seed: int, plays: list[_Play], src: SourceRef) -> InfiniDyskStats:
    rng = random.Random(seed + int(time.time() // 10))
    t = time.time() / 30
    base = 70 + 25 * math.sin(t)
    throughput = [
        max(5.0, base + 18 * math.sin(t - i / 3) + rng.uniform(-8, 8)) * 1024**2
        for i in range(30, 0, -1)
    ]
    reads = [
        ActiveRead(
            read_id=f"r{i}",
            file_name=_demo_file(p),
            path=f"/content/movies/{_demo_file(p)}",
            client_ip="172.18.0.5",
            client_label="Emby",
            started_at=datetime.now(UTC) - timedelta(minutes=12 + 7 * i),
            last_activity_at=datetime.now(UTC),
            bytes_read=int((18.4 - 6 * i) * GB),
            file_size=int((29.7 - 5 * i) * GB),
            offset_pct=62.0 - 13 * i,
            speed_bps=(95 - 17 * i) * 1024**2,
            providers=["Newshosting", "Eweka"] if i else ["Newshosting"],
            problems=_demo_problems((i + seed) % 3, _demo_file(p)),
        )
        for i, p in enumerate(plays)
        if p.method is not PlayMethod.UNKNOWN and not p.paused
    ]
    return InfiniDyskStats(
        healthy=True,
        ready=True,
        live=True,
        active_reads=len(reads),
        articles_per_min=1830 + rng.uniform(-90, 90),
        errors_per_min=rng.choice([0, 0, 0, 1, 2]),
        bytes_served_per_min=throughput[-1] * 60,
        throughput=throughput,
        latency_p50_ms=41 + rng.uniform(-4, 4),
        latency_p95_ms=118 + rng.uniform(-10, 10),
        latency_p99_ms=262 + rng.uniform(-20, 20),
        providers=[
            ProviderStats(
                name="Newshosting",
                circuit=CircuitState.CLOSED,
                speed_mbps=84.2,
                error_rate=0.002,
                connections_active=18,
                connections_max=50,
            ),
            ProviderStats(
                name="Eweka",
                circuit=CircuitState.CLOSED,
                speed_mbps=21.7,
                error_rate=0.011,
                connections_active=4,
                connections_max=20,
                saves=37,
                last_failure="430 No Such Article",
            ),
        ],
        cache=CacheStats(
            used_bytes=245 * GB,
            max_bytes=int(1.2 * 1024 * GB),
            entries=412_093,
            hit_rate=0.62 + rng.uniform(-0.03, 0.03),
        ),
        reads=reads,
        articles_recovered=41,
        queue_count=42 if seed == 1 else 18,
    )


class DemoIntegration(Integration):
    """Stands in for any service type in demo mode."""

    capabilities = frozenset(Capability)
    default_interval_s = 5

    def __init__(self, config: InstanceConfig, source: SourceRef, *, remote: bool = False) -> None:
        super().__init__(config, source)
        self.service_type = config.service_type  # type: ignore[misc]
        self._remote = remote

    async def test_connection(self) -> ConnectionTestResult:
        result = ConnectionTestResult(ok=True, version="demo")
        result.add("Demo instance", True, "Generated data")
        return result

    async def poll(self, *, full: bool) -> PollResult:
        src = self.source
        st = self.config.service_type
        if st is ServiceType.EMBY:
            plays = REMOTE_PLAYS if self._remote else LOCAL_PLAYS
            return PollResult(
                version="4.9.1.80",
                sessions=[_session(p, src, i) for i, p in enumerate(plays)],
            )
        if st in (ServiceType.SONARR, ServiceType.RADARR):
            if st is ServiceType.SONARR:
                queue = SONARR_REMOTE if self._remote else SONARR_LOCAL
            else:
                queue = RADARR_REMOTE if self._remote else RADARR_LOCAL
            checks = []
            if st is ServiceType.SONARR and not self._remote:
                checks.append(
                    HealthCheck(
                        source="IndexerLongTermStatusCheck",
                        level=Severity.WARNING,
                        message="Indexers unavailable due to failures for more than 6 hours: "
                        "DrunkenSlug",
                        wiki_url="https://wiki.servarr.com/sonarr/system#indexers-are-unavailable-due-to-failures",
                    )
                )
            return PollResult(
                version="4.0.20.3014" if st is ServiceType.SONARR else "6.4.4.10685",
                health_checks=checks,
                downloads=[_download(d, src, i) for i, d in enumerate(queue)],
                history=[] if self._remote else _history(st, src),
            )
        plays = REMOTE_PLAYS if self._remote else LOCAL_PLAYS
        return PollResult(
            version="1.6.3",
            infinidysk=_infinidysk(2 if self._remote else 1, plays, src),
        )


DEMO_LOCAL = [
    (ServiceType.EMBY, "Emby"),
    (ServiceType.SONARR, "Sonarr"),
    (ServiceType.RADARR, "Radarr"),
    (ServiceType.INFINIDYSK, "InfiniDysk"),
]

DEMO_PEER_ID = "demo-peer-brother"
DEMO_PEER_SERVER_ID = "00000000-0000-4000-8000-00000000b0b0"
DEMO_PEER_NAME = "Brother Server"


def demo_local_configs() -> list[InstanceConfig]:
    return [
        InstanceConfig(id=f"demo-{st.value}", service_type=st, name=name, base_url="demo://")
        for st, name in DEMO_LOCAL
    ]


class DemoPeerTarget(PollTarget):
    """A fake remote MediaHub peer, produced locally."""

    key = DEMO_PEER_ID

    def __init__(self, store: SnapshotStore) -> None:
        super().__init__()
        self._store = store

    def interval(self, active: bool) -> float:
        return 10.0 if active else 60.0

    async def run_once(self, active: bool) -> bool:
        now = datetime.now(UTC)
        services = []
        for st, name in DEMO_LOCAL:
            src = SourceRef(
                server_id=DEMO_PEER_SERVER_ID,
                server_name=DEMO_PEER_NAME,
                instance_id=f"demo-remote-{st.value}",
                instance_name=name,
                service_type=st,
                is_remote=True,
            )
            cfg = InstanceConfig(id=src.instance_id, service_type=st, name=name, base_url="demo://")
            result = await DemoIntegration(cfg, src, remote=True).poll(full=True)
            services.append(
                ServiceSnapshot(
                    source=src,
                    status=ServiceStatus.OK,
                    version=result.version,
                    checked_at=now,
                    fetched_at=now,
                    poll_interval_s=self.interval(active),
                    sessions=result.sessions or [],
                    downloads=result.downloads or [],
                    history=result.history or [],
                    infinidysk=result.infinidysk,
                )
            )
        state = self._store.peer(DEMO_PEER_ID, DEMO_PEER_NAME)
        state.status = ServiceStatus.OK
        state.error = None
        state.last_success_at = now
        state.snapshot = redact_server(
            ServerSnapshot(
                server_id=DEMO_PEER_SERVER_ID,
                server_name=DEMO_PEER_NAME,
                is_local=False,
                generated_at=now,
                services=services,
            )
        )
        return True


# What the next-episode check would show for the demo's episodes (by series title).
DEMO_NEXT_CHECKS = {
    "One Piece": NextEpisodeCheck(
        state=NextCheckState.CHECKING, code="S21E1077", name="Luffy's Dream", progress=40
    ),
    "Reacher": NextEpisodeCheck(
        state=NextCheckState.REPAIRED_PAR2,
        code="S02E07",
        name="Hard Rain",
    ),
}


class DemoNextEpisodeTarget(PollTarget):
    """Sample next-episode check outcomes on the demo's local Emby episodes."""

    key = "demo-next-episode"

    def __init__(self, store: SnapshotStore) -> None:
        super().__init__()
        self._store = store

    def interval(self, active: bool) -> float:
        return 10.0 if active else 60.0

    async def run_once(self, active: bool) -> bool:
        for snapshot in self._store.local_snapshots():
            if not snapshot.source.instance_id.startswith("demo-"):
                continue
            for session in snapshot.sessions:
                check = DEMO_NEXT_CHECKS.get(session.media.title)
                if check is not None and session.media.kind is MediaKind.EPISODE:
                    key = (snapshot.source.instance_id, session.session_id, session.item_id or "")
                    self._store.put_next_check(key, check)
        return True
