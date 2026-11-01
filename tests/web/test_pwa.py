from __future__ import annotations

import json
import re

from fastapi.testclient import TestClient

from tests.web.conftest import setup_admin


def test_manifest_is_public_and_installable(client: TestClient) -> None:
    resp = client.get("/manifest.webmanifest")
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("application/manifest+json")
    data = resp.json()
    assert data["start_url"] == "/" and data["scope"] == "/"
    assert data["display"] == "standalone"
    assert data["name"].startswith("MediaHub")
    sizes = {(icon["sizes"], icon.get("purpose", "any")) for icon in data["icons"]}
    assert {("192x192", "any"), ("512x512", "any"), ("512x512", "maskable")} <= sizes
    for icon in data["icons"]:
        assert client.get(icon["src"]).headers["content-type"] == "image/png"


def _precache(script: str) -> list[str]:
    match = re.search(r"const PRECACHE = (\[.*?\]);", script)
    assert match, "precache list not filled in"
    return list(json.loads(match.group(1)))


def test_service_worker_served_from_root(client: TestClient) -> None:
    resp = client.get("/sw.js")
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/javascript")
    assert resp.headers["cache-control"] == "no-cache"
    assert "__VERSION__" not in resp.text and "__PRECACHE__" not in resp.text
    assert re.search(r'const VERSION = "[0-9a-f]{12}";', resp.text)
    precache = _precache(resp.text)
    assert "/offline" in precache
    for url in precache:
        assert client.get(url).status_code == 200, url


def test_service_worker_shows_notifications(client: TestClient) -> None:
    script = client.get("/sw.js").text
    for event in ("push", "notificationclick", "pushsubscriptionchange"):
        assert f'addEventListener("{event}"' in script
    assert "__ICON__" not in script
    icon = re.search(r'const ICON = "([^"]+)";', script)
    assert icon, "icon not filled in"
    assert client.get(icon.group(1)).headers["content-type"] == "image/png"


def test_service_worker_stays_clear_of_notification_routes(client: TestClient) -> None:
    """Subscribing is a POST, and the bootstrap holds a CSRF token: neither may be cached."""
    script = client.get("/sw.js").text
    assert 'request.method !== "GET"' in script
    assert 'url.pathname.startsWith("/static/")' in script


def test_offline_page_has_no_session_context(client: TestClient) -> None:
    setup_admin(client)
    resp = client.get("/offline")
    assert resp.status_code == 200
    assert "X-CSRF-Token" not in resp.text and "csrf_token" not in resp.text
    assert "<script>" not in resp.text  # CSP: no inline script


def test_pages_link_manifest_and_icons(client: TestClient) -> None:
    html = client.get("/login", follow_redirects=True).text
    assert '<link rel="manifest" href="/manifest.webmanifest">' in html
    assert 'rel="apple-touch-icon"' in html
