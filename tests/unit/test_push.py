"""Encrypting, signing and delivering notifications, and what's kept about devices."""

from __future__ import annotations

import base64
import json
import logging
from datetime import UTC, datetime, timedelta
from pathlib import Path

import http_ece
import httpx
import pytest
import respx
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec
from sqlalchemy import text, update
from sqlalchemy.orm import Session, sessionmaker

from mediahub.db.models import PushSubscription, PushVapid
from mediahub.db.session import make_engine, make_sessionmaker, run_migrations, session_scope
from mediahub.push import messages, repository
from mediahub.push.dispatcher import RETRY_DELAYS_S, PushDispatcher
from mediahub.push.messages import Alert, PushMessage, clear_message, raise_message, safe_url
from mediahub.push.webpush import (
    MAX_RETRY_AFTER_S,
    Device,
    InvalidSubscription,
    Outcome,
    Result,
    VapidKeys,
    WebPushTransport,
    b64url_decode,
    b64url_encode,
    classify,
    encrypt,
    endpoint_host,
    generate_vapid,
    validate_keys,
)

ENDPOINT = "https://fcm.googleapis.com/fcm/send/abc123"
NOW = datetime(2026, 1, 1, tzinfo=UTC)


def browser_keys() -> tuple[ec.EllipticCurvePrivateKey, str, str]:
    private = ec.generate_private_key(ec.SECP256R1())
    point = private.public_key().public_bytes(
        serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint
    )
    return private, b64url_encode(point), b64url_encode(b"0123456789abcdef")


def device(device_id: str = "d1", endpoint: str = ENDPOINT) -> Device:
    _, p256dh, auth = browser_keys()
    return Device(device_id, endpoint, p256dh, auth, "fcm.googleapis.com")


MESSAGE = PushMessage("tag", "Sonarr is unreachable", "Refused", "/attention", True)


# ----- encryption and signing ------------------------------------------------------------------
def test_browser_can_decrypt_what_is_sent() -> None:
    private, p256dh, auth = browser_keys()
    target = Device("d", ENDPOINT, p256dh, auth, "h")
    body = encrypt(b'{"title":"x"}', target)
    assert (
        http_ece.decrypt(
            body, private_key=private, auth_secret=b64url_decode(auth), version="aes128gcm"
        )
        == b'{"title":"x"}'
    )


def claims(header: str) -> dict[str, object]:
    token = header.removeprefix("vapid t=").split(",")[0]
    return json.loads(b64url_decode(token.split(".")[1]))


def test_jwt_stays_inside_apples_limits() -> None:
    transport = WebPushTransport(
        generate_vapid(), "https://github.com/Fahmula/mediahub", lambda: 1000.0
    )
    sent = claims(transport._authorization("https://web.push.apple.com/abc"))
    assert sent["aud"] == "https://web.push.apple.com"
    assert sent["sub"] == "https://github.com/Fahmula/mediahub"
    assert 0 < int(sent["exp"]) - 1000 < 86400


def test_contact_may_be_a_url_with_a_path_or_mailto() -> None:
    for contact in ("https://github.com/Fahmula/mediahub", "mailto:me@example.com"):
        WebPushTransport(generate_vapid(), contact)._authorization(ENDPOINT)


def test_jwt_is_reused_per_origin_then_refreshed() -> None:
    now = [1000.0]
    transport = WebPushTransport(generate_vapid(), "mailto:a@b.c", lambda: now[0])
    first = transport._authorization(ENDPOINT)
    assert transport._authorization(f"{ENDPOINT}/other") == first
    assert transport._authorization("https://web.push.apple.com/x") != first
    now[0] += 7 * 3600
    assert transport._authorization(ENDPOINT) != first


def test_signing_key_survives_being_stored() -> None:
    keys = generate_vapid()
    assert len(b64url_decode(keys.public)) == 65
    assert len(b64url_decode(keys.private)) == 32
    header = WebPushTransport(keys, "mailto:a@b.c")._authorization(ENDPOINT)
    assert header.endswith(f"k={keys.public}")


# ----- the allowlist ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "endpoint",
    [
        ENDPOINT,
        "https://web.push.apple.com/QGm",
        "https://updates.push.services.mozilla.com/wpush/v2/abc",
        "https://wns2-par02p.notify.windows.com/w/?token=abc",
        "https://fcm.googleapis.com:443/x",
    ],
)
def test_browser_push_services_are_allowed(endpoint: str) -> None:
    assert endpoint_host(endpoint)


@pytest.mark.parametrize(
    "endpoint",
    [
        "http://fcm.googleapis.com/x",
        "https://evil.example/x",
        "https://fcm.googleapis.com.evil.example/x",
        "https://notfcm.googleapis.com/x",
        "https://push.apple.com.evil.example/x",
        "https://user:pw@fcm.googleapis.com/x",
        "https://evil.example\\@fcm.googleapis.com/x",
        "https://fcm.googleapis.com:8443/x",
        "https://127.0.0.1/x",
        "https://[::1]/x",
        "https://169.254.169.254/latest",
        "https://localhost/x",
        "https://fcm.googleapis.com/\nx",
        "https://fcm.googleapis.com:bad/x",
        "file:///etc/passwd",
        "",
        f"https://fcm.googleapis.com/{'x' * 2048}",
    ],
)
def test_anything_else_is_refused(endpoint: str) -> None:
    with pytest.raises(InvalidSubscription):
        endpoint_host(endpoint)


def test_valid_browser_keys_pass() -> None:
    _, p256dh, auth = browser_keys()
    validate_keys(p256dh, auth)


@pytest.mark.parametrize(
    ("p256dh", "auth"),
    [
        ("", ""),
        (b64url_encode(b"\x04" + b"\x00" * 64), b64url_encode(b"0" * 16)),  # not on the curve
        (b64url_encode(b"short"), b64url_encode(b"0" * 16)),
        (None, b64url_encode(b"0" * 15)),
        (None, "not base64!"),
    ],
)
def test_unusable_keys_are_refused(p256dh: str | None, auth: str) -> None:
    with pytest.raises(InvalidSubscription):
        validate_keys(p256dh or browser_keys()[1], auth)


# ----- sending ---------------------------------------------------------------------------------
@pytest.mark.parametrize(
    ("status", "outcome"),
    [
        (201, Outcome.SENT),
        (404, Outcome.GONE),
        (410, Outcome.GONE),
        (413, Outcome.FAILED),
        (429, Outcome.RETRY),
        (500, Outcome.RETRY),
        (503, Outcome.RETRY),
        (301, Outcome.FAILED),
        (400, Outcome.FAILED),
        (401, Outcome.FAILED),
        (403, Outcome.FAILED),
    ],
)
def test_response_handling(status: int, outcome: Outcome) -> None:
    assert classify(status, None, None, NOW).outcome is outcome


def test_retry_after_in_seconds() -> None:
    assert classify(429, "120", None, NOW).retry_after_s == 120


def test_retry_after_as_a_date() -> None:
    when = (NOW + timedelta(minutes=2)).strftime("%a, %d %b %Y %H:%M:%S GMT")
    assert classify(429, when, None, NOW).retry_after_s == 120


def test_retry_after_is_capped() -> None:
    assert classify(429, "999999", None, NOW).retry_after_s == MAX_RETRY_AFTER_S


def test_unreadable_retry_after_is_ignored() -> None:
    assert classify(429, "soon", None, NOW).retry_after_s is None


@respx.mock
async def test_sends_what_a_push_service_needs() -> None:
    route = respx.post(ENDPOINT).mock(return_value=httpx.Response(201))
    transport = WebPushTransport(generate_vapid(), "mailto:a@b.c")
    result = await transport.send(device(), MESSAGE)
    await transport.aclose()
    assert result.outcome is Outcome.SENT
    headers = route.calls[0].request.headers
    assert headers["content-encoding"] == "aes128gcm"
    assert headers["authorization"].startswith("vapid t=")
    assert headers["ttl"] == "86400"
    assert headers["topic"] == "tag"
    assert headers["urgency"] == "high"
    assert MESSAGE.title.encode() not in route.calls[0].request.content


@respx.mock
async def test_clears_are_not_urgent() -> None:
    route = respx.post(ENDPOINT).mock(return_value=httpx.Response(201))
    transport = WebPushTransport(generate_vapid(), "mailto:a@b.c")
    await transport.send(device(), clear_message("tag", ["x"], []))
    await transport.aclose()
    assert route.calls[0].request.headers["urgency"] == "normal"


@respx.mock
async def test_apples_reason_is_reported_verbatim() -> None:
    respx.post(ENDPOINT).mock(return_value=httpx.Response(403, json={"reason": "BadJwtToken"}))
    transport = WebPushTransport(generate_vapid(), "mailto:a@b.c")
    result = await transport.send(device(), MESSAGE)
    await transport.aclose()
    assert result == Result(Outcome.FAILED, "HTTP 403: BadJwtToken")


@respx.mock
async def test_redirects_are_not_followed() -> None:
    respx.post(ENDPOINT).mock(
        return_value=httpx.Response(302, headers={"location": "http://169.254.169.254/"})
    )
    transport = WebPushTransport(generate_vapid(), "mailto:a@b.c")
    result = await transport.send(device(), MESSAGE)
    await transport.aclose()
    assert result.outcome is Outcome.FAILED


@respx.mock
async def test_connection_errors_retry_without_quoting_the_endpoint() -> None:
    respx.post(ENDPOINT).mock(side_effect=httpx.ConnectError(f"failed: {ENDPOINT}"))
    transport = WebPushTransport(generate_vapid(), "mailto:a@b.c")
    result = await transport.send(device(), MESSAGE)
    await transport.aclose()
    assert result == Result(Outcome.RETRY, "ConnectError")


# ----- messages --------------------------------------------------------------------------------
def test_single_alert_keeps_its_link_and_detail() -> None:
    message = raise_message("t", [Alert("Sonarr is failing", "Refused", "/sonarr?instance=1")])
    assert (message.title, message.body) == ("Sonarr is failing", "Refused")
    assert message.url == "/sonarr?instance=1"


def test_group_lists_a_few_then_counts() -> None:
    message = raise_message("t", [Alert(f"Problem {n}", None, "/x") for n in range(6)])
    assert message.title == "6 problems need attention"
    assert message.body.splitlines()[-1] == "+2 more"
    assert message.url == "/attention"


@pytest.mark.parametrize(
    "link",
    [
        None,
        "",
        "https://wiki.servarr.com/x",
        "//evil.example/x",
        "/\\evil.example/x",
        "javascript:alert(1)",
        f"/{'x' * 600}",
    ],
)
def test_links_elsewhere_become_the_attention_page(link: str | None) -> None:
    assert safe_url(link) == "/attention"


def test_paths_never_reach_a_push_service() -> None:
    message = raise_message("t", [Alert("Bad /mnt/media/tv/show.mkv", '"/data/a b/c"', "/x")])
    assert "/mnt" not in message.title
    assert "/data" not in message.body
    cleared = clear_message("t", ["Bad /mnt/media/tv/show.mkv"], ["Worse /srv/private/file"])
    assert "/mnt" not in cleared.title
    assert "/srv" not in cleared.body


def test_long_text_is_cut() -> None:
    message = raise_message("t", [Alert("x" * 500, "y" * 900, "/x")])
    assert len(message.title) == messages.TITLE_MAX
    assert len(message.body) == messages.BODY_MAX


def test_payload_stays_under_the_limit_whatever_the_text() -> None:
    wide = "\U0001f4a5" * 900
    payload = raise_message("t" * 32, [Alert(wide, wide, "/x")]).payload()
    assert len(payload) <= messages.PAYLOAD_MAX
    assert json.loads(payload)["title"]


def test_payload_tells_the_worker_whether_to_ring() -> None:
    assert json.loads(MESSAGE.payload())["renotify"] is True
    assert json.loads(clear_message("t", ["x"], []).payload())["renotify"] is False


# ----- the dispatcher --------------------------------------------------------------------------
class FakeTransport:
    def __init__(self) -> None:
        self.results: dict[str, list[Result]] = {}
        self.sent: list[tuple[str, str]] = []
        self.closed = False

    async def send(self, target: Device, message: PushMessage) -> Result:
        self.sent.append((target.id, message.title))
        queued = self.results.get(target.endpoint)
        return queued.pop(0) if queued else Result(Outcome.SENT)

    async def aclose(self) -> None:
        self.closed = True


@pytest.fixture
def sessions(tmp_path: Path) -> sessionmaker[Session]:
    engine = make_engine(f"sqlite:///{tmp_path / 'test.db'}")
    run_migrations(engine)
    return make_sessionmaker(engine)


def add_device(sessions: sessionmaker[Session], endpoint: str = ENDPOINT) -> str:
    _, p256dh, auth = browser_keys()
    with session_scope(sessions) as db:
        row = repository.upsert_subscription(
            db, endpoint=endpoint, p256dh=p256dh, auth=auth, label="Phone", session_hash="s1"
        )
        return row.id


class Rig:
    def __init__(self, sessions: sessionmaker[Session]) -> None:
        self.now = 0.0
        self.transport = FakeTransport()
        self.dispatcher = PushDispatcher(
            sessions, self.transport, "pub", clock=lambda: self.now, wall=lambda: NOW
        )

    async def flush(self) -> None:
        for queued in list(self.dispatcher._queue.values()):
            if queued.not_before <= self.now:
                await self.dispatcher.deliver(queued)


@pytest.fixture
def rig(sessions: sessionmaker[Session]) -> Rig:
    return Rig(sessions)


async def test_every_device_gets_it_and_success_is_recorded(
    rig: Rig, sessions: sessionmaker[Session]
) -> None:
    add_device(sessions)
    add_device(sessions, "https://web.push.apple.com/two")
    rig.dispatcher.enqueue(MESSAGE)
    await rig.flush()
    assert len(rig.transport.sent) == 2
    assert rig.dispatcher.queued == []
    with session_scope(sessions) as db:
        assert all(r.last_success_at == NOW for r in repository.list_subscriptions(db))


async def test_only_the_failed_device_is_retried(rig: Rig, sessions: sessionmaker[Session]) -> None:
    other = add_device(sessions)
    failing = "https://web.push.apple.com/two"
    failing_id = add_device(sessions, failing)
    rig.transport.results[failing] = [Result(Outcome.RETRY, "HTTP 503")]
    rig.dispatcher.enqueue(MESSAGE)
    await rig.flush()
    rig.transport.sent.clear()
    await rig.flush()
    assert rig.transport.sent == []  # not due yet
    rig.now += RETRY_DELAYS_S[0]
    await rig.flush()
    assert [device_id for device_id, _ in rig.transport.sent] == [failing_id]
    assert other != failing_id
    assert rig.dispatcher.queued == []


async def test_retries_run_out(rig: Rig, sessions: sessionmaker[Session]) -> None:
    add_device(sessions)
    rig.transport.results[ENDPOINT] = [Result(Outcome.RETRY, "HTTP 503")] * 10
    rig.dispatcher.enqueue(MESSAGE)
    await rig.flush()
    for delay in RETRY_DELAYS_S:
        rig.now += delay
        await rig.flush()
    assert len(rig.transport.sent) == len(RETRY_DELAYS_S) + 1
    assert rig.dispatcher.queued == []


async def test_retry_after_outlasts_the_schedule(rig: Rig, sessions: sessionmaker[Session]) -> None:
    add_device(sessions)
    rig.transport.results[ENDPOINT] = [Result(Outcome.RETRY, "HTTP 429", 900.0)]
    rig.dispatcher.enqueue(MESSAGE)
    await rig.flush()
    rig.now += RETRY_DELAYS_S[0]
    await rig.flush()
    assert len(rig.transport.sent) == 1
    rig.now += 900
    await rig.flush()
    assert len(rig.transport.sent) == 2


async def test_a_dead_subscription_is_deleted(rig: Rig, sessions: sessionmaker[Session]) -> None:
    add_device(sessions)
    rig.transport.results[ENDPOINT] = [Result(Outcome.GONE, "HTTP 410")]
    rig.dispatcher.enqueue(MESSAGE)
    await rig.flush()
    with session_scope(sessions) as db:
        assert repository.list_subscriptions(db) == []


async def test_failure_is_shown_and_success_clears_it(
    rig: Rig, sessions: sessionmaker[Session]
) -> None:
    add_device(sessions)
    rig.transport.results[ENDPOINT] = [Result(Outcome.FAILED, "HTTP 403: BadJwtToken")]
    rig.dispatcher.enqueue(MESSAGE)
    await rig.flush()
    with session_scope(sessions) as db:
        assert repository.list_subscriptions(db)[0].last_error == "HTTP 403: BadJwtToken"
    rig.dispatcher.enqueue(MESSAGE)
    await rig.flush()
    with session_scope(sessions) as db:
        assert repository.list_subscriptions(db)[0].last_error is None


async def test_a_device_failing_for_a_month_is_dropped(
    rig: Rig, sessions: sessionmaker[Session]
) -> None:
    add_device(sessions)
    with session_scope(sessions) as db:
        db.execute(update(PushSubscription).values(created_at=NOW - timedelta(days=31)))
    rig.transport.results[ENDPOINT] = [Result(Outcome.FAILED, "HTTP 403")]
    rig.dispatcher.enqueue(MESSAGE)
    await rig.flush()
    with session_scope(sessions) as db:
        assert repository.list_subscriptions(db) == []


async def test_a_device_with_nothing_to_receive_is_kept(
    rig: Rig, sessions: sessionmaker[Session]
) -> None:
    add_device(sessions)
    with session_scope(sessions) as db:
        db.execute(update(PushSubscription).values(created_at=NOW - timedelta(days=365)))
    rig.dispatcher.enqueue(MESSAGE)
    await rig.flush()
    with session_scope(sessions) as db:
        assert len(repository.list_subscriptions(db)) == 1


async def test_a_queued_message_is_replaced_by_its_tag(
    rig: Rig, sessions: sessionmaker[Session]
) -> None:
    add_device(sessions)
    rig.transport.results[ENDPOINT] = [Result(Outcome.RETRY, "HTTP 503")]
    rig.dispatcher.enqueue(MESSAGE)
    await rig.flush()
    rig.dispatcher.enqueue(clear_message(MESSAGE.tag, ["Sonarr is unreachable"], []))
    await rig.flush()
    assert rig.dispatcher.queued == []
    assert rig.transport.sent[-1][1] == "Resolved: Sonarr is unreachable"


async def test_nobody_subscribed_sends_nothing(rig: Rig) -> None:
    rig.dispatcher.enqueue(MESSAGE)
    await rig.flush()
    assert rig.transport.sent == []
    assert rig.dispatcher.queued == []


async def test_a_database_error_keeps_the_message(
    rig: Rig, sessions: sessionmaker[Session], monkeypatch: pytest.MonkeyPatch
) -> None:
    add_device(sessions)
    rig.dispatcher.enqueue(MESSAGE)

    def fail(_db: Session) -> list[Device]:
        raise RuntimeError(f"cannot read {ENDPOINT}")

    with monkeypatch.context() as patch:
        patch.setattr(repository, "devices", fail)
        await rig.flush()
    assert rig.dispatcher.queued == [MESSAGE]
    rig.now += RETRY_DELAYS_S[0]
    await rig.flush()
    assert rig.dispatcher.queued == []


async def test_task_sends_then_stops_cleanly(rig: Rig, sessions: sessionmaker[Session]) -> None:
    add_device(sessions)
    rig.dispatcher.start()
    rig.dispatcher.enqueue(MESSAGE)
    await rig.dispatcher.stop()
    assert len(rig.transport.sent) == 1
    assert rig.transport.closed


async def test_endpoint_never_reaches_the_logs(
    rig: Rig, sessions: sessionmaker[Session], caplog: pytest.LogCaptureFixture
) -> None:
    add_device(sessions)
    rig.transport.results[ENDPOINT] = [Result(Outcome.RETRY, "ConnectError")]
    caplog.set_level(logging.DEBUG)
    rig.dispatcher.enqueue(MESSAGE)
    await rig.flush()
    assert ENDPOINT not in caplog.text


# ----- stored state ----------------------------------------------------------------------------
def test_signing_key_is_created_once(sessions: sessionmaker[Session]) -> None:
    with session_scope(sessions) as db:
        first = repository.ensure_vapid(db)
    with session_scope(sessions) as db:
        assert repository.ensure_vapid(db) == first


def test_lost_signing_key_is_replaced_along_with_its_subscriptions(
    sessions: sessionmaker[Session],
) -> None:
    with session_scope(sessions) as db:
        first = repository.ensure_vapid(db)
    add_device(sessions)
    with session_scope(sessions) as db:
        db.execute(text("UPDATE push_vapid SET private_key = 'not-decryptable'"))
    with session_scope(sessions) as db:
        second = repository.ensure_vapid(db)
    assert isinstance(second, VapidKeys)
    assert second.public != first.public
    with session_scope(sessions) as db:
        assert repository.list_subscriptions(db) == []
        assert db.get(PushVapid, 1).public_key == second.public


def test_undecryptable_subscription_is_dropped(sessions: sessionmaker[Session]) -> None:
    add_device(sessions)
    with session_scope(sessions) as db:
        db.execute(text("UPDATE push_subscriptions SET secrets = 'not-decryptable'"))
    with session_scope(sessions) as db:
        assert repository.devices(db) == []
    with session_scope(sessions) as db:
        assert repository.list_subscriptions(db) == []


def test_secrets_are_encrypted_at_rest(sessions: sessionmaker[Session]) -> None:
    add_device(sessions)
    with session_scope(sessions) as db:
        stored = db.execute(
            text("SELECT secrets, endpoint_hash, host FROM push_subscriptions")
        ).one()
    assert "fcm/send" not in stored.secrets
    assert "fcm/send" not in stored.endpoint_hash
    assert "fcm/send" not in stored.host
    assert base64.urlsafe_b64decode(stored.secrets + "=" * (-len(stored.secrets) % 4))


def test_same_endpoint_updates_in_place(sessions: sessionmaker[Session]) -> None:
    first = add_device(sessions)
    assert add_device(sessions) == first


def test_rotated_endpoint_keeps_the_row(sessions: sessionmaker[Session]) -> None:
    first = add_device(sessions)
    _, p256dh, auth = browser_keys()
    new = "https://fcm.googleapis.com/fcm/send/rotated"
    with session_scope(sessions) as db:
        row = repository.upsert_subscription(
            db,
            endpoint=new,
            p256dh=p256dh,
            auth=auth,
            label="Phone",
            session_hash="s1",
            old_endpoint=ENDPOINT,
        )
        assert row.id == first
    with session_scope(sessions) as db:
        assert [d.endpoint for d in repository.devices(db)] == [new]


def test_device_limit(sessions: sessionmaker[Session]) -> None:
    for n in range(repository.MAX_DEVICES):
        add_device(sessions, f"https://fcm.googleapis.com/{n}")
    with pytest.raises(repository.TooManyDevices):
        add_device(sessions, "https://fcm.googleapis.com/one-too-many")
