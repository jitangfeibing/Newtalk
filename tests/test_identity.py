import re

from fastapi.testclient import TestClient
import pytest
from starlette.websockets import WebSocketDisconnect

from newtalk.app import create_app
from newtalk.config import AppConfig
from newtalk.identity import IdentityService, InMemoryIdentityStore
from newtalk.identity.security import (
    digest_recovery_code,
    digest_secret,
    generate_device_credential,
    generate_device_id,
    generate_recovery_code,
)


def make_app(config: AppConfig | None = None):
    return create_app(
        config or AppConfig(),
        identity_service=IdentityService(InMemoryIdentityStore()),
    )


def test_device_secrets_have_expected_shape_and_stable_digests() -> None:
    device_id = generate_device_id()
    credential = generate_device_credential()
    recovery_code = generate_recovery_code()

    assert re.fullmatch(r"[0-9a-f]{2}(?::[0-9a-f]{2}){5}", device_id)
    assert int(device_id[:2], 16) & 0x02
    assert len(credential) >= 40
    assert re.fullmatch(r"NT-(?:[A-Z2-7]{4}-){5}[A-Z2-7]{4}", recovery_code)
    assert digest_secret(credential) == digest_secret(credential)
    assert digest_recovery_code(recovery_code.lower().replace("-", " ")) == (
        digest_recovery_code(recovery_code)
    )


def test_device_registration_is_required_before_api_and_websocket_use() -> None:
    with TestClient(make_app()) as client:
        assert client.get("/api/device").status_code == 401
        assert client.get("/api/members").status_code == 401
        with pytest.raises(WebSocketDisconnect) as closed:
            with client.websocket_connect("/ws"):
                pass
        assert closed.value.code == 4401


def test_create_device_sets_http_only_cookie_and_returns_recovery_once() -> None:
    with TestClient(make_app()) as client:
        created = client.post("/api/device")
        repeated = client.post("/api/device")

        assert created.status_code == 201
        assert created.json()["recovery_code"].startswith("NT-")
        assert "HttpOnly" in created.headers["set-cookie"]
        assert "SameSite=lax" in created.headers["set-cookie"]
        assert repeated.status_code == 200
        assert repeated.json()["device_id"] == created.json()["device_id"]
        assert repeated.json()["recovery_code"] is None
        assert client.get("/ready").json() == {
            "status": "ready",
            "database": "ok",
        }


def test_member_crud_is_scoped_to_authenticated_device() -> None:
    app = make_app()
    with TestClient(app) as family_a, TestClient(app) as family_b:
        family_a.post("/api/device")
        family_b.post("/api/device")
        created = family_a.post(
            "/api/members",
            json={
                "display_name": "小明",
                "nickname": "明明",
                "relationship": "孩子",
                "avatar": None,
            },
        )
        identity_id = created.json()["identity_id"]

        assert created.status_code == 201
        assert family_a.get("/api/members").json()[0]["display_name"] == "小明"
        assert family_b.get("/api/members").json() == []
        assert family_b.patch(
            f"/api/members/{identity_id}", json={"display_name": "越界修改"}
        ).status_code == 404
        assert family_b.delete(f"/api/members/{identity_id}").status_code == 404
        assert family_a.patch(
            f"/api/members/{identity_id}", json={"display_name": None}
        ).status_code == 422

        updated = family_a.patch(
            f"/api/members/{identity_id}",
            json={"display_name": "小明同学", "nickname": None},
        )
        assert updated.status_code == 200
        assert updated.json()["display_name"] == "小明同学"
        assert updated.json()["nickname"] is None
        assert family_a.delete(f"/api/members/{identity_id}").status_code == 204
        assert family_a.get("/api/members").json() == []


def test_recovery_rotates_device_credential_and_preserves_members() -> None:
    app = make_app()
    with TestClient(app) as old_browser, TestClient(app) as new_browser:
        registered = old_browser.post("/api/device").json()
        old_browser.post("/api/members", json={"display_name": "小明"})

        recovered = new_browser.post(
            "/api/device/recover",
            json={"recovery_code": registered["recovery_code"].lower()},
        )

        assert recovered.status_code == 200
        assert recovered.json()["device_id"] == registered["device_id"]
        assert new_browser.get("/api/members").json()[0]["display_name"] == "小明"
        assert old_browser.get("/api/device").status_code == 401


def test_rotating_recovery_code_invalidates_the_old_code() -> None:
    app = make_app()
    with TestClient(app) as owner, TestClient(app) as recovery_browser:
        old_code = owner.post("/api/device").json()["recovery_code"]
        new_code = owner.post("/api/device/recovery-code").json()["recovery_code"]

        assert new_code != old_code
        assert recovery_browser.post(
            "/api/device/recover", json={"recovery_code": old_code}
        ).status_code == 401
        assert recovery_browser.post(
            "/api/device/recover", json={"recovery_code": new_code}
        ).status_code == 200


def test_recovery_attempts_are_rate_limited() -> None:
    config = AppConfig(recovery_max_attempts=2, recovery_window_seconds=60)
    with TestClient(make_app(config)) as client:
        payload = {"recovery_code": "NT-AAAA-AAAA-AAAA-AAAA-AAAA-AAAA"}
        assert client.post("/api/device/recover", json=payload).status_code == 401
        assert client.post("/api/device/recover", json=payload).status_code == 401
        assert client.post("/api/device/recover", json=payload).status_code == 429
