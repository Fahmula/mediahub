"""The Servers page: status and management in one place, and the nav around it."""

from __future__ import annotations

import re
import time
from collections.abc import Iterator
from pathlib import Path

import httpx
import pytest
import respx
from fastapi.testclient import TestClient

from mediahub.config import LogFormat, Settings
from mediahub.main import create_app

from .conftest import csrf_from, setup_admin

TOKEN = "mhp_aaaaaaaaaaaaaaaaaaaaaaaa"


@pytest.fixture
def demo(tmp_path: Path) -> Iterator[TestClient]:
    settings = Settings(
        config_dir=tmp_path / "config", log_format=LogFormat.CONSOLE, demo_mode=True
    )
    with TestClient(create_app(settings), base_url="http://testserver") as client:
        setup_admin(client)
        deadline = time.monotonic() + 5
        while "Interstellar" not in client.get("/").text and time.monotonic() < deadline:
            time.sleep(0.05)
        yield client


def _csrf(client: TestClient) -> dict[str, str]:
    return {"X-CSRF-Token": csrf_from(client.get("/servers").text), "HX-Request": "true"}


def _add_sonarr(client: TestClient, *, enabled: bool = True) -> str:
    data = {
        "service_type": "sonarr",
        "name": "TV",
        "base_url": "http://sonarr:8989/",
        "secret_api_key": "sonarr-secret-key-123",
        "verify_tls": "on",
        "csrf_token": csrf_from(client.get("/servers").text),
    }
    if enabled:
        data["enabled"] = "on"
    resp = client.post("/servers/services/new", data=data, follow_redirects=False)
    assert resp.status_code == 303, resp.text
    match = re.search(r'href="/servers/services/([0-9a-f-]{36})"', client.get("/servers").text)
    assert match
    return match.group(1)


def _link_peer(client: TestClient, *, enabled: bool) -> str:
    data = {
        "name": "Alpha",
        "base_url": "https://alpha.tail1234.ts.net",
        "token": TOKEN,
        "csrf_token": csrf_from(client.get("/servers").text),
    }
    if enabled:
        data["enabled"] = "on"
    resp = client.post("/servers/peers/new", data=data, follow_redirects=False)
    assert resp.status_code == 303, resp.text
    match = re.search(r'href="/servers/peers/([0-9a-f-]{36})"', client.get("/servers").text)
    assert match
    return match.group(1)


def test_a_disabled_peer_stays_listed_and_can_be_enabled(authed: TestClient) -> None:
    with respx.mock(assert_all_called=False) as router:
        router.get("https://alpha.tail1234.ts.net/peer/v1/snapshot").mock(
            return_value=httpx.Response(503)
        )
        peer_id = _link_peer(authed, enabled=False)
        assert authed.app.state.ctx.monitor.store.get_peer(peer_id) is None  # type: ignore[attr-defined]
        partial = authed.get("/partials/servers").text
        assert "Alpha" in partial and "Disabled" in partial and "Enable" in partial

        resp = authed.post(f"/servers/peers/{peer_id}/toggle", headers=_csrf(authed))
        assert resp.status_code == 204 and resp.headers["hx-redirect"] == "/servers"
        partial = authed.get("/partials/servers").text
    assert "Alpha" in partial and "Disable" in partial


def test_service_rows_link_to_their_page_beside_the_menu(authed: TestClient) -> None:
    with respx.mock(assert_all_called=False) as router:
        router.get("http://sonarr:8989/api/v3/system/status").mock(
            return_value=httpx.Response(200, json={"version": "4.0.20"})
        )
        router.get("http://sonarr:8989/api/v3/health").mock(
            return_value=httpx.Response(200, json=[])
        )
        instance_id = _add_sonarr(authed)
        html = authed.get("/partials/servers").text
    assert f'href="/sonarr?server=all&amp;instance={instance_id}"' in html
    assert f'hx-post="/servers/services/{instance_id}/toggle"' in html


def test_a_disabled_service_can_be_enabled(authed: TestClient) -> None:
    instance_id = _add_sonarr(authed, enabled=False)
    html = authed.get("/partials/servers").text
    assert "Disabled" in html and f"/servers/services/{instance_id}/toggle" in html


def test_demo_services_have_no_menus(demo: TestClient) -> None:
    html = demo.get("/partials/servers").text
    assert "Brother Server" in html
    assert "Actions for" not in html and "/toggle" not in html


def test_tokens_stay_out_of_the_polled_section(demo: TestClient) -> None:
    page = demo.get("/servers").text
    assert page.index('id="sec-servers"') < page.index('id="tokens"')
    assert 'id="tokens"' not in demo.get("/partials/servers").text
    assert 'id="peer-grants"' in page


@pytest.mark.parametrize(
    ("old", "new"),
    [
        ("/settings/services/new?type=emby", "/servers/services/new?type=emby"),
        ("/settings/peers/new", "/servers/peers/new"),
        ("/settings/services/abc", "/servers/services/abc"),
    ],
)
def test_old_form_urls_redirect(authed: TestClient, old: str, new: str) -> None:
    resp = authed.get(old, follow_redirects=False)
    assert resp.status_code == 308 and resp.headers["location"] == new


def test_settings_keeps_only_general_account_and_about(authed: TestClient) -> None:
    html = authed.get("/settings").text
    assert "General" in html and "Account" in html and "About" in html
    assert "Add service" not in html and "Access tokens" not in html


def test_attention_count_in_the_nav(demo: TestClient) -> None:
    html = demo.get("/partials/nav").text
    counts = re.findall(r'id="nav-attention-(?:side|bottom)" hx-swap-oob="innerHTML">(\d+)<', html)
    assert len(counts) == 2 and counts[0] == counts[1] and int(counts[0]) > 0


def test_attention_count_is_empty_without_issues(authed: TestClient) -> None:
    html = authed.get("/partials/nav").text
    assert 'hx-swap-oob="innerHTML"></span>' in html


def test_nav_poll_is_not_demand(authed: TestClient) -> None:
    monitor = authed.app.state.ctx.monitor  # type: ignore[attr-defined]
    monitor.demand._last = float("-inf")
    authed.get("/partials/nav")
    assert not monitor.demand.is_active()


def test_every_page_reaches_attention_servers_and_the_account_menu(authed: TestClient) -> None:
    html = authed.get("/").text
    nav = html[html.index('class="fixed inset-x-0 bottom-0') :]
    assert 'href="/attention"' in nav and 'href="/servers"' in nav
    assert 'href="/settings"' not in nav
    assert 'class="dropdown dropdown-end"' in html and "data-theme-toggle" in html
