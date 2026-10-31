from __future__ import annotations

import re
import time
from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from mediahub.config import LogFormat, Settings
from mediahub.main import create_app

from .conftest import setup_admin

PAGES = [
    "/",
    "/activity",
    "/activity?tab=downloads",
    "/activity?tab=history",
    "/activity?tab=streams",
    "/attention",
    "/servers",
    "/emby",
    "/sonarr",
    "/radarr",
    "/infinidysk",
    "/partials/dashboard/streams",
    "/partials/dashboard/summary",
    "/partials/activity/downloads",
    "/partials/service/infinidysk",
]


@pytest.fixture
def demo_client(tmp_path: Path) -> Iterator[TestClient]:
    settings = Settings(
        config_dir=tmp_path / "config", log_format=LogFormat.CONSOLE, demo_mode=True
    )
    with TestClient(create_app(settings), base_url="http://testserver") as c:
        setup_admin(c)
        deadline = time.monotonic() + 5
        while "Interstellar" not in c.get("/").text and time.monotonic() < deadline:
            time.sleep(0.05)
        yield c


@pytest.mark.parametrize("path", PAGES)
def test_pages_render(demo_client: TestClient, path: str) -> None:
    resp = demo_client.get(path)
    assert resp.status_code == 200, resp.text[:500]


def test_dashboard_shows_both_servers_and_issues(demo_client: TestClient) -> None:
    html = demo_client.get("/").text
    assert "Brother Server" in html
    assert "Needs attention" in html
    assert "The download is stalled with no connections" in html
    assert "sec-status" not in html and "Services online" not in html


def test_playing_media_comes_first(demo_client: TestClient) -> None:
    html = demo_client.get("/").text
    assert html.index('id="sec-streams"') < html.index('id="sec-attention"')
    assert html.index('id="sec-attention"') < html.index('id="sec-downloads"')
    assert "Now playing" in html


def test_servers_summary_comes_last(demo_client: TestClient) -> None:
    html = demo_client.get("/").text
    assert html.index('id="sec-downloads"') < html.index('id="sec-summary"')
    assert html.index('id="sec-infinidysk"') < html.index('id="sec-summary"')


def test_row_states_are_text_not_tooltips(demo_client: TestClient) -> None:
    html = demo_client.get("/partials/dashboard/streams").text
    assert '<span class="text-warning">Paused</span>' in html
    assert 'title="Paused"' not in html


def test_old_activity_tabs_still_open(demo_client: TestClient) -> None:
    page = demo_client.get("/activity?tab=history").text
    assert "Recent grabs and imports" in page and "/partials/activity/downloads" in page
    assert "/partials/activity/playback" in demo_client.get("/activity?tab=streams").text
    assert demo_client.get("/partials/activity/history").status_code == 200
    assert demo_client.get("/activity?tab=nope").status_code == 404


def test_server_select_keeps_the_query(demo_client: TestClient) -> None:
    html = demo_client.get("/activity?tab=downloads&server=local").text
    assert 'hx-get="/activity?tab=downloads"' in html


def test_status_partial_is_gone(demo_client: TestClient) -> None:
    assert demo_client.get("/partials/dashboard/status").status_code == 404


def test_scope_selection_is_remembered(demo_client: TestClient) -> None:
    resp = demo_client.get("/?server=local")
    assert "mh_server=local" in resp.headers.get("set-cookie", "")
    html = demo_client.get("/").text
    assert "Brother Server</p>" not in html  # local scope: no peer server card


def test_unknown_partial_404(demo_client: TestClient) -> None:
    assert demo_client.get("/partials/dashboard/nope").status_code == 404


def test_demo_poster_escapes_title(demo_client: TestClient) -> None:
    resp = demo_client.get("/img/demo/x.svg", params={"t": "<script>alert(1)</script>"})
    assert resp.status_code == 200
    assert "<script>" not in resp.text


def test_layout_grids_have_a_base_column_template() -> None:
    """An implicit grid column grows to its widest unbreakable line (a truncated title, a
    release name) and pushes the page sideways on phones; `grid-cols-1` is minmax(0, 1fr)."""
    templates = Path(__file__).parents[2] / "src" / "mediahub" / "templates"
    unsized = []
    for path in templates.rglob("*.html"):
        for classes in re.findall(r'class="([^"]*)"', path.read_text()):
            names = classes.split()
            if "grid" in names and not any(re.fullmatch(r"grid-cols-\S+", n) for n in names):
                unsized.append(f"{path.relative_to(templates)}: {classes}")
    assert unsized == []
