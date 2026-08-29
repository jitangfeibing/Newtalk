from fastapi.testclient import TestClient
import pytest

from newtalk.app import create_app
from newtalk.config import AppConfig
from newtalk.identity import IdentityService, InMemoryIdentityStore


client = TestClient(
    create_app(
        AppConfig(),
        identity_service=IdentityService(InMemoryIdentityStore()),
    )
)


def test_health() -> None:
    response = client.get("/health")

    assert response.status_code == 200
    assert response.json() == {
        "status": "ok",
        "service": "newtalk",
        "version": "0.7.0",
    }


def test_web_page_is_served() -> None:
    response = client.get("/")

    assert response.status_code == 200
    assert "Newtalk Voice Console" in response.text
    assert 'id="chatForm"' in response.text
    assert 'id="messageInput"' in response.text
    assert 'id="stopAudioButton"' in response.text
    assert 'id="micButton"' in response.text
    assert 'id="deviceOnboarding"' in response.text
    assert 'id="memberForm"' in response.text


def test_app_fails_fast_when_web_root_is_missing(tmp_path) -> None:
    config = AppConfig(web_root=tmp_path / "missing")

    with pytest.raises(RuntimeError, match="Web root does not exist"):
        create_app(config)
