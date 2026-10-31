"""What a notification says. Everything leaving for a push service is built here.

Text goes through ``redact_text`` because it reaches a third party, encrypted or not, and is
shown on a lock screen. Links can come from a peer or an upstream health check, so only paths
on this server are kept.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Sequence
from dataclasses import dataclass

from mediahub.peers.redact import redact_text

TITLE_MAX = 120
BODY_MAX = 300
PAYLOAD_MAX = 3000  # bytes, well inside the 4 KB push services accept
URL_MAX = 512
GROUP_TITLES = 4
FALLBACK_URL = "/attention"


@dataclass(frozen=True, slots=True)
class Alert:
    title: str
    detail: str | None
    url: str


@dataclass(frozen=True, slots=True)
class PushMessage:
    tag: str  # a later notification with the same tag replaces this one on the device
    title: str
    body: str
    url: str
    urgent: bool  # sent high-priority, and makes a sound even when replacing an alert

    def payload(self) -> bytes:
        data = {
            "title": self.title,
            "body": self.body,
            "url": self.url,
            "tag": self.tag,
            "renotify": self.urgent,
        }
        raw = json.dumps(data).encode()
        if len(raw) > PAYLOAD_MAX:
            raw = json.dumps({**data, "body": ""}).encode()
        return raw


def _clip(text: str, limit: int) -> str:
    text = " ".join(redact_text(text).split())
    return text if len(text) <= limit else f"{text[: limit - 1]}…"


def safe_url(link: str | None) -> str:
    """A path on this server, or the attention page: never somewhere else."""
    if (
        not link
        or len(link) > URL_MAX
        or not link.startswith("/")
        or link.startswith("//")
        or "\\" in link
    ):
        return FALLBACK_URL
    return link


def group_tag(keys: Sequence[str]) -> str:
    return hashlib.sha256("|".join(sorted(keys)).encode()).hexdigest()[:32]


def _listing(titles: Sequence[str]) -> str:
    shown = [_clip(t, TITLE_MAX) for t in titles[:GROUP_TITLES]]
    if extra := len(titles) - len(shown):
        shown.append(f"+{extra} more")
    return "\n".join(shown)


def raise_message(tag: str, alerts: Sequence[Alert]) -> PushMessage:
    if len(alerts) == 1:
        alert = alerts[0]
        return PushMessage(
            tag=tag,
            title=_clip(alert.title, TITLE_MAX),
            body=_clip(alert.detail or "", BODY_MAX),
            url=safe_url(alert.url),
            urgent=True,
        )
    return PushMessage(
        tag=tag,
        title=f"{len(alerts)} problems need attention",
        body=_listing([a.title for a in alerts])[:BODY_MAX],
        url=FALLBACK_URL,
        urgent=True,
    )


def clear_message(tag: str, cleared: Sequence[str], active: Sequence[str]) -> PushMessage:
    """``active`` is what's left of a grouped alert: this message replaces it on the device."""
    if len(cleared) == 1:
        title = _clip(f"Resolved: {cleared[0]}", TITLE_MAX)
        body = ""
    else:
        title = f"{len(cleared)} problems resolved"
        body = _listing(cleared)
    if active:
        still = f"Still active:\n{_listing(active)}"
        body = f"{body}\n{still}" if body else still
    return PushMessage(tag=tag, title=title, body=body[:BODY_MAX], url=FALLBACK_URL, urgent=False)


def test_message() -> PushMessage:
    return PushMessage(
        tag="test",
        title="MediaHub notifications are on",
        body="You'll hear about errors here, and when they clear.",
        url="/settings",
        urgent=True,
    )
