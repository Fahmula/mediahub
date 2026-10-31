from __future__ import annotations

import json
import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from mediahub.integrations import image_refs
from mediahub.integrations.emby.mapper import map_session
from mediahub.models.common import ServiceStatus, ServiceType, SourceRef
from mediahub.models.infinidysk import (
    ActiveRead,
    InfiniDyskStats,
    ProblemOutcome,
    ReadProblem,
)
from mediahub.models.playback import MediaKind, NextCheckState, NextEpisodeCheck
from mediahub.models.snapshot import ServiceSnapshot

FIXTURES = Path(__file__).parent.parent / "fixtures" / "emby"


class FakeIntegration:
    def __init__(self, result: tuple[bytes, str] | None) -> None:
        self.result = result
        self.calls: list[str] = []

    async def fetch_image(self, ref: str) -> tuple[bytes, str] | None:
        self.calls.append(ref)
        return self.result


def _url(instance_id: str = "inst-1", item_id: str = "123", tag: str = "abc") -> str:
    url = image_refs.image_url(instance_id, item_id, tag)
    assert url is not None
    return url


def _use(client: TestClient, monkeypatch: pytest.MonkeyPatch, fake: FakeIntegration) -> None:
    monitor = client.app.state.ctx.monitor  # type: ignore[attr-defined]
    monkeypatch.setattr(monitor, "integration", lambda _id: fake)


def test_image_requires_valid_signature(authed: TestClient) -> None:
    *base, _ = _url().split("/")
    assert authed.get("/".join([*base, "forged"])).status_code == 404


def test_image_unknown_instance(authed: TestClient) -> None:
    assert authed.get(_url()).status_code == 404


def test_image_is_proxied_and_cached(authed: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    fake = FakeIntegration((b"\xff\xd8jpeg", "image/jpeg"))
    _use(authed, monkeypatch, fake)
    for _ in range(2):
        resp = authed.get(_url())
        assert resp.status_code == 200
        assert resp.content == b"\xff\xd8jpeg"
        assert resp.headers["content-type"] == "image/jpeg"
        assert "immutable" in resp.headers["cache-control"]
    assert fake.calls == ["123/abc"]  # second request served from the disk cache


def test_image_rejects_svg(authed: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    _use(authed, monkeypatch, FakeIntegration((b"<svg/>", "image/svg+xml")))
    assert authed.get(_url()).status_code == 404


def test_image_requires_login(client: TestClient) -> None:
    resp = client.get(_url(), follow_redirects=False)
    assert resp.status_code in (303, 307)


def _captured_snapshot(client: TestClient, name: str) -> ServiceSnapshot:
    identity = client.app.state.ctx.identity  # type: ignore[attr-defined]
    source = SourceRef(
        server_id=identity.server_id,
        server_name=identity.server_name,
        instance_id="emby-1",
        instance_name="Emby",
        service_type=ServiceType.EMBY,
    )
    raw: list[Any] = json.loads((FIXTURES / f"{name}.json").read_text())["sessions"]
    sessions = [
        map_session(s, source, lambda i, t: image_refs.image_url("emby-1", i, t)) for s in raw
    ]
    now = datetime.now(UTC)
    return ServiceSnapshot(
        source=source,
        status=ServiceStatus.OK,
        version="4.11.0.9",
        checked_at=now,
        fetched_at=now,
        poll_interval_s=5,
        sessions=[s for s in sessions if s is not None],
    )


@pytest.mark.parametrize(
    ("fixture", "expected"),
    [
        ("show-and-livetv", ["Series 1", "S01E01 · Episode 1", "Channel 1", "Live"]),
        ("movie-and-paused", ["Movie 1", "Paused", "transcoded 3%"]),
        ("hdr-and-direct-play", ["Dolby Vision", "Video copied", "audio codec not supported"]),
        ("hdr-tonemap", ["Hardware decode + encode"]),
    ],
)
def test_emby_page_renders_real_sessions(
    authed: TestClient, fixture: str, expected: list[str]
) -> None:
    store = authed.app.state.ctx.monitor.store  # type: ignore[attr-defined]
    store.put_local(_captured_snapshot(authed, fixture))
    html = authed.get("/partials/service/emby?server=local").text
    for text in expected:
        assert text in html, text
    assert "/img/emby-1/" in html


@pytest.mark.parametrize(
    ("check", "expected"),
    [
        (
            NextEpisodeCheck(state=NextCheckState.QUEUED, code="S01E02", name="Episode 2"),
            "Next S01E02 · health check queued",
        ),
        (
            NextEpisodeCheck(
                state=NextCheckState.NOT_FOUND, code="S01E02", message="File no longer exists."
            ),
            "Next S01E02 · not in InfiniDysk · File no longer exists.",
        ),
        (NextEpisodeCheck(state=NextCheckState.NO_NEXT), "No next episode in the library"),
        (
            NextEpisodeCheck(state=NextCheckState.CHECKING, code="S01E02", progress=40),
            "Next S01E02 · being checked · 40%",
        ),
        (
            NextEpisodeCheck(state=NextCheckState.HEALTHY, code="S01E02"),
            "Next S01E02 · healthy",
        ),
        (
            NextEpisodeCheck(
                state=NextCheckState.REPAIRED_PAR2, code="S01E02", message="Repaired from PAR2."
            ),
            "Next S01E02 · repaired via PAR2 · Repaired from PAR2.",
        ),
        (
            NextEpisodeCheck(state=NextCheckState.ERROR, message="Connection refused"),
            "Next episode · couldn&#39;t request a health check · Connection refused",
        ),
    ],
)
def test_stream_card_shows_the_next_episode_check(
    authed: TestClient, check: NextEpisodeCheck, expected: str
) -> None:
    store = authed.app.state.ctx.monitor.store  # type: ignore[attr-defined]
    snapshot = _captured_snapshot(authed, "show-and-livetv")
    store.put_local(snapshot)
    episode = next(s for s in snapshot.sessions if s.media.kind is MediaKind.EPISODE)
    assert episode.item_id
    html = authed.get("/partials/service/emby?server=local").text
    assert "Next S01E02" not in html and "No next episode" not in html
    store.put_next_check(("emby-1", episode.session_id, "another-item"), check)
    assert expected not in authed.get("/partials/service/emby?server=local").text
    store.put_next_check(("emby-1", episode.session_id, episode.item_id), check)
    html = authed.get("/partials/service/emby?server=local").text
    assert expected in html
    assert html.count(expected) == 1  # visible text, not a tooltip


def test_layout_toggle_is_remembered(authed: TestClient) -> None:
    store = authed.app.state.ctx.monitor.store  # type: ignore[attr-defined]
    store.put_local(_captured_snapshot(authed, "movie-and-paused"))
    assert "md:grid-cols-2" in authed.get("/partials/service/emby").text  # detailed by default
    page = authed.get("/emby?layout=compact")
    assert "mh_layout=compact" in page.headers.get("set-cookie", "")
    assert re.search(r'href="\?layout=compact"[^>]*aria-current="true"', page.text)
    assert re.search(r'hx-get="/partials/service/emby\?[^"]*layout=compact"', page.text)
    partial = authed.get("/partials/service/emby").text  # cookie now carries the choice
    assert "md:grid-cols-2" not in partial and "Movie 1" in partial
    assert "Detailed" in authed.get("/emby?layout=bogus").text


def _infinidysk_snapshot(client: TestClient, problem: str | None) -> ServiceSnapshot:
    identity = client.app.state.ctx.identity  # type: ignore[attr-defined]
    problems = (
        [ReadProblem(at=datetime.now(UTC), outcome=ProblemOutcome.FAILED, message=problem)]
        if problem
        else []
    )
    return ServiceSnapshot(
        source=SourceRef(
            server_id=identity.server_id,
            server_name=identity.server_name,
            instance_id="idk-1",
            instance_name="InfiniDysk",
            service_type=ServiceType.INFINIDYSK,
        ),
        status=ServiceStatus.OK,
        infinidysk=InfiniDyskStats(
            live=True,
            reads=[
                ActiveRead(read_id="r1", file_name="file", providers=["Primary"], problems=problems)
            ],
        ),
    )


def test_stream_health_replaces_transcode_bar(authed: TestClient) -> None:
    store = authed.app.state.ctx.monitor.store  # type: ignore[attr-defined]
    store.put_local(_captured_snapshot(authed, "movie-and-paused"))  # Movie 1 plays file.strm
    store.put_local(_infinidysk_snapshot(authed, 'File "file" has missing articles: gone'))
    html = authed.get("/partials/service/emby").text
    assert "Playback problem" in html and "has missing articles: gone" in html
    assert "Transcoding audio only" not in html  # the old bar
    assert "Transcode (" in html and "transcoded 3%" in html  # folded into the details
    rows = authed.get("/partials/dashboard/streams").text
    assert "InfiniDysk: Playback problem" in rows

    store.put_local(_infinidysk_snapshot(authed, None))
    html = authed.get("/activity?tab=playback").text
    assert "No problems reported" in html and "Served by Primary" in html
