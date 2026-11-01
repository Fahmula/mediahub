"""Subscribing to notifications from Settings."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec
from fastapi.testclient import TestClient
from sqlalchemy import delete

from mediahub.config import Settings
from mediahub.db.models import AuthSession
from mediahub.db.session import session_scope
from mediahub.main import create_app
from mediahub.push import repository
from mediahub.push.messages import PushMessage
from mediahub.push.webpush import Device, Outcome, Result, b64url_encode
from mediahub.web.routes import notifications
from mediahub.web.routes.notifications import device_label
from tests.web.conftest import PASSWORD, csrf_from, setup_admin

ENDPOINT = "https://web.push.apple.com/QGm_device"
API = "/settings/notifications"
SAFARI = "Mozilla/5.0 (iPhone; CPU iPhone OS 18_0 like Mac OS X) Version/18.0 Mobile Safari/604.1"


class FakeTransport:
    def __init__(self) -> None:
        self.sent: list[tuple[Device, PushMessage]] = []
        self.result = Result(Outcome.SENT)

    async def send(self, device: Device, message: PushMessage) -> Result:
        self.sent.append((device, message))
        return self.result

    async def aclose(self) -> None:
        pass


def keys() -> dict[str, str]:
    point = (
        ec.generate_private_key(ec.SECP256R1())
        .public_key()
        .public_bytes(serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint)
    )
    return {"p256dh": b64url_encode(point), "auth": b64url_encode(b"0123456789abcdef")}


def token(client: TestClient) -> dict[str, str]:
    return {"X-CSRF-Token": csrf_from(client.get("/settings").text)}


def subscribe(client: TestClient, endpoint: str = ENDPOINT, **extra: object):
    body = {"endpoint": endpoint, "keys": keys(), **extra}
    return client.post(f"{API}/subscribe", json=body, headers=token(client))


def stored(client: TestClient) -> list[str]:
    ctx = client.app.state.ctx
    with session_scope(ctx.sessions) as db:
        return [d.endpoint for d in repository.devices(db)]


@pytest.fixture
def transport(authed: TestClient) -> FakeTransport:
    fake = FakeTransport()
    authed.app.state.ctx.monitor.push.transport = fake
    notifications.test_limiter._hits.clear()
    return fake


def test_routes_need_a_login(client: TestClient) -> None:
    setup_admin(client)
    client.post("/logout", headers=token(client))
    response = client.post(f"{API}/subscribe", json={}, follow_redirects=False)
    assert response.status_code == 303


def test_routes_need_a_csrf_token(authed: TestClient) -> None:
    response = authed.post(f"{API}/subscribe", json={"endpoint": ENDPOINT, "keys": keys()})
    assert response.status_code == 403
    assert stored(authed) == []


def test_panel_is_in_settings_with_the_public_key(authed: TestClient) -> None:
    html = authed.get("/settings").text
    key = authed.app.state.ctx.monitor.push.public_key
    assert f'data-public-key="{key}"' in html
    assert '<h2 class="section-title mb-3">Notifications</h2>' in html


def test_page_marks_who_is_signed_in(client: TestClient) -> None:
    assert "data-signed-in" not in client.get("/setup").text


def test_subscribing_stores_the_device(authed: TestClient) -> None:
    response = authed.post(
        f"{API}/subscribe",
        json={"endpoint": ENDPOINT, "keys": keys(), "expirationTime": None},
        headers={**token(authed), "User-Agent": SAFARI},
    )
    assert response.json() == {"ok": True}
    assert stored(authed) == [ENDPOINT]
    assert "Safari on iPhone" in authed.get(f"{API}/devices").text


def test_device_list_never_shows_the_endpoint(authed: TestClient) -> None:
    subscribe(authed)
    assert "QGm_device" not in authed.get(f"{API}/devices").text
    assert "QGm_device" not in authed.get("/settings").text


def test_unknown_push_service_is_refused_by_name(authed: TestClient) -> None:
    response = subscribe(authed, "https://evil.example/hook")
    assert response.status_code == 422
    assert "evil.example" in response.json()["detail"]
    assert stored(authed) == []


def test_private_address_is_refused(authed: TestClient) -> None:
    assert subscribe(authed, "https://169.254.169.254/latest").status_code == 422


def test_bad_keys_are_refused(authed: TestClient) -> None:
    body = {"endpoint": ENDPOINT, "keys": {"p256dh": "AAAA", "auth": "AAAA"}}
    response = authed.post(f"{API}/subscribe", json=body, headers=token(authed))
    assert response.status_code == 422


def test_rotation_replaces_the_old_endpoint(authed: TestClient) -> None:
    subscribe(authed)
    new = "https://web.push.apple.com/rotated"
    assert subscribe(authed, new, old_endpoint=ENDPOINT).status_code == 200
    assert stored(authed) == [new]


def test_too_many_devices_says_so(authed: TestClient) -> None:
    for n in range(repository.MAX_DEVICES):
        assert subscribe(authed, f"{ENDPOINT}{n}").status_code == 200
    response = subscribe(authed)
    assert response.status_code == 409
    assert "Remove one" in response.json()["detail"]


def test_unsubscribing_removes_this_device(authed: TestClient) -> None:
    subscribe(authed)
    authed.post(f"{API}/unsubscribe", json={"endpoint": ENDPOINT}, headers=token(authed))
    assert stored(authed) == []


def test_touch_reports_a_device_removed_elsewhere(authed: TestClient) -> None:
    subscribe(authed)
    touch = {"endpoint": ENDPOINT}
    assert authed.post(f"{API}/touch", json=touch, headers=token(authed)).json() == {
        "subscribed": True
    }
    ctx = authed.app.state.ctx
    with session_scope(ctx.sessions) as db:
        device = repository.devices(db)[0]
    authed.post(f"{API}/devices/{device.id}/remove", headers=token(authed))
    assert authed.post(f"{API}/touch", json=touch, headers=token(authed)).json() == {
        "subscribed": False
    }


def test_removing_a_device_renders_the_list(authed: TestClient) -> None:
    subscribe(authed)
    ctx = authed.app.state.ctx
    with session_scope(ctx.sessions) as db:
        device = repository.devices(db)[0]
    response = authed.post(f"{API}/devices/{device.id}/remove", headers=token(authed))
    assert response.status_code == 200
    assert 'id="push-devices"' in response.text
    assert "/remove" not in response.text


def test_bootstrap_gives_the_worker_what_it_needs(authed: TestClient) -> None:
    response = authed.get(f"{API}/bootstrap")
    assert response.headers["cache-control"] == "no-store"
    assert response.json()["public_key"] == authed.app.state.ctx.monitor.push.public_key
    assert response.json()["csrf_token"] == token(authed)["X-CSRF-Token"]


def test_send_test_reaches_this_device(authed: TestClient, transport: FakeTransport) -> None:
    subscribe(authed)
    response = authed.post(f"{API}/test", json={"endpoint": ENDPOINT}, headers=token(authed))
    assert response.json() == {"ok": True, "error": None}
    [(device, message)] = transport.sent
    assert device.endpoint == ENDPOINT
    assert message.title == "MediaHub notifications are on"


def test_send_test_reports_the_refusal(authed: TestClient, transport: FakeTransport) -> None:
    subscribe(authed)
    transport.result = Result(Outcome.FAILED, "HTTP 403: BadJwtToken")
    response = authed.post(f"{API}/test", json={"endpoint": ENDPOINT}, headers=token(authed))
    assert response.json() == {"ok": False, "error": "HTTP 403: BadJwtToken"}
    assert "HTTP 403: BadJwtToken" in authed.get(f"{API}/devices").text


def test_send_test_to_an_unknown_device(authed: TestClient, transport: FakeTransport) -> None:
    response = authed.post(f"{API}/test", json={"endpoint": ENDPOINT}, headers=token(authed))
    assert response.status_code == 404
    assert transport.sent == []


def test_send_test_is_rate_limited(authed: TestClient, transport: FakeTransport) -> None:
    subscribe(authed)
    headers = token(authed)
    codes = [
        authed.post(f"{API}/test", json={"endpoint": ENDPOINT}, headers=headers).status_code
        for _ in range(7)
    ]
    assert codes == [200] * 5 + [429] * 2


def test_logging_out_stops_this_devices_notifications(authed: TestClient) -> None:
    subscribe(authed)
    ctx = authed.app.state.ctx
    authed.post("/logout", headers=token(authed))
    with session_scope(ctx.sessions) as db:
        assert repository.devices(db) == []


def test_logout_leaves_other_devices_alone(authed: TestClient) -> None:
    subscribe(authed)
    ctx = authed.app.state.ctx
    with session_scope(ctx.sessions) as db:
        repository.touch_subscription(db, ENDPOINT, "another-session")
    authed.post("/logout", headers=token(authed))
    with session_scope(ctx.sessions) as db:
        assert len(repository.devices(db)) == 1


def test_changing_the_password_keeps_only_this_device(authed: TestClient) -> None:
    subscribe(authed)
    other = "https://web.push.apple.com/other-phone"
    subscribe(authed, other)
    ctx = authed.app.state.ctx
    with session_scope(ctx.sessions) as db:
        repository.touch_subscription(db, other, "another-session")
    response = authed.post(
        "/settings/account/password",
        data={
            "current_password": PASSWORD,
            "new_password": "an entirely new password",
            "confirm_password": "an entirely new password",
        },
        headers=token(authed),
    )
    assert "notifications turned off" in response.text
    assert stored(authed) == [ENDPOINT]


def test_session_expiring_keeps_the_device(authed: TestClient) -> None:
    subscribe(authed)
    ctx = authed.app.state.ctx
    with session_scope(ctx.sessions) as db:
        db.execute(delete(AuthSession))
    with session_scope(ctx.sessions) as db:
        assert len(repository.devices(db)) == 1


def test_recording_a_gone_device_removes_it(authed: TestClient) -> None:
    subscribe(authed)
    ctx = authed.app.state.ctx
    with session_scope(ctx.sessions) as db:
        device = repository.devices(db)[0]
        repository.record_result(db, device.id, Result(Outcome.GONE, "HTTP 410"), datetime.now(UTC))
    assert stored(authed) == []


@pytest.mark.parametrize(
    ("agent", "label"),
    [
        (SAFARI, "Safari on iPhone"),
        ("Mozilla/5.0 (Linux; Android 14) Chrome/130.0 Mobile Safari/537.36", "Chrome on Android"),
        ("Mozilla/5.0 (Windows NT 10.0) Chrome/130.0 Safari/537.36 Edg/130.0", "Edge on Windows"),
        ("Mozilla/5.0 (X11; Linux x86_64; rv:130.0) Firefox/130.0", "Firefox on Linux"),
        ("Mozilla/5.0 (Macintosh; Intel Mac OS X) Version/18.0 Safari/605.1", "Safari on Mac"),
        ("", "Browser"),
    ],
)
def test_device_labels(agent: str, label: str) -> None:
    assert device_label(agent) == label


def test_demo_mode_has_no_notifications(tmp_path) -> None:
    settings = Settings(config_dir=tmp_path, demo_mode=True, log_level="WARNING")
    with TestClient(create_app(settings)) as client:
        setup_admin(client)
        assert client.app.state.ctx.monitor.push is None
        assert "Notifications are off in demo mode" in client.get("/settings").text
        assert "__notify__" not in client.app.state.ctx.monitor.poller.keys
        response = client.post(
            f"{API}/subscribe",
            json={"endpoint": ENDPOINT, "keys": keys()},
            headers=token(client),
        )
        assert response.status_code == 409
        assert client.get(f"{API}/bootstrap").status_code == 409


def test_the_notifier_runs_outside_demo_mode(authed: TestClient) -> None:
    assert "__notify__" in authed.app.state.ctx.monitor.poller.keys
