"""Installable-app plumbing: web manifest, service worker and its offline page.

Unauthenticated on purpose: browsers fetch the manifest without cookies, and the service worker
must load before login. Nothing here exposes monitoring data; the server name is already shown
on the login page.
"""

from __future__ import annotations

import hashlib
import json
from functools import cache

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, JSONResponse, Response

from mediahub import __version__
from mediahub.context import AppContext
from mediahub.web.templating import STATIC_DIR, TEMPLATES_DIR, static_url, templates

router = APIRouter()

APP_BACKGROUND = "#0b1220"  # dark theme base-200, also the maskable icon background
OFFLINE_PATH = "/offline"
OFFLINE_TEMPLATE = "pages/offline.html"
# Files the offline page needs; the service worker caches them at install.
OFFLINE_ASSETS = ("dist/app.css", "js/offline.js", "img/favicon.svg")


@router.get("/manifest.webmanifest", include_in_schema=False)
async def manifest(request: Request) -> JSONResponse:
    ctx: AppContext = request.app.state.ctx
    data = {
        "id": "/",
        "name": f"MediaHub · {ctx.identity.server_name}",
        "short_name": "MediaHub",
        "description": "Emby, Sonarr, Radarr and InfiniDysk at a glance.",
        "start_url": "/",
        "scope": "/",
        "display": "standalone",
        "background_color": APP_BACKGROUND,
        "theme_color": APP_BACKGROUND,
        "icons": [
            {"src": static_url("img/icon-192.png"), "sizes": "192x192", "type": "image/png"},
            {"src": static_url("img/icon-512.png"), "sizes": "512x512", "type": "image/png"},
            {
                "src": static_url("img/icon-maskable-512.png"),
                "sizes": "512x512",
                "type": "image/png",
                "purpose": "maskable",
            },
        ],
        "shortcuts": [
            {"name": "Activity", "url": "/activity"},
            {"name": "Needs Attention", "url": "/attention"},
        ],
    }
    return JSONResponse(
        data, media_type="application/manifest+json", headers={"Cache-Control": "no-cache"}
    )


@cache
def _service_worker() -> str:
    precache = [OFFLINE_PATH, *(static_url(path) for path in OFFLINE_ASSETS)]
    source = (STATIC_DIR / "js" / "sw.js").read_text()
    template = (TEMPLATES_DIR / OFFLINE_TEMPLATE).read_text()
    notification_icon = static_url("img/icon-192.png")
    version = hashlib.sha256(
        "\n".join([__version__, source, template, notification_icon, *precache]).encode()
    ).hexdigest()[:12]
    return (
        source.replace('"__VERSION__"', json.dumps(version))
        .replace("__PRECACHE__", json.dumps(precache))
        .replace('"__ICON__"', json.dumps(notification_icon))
    )


@router.get("/sw.js", include_in_schema=False)
async def service_worker() -> Response:
    # Served from the root so its scope covers the whole app. The version changes whenever the
    # worker, the offline page or its assets change, which makes browsers install the update.
    return Response(
        _service_worker(),
        media_type="text/javascript",
        headers={"Cache-Control": "no-cache"},
    )


@router.get(OFFLINE_PATH, include_in_schema=False, response_class=HTMLResponse)
async def offline(request: Request) -> HTMLResponse:
    # The service worker keeps a copy, so it must not depend on the request: no session, CSRF
    # token or server name. offline.js applies the theme cookie.
    return templates.TemplateResponse(request, OFFLINE_TEMPLATE)
