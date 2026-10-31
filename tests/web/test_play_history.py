"""The Play history tab: what was recorded, filtered, paged and rendered verbatim."""

from __future__ import annotations

import re
import time
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from mediahub.config import LogFormat, Settings
from mediahub.db.session import session_scope
from mediahub.history import repository
from mediahub.integrations.demo import DEMO_PEER_ID
from mediahub.main import create_app
from mediahub.models.common import ServiceStatus, ServiceType, SourceRef
from mediahub.models.infinidysk import InfiniDyskStats
from mediahub.models.snapshot import ServiceSnapshot

from .conftest import setup_admin

NOW = datetime.now(UTC)


def _row(n: int, *, outcome: str = "failed", **overrides: Any) -> dict[str, Any]:
    started = NOW - timedelta(minutes=n + 1)
    problems = (
        []
        if outcome == "ok"
        else [{"at": started.isoformat(), "outcome": "failed", "message": f"problem {n}"}]
    )
    return {
        "id": f"00000000-0000-4000-9000-{n:012d}",
        "instance_id": "emby-1",
        "instance_name": "Emby",
        "session_id": f"s{n}",
        "item_key": f"i{n}",
        "title": f"Title {n}",
        "subtitle": None,
        "year": 2024,
        "kind": "movie",
        "user": "Sam",
        "device": "TV",
        "file_name": f"file{n}.mkv",
        "started_at": started,
        "last_seen_at": started,
        "ended_at": started,
        "position_s": 30.0,
        "duration_s": 100.0,
        "outcome": outcome,
        "problem_count": len(problems),
        "problems": problems,
        "incomplete": False,
        **overrides,
    }


def _insert(client: TestClient, *rows: dict[str, Any]) -> None:
    with session_scope(client.app.state.ctx.sessions) as db:  # type: ignore[attr-defined]
        repository.upsert_plays(db, list(rows))


@pytest.fixture
def demo(tmp_path: Path) -> Iterator[TestClient]:
    settings = Settings(
        config_dir=tmp_path / "config", log_format=LogFormat.CONSOLE, demo_mode=True
    )
    with TestClient(create_app(settings), base_url="http://testserver") as client:
        setup_admin(client)
        deadline = time.monotonic() + 5
        store = client.app.state.ctx.monitor.store  # type: ignore[attr-defined]
        while not store.get_peer(DEMO_PEER_ID) and time.monotonic() < deadline:
            time.sleep(0.02)
        yield client


def test_opens_on_plays_with_problems(authed: TestClient) -> None:
    _insert(authed, _row(1), _row(2, outcome="ok"))
    html = authed.get("/activity?tab=plays").text
    assert "Title 1" in html and "problem 1" in html and "Title 2" not in html
    assert "stopped at 30%" in html


def test_all_plays_include_clean_ones(authed: TestClient) -> None:
    _insert(authed, _row(1), _row(2, outcome="ok"))
    html = authed.get("/activity?tab=plays&show=all").text
    assert "Title 1" in html and "Title 2" in html
    assert re.search(r"All plays <span[^>]*>2<", html)
    assert re.search(r"With problems <span[^>]*>1<", html)


def test_an_open_play_says_so(authed: TestClient) -> None:
    _insert(authed, _row(1, ended_at=None))
    assert "In progress" in authed.get("/activity?tab=plays").text


def test_load_more_pages_without_gaps_or_repeats(authed: TestClient) -> None:
    same_time = NOW - timedelta(hours=1)
    rows = [_row(n, started_at=same_time) for n in range(55)]  # ties are the hard case
    _insert(authed, *rows)
    first = authed.get("/activity?tab=plays").text
    match = re.search(r'hx-get="(/partials/plays[^"]+)"', first)
    assert match
    second = authed.get(match.group(1).replace("&amp;", "&")).text
    assert "Load more" not in second
    titles = re.findall(r"Title \d+", first) + re.findall(r"Title \d+", second)
    assert sorted(titles) == sorted(f"Title {n}" for n in range(55))


def test_a_bad_cursor_is_rejected(authed: TestClient) -> None:
    assert authed.get("/partials/plays?before=nonsense").status_code == 400


def test_messages_are_verbatim_and_escaped(authed: TestClient) -> None:
    message = 'Failed: <script>alert(1)</script> "/content/a.mkv"'
    problem = {"at": NOW.isoformat(), "outcome": "failed", "message": message}
    _insert(authed, _row(1, problems=[problem], problem_count=1))
    html = authed.get("/activity?tab=plays").text
    assert "<script>alert(1)" not in html
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in html


def test_trimmed_problems_are_owned_up_to(authed: TestClient) -> None:
    _insert(authed, _row(1, problem_count=120))
    assert "119 more not kept" in authed.get("/activity?tab=plays").text


def test_an_incomplete_play_says_problems_may_be_missing(authed: TestClient) -> None:
    _insert(authed, _row(1, outcome="ok", incomplete=True))
    html = authed.get("/activity?tab=plays&show=all").text
    assert "some problems may be missing" in html


def test_the_section_is_not_polled(authed: TestClient) -> None:
    _insert(authed, _row(1))
    html = authed.get("/activity?tab=plays").text
    section = html[html.index('id="sec-activity"') - 10 : html.index('id="sec-activity"') + 80]
    assert "hx-trigger" not in section and "data-poll" not in section


def test_peers_are_told_where_their_history_is(demo: TestClient) -> None:
    html = demo.get(f"/activity?tab=plays&server={DEMO_PEER_ID}").text
    assert "stays on the server that recorded it" in html and "With problems" not in html


def test_demo_history_is_visible_and_captioned(demo: TestClient) -> None:
    html = demo.get("/activity?tab=plays&server=all").text
    assert "Dune: Part Two" in html and "Oppenheimer" not in html
    assert "Plays on MediaHub only" in html


def test_a_disconnected_feed_is_reported_verbatim(authed: TestClient) -> None:
    identity = authed.app.state.ctx.identity  # type: ignore[attr-defined]
    authed.app.state.ctx.monitor.store.put_local(  # type: ignore[attr-defined]
        ServiceSnapshot(
            source=SourceRef(
                server_id=identity.server_id,
                server_name=identity.server_name,
                instance_id="idk-1",
                instance_name="Dysk",
                service_type=ServiceType.INFINIDYSK,
            ),
            status=ServiceStatus.OK,
            infinidysk=InfiniDyskStats(live=False, feed_error="WebSocket refused (HTTP 502)"),
        )
    )
    html = authed.get("/activity?tab=plays").text
    assert "Dysk's live feed isn't connected" in html
    assert "WebSocket refused (HTTP 502)" in html


def test_dashboard_links_recent_problems_for_this_server_only(demo: TestClient) -> None:
    store = demo.app.state.ctx.monitor.store  # type: ignore[attr-defined]
    store.recent_play_problems = 2
    assert "2 plays with problems in the last 24 h" in demo.get("/?server=local").text
    html = demo.get(f"/?server={DEMO_PEER_ID}").text
    assert "with problems in the last 24 h" not in html
