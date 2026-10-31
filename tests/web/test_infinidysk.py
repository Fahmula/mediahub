"""InfiniDysk page rendered from the constructed fixture."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from mediahub.integrations.infinidysk.mapper import LiveState, build_stats, map_read
from mediahub.integrations.infinidysk.metrics import Metrics
from mediahub.models.common import ServiceStatus, ServiceType, SourceRef
from mediahub.models.snapshot import ServiceSnapshot

FIXTURE: dict[str, Any] = json.loads(
    (Path(__file__).parent.parent / "fixtures" / "infinidysk" / "constructed.json").read_text()
)


def _put(client: TestClient, live: LiveState | None) -> None:
    ctx = client.app.state.ctx  # type: ignore[attr-defined]
    source = SourceRef(
        server_id=ctx.identity.server_id,
        server_name=ctx.identity.server_name,
        instance_id="idk-1",
        instance_name="InfiniDysk",
        service_type=ServiceType.INFINIDYSK,
    )
    stats = build_stats(
        usage=FIXTURE["provider_usage"],
        overview=FIXTURE["overview"],
        metrics=Metrics.from_text(FIXTURE["metrics"]),
        live=live,
        healthy=True,
        ready=True,
    )
    now = datetime.now(UTC)
    ctx.monitor.store.put_local(
        ServiceSnapshot(
            source=source, status=ServiceStatus.OK, checked_at=now, fetched_at=now, infinidysk=stats
        )
    )


def test_page_shows_providers_cache_and_reads(authed: TestClient) -> None:
    message = next(f for f in FIXTURE["ws_frames"] if f["Topic"] == "ar")["Message"]
    read = map_read(json.loads(message)["reads"][0], speed_bps=12_000_000)
    _put(authed, LiveState(connected=True, reads=[read]))
    html = authed.get("/partials/service/infinidysk?server=local").text
    for text in ["Primary", "block.example.net", "Over limit", "42.5 MB/s", "12/50", "75%"]:
        assert text in html, text
    assert "Movie.1.2026.2160p.WEB-DL.mkv" in html and "Emby" in html
    assert "/content/movies" not in html and "172.18.0.5" not in html  # never rendered


@pytest.mark.parametrize(
    ("live", "hint"),
    [
        (LiveState(connected=True), "Nothing is streaming right now."),
        (LiveState(error="Internal key rejected by InfiniDysk"), "Live feed: Internal key"),
        (None, "Configure the backend URL and internal key"),
    ],
)
def test_empty_reads_explain_why(authed: TestClient, live: LiveState | None, hint: str) -> None:
    _put(authed, live)
    assert hint in authed.get("/partials/service/infinidysk?server=local").text


def test_idle_read_is_marked(authed: TestClient) -> None:
    message = next(f for f in FIXTURE["ws_frames"] if f["Topic"] == "ar")["Message"]
    read = map_read(json.loads(message)["reads"][0], 0.0, idle=True)
    _put(authed, LiveState(connected=True, reads=[read]))
    html = authed.get("/partials/service/infinidysk?server=local").text
    assert "may be playing from its buffer" in html and "opacity-60" in html
