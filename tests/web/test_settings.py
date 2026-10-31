from __future__ import annotations

import re

import httpx
import pytest
import respx
from fastapi.testclient import TestClient
from sqlalchemy import text

from .conftest import PASSWORD, csrf_from

SECRET = "sonarr-secret-key-123"


@pytest.fixture
def sonarr_api() -> respx.MockRouter:
    with respx.mock(assert_all_called=False) as router:
        router.get("http://sonarr:8989/api/v3/system/status").mock(
            return_value=httpx.Response(200, json={"appName": "Sonarr", "version": "4.0.20"})
        )
        router.get("http://sonarr:8989/api/v3/health").mock(
            return_value=httpx.Response(200, json=[])
        )
        yield router


def _token(client: TestClient) -> str:
    return csrf_from(client.get("/settings").text)


def _create(client: TestClient, **overrides: str) -> httpx.Response:
    data = {
        "service_type": "sonarr",
        "name": "TV",
        "base_url": "http://sonarr:8989/",
        "secret_api_key": SECRET,
        "enabled": "on",
        "verify_tls": "on",
        "csrf_token": _token(client),
        **overrides,
    }
    return client.post("/servers/services/new", data=data, follow_redirects=False)


def _instance_id(client: TestClient) -> str:
    match = re.search(r'href="/servers/services/([0-9a-f-]{36})"', client.get("/servers").text)
    assert match
    return match.group(1)


def test_create_edit_and_secrets_never_rendered(
    authed: TestClient, sonarr_api: respx.MockRouter
) -> None:
    resp = _create(authed)
    assert resp.status_code == 303, resp.text
    assert "TV" in authed.get("/servers").text
    instance_id = _instance_id(authed)

    edit = authed.get(f"/servers/services/{instance_id}").text
    assert SECRET not in edit
    assert "saved — leave blank to keep" in edit
    for page in ("/", "/sonarr", "/servers", "/settings"):
        assert SECRET not in authed.get(page).text

    # Editing without re-entering the key keeps it.
    resp = authed.post(
        f"/servers/services/{instance_id}",
        data={
            "name": "TV Shows",
            "base_url": "http://sonarr:8989",
            "enabled": "on",
            "verify_tls": "on",
            "csrf_token": _token(authed),
        },
        follow_redirects=False,
    )
    assert resp.status_code == 303, resp.text
    assert sonarr_api.calls.last.request.headers["x-api-key"] == SECRET
    assert "TV Shows" in authed.get("/servers").text


def test_secret_encrypted_in_database(authed: TestClient, sonarr_api: respx.MockRouter) -> None:
    _create(authed)
    engine = authed.app.state.ctx.engine  # type: ignore[attr-defined]
    with engine.connect() as conn:
        raw = conn.execute(text("SELECT secrets FROM service_instances")).scalar_one()
    assert SECRET not in raw


def test_validation_errors(authed: TestClient) -> None:
    resp = _create(authed, base_url="sonarr:8989", secret_api_key="", name="")
    assert resp.status_code == 422
    assert "Include the scheme" in resp.text
    assert "API key is required" in resp.text


def test_duplicate_name_rejected(authed: TestClient, sonarr_api: respx.MockRouter) -> None:
    assert _create(authed).status_code == 303
    resp = _create(authed)
    assert resp.status_code == 422
    assert "already uses this name" in resp.text


def test_connection_test_endpoint(authed: TestClient, sonarr_api: respx.MockRouter) -> None:
    token = _token(authed)
    resp = authed.post(
        "/servers/services/test",
        data={
            "service_type": "sonarr",
            "name": "TV",
            "base_url": "http://sonarr:8989",
            "secret_api_key": SECRET,
        },
        headers={"X-CSRF-Token": token, "HX-Request": "true"},
    )
    assert resp.status_code == 200
    assert "Connected" in resp.text and "4.0.20" in resp.text

    sonarr_api.get("http://sonarr:8989/api/v3/system/status").mock(return_value=httpx.Response(401))
    resp = authed.post(
        "/servers/services/test",
        data={
            "service_type": "sonarr",
            "name": "TV",
            "base_url": "http://sonarr:8989",
            "secret_api_key": "wrong",
        },
        headers={"X-CSRF-Token": token, "HX-Request": "true"},
    )
    assert "Connection failed" in resp.text
    assert "Credentials rejected" in resp.text


def test_toggle_and_delete(authed: TestClient, sonarr_api: respx.MockRouter) -> None:
    _create(authed)
    instance_id = _instance_id(authed)
    headers = {"X-CSRF-Token": _token(authed), "HX-Request": "true"}
    resp = authed.post(f"/servers/services/{instance_id}/toggle", headers=headers)
    assert resp.status_code == 204 and resp.headers["hx-redirect"] == "/servers"
    assert "Enable" in authed.get("/servers").text
    resp = authed.post(f"/servers/services/{instance_id}/delete", headers=headers)
    assert resp.status_code == 204
    assert instance_id not in authed.get("/servers").text


def test_state_change_without_csrf_rejected(authed: TestClient) -> None:
    resp = authed.post("/settings/general", data={"server_name": "Hacked"})
    assert resp.status_code == 403


def test_rename_server(authed: TestClient) -> None:
    resp = authed.post(
        "/settings/general",
        data={"server_name": "Skynet"},
        headers={"X-CSRF-Token": _token(authed), "HX-Request": "true"},
    )
    assert "Saved" in resp.text
    assert "Skynet" in authed.get("/").text


def test_change_password(authed: TestClient) -> None:
    headers = {"X-CSRF-Token": _token(authed), "HX-Request": "true"}
    bad = authed.post(
        "/settings/account/password",
        data={"current_password": "nope", "new_password": "x" * 12, "confirm_password": "x" * 12},
        headers=headers,
    )
    assert "incorrect" in bad.text
    ok = authed.post(
        "/settings/account/password",
        data={
            "current_password": PASSWORD,
            "new_password": "a brand new password",
            "confirm_password": "a brand new password",
        },
        headers=headers,
    )
    assert "Password changed" in ok.text


def test_emby_next_episode_options_round_trip(authed: TestClient) -> None:
    with respx.mock(assert_all_called=False) as router:
        router.get("http://emby:8096/emby/System/Info").mock(
            return_value=httpx.Response(200, json={"Version": "4.11.0.9"})
        )
        router.get("http://emby:8096/emby/Sessions").mock(return_value=httpx.Response(200, json=[]))
        form = authed.get("/servers/services/new?type=emby").text
        assert 'name="option_next_episode_check"' in form and 'type="checkbox"' in form
        resp = _create(
            authed,
            service_type="emby",
            name="Emby",
            base_url="http://emby:8096",
            option_next_episode_check="on",
            option_next_episode_percent="70",
        )
        assert resp.status_code == 303, resp.text
        edit = authed.get(f"/servers/services/{_instance_id(authed)}").text
    assert re.search(r'name="option_next_episode_check"\s+checked', edit)
    assert re.search(r'name="option_next_episode_percent" value="70"', edit)
