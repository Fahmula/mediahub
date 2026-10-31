"""Monitoring pages and their polled partials.

Handlers here are ``async`` and read the in-memory snapshot store, so pages render instantly
regardless of how slow or unreachable the monitored services are. The exception is Play
history: recorded plays live in the database, read in a thread, and never reach upstream.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse
from starlette.concurrency import run_in_threadpool

from mediahub.aggregation.dashboard import SCOPE_ALL, SCOPE_LOCAL, DashboardView, build_view
from mediahub.context import AppContext, get_context
from mediahub.db.models import PlayRecord
from mediahub.db.session import session_scope
from mediahub.history import repository as history
from mediahub.models.common import ServiceType
from mediahub.models.downloads import ActivityEvent, DownloadItem
from mediahub.models.infinidysk import ActiveRead, ReadProblem, StreamHealth
from mediahub.models.playback import PlaybackSession
from mediahub.models.snapshot import ServiceSnapshot
from mediahub.security.csrf import verify_csrf
from mediahub.web.deps import require_auth
from mediahub.web.scope import current_scope, remember_scope, scope_context
from mediahub.web.templating import render

router = APIRouter(dependencies=[Depends(require_auth), Depends(verify_csrf)])

DASHBOARD_SECTIONS = frozenset({"attention", "summary", "streams", "downloads", "infinidysk"})
ACTIVITY_TABS = frozenset({"playback", "plays", "downloads"})
PLAYS_PER_PAGE = 50
# Tabs that were folded into others; old links and open pages still ask for them.
ACTIVITY_TAB_ALIASES = {"history": "downloads", "streams": "playback"}
# Mute choices on Needs Attention, in hours.
MUTE_HOURS = (1, 24, 168)
# Card layout on service pages (Emby: detailed cards or compact rows), remembered in a cookie.
LAYOUT_COOKIE = "mh_layout"
LAYOUTS = ("detailed", "compact")


def _view(request: Request, ctx: AppContext) -> tuple[DashboardView, dict[str, object]]:
    ctx.monitor.demand.touch()
    scoped = scope_context(request, ctx)
    view = build_view(ctx.monitor.store, str(scoped["scope"]))
    return view, {"view": view, **scoped}


def _page(
    request: Request, ctx: AppContext, template: str, extra: dict[str, object]
) -> HTMLResponse:
    _, base = _view(request, ctx)
    response = render(request, template, {**base, **extra})
    remember_scope(request, response, str(base["scope"]))
    return response


# ----- dashboard ---------------------------------------------------------------------------
@router.get("/", response_class=HTMLResponse)
async def dashboard(request: Request, ctx: AppContext = Depends(get_context)) -> HTMLResponse:
    return _page(request, ctx, "pages/dashboard.html", {"page_title": "Dashboard"})


@router.get("/partials/dashboard/{section}", response_class=HTMLResponse)
async def dashboard_partial(
    section: str, request: Request, ctx: AppContext = Depends(get_context)
) -> HTMLResponse:
    if section not in DASHBOARD_SECTIONS:
        raise HTTPException(404)
    _, base = _view(request, ctx)
    return render(request, f"partials/dashboard/{section}.html", base)


@router.get("/partials/nav", response_class=HTMLResponse)
async def nav_partial(request: Request, ctx: AppContext = Depends(get_context)) -> HTMLResponse:
    """The Needs Attention count in the nav. On every page, so it doesn't count as demand."""
    view = build_view(ctx.monitor.store, current_scope(request, ctx))
    return render(request, "partials/nav_counts.html", {"attention_count": len(view.issues)})


@router.get("/attention", response_class=HTMLResponse)
async def attention(request: Request, ctx: AppContext = Depends(get_context)) -> HTMLResponse:
    return _page(request, ctx, "pages/attention.html", {"page_title": "Needs Attention"})


@router.get("/partials/attention", response_class=HTMLResponse)
async def attention_partial(
    request: Request, ctx: AppContext = Depends(get_context)
) -> HTMLResponse:
    _, base = _view(request, ctx)
    return render(request, "partials/attention_list.html", base)


@router.post("/attention/issues", response_class=HTMLResponse)
async def update_issue(request: Request, ctx: AppContext = Depends(get_context)) -> HTMLResponse:
    """Acknowledge, mute or restore one issue; responds with the refreshed list."""
    form = await request.form()
    fingerprint, action = form.get("fingerprint"), form.get("action")
    states = ctx.monitor.store.issue_states
    everything = build_view(ctx.monitor.store, SCOPE_ALL)
    issue = next(
        (i for i in everything.issues + everything.hidden_issues if i.fingerprint == fingerprint),
        None,
    )
    # An issue that resolved in the meantime is simply gone from the refreshed list.
    if issue is not None:
        if action == "ack":
            await states.acknowledge(issue)
        elif action == "mute":
            hours = form.get("hours")
            if not (isinstance(hours, str) and hours.isdigit() and int(hours) in MUTE_HOURS):
                raise HTTPException(400, "Invalid mute duration")
            await states.mute(issue, datetime.now(UTC) + timedelta(hours=int(hours)))
        elif action == "restore":
            await states.restore(issue.fingerprint)
        else:
            raise HTTPException(400, "Unknown action")
    _, base = _view(request, ctx)
    return render(request, "partials/attention_list.html", base)


# ----- activity / service pages ------------------------------------------------------------
@dataclass
class FilteredView:
    """A dashboard view narrowed to one service type and optionally one instance."""

    service_type: ServiceType | None
    instance: str | None
    instances: list[ServiceSnapshot] = field(default_factory=list)
    sessions: list[PlaybackSession] = field(default_factory=list)
    downloads: list[DownloadItem] = field(default_factory=list)
    history: list[ActivityEvent] = field(default_factory=list)
    snapshots: list[ServiceSnapshot] = field(default_factory=list)
    reads: list[tuple[ActiveRead, ServiceSnapshot]] = field(default_factory=list)


def filter_view(
    view: DashboardView, service_type: ServiceType | None, instance: str | None
) -> FilteredView:
    out = FilteredView(service_type=service_type, instance=instance)
    out.instances = [
        s for s in view.snapshots if service_type is None or s.source.service_type is service_type
    ]
    if instance and not any(s.source.instance_id == instance for s in out.instances):
        instance = out.instance = None
    out.snapshots = [
        s for s in out.instances if instance is None or s.source.instance_id == instance
    ]
    ids = {s.source.key for s in out.snapshots}
    out.sessions = [s for s in view.sessions if s.source.key in ids]
    out.downloads = [d for d in view.downloads if d.source.key in ids]
    out.history = [e for e in view.history if e.source.key in ids]
    out.reads = [
        (r, snap) for snap in out.snapshots if snap.infinidysk for r in snap.infinidysk.reads
    ]
    return out


SERVICE_PAGES: dict[str, ServiceType] = {t.value: t for t in ServiceType}


def _activity_tab(tab: str) -> str:
    tab = ACTIVITY_TAB_ALIASES.get(tab, tab)
    if tab not in ACTIVITY_TABS:
        raise HTTPException(404)
    return tab


def _service_context(
    request: Request, ctx: AppContext, service_type: ServiceType | None
) -> tuple[DashboardView, dict[str, object]]:
    view, base = _view(request, ctx)
    instance = request.query_params.get("instance")
    filtered = filter_view(view, service_type, instance)
    requested = request.query_params.get("layout") or request.cookies.get(LAYOUT_COOKIE)
    layout = requested if requested in LAYOUTS else LAYOUTS[0]
    return view, {**base, "fv": filtered, "service_type": service_type, "layout": layout}


@dataclass(frozen=True)
class PlayRow:
    play: PlayRecord
    health: StreamHealth
    problems: list[ReadProblem]

    @classmethod
    def of(cls, play: PlayRecord) -> PlayRow:
        problems = [ReadProblem.model_validate(p) for p in play.problems]
        return cls(play, StreamHealth(play.outcome), problems)

    @property
    def stopped_at_pct(self) -> float | None:
        position, duration = self.play.position_s, self.play.duration_s
        return 100 * position / duration if position is not None and duration else None

    @property
    def cursor(self) -> str:
        return f"{self.play.started_at.isoformat()}~{self.play.id}"


def _parse_cursor(value: str | None) -> tuple[datetime, str] | None:
    if not value:
        return None
    started_at, _, play_id = value.partition("~")
    try:
        return datetime.fromisoformat(started_at), play_id
    except ValueError as exc:
        raise HTTPException(400, "Invalid cursor") from exc


def _plays_page(ctx: AppContext, show: str, before: str | None) -> dict[str, object]:
    """One page of recorded plays (blocking: DB)."""
    only_problems = show != "all"
    cursor = _parse_cursor(before)
    with session_scope(ctx.sessions) as db:
        plays = history.list_plays(
            db, only_problems=only_problems, limit=PLAYS_PER_PAGE + 1, before=cursor
        )
        with_problems, total = history.count_plays(db)
    rows = [PlayRow.of(p) for p in plays[:PLAYS_PER_PAGE]]
    return {
        "plays": rows,
        "show": "all" if not only_problems else "problems",
        "next_cursor": rows[-1].cursor if len(plays) > PLAYS_PER_PAGE else None,
        "plays_with_problems": with_problems,
        "plays_total": total,
    }


async def _activity_context(
    request: Request, ctx: AppContext, tab: str, show: str
) -> dict[str, object]:
    _, base = _service_context(request, ctx, None)
    if tab == "plays" and base["scope"] in (SCOPE_ALL, SCOPE_LOCAL):
        base.update(await run_in_threadpool(_plays_page, ctx, show, None))
    return {**base, "tab": tab}


@router.get("/activity", response_class=HTMLResponse)
async def activity(
    request: Request,
    ctx: AppContext = Depends(get_context),
    tab: str = "playback",
    show: str = "problems",
) -> HTMLResponse:
    tab = _activity_tab(tab)
    base = await _activity_context(request, ctx, tab, show)
    response = render(request, "pages/activity.html", {**base, "page_title": "Activity"})
    remember_scope(request, response, str(base["scope"]))
    return response


@router.get("/partials/activity/{tab}", response_class=HTMLResponse)
async def activity_partial(
    tab: str,
    request: Request,
    ctx: AppContext = Depends(get_context),
    show: str = "problems",
) -> HTMLResponse:
    tab = _activity_tab(tab)
    base = await _activity_context(request, ctx, tab, show)
    return render(request, "partials/activity_list.html", base)


@router.get("/partials/plays", response_class=HTMLResponse)
async def plays_partial(
    request: Request,
    ctx: AppContext = Depends(get_context),
    show: str = "problems",
    before: str | None = None,
) -> HTMLResponse:
    """The "Load more" button: the next rows, and a new button if there are more."""
    page = await run_in_threadpool(_plays_page, ctx, show, before)
    return render(request, "partials/play_rows.html", page)


def _service_page_handler(service_type: ServiceType):  # type: ignore[no-untyped-def]
    async def handler(request: Request, ctx: AppContext = Depends(get_context)) -> HTMLResponse:
        _, base = _service_context(request, ctx, service_type)
        response = render(
            request,
            "pages/service.html",
            {**base, "page_title": service_type.label, "back_href": "/servers"},
        )
        remember_scope(request, response, str(base["scope"]))
        layout = str(base["layout"])
        if request.query_params.get("layout") and request.cookies.get(LAYOUT_COOKIE) != layout:
            response.set_cookie(
                LAYOUT_COOKIE, layout, max_age=31_536_000, samesite="lax", httponly=True
            )
        return response

    return handler


# Explicit paths (not a "/{service}" catch-all, which would shadow /servers and /settings).
for _service_type in ServiceType:
    router.add_api_route(
        f"/{_service_type.value}",
        _service_page_handler(_service_type),
        methods=["GET"],
        response_class=HTMLResponse,
    )


@router.get("/partials/service/{service}", response_class=HTMLResponse)
async def service_partial(
    service: str, request: Request, ctx: AppContext = Depends(get_context)
) -> HTMLResponse:
    service_type = SERVICE_PAGES.get(service)
    if service_type is None:
        raise HTTPException(404)
    _, base = _service_context(request, ctx, service_type)
    return render(request, "partials/service_body.html", base)
