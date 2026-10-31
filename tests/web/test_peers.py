"""Two MediaHub apps linked in-process: A grants a token, B links A and shows its data."""

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
from mediahub.integrations.demo import DEMO_PEER_SERVER_ID
from mediahub.main import create_app
from mediahub.peers import api
from mediahub.peers.client import DEMAND_HEADER
from mediahub.peers.target import target_key

from .conftest import csrf_from, setup_admin

A_URL = "https://alpha.tail1234.ts.net"
PNG = b"\x89PNG\r\n\x1a\n" + b"0" * 32


@pytest.fixture(autouse=True)
def _fresh_limits() -> None:
    api.failed_auth._hits.clear()
    api.grant_limiter._hits.clear()


def _app(tmp_path: Path, name: str, *, demo: bool = False) -> TestClient:
    settings = Settings(
        config_dir=tmp_path / name,
        log_format=LogFormat.CONSOLE,
        server_name=name,
        demo_mode=demo,
    )
    return TestClient(create_app(settings), base_url=f"http://{name.lower()}")


@pytest.fixture
def server_a(tmp_path: Path) -> Iterator[TestClient]:
    with _app(tmp_path, "Alpha", demo=True) as client:
        setup_admin(client)
        yield client


@pytest.fixture
def server_b(tmp_path: Path, server_a: TestClient) -> Iterator[TestClient]:
    with _app(tmp_path, "Bravo") as client:
        setup_admin(client)
        yield client


def _csrf(client: TestClient) -> dict[str, str]:
    return {"X-CSRF-Token": csrf_from(client.get("/settings").text)}


def _grant(client: TestClient, label: str = "Bravo") -> str:
    resp = client.post("/servers/peers/grants", data={"label": label}, headers=_csrf(client))
    match = re.search(r'value="(mhp_[^"]+)"', resp.text)
    assert match, resp.text
    return match.group(1)


def _wait_for_services(client: TestClient, n: int) -> None:
    store = client.app.state.ctx.monitor.store  # type: ignore[attr-defined]
    deadline = time.monotonic() + 5
    while len(store.local_snapshots()) < n and time.monotonic() < deadline:
        time.sleep(0.02)


@pytest.fixture
def forward(server_a: TestClient) -> Iterator[list[httpx.Request]]:
    """Route B's requests for A_URL to app A; collect them."""
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        headers = {k: v for k, v in request.headers.items() if k.lower() != "host"}
        resp = server_a.get(request.url.path, headers=headers)
        return httpx.Response(resp.status_code, content=resp.content, headers=resp.headers)

    with respx.mock(assert_all_called=False) as router:
        router.route(host="alpha.tail1234.ts.net").mock(side_effect=handler)
        yield seen


def _link(server_b: TestClient, token: str, **overrides: str) -> httpx.Response:
    data = {
        "name": "Alpha",
        "base_url": A_URL,
        "token": token,
        "enabled": "on",
        "csrf_token": csrf_from(server_b.get("/settings").text),
        **overrides,
    }
    return server_b.post("/servers/peers/new", data=data, follow_redirects=False)


def _peer_id(server_b: TestClient) -> str:
    match = re.search(r'href="/servers/peers/([0-9a-f-]{36})"', server_b.get("/servers").text)
    assert match
    return match.group(1)


# ----- the /peer/v1 API ------------------------------------------------------------------------
def test_snapshot_needs_a_valid_token(server_a: TestClient) -> None:
    assert server_a.get("/peer/v1/snapshot").status_code == 401
    bad = {"Authorization": "Bearer mhp_not-a-real-token-at-all"}
    assert server_a.get("/peer/v1/snapshot", headers=bad).status_code == 401
    # Session cookies don't count: the API is token-only.
    assert server_a.get("/peer/v1/snapshot").json() == {
        "detail": "Missing, invalid or revoked access token"
    }


def test_snapshot_serves_only_local_redacted_data(server_a: TestClient) -> None:
    token = _grant(server_a)
    _wait_for_services(server_a, 4)
    resp = server_a.get("/peer/v1/snapshot", headers={"Authorization": f"Bearer {token}"})
    assert resp.status_code == 200 and resp.headers["cache-control"] == "no-store"
    body = resp.json()
    assert body["server_name"] == "Alpha" and body["schema_version"] == 1
    assert len(body["services"]) == 4
    # A's own demo peer is never relayed.
    assert DEMO_PEER_SERVER_ID not in resp.text
    sessions = [s for svc in body["services"] for s in svc["sessions"]]
    assert sessions and all(s["remote_address"] is None for s in sessions)


def test_token_is_shown_once_and_revocation_cuts_access(server_a: TestClient) -> None:
    token = _grant(server_a)
    page = server_a.get("/servers").text
    assert token not in page and token[:10] in page
    auth = {"Authorization": f"Bearer {token}"}
    assert server_a.get("/peer/v1/snapshot", headers=auth).status_code == 200
    assert "last used" in server_a.get("/servers").text

    grant_id = re.search(r"/servers/peers/grants/(\d+)/revoke", page)
    assert grant_id
    server_a.post(f"/servers/peers/grants/{grant_id.group(1)}/revoke", headers=_csrf(server_a))
    assert server_a.get("/peer/v1/snapshot", headers=auth).status_code == 401
    assert "Revoked" in server_a.get("/servers").text


def test_failed_attempts_are_rate_limited(server_a: TestClient) -> None:
    bad = {"Authorization": "Bearer mhp_wrong-wrong-wrong-wrong"}
    codes = [server_a.get("/peer/v1/snapshot", headers=bad).status_code for _ in range(21)]
    assert codes[:20] == [401] * 20 and codes[20] == 429


# ----- linking -------------------------------------------------------------------------------
def test_link_a_peer_and_see_its_servers(
    server_a: TestClient, server_b: TestClient, forward: list[httpx.Request]
) -> None:
    token = _grant(server_a)
    _wait_for_services(server_a, 4)
    resp = _link(server_b, token)
    assert resp.status_code == 303, resp.text

    store = server_b.app.state.ctx.monitor.store  # type: ignore[attr-defined]
    peer_id = _peer_id(server_b)
    state = store.get_peer(peer_id)
    assert state is not None and state.status.value == "ok", state and state.error
    assert state.snapshot is not None and len(state.snapshot.services) == 4
    assert all(s.source.is_remote for s in state.snapshot.services)

    servers = server_b.get("/servers").text
    assert "Alpha" in servers and "Peer · read-only" in servers

    # The token never reaches the page; it's write-only.
    assert token not in server_b.get(f"/servers/peers/{peer_id}").text

    # While someone watches B, B asks A for fresh data.
    server_b.get("/")
    monitor = server_b.app.state.ctx.monitor  # type: ignore[attr-defined]
    server_b.portal.call(monitor.poller.poll_now, target_key(peer_id))  # type: ignore[union-attr]
    assert forward[-1].headers.get(DEMAND_HEADER) == "active"
    assert forward[0].headers.get(DEMAND_HEADER) is None  # B had no viewer yet


def test_peer_connection_test_and_wrong_token(
    server_a: TestClient, server_b: TestClient, forward: list[httpx.Request]
) -> None:
    token = _grant(server_a)
    ok = server_b.post(
        "/servers/peers/test", data={"base_url": A_URL, "token": token}, headers=_csrf(server_b)
    )
    assert "Connected" in ok.text and "Alpha" in ok.text
    bad = server_b.post(
        "/servers/peers/test",
        data={"base_url": A_URL, "token": "mhp_wrong-wrong-wrong-wrong"},
        headers=_csrf(server_b),
    )
    assert "Connection failed" in bad.text and "revoked" in bad.text


def test_link_form_validates(server_b: TestClient) -> None:
    resp = _link(server_b, "not-a-token", base_url="ftp://x")
    assert resp.status_code == 422
    assert "start with mhp_" in resp.text and "http://" in resp.text


def test_edit_keeps_token_and_unlink_removes_peer(
    server_a: TestClient, server_b: TestClient, forward: list[httpx.Request]
) -> None:
    _link(server_b, _grant(server_a))
    peer_id = _peer_id(server_b)
    data = {
        "name": "Alpha renamed",
        "base_url": A_URL,
        "token": "",
        "enabled": "on",
        "csrf_token": csrf_from(server_b.get("/settings").text),
    }
    resp = server_b.post(f"/servers/peers/{peer_id}", data=data, follow_redirects=False)
    assert resp.status_code == 303
    store = server_b.app.state.ctx.monitor.store  # type: ignore[attr-defined]
    state = store.get_peer(peer_id)
    assert state.name == "Alpha renamed" and state.status.value == "ok", state.error

    server_b.post(f"/servers/peers/{peer_id}/delete", headers=_csrf(server_b))
    assert store.get_peer(peer_id) is None
    assert "Alpha renamed" not in server_b.get("/servers").text


def test_peer_images_are_proxied_through_the_peer(server_b: TestClient) -> None:
    with respx.mock(assert_all_called=False) as router:
        router.get(f"{A_URL}/peer/v1/snapshot").mock(return_value=httpx.Response(503))
        image = router.get(f"{A_URL}/peer/v1/img/inst/42/poster/sig").mock(
            return_value=httpx.Response(200, content=PNG, headers={"content-type": "image/png"})
        )
        _link(server_b, "mhp_aaaaaaaaaaaaaaaaaaaaaaaa")
        peer_id = _peer_id(server_b)
        resp = server_b.get(f"/img/peer/{peer_id}/inst/42/poster/sig")
        assert resp.status_code == 200 and resp.content == PNG
        assert image.calls.last.request.headers["authorization"].startswith("Bearer mhp_")
        # Cached: a second request doesn't reach the peer.
        server_b.get(f"/img/peer/{peer_id}/inst/42/poster/sig")
        assert image.call_count == 1
        assert server_b.get(f"/img/peer/{peer_id}/inst/42/poster/x.y").status_code == 404
