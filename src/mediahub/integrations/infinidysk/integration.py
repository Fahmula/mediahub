"""InfiniDysk integration.

InfiniDysk exposes two HTTP surfaces:

* the frontend (port 3000) proxies ``/api/*`` and accepts the user API key (``x-api-key``);
* the backend (port 8080, internal) serves ``/health``, ``/ready``, ``/metrics`` and the
  ``/ws`` WebSocket, which only accepts the internal ``FRONTEND_BACKEND_API_KEY``.

Several InfiniDysk GET routes change state (backups, queue resets, SAB pause/delete), so
every request goes through :meth:`InfiniDyskIntegration._get`, which enforces a read-only
allowlist. The one exception is :meth:`InfiniDyskIntegration.recheck_file` (``POST
/api/recheck-file``), used only by the opt-in next-episode check: it queues one file's health
check, the same request InfiniDysk's own Files page sends. That check then follows the file
through the read-only active-checks and history routes.

Every poll reads provider usage (frontend) and, with a backend URL, ``/health``, ``/ready`` and
``/metrics``: cheap, and enough for issues (circuits, byte limits, health). While someone is
looking, polls add the overview statistics (at most every 30 s, like InfiniDysk's own UI;
the query is not free). The live WebSocket feed is kept open all the time. See ``mapper.py``.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Any, ClassVar
from urllib.parse import urlsplit, urlunsplit
from uuid import UUID

import httpx
from websockets.asyncio.client import connect as ws_connect
from websockets.exceptions import ConnectionClosed, InvalidHandshake, InvalidStatus

from mediahub.integrations.base import (
    Capability,
    ConnectionTestResult,
    FieldSpec,
    InstanceConfig,
    Integration,
    PollResult,
)
from mediahub.integrations.errors import ErrorKind, IntegrationError
from mediahub.integrations.http import USER_AGENT, get_json, make_client, request
from mediahub.integrations.infinidysk.mapper import build_stats
from mediahub.integrations.infinidysk.metrics import Metrics
from mediahub.integrations.infinidysk.ws import LiveFeed
from mediahub.logging import get_logger
from mediahub.models.common import ServiceStatus, ServiceType, SourceRef
from mediahub.models.issues import Severity
from mediahub.models.snapshot import HealthCheck

log = get_logger(__name__)

# Read-only routes only. Anything else is refused before a request is made.
ACTIVE_CHECKS_PATH = "/api/get-active-health-checks"
CHECK_HISTORY_PATH = "/api/get-health-check-history"
FRONTEND_READ_PATHS = frozenset(
    {
        "/api/get-overview-stats",
        "/api/get-provider-usage",
        "/api/get-arr-health",
        "/api/get-logs",
        ACTIVE_CHECKS_PATH,
        CHECK_HISTORY_PATH,
    }
)
SAB_READ_MODES = frozenset({"queue", "history", "warnings", "status", "server_stats"})
BACKEND_READ_PATHS = frozenset({"/health", "/ready", "/metrics"})
# The only write MediaHub sends (see the module docstring). Never reachable through _get.
RECHECK_PATH = "/api/recheck-file"

OVERVIEW_PARAMS = {"window": "1h", "sections": "window,detail"}
# Once per feed connect: problems logged before MediaHub was watching (see logs.py).
LOG_BACKFILL_PARAMS = {"levels": "Information,Warning,Error,Fatal", "limit": "2000"}
OVERVIEW_EVERY_S = 30.0
OVERVIEW_MAX_AGE_S = 90.0  # keep showing the last overview through a failed refresh
# Newest health-check results read per look (newest first; InfiniDysk allows up to 250).
CHECK_HISTORY_PAGE = 100


def assert_read_only(path: str, params: dict[str, Any] | None, *, backend: bool) -> None:
    if backend:
        allowed = path in BACKEND_READ_PATHS
    elif path == "/api":
        p = params or {}
        # SAB "queue"/"history" become deletes when given name=delete.
        allowed = p.get("mode") in SAB_READ_MODES and "name" not in p and "value" not in p
    else:
        allowed = path in FRONTEND_READ_PATHS
    if not allowed:
        raise IntegrationError(ErrorKind.PROTOCOL, f"Refusing non-allowlisted request to {path}")


@dataclass(frozen=True)
class RecheckOutcome:
    """InfiniDysk's answer to a recheck request.

    ``state`` is ``queued``, ``already-queued`` or ``already-running`` when accepted (202),
    else None with InfiniDysk's ``error`` text, e.g. "File no longer exists." (404).
    """

    state: str | None
    error: str | None = None
    not_found: bool = False


@dataclass(frozen=True)
class ActiveCheck:
    """A health check InfiniDysk is running now. ``phase``: "Checking" or "Repairing"."""

    phase: str | None
    progress: int | None  # percent


@dataclass(frozen=True)
class CheckResult:
    """A finished health check from InfiniDysk's history.

    ``result``: 0 healthy, 1 unhealthy, 2 degraded. ``repair``: 0 none, 1 repaired, 2 deleted,
    3 action needed, 4 repaired via PAR2 (InfiniDysk's ``HealthCheckResult`` enums).
    """

    dav_item_id: UUID
    at: datetime
    result: int
    repair: int
    message: str | None


def _items(data: Any, path: str) -> list[dict[str, Any]]:
    items = data.get("items") if isinstance(data, dict) else None
    if not isinstance(items, list):
        raise IntegrationError(ErrorKind.PROTOCOL, f"Unexpected reply from {path}")
    return [i for i in items if isinstance(i, dict)]


def _uuid(value: Any) -> UUID | None:
    try:
        return UUID(value) if isinstance(value, str) else None
    except ValueError:
        return None


def ws_url(backend_url: str) -> str:
    parts = urlsplit(backend_url)
    scheme = "wss" if parts.scheme == "https" else "ws"
    return urlunsplit((scheme, parts.netloc, parts.path.rstrip("/") + "/ws", "", ""))


class InfiniDyskIntegration(Integration):
    service_type = ServiceType.INFINIDYSK
    capabilities = frozenset({Capability.STREAM_STATS})
    default_interval_s = 10
    default_url = "http://infinidysk:3000"
    secret_fields: ClassVar[tuple[FieldSpec, ...]] = (
        FieldSpec(
            "api_key",
            "API key",
            secret=True,
            required=True,
            help="InfiniDysk → Settings → SABnzbd → API Key.",
        ),
        FieldSpec(
            "internal_key",
            "Internal API key",
            secret=True,
            help="The FRONTEND_BACKEND_API_KEY from InfiniDysk's container settings. Pin it "
            "there so it doesn't change on restart. Enables the live stream list.",
        ),
    )
    option_fields: ClassVar[tuple[FieldSpec, ...]] = (
        FieldSpec(
            "backend_url",
            "Backend URL",
            kind="url",
            placeholder="http://infinidysk:8080",
            help="InfiniDysk's internal backend port (8080). Needed for health, cache metrics "
            "and live streams. Put both containers on the same Docker network.",
        ),
    )

    def __init__(self, config: InstanceConfig, source: SourceRef) -> None:
        super().__init__(config, source)
        verify = config.options.get("verify_tls", True)
        headers = {"x-api-key": config.secrets.get("api_key", "")}
        self._client = make_client(config.base_url, headers=headers, verify_tls=verify)
        backend_url = config.options.get("backend_url")
        self._backend_url: str | None = backend_url or None
        self._backend = (
            make_client(backend_url, headers=headers, verify_tls=verify) if backend_url else None
        )
        internal_key = config.secrets.get("internal_key")
        self._feed = (
            LiveFeed(ws_url(backend_url), internal_key) if backend_url and internal_key else None
        )
        self._overview: dict[str, Any] | None = None
        self._overview_at = 0.0

    async def _get(self, path: str, params: dict[str, Any] | None = None) -> Any:
        assert_read_only(path, params, backend=False)
        return await get_json(self._client, path, params=params)

    async def recheck_file(self, dav_item_id: UUID) -> RecheckOutcome:
        """Ask InfiniDysk to health-check one file soon. Raises ``IntegrationError``."""
        response = await request(
            self._client,
            "POST",
            RECHECK_PATH,
            accept=(202, 404, 409),
            # A multipart form with exactly one plain field (no filename): what InfiniDysk accepts.
            files={"davItemId": (None, str(dav_item_id))},
        )
        try:
            body = response.json()
        except (json.JSONDecodeError, UnicodeDecodeError):
            body = None
        if not isinstance(body, dict):
            if response.status_code == 404:  # not InfiniDysk's JSON 404: a wrong base URL
                raise IntegrationError(
                    ErrorKind.PROTOCOL, f"Not found: {RECHECK_PATH} — check the base URL"
                )
            raise IntegrationError(
                ErrorKind.PROTOCOL,
                f"Unexpected reply from {RECHECK_PATH} (HTTP {response.status_code})",
            )
        if response.status_code == 202:
            state = body.get("state")
            return RecheckOutcome(state=state if isinstance(state, str) else "queued")
        error = body.get("error")
        return RecheckOutcome(
            state=None,
            error=error if isinstance(error, str) and error else f"HTTP {response.status_code}",
            not_found=response.status_code == 404,
        )

    async def active_checks(self) -> dict[UUID, ActiveCheck]:
        """Health checks running now, by InfiniDysk item id. Raises ``IntegrationError``."""
        data = await self._get(ACTIVE_CHECKS_PATH)
        out: dict[UUID, ActiveCheck] = {}
        for item in _items(data, ACTIVE_CHECKS_PATH):
            item_id = _uuid(item.get("id"))
            if item_id is None:
                continue
            phase, progress = item.get("phase"), item.get("progress")
            out[item_id] = ActiveCheck(
                phase=phase if isinstance(phase, str) else None,
                progress=progress if isinstance(progress, int) else None,
            )
        return out

    async def check_results(self) -> list[CheckResult]:
        """The newest finished health checks, newest first. Raises ``IntegrationError``."""
        data = await self._get(CHECK_HISTORY_PATH, {"pageSize": str(CHECK_HISTORY_PAGE)})
        out: list[CheckResult] = []
        for item in _items(data, CHECK_HISTORY_PATH):
            item_id = _uuid(item.get("davItemId"))
            result, repair = item.get("result"), item.get("repairStatus")
            try:
                at = datetime.fromisoformat(str(item.get("createdAt")))
            except ValueError:
                continue
            if item_id is None or not isinstance(result, int) or not isinstance(repair, int):
                continue
            message = item.get("message")
            out.append(
                CheckResult(
                    dav_item_id=item_id,
                    at=at,
                    result=result,
                    repair=repair,
                    message=message if isinstance(message, str) and message else None,
                )
            )
        return out

    async def _backend_text(self, path: str) -> str:
        assert self._backend is not None
        assert_read_only(path, None, backend=True)
        response = await request(self._backend, "GET", path, headers={"Accept": "text/plain"})
        return response.text

    async def _backend_status(self, path: str) -> bool:
        """``/health`` and ``/ready`` return 503 with 'Unhealthy' when failing."""
        assert self._backend is not None
        assert_read_only(path, None, backend=True)
        try:
            response = await self._backend.get(path, headers={"Accept": "text/plain"})
        except httpx.TransportError as exc:
            raise IntegrationError(ErrorKind.UNREACHABLE, "Backend URL unreachable") from exc
        return response.status_code == 200 and "unhealthy" not in response.text.lower()

    async def _check_websocket(self) -> tuple[bool, str]:
        key = self.config.secrets.get("internal_key")
        if not key or not self._backend_url:
            return False, "Not configured"
        try:
            async with ws_connect(
                ws_url(self._backend_url),
                open_timeout=4,
                close_timeout=1,
                user_agent_header=USER_AGENT,
            ) as ws:
                await ws.send(key)
                # Authenticated and idle is fine: InfiniDysk only pushes after a subscription.
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(ws.recv(), timeout=2)
                return True, "Live stream feed available"
        except ConnectionClosed as exc:
            code = exc.rcvd.code if exc.rcvd else None
            return (
                False,
                f"Internal key rejected (connection closed{f', code {code}' if code else ''})",
            )
        except InvalidStatus as exc:
            return False, f"WebSocket refused (HTTP {exc.response.status_code})"
        except (InvalidHandshake, OSError, TimeoutError):
            return False, "Could not open the WebSocket on the backend URL"

    async def test_connection(self) -> ConnectionTestResult:
        result = ConnectionTestResult(ok=False)
        try:
            usage = await self._get("/api/get-provider-usage")
        except IntegrationError as exc:
            label = "API key accepted" if exc.kind is ErrorKind.AUTH else "Reach InfiniDysk"
            result.add(label, False, exc.message)
            return result
        providers = usage.get("providers") if isinstance(usage, dict) else None
        if not isinstance(providers, list):
            result.add("Reach InfiniDysk", False, "Response doesn't look like InfiniDysk")
            return result
        result.add("Reach InfiniDysk", True)
        result.add("API key accepted", True, f"{len(providers)} Usenet provider(s) configured")
        result.ok = True

        if self._backend is None:
            result.add("Backend URL", False, "Not set: health and cache metrics unavailable")
            return result
        try:
            healthy = await self._backend_status("/health")
            result.add("Backend health", healthy, "Healthy" if healthy else "Unhealthy")
        except IntegrationError as exc:
            result.add("Backend health", False, exc.message)
            return result
        try:
            metrics = await self._backend_text("/metrics")
            series = sum(1 for line in metrics.splitlines() if line.startswith("nzbdav_"))
            result.add("Prometheus metrics", series > 0, f"{series} series")
        except IntegrationError as exc:
            result.add("Prometheus metrics", False, exc.message)
        ok, detail = await self._check_websocket()
        result.add("Live stream feed", ok, detail)
        return result

    async def poll(self, *, full: bool) -> PollResult:
        usage = await self._get("/api/get-provider-usage")  # also reachability + auth
        checks: list[HealthCheck] = []
        healthy = ready = None
        metrics = None
        if self._backend is not None:
            healthy, ready = await asyncio.gather(
                self._backend_status("/health"), self._backend_status("/ready")
            )
            if not healthy:
                checks.append(
                    HealthCheck(level=Severity.ERROR, message="Backend reports Unhealthy")
                )
            if not ready:
                checks.append(
                    HealthCheck(level=Severity.WARNING, message="Streaming readiness check failing")
                )
            try:
                metrics = Metrics.from_text(await self._backend_text("/metrics"))
            except IntegrationError as exc:
                checks.append(
                    HealthCheck(
                        level=Severity.WARNING, message=f"Metrics unavailable: {exc.message}"
                    )
                )

        # The overview query isn't free: only while someone is looking. The live feed always
        # runs, or a read burst of a buffering player is missed and its stream can't be matched.
        overview = await self._fresh_overview(checks) if full else None
        if self._feed is not None:
            self._feed.start()
            if self._feed.connected and self._feed.needs_backfill:
                await self._backfill_logs(self._feed)

        status = ServiceStatus.DEGRADED if checks else ServiceStatus.OK
        return PollResult(
            status=status,
            health_checks=checks,
            infinidysk=build_stats(
                usage=usage,
                overview=overview,
                metrics=metrics,
                live=self._feed.state() if self._feed is not None else None,
                healthy=healthy,
                ready=ready,
            ),
        )

    def on_read_started(self, callback: Callable[[], None]) -> None:
        """Called from the live feed as soon as InfiniDysk starts reading a file."""
        if self._feed is not None:
            self._feed.on_read_started = callback

    async def _backfill_logs(self, feed: LiveFeed) -> None:
        feed.needs_backfill = False  # one try per connect; the live topic covers the rest
        try:
            data = await self._get("/api/get-logs", LOG_BACKFILL_PARAMS)
        except IntegrationError as exc:
            log.warning("infinidysk_log_backfill_failed", error=exc.message)
            return
        entries = data.get("entries") if isinstance(data, dict) else None
        if isinstance(entries, list):
            feed.logs.add(entries)

    async def _fresh_overview(self, checks: list[HealthCheck]) -> dict[str, Any] | None:
        now = time.monotonic()
        if self._overview is not None and now - self._overview_at < OVERVIEW_EVERY_S:
            return self._overview
        try:
            data = await self._get("/api/get-overview-stats", OVERVIEW_PARAMS)
        except IntegrationError as exc:
            checks.append(
                HealthCheck(
                    level=Severity.WARNING,
                    message=f"Overview statistics unavailable: {exc.message}",
                )
            )
            fresh_enough = now - self._overview_at <= OVERVIEW_MAX_AGE_S
            return self._overview if fresh_enough else None
        if not isinstance(data, dict):
            raise IntegrationError(
                ErrorKind.PROTOCOL, "Unexpected /api/get-overview-stats response"
            )
        self._overview, self._overview_at = data, now
        return data

    async def aclose(self) -> None:
        if self._feed is not None:
            await self._feed.stop()
        await self._client.aclose()
        if self._backend is not None:
            await self._backend.aclose()
