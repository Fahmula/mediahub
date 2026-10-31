"""Settings: general, account, about."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from sqlalchemy import delete, select
from starlette.concurrency import run_in_threadpool

from mediahub import __version__
from mediahub.context import AppContext, get_context
from mediahub.db.app_settings import SERVER_NAME, ServerIdentity, set_setting
from mediahub.db.models import AuthSession, User
from mediahub.db.session import session_scope
from mediahub.logging import get_logger
from mediahub.security.csrf import verify_csrf
from mediahub.security.passwords import MIN_PASSWORD_LENGTH, hash_password, verify_password
from mediahub.web.deps import CurrentUser, require_auth
from mediahub.web.templating import render

log = get_logger(__name__)
router = APIRouter(prefix="/settings", dependencies=[Depends(require_auth), Depends(verify_csrf)])


# ----- overview / general ------------------------------------------------------------------
@router.get("", response_class=HTMLResponse)
async def settings_home(request: Request, ctx: AppContext = Depends(get_context)) -> HTMLResponse:
    return render(
        request,
        "pages/settings/index.html",
        {
            "page_title": "Settings",
            "back_href": "/",
            "min_length": MIN_PASSWORD_LENGTH,
            "about": {
                "version": __version__,
                "server_id": ctx.identity.server_id,
                "config_dir": str(ctx.settings.config_dir),
                "demo_mode": ctx.settings.demo_mode,
            },
        },
    )


@router.post("/general")
async def save_general(request: Request, ctx: AppContext = Depends(get_context)) -> Response:
    form = await request.form()
    raw = form.get("server_name")
    name = raw.strip() if isinstance(raw, str) else ""
    if not name or len(name) > 64:
        return render(
            request,
            "partials/settings/general_form.html",
            {"error": "Use 1–64 characters.", "server_name": name},
        )

    def save() -> None:
        with session_scope(ctx.sessions) as db:
            set_setting(db, SERVER_NAME, name)

    await run_in_threadpool(save)
    ctx.identity = ServerIdentity(server_id=ctx.identity.server_id, server_name=name)
    ctx.monitor.store.rename(ctx.identity)
    await ctx.monitor.refresh_identity()
    log.info("server_renamed", name=name)
    return render(
        request, "partials/settings/general_form.html", {"saved": True, "server_name": name}
    )


# ----- moved pages -------------------------------------------------------------------------
@router.get("/services/{rest:path}")
@router.get("/peers/{rest:path}")
async def moved_to_servers(request: Request) -> RedirectResponse:
    """Service and linked-server forms moved to /servers; keep old bookmarks working."""
    url = request.url.path.replace("/settings", "/servers", 1)
    query = request.url.query
    return RedirectResponse(f"{url}?{query}" if query else url, status_code=308)


# ----- account -----------------------------------------------------------------------------
@router.post("/account/password", response_class=HTMLResponse)
async def change_password(
    request: Request, auth: CurrentUser, ctx: AppContext = Depends(get_context)
) -> HTMLResponse:
    data = await request.form()
    current = str(data.get("current_password") or "")
    new = str(data.get("new_password") or "")
    confirm = str(data.get("confirm_password") or "")

    def apply() -> str | None:
        with session_scope(ctx.sessions) as db:
            user = db.scalar(select(User).where(User.id == auth.user_id))
            if user is None or not verify_password(user.password_hash, current):
                return "Current password is incorrect."
            if len(new) < MIN_PASSWORD_LENGTH:
                return f"New password must be at least {MIN_PASSWORD_LENGTH} characters."
            if new != confirm:
                return "New passwords don't match."
            user.password_hash = hash_password(new)
            # Sign out every other session; keep the one making this change.
            db.execute(
                delete(AuthSession).where(
                    AuthSession.user_id == user.id, AuthSession.id_hash != auth.session_hash
                )
            )
            return None

    error = await run_in_threadpool(apply)
    if error is None:
        log.info("password_changed", username=auth.username)
    return render(
        request,
        "partials/settings/password_form.html",
        {"error": error, "saved": error is None, "min_length": MIN_PASSWORD_LENGTH},
    )
