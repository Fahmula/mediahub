"""Encrypting, signing and sending one Web Push message (RFC 8030, 8291, 8292).

A subscription hands this server a URL to POST to, so endpoints are checked against an
allowlist of the browsers' push services rather than a denylist of private addresses. An
endpoint is a credential: log the host at most, and never an httpx exception's text.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from enum import StrEnum
from typing import Protocol
from urllib.parse import urlsplit

import http_ece
import httpx
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec
from py_vapid import Vapid02

from mediahub.push.messages import PushMessage

ENDPOINT_MAX = 2048
AUTH_BYTES = 16
P256_POINT_BYTES = 65
ALLOWED_HOSTS = frozenset({"fcm.googleapis.com"})
ALLOWED_SUFFIXES = (".push.apple.com", ".push.services.mozilla.com", ".notify.windows.com")

TTL_S = 86400
JWT_LIFETIME_S = 12 * 3600  # Apple refuses tokens valid for more than a day
JWT_REUSE_S = 6 * 3600  # and asks that they aren't refreshed more than hourly
MAX_RETRY_AFTER_S = 3600.0


class InvalidSubscription(ValueError):
    """The message is safe to show the user."""


def b64url_decode(value: str) -> bytes:
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


def b64url_encode(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode()


def endpoint_hash(endpoint: str) -> str:
    return hashlib.sha256(endpoint.encode()).hexdigest()


def endpoint_host(endpoint: str) -> str:
    """The push service's host, after checking the endpoint may be called at all."""
    if len(endpoint) > ENDPOINT_MAX or any(c <= " " for c in endpoint):
        raise InvalidSubscription("This push address isn't valid.")
    try:
        url = urlsplit(endpoint)
        port = url.port
    except ValueError as exc:
        raise InvalidSubscription("This push address isn't valid.") from exc
    host = (url.hostname or "").lower()
    if url.scheme != "https" or url.username or url.password or port not in (None, 443):
        raise InvalidSubscription("This push address isn't valid.")
    if host not in ALLOWED_HOSTS and not host.endswith(ALLOWED_SUFFIXES):
        raise InvalidSubscription(f"{host or 'This host'} isn't a known browser push service.")
    return host


def validate_keys(p256dh: str, auth: str) -> None:
    try:
        point, secret = b64url_decode(p256dh), b64url_decode(auth)
        if len(point) != P256_POINT_BYTES or len(secret) != AUTH_BYTES:
            raise ValueError("wrong length")
        ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), point)
    except (ValueError, binascii.Error) as exc:
        raise InvalidSubscription("This browser sent unusable push keys.") from exc


@dataclass(frozen=True, slots=True)
class VapidKeys:
    private: str  # base64url of the private scalar
    public: str  # base64url of the uncompressed point, as the browser wants it


def generate_vapid() -> VapidKeys:
    key = ec.generate_private_key(ec.SECP256R1())
    scalar = key.private_numbers().private_value.to_bytes(32, "big")
    point = key.public_key().public_bytes(
        serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint
    )
    return VapidKeys(private=b64url_encode(scalar), public=b64url_encode(point))


@dataclass(frozen=True, slots=True)
class Device:
    id: str
    endpoint: str
    p256dh: str
    auth: str
    host: str


class Outcome(StrEnum):
    SENT = "sent"
    GONE = "gone"  # the subscription no longer exists
    RETRY = "retry"
    FAILED = "failed"


@dataclass(frozen=True, slots=True)
class Result:
    outcome: Outcome
    error: str | None = None
    retry_after_s: float | None = None


def _retry_after(value: str | None, now: datetime) -> float | None:
    if not value:
        return None
    try:
        seconds = float(value)
    except ValueError:
        try:
            seconds = (parsedate_to_datetime(value) - now).total_seconds()
        except (TypeError, ValueError):
            return None
    return min(max(seconds, 0.0), MAX_RETRY_AFTER_S)


def classify(status: int, retry_after: str | None, reason: str | None, now: datetime) -> Result:
    if 200 <= status < 300:
        return Result(Outcome.SENT)
    error = f"HTTP {status}: {reason}" if reason else f"HTTP {status}"
    if status in (404, 410):
        return Result(Outcome.GONE, error)
    if status == 429:
        return Result(Outcome.RETRY, error, _retry_after(retry_after, now))
    if status >= 500:
        return Result(Outcome.RETRY, error)
    return Result(Outcome.FAILED, error)


def _reason(response: httpx.Response) -> str | None:
    """Apple explains a refusal as ``{"reason": ...}``; other response text isn't kept."""
    try:
        data = response.json()
    except ValueError:
        return None
    reason = data.get("reason") if isinstance(data, dict) else None
    return reason[:48] if isinstance(reason, str) else None


def encrypt(payload: bytes, device: Device) -> bytes:
    sender = ec.generate_private_key(ec.SECP256R1())
    return http_ece.encrypt(
        payload,
        private_key=sender,
        dh=b64url_decode(device.p256dh),
        auth_secret=b64url_decode(device.auth),
        version="aes128gcm",
    )


class Transport(Protocol):
    async def send(self, device: Device, message: PushMessage) -> Result: ...

    async def aclose(self) -> None: ...


class WebPushTransport:
    def __init__(
        self,
        keys: VapidKeys,
        contact: str,
        wall: Callable[[], float] = time.time,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        scalar = int.from_bytes(b64url_decode(keys.private), "big")
        # Not strict: it would refuse a contact URL that has a path.
        self._vapid = Vapid02(
            ec.derive_private_key(scalar, ec.SECP256R1()), conf={"no-strict": True}
        )
        self._contact = contact
        self._wall = wall
        self._tokens: dict[str, tuple[str, float]] = {}  # push service origin -> header, made at
        self._client = httpx.AsyncClient(
            timeout=httpx.Timeout(10.0, connect=5.0), follow_redirects=False, transport=transport
        )

    def _authorization(self, endpoint: str) -> str:
        url = urlsplit(endpoint)
        origin = f"{url.scheme}://{url.netloc}"
        now = self._wall()
        cached = self._tokens.get(origin)
        if cached is not None and now - cached[1] < JWT_REUSE_S:
            return cached[0]
        claims = {"aud": origin, "sub": self._contact, "exp": int(now) + JWT_LIFETIME_S}
        header = str(self._vapid.sign(claims)["Authorization"])
        self._tokens[origin] = (header, now)
        return header

    async def send(self, device: Device, message: PushMessage) -> Result:
        headers = {
            "Authorization": self._authorization(device.endpoint),
            "Content-Encoding": "aes128gcm",
            "Content-Type": "application/octet-stream",
            "TTL": str(TTL_S),
            "Topic": message.tag,
            "Urgency": "high" if message.urgent else "normal",
        }
        body = encrypt(message.payload(), device)
        try:
            response = await self._client.post(device.endpoint, content=body, headers=headers)
        except httpx.HTTPError as exc:
            return Result(Outcome.RETRY, type(exc).__name__)
        return classify(
            response.status_code,
            response.headers.get("retry-after"),
            None if response.is_success else _reason(response),
            datetime.fromtimestamp(self._wall(), UTC),
        )

    async def aclose(self) -> None:
        await self._client.aclose()
