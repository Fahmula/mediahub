"""Notification subscriptions: this browser's on/off switch and the list of devices.

The JSON routes are called with fetch (subscribing is a browser API, not a form), so unlike
htmx partials they answer with real status codes.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import BaseModel, Field
from starlette.concurrency import run_in_threadpool

from mediahub.context import AppContext, get_context
from mediahub.db.session import session_scope
from mediahub.logging import get_logger
from mediahub.push import repository
from mediahub.push.dispatcher import PushDispatcher
from mediahub.push.messages import test_message
from mediahub.push.webpush import (
    ENDPOINT_MAX,
    Device,
    InvalidSubscription,
    Outcome,
    endpoint_host,
    validate_keys,
)
from mediahub.security.csrf import verify_csrf
from mediahub.security.ratelimit import RateLimiter
from mediahub.web.deps import CurrentUser, require_auth
from mediahub.web.templating import render

log = get_logger(__name__)
router = APIRouter(
    prefix="/settings/notifications",
    dependencies=[Depends(require_auth), Depends(verify_csrf)],
)
test_limiter = RateLimiter(limit=5, window_s=60)


class Keys(BaseModel):
    p256dh: str = Field(max_length=256)
    auth: str = Field(max_length=64)


class Subscription(BaseModel):
    endpoint: str = Field(max_length=ENDPOINT_MAX)
    keys: Keys
    old_endpoint: str | None = Field(default=None, max_length=ENDPOINT_MAX)


class Endpoint(BaseModel):
    endpoint: str = Field(max_length=ENDPOINT_MAX)


def _dispatcher(ctx: AppContext = Depends(get_context)) -> PushDispatcher:
    if ctx.monitor.push is None:
        raise HTTPException(status.HTTP_409_CONFLICT, "Notifications are off in demo mode.")
    return ctx.monitor.push


def device_label(user_agent: str) -> str:
    """Enough to tell devices apart in the list, e.g. "Safari on iPhone"."""
    browsers = (
        ("Edg", "Edge"),
        ("Firefox", "Firefox"),
        ("FxiOS", "Firefox"),
        ("Chrome", "Chrome"),
        ("CriOS", "Chrome"),
        ("Safari", "Safari"),
    )
    systems = (
        ("iPhone", "iPhone"),
        ("iPad", "iPad"),
        ("Android", "Android"),
        ("Windows", "Windows"),
        ("Macintosh", "Mac"),
        ("Linux", "Linux"),
    )
    browser = next((name for mark, name in browsers if mark in user_agent), "Browser")
    system = next((name for mark, name in systems if mark in user_agent), None)
    return f"{browser} on {system}" if system else browser


def device_list(ctx: AppContext) -> list[dict[str, Any]]:
    with session_scope(ctx.sessions) as db:
        return [
            {
                "id": row.id,
                "label": row.label,
                "host": row.host,
                "created_at": row.created_at,
                "last_success_at": row.last_success_at,
                "last_error": row.last_error,
            }
            for row in repository.list_subscriptions(db)
        ]


def _render_devices(request: Request, devices: list[dict[str, Any]]) -> HTMLResponse:
    return render(request, "partials/settings/notification_devices.html", {"devices": devices})


@router.get("/bootstrap")
async def bootstrap(auth: CurrentUser, push: PushDispatcher = Depends(_dispatcher)) -> JSONResponse:
    """For the service worker, which can't read a page to find these."""
    return JSONResponse(
        {"public_key": push.public_key, "csrf_token": auth.csrf_token},
        headers={"Cache-Control": "no-store"},
    )


@router.post("/subscribe")
async def subscribe(
    body: Subscription,
    request: Request,
    auth: CurrentUser,
    ctx: AppContext = Depends(get_context),
    _: PushDispatcher = Depends(_dispatcher),
) -> JSONResponse:
    try:
        host = endpoint_host(body.endpoint)
        validate_keys(body.keys.p256dh, body.keys.auth)
    except InvalidSubscription as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, str(exc)) from exc
    label = device_label(request.headers.get("user-agent", ""))

    def save() -> None:
        with session_scope(ctx.sessions) as db:
            repository.upsert_subscription(
                db,
                endpoint=body.endpoint,
                p256dh=body.keys.p256dh,
                auth=body.keys.auth,
                label=label,
                session_hash=auth.session_hash,
                old_endpoint=body.old_endpoint,
            )

    try:
        await run_in_threadpool(save)
    except repository.TooManyDevices as exc:
        detail = f"Up to {repository.MAX_DEVICES} devices can get notifications. Remove one first."
        raise HTTPException(status.HTTP_409_CONFLICT, detail) from exc
    log.info("push_subscribed", host=host)
    return JSONResponse({"ok": True})


@router.post("/unsubscribe")
async def unsubscribe(body: Endpoint, ctx: AppContext = Depends(get_context)) -> JSONResponse:
    def remove() -> None:
        with session_scope(ctx.sessions) as db:
            repository.delete_by_endpoint(db, body.endpoint)

    await run_in_threadpool(remove)
    return JSONResponse({"ok": True})


@router.post("/touch")
async def touch(
    body: Endpoint, auth: CurrentUser, ctx: AppContext = Depends(get_context)
) -> JSONResponse:
    """Is this browser still subscribed? It may have been removed from another device."""

    def apply() -> bool:
        with session_scope(ctx.sessions) as db:
            return repository.touch_subscription(db, body.endpoint, auth.session_hash)

    return JSONResponse({"subscribed": await run_in_threadpool(apply)})


@router.post("/test")
async def send_test(
    body: Endpoint,
    auth: CurrentUser,
    ctx: AppContext = Depends(get_context),
    push: PushDispatcher = Depends(_dispatcher),
) -> JSONResponse:
    if test_limiter.blocked(auth.session_hash):
        raise HTTPException(
            status.HTTP_429_TOO_MANY_REQUESTS, "Too many test notifications. Try again in a minute."
        )
    test_limiter.hit(auth.session_hash)

    def find() -> Device | None:
        with session_scope(ctx.sessions) as db:
            return repository.device_for_endpoint(db, body.endpoint)

    device = await run_in_threadpool(find)
    if device is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "This device isn't subscribed.")
    result = await push.send_test(device, test_message())
    return JSONResponse({"ok": result.outcome is Outcome.SENT, "error": result.error})


@router.get("/devices", response_class=HTMLResponse)
async def devices(request: Request, ctx: AppContext = Depends(get_context)) -> HTMLResponse:
    return _render_devices(request, await run_in_threadpool(device_list, ctx))


@router.post("/devices/{device_id}/remove", response_class=HTMLResponse)
async def remove_device(
    device_id: str, request: Request, ctx: AppContext = Depends(get_context)
) -> HTMLResponse:
    def remove() -> list[dict[str, Any]]:
        with session_scope(ctx.sessions) as db:
            repository.delete_by_id(db, device_id)
        return device_list(ctx)

    return _render_devices(request, await run_in_threadpool(remove))
