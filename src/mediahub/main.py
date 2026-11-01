"""FastAPI application factory and lifespan."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from urllib.parse import quote

from fastapi import FastAPI, Request
from fastapi.exceptions import HTTPException
from fastapi.responses import JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from sqlalchemy.orm import Session

from mediahub import __version__
from mediahub.config import Settings
from mediahub.context import AppContext
from mediahub.db.app_settings import ensure_identity
from mediahub.db.crypto import Keyring, load_master_secret, set_keyring
from mediahub.db.models import User
from mediahub.db.session import make_engine, make_sessionmaker, run_migrations, session_scope
from mediahub.history import repository as history
from mediahub.logging import configure_logging, get_logger
from mediahub.monitoring.demand import DemandTracker
from mediahub.monitoring.issue_states import IssueStates
from mediahub.monitoring.play_history import RETENTION
from mediahub.monitoring.service import MonitoringService
from mediahub.monitoring.store import SnapshotStore
from mediahub.peers import api as peer_api
from mediahub.push.dispatcher import PushDispatcher
from mediahub.push.repository import ensure_vapid
from mediahub.push.webpush import WebPushTransport
from mediahub.runtime_lock import InstanceLock
from mediahub.security.headers import SecurityHeadersMiddleware
from mediahub.security.passwords import MIN_PASSWORD_LENGTH, hash_password
from mediahub.security.sessions import purge_expired
from mediahub.web.deps import LoginRequired, SetupRequired, has_users
from mediahub.web.image_cache import ImageCache
from mediahub.web.routes import auth, health, images, notifications, pages, peers, pwa, servers
from mediahub.web.routes import settings as settings_routes
from mediahub.web.templating import STATIC_DIR, is_htmx, render

log = get_logger(__name__)


def _build_context(settings: Settings) -> AppContext:
    settings.config_dir.mkdir(parents=True, exist_ok=True)
    override = settings.secret_key.get_secret_value() if settings.secret_key else None
    keyring = Keyring(load_master_secret(settings.secret_key_path, override))
    set_keyring(keyring)

    engine = make_engine(settings.database_url)
    run_migrations(engine)
    sessions = make_sessionmaker(engine)
    with session_scope(sessions) as db:
        identity = ensure_identity(db, settings.server_name)
        purge_expired(db)
        history.prune(db, datetime.now(UTC) - RETENTION)
        _bootstrap_admin(settings, db)
        vapid = None if settings.demo_mode else ensure_vapid(db)

    issue_states = IssueStates(sessions)
    issue_states.load()
    store = SnapshotStore(identity=identity, issue_states=issue_states)
    push = None
    if vapid is not None:
        transport = WebPushTransport(vapid, settings.push_contact)
        push = PushDispatcher(sessions, transport, vapid.public)
    monitor = MonitoringService(settings, sessions, store, DemandTracker(), push)
    return AppContext(
        settings=settings,
        engine=engine,
        sessions=sessions,
        keyring=keyring,
        identity=identity,
        monitor=monitor,
        images=ImageCache(settings.cache_dir / "images"),
    )


def _bootstrap_admin(settings: Settings, db: Session) -> None:
    """Create the admin from MEDIAHUB_ADMIN_USER/PASSWORD when no user exists yet."""
    if not (settings.admin_user and settings.admin_password) or has_users(db):
        return
    password = settings.admin_password.get_secret_value()
    if len(password) < MIN_PASSWORD_LENGTH:
        log.error("admin_bootstrap_skipped", reason="password too short")
        return
    db.add(User(username=settings.admin_user, password_hash=hash_password(password)))
    log.info("admin_bootstrapped", username=settings.admin_user)


def _install_error_handlers(app: FastAPI) -> None:
    @app.exception_handler(LoginRequired)
    async def _login_required(request: Request, exc: LoginRequired) -> Response:
        if is_htmx(request):
            return Response(status_code=204, headers={"HX-Redirect": "/login"})
        target = "/login"
        if exc.next_path and exc.next_path != "/":
            target = f"/login?next={quote(exc.next_path, safe='/')}"
        return RedirectResponse(target, status_code=303)

    @app.exception_handler(SetupRequired)
    async def _setup_required(request: Request, exc: SetupRequired) -> Response:
        if is_htmx(request):
            return Response(status_code=204, headers={"HX-Redirect": "/setup"})
        return RedirectResponse("/setup", status_code=303)

    @app.exception_handler(HTTPException)
    async def _http_error(request: Request, exc: HTTPException) -> Response:
        if request.url.path.startswith("/peer/") or "text/html" not in request.headers.get(
            "accept", ""
        ):
            return JSONResponse({"detail": exc.detail}, status_code=exc.status_code)
        titles = {403: "Forbidden", 404: "Not found", 405: "Not allowed"}
        return render(
            request,
            "pages/error.html",
            {"title": titles.get(exc.status_code, "Error"), "message": exc.detail},
            status_code=exc.status_code,
        )


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or Settings()
    configure_logging(settings.log_level, settings.log_format)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        lock = InstanceLock(settings.lock_path)
        lock.acquire()
        ctx = _build_context(settings)
        app.state.ctx = ctx
        await ctx.monitor.start()
        log.info(
            "startup",
            version=__version__,
            server_id=ctx.identity.server_id,
            server_name=ctx.identity.server_name,
            config_dir=str(settings.config_dir),
        )
        try:
            yield
        finally:
            await ctx.monitor.stop()
            ctx.engine.dispose()
            lock.release()
            log.info("shutdown")

    app = FastAPI(
        title="MediaHub",
        version=__version__,
        lifespan=lifespan,
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )
    app.add_middleware(SecurityHeadersMiddleware)
    _install_error_handlers(app)
    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
    app.include_router(health.router)
    app.include_router(pwa.router)
    app.include_router(auth.router)
    app.include_router(auth.logout_router)
    app.include_router(images.router)
    app.include_router(peer_api.router)
    app.include_router(notifications.router)
    app.include_router(settings_routes.router)
    app.include_router(servers.router)
    app.include_router(peers.router)
    app.include_router(pages.router)
    return app
