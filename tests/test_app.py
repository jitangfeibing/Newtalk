import asyncio

from fastapi.testclient import TestClient
import pytest

from newtalk.app import _close_lifecycle_components, create_app
from newtalk.config import AppConfig
from newtalk.identity import IdentityService, InMemoryIdentityStore


client = TestClient(
    create_app(
        AppConfig(),
        identity_service=IdentityService(InMemoryIdentityStore()),
        persistence_backend="memory",
    )
)


def test_health() -> None:
    response = client.get("/health")

    assert response.status_code == 200
    assert response.json() == {
        "status": "ok",
        "service": "newtalk",
        "version": "0.13.0",
    }


def test_web_page_is_served() -> None:
    response = client.get("/")

    assert response.status_code == 200
    assert "Newtalk · 家庭陪伴助手" in response.text
    assert 'class="primary-nav"' in response.text
    assert 'id="conversation" data-view="conversation" aria-labelledby="chatTitle" hidden' in response.text
    assert 'id="chatForm"' in response.text
    assert 'id="messageInput"' in response.text
    assert 'id="stopAudioButton"' in response.text
    assert 'id="clearDialogueButton"' in response.text
    assert 'id="deviceWorkspace"' in response.text
    assert 'id="memoryServiceState"' in response.text
    assert 'id="memoryRetryButton"' in response.text
    assert 'id="micButton"' in response.text
    assert 'id="deviceOnboarding"' in response.text
    assert 'id="memberForm"' in response.text
    assert 'id="memoryCenter"' in response.text
    assert 'id="profileList"' in response.text
    assert 'id="memoryList"' in response.text


def test_app_fails_fast_when_web_root_is_missing(tmp_path) -> None:
    config = AppConfig(web_root=tmp_path / "missing")

    with pytest.raises(RuntimeError, match="Web root does not exist"):
        create_app(config)


def test_app_rejects_unknown_persistence_backend() -> None:
    with pytest.raises(ValueError, match="Persistence backend"):
        create_app(AppConfig(), persistence_backend="invalid")  # type: ignore[arg-type]


def test_shutdown_closes_remaining_components_after_failure() -> None:
    async def scenario() -> None:
        closed: list[str] = []

        async def fail_first() -> None:
            closed.append("first")
            raise RuntimeError("close failed")

        async def close_second() -> None:
            closed.append("second")

        async def close_third() -> None:
            closed.append("third")

        with pytest.raises(RuntimeError, match="close failed"):
            await _close_lifecycle_components(
                ("first", fail_first),
                ("second", close_second),
                ("third", close_third),
            )

        assert closed == ["first", "second", "third"]

    asyncio.run(scenario())
