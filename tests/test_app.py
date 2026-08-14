from fastapi.testclient import TestClient
import pytest

from newtalk.app import create_app
from newtalk.config import AppConfig


client = TestClient(create_app())


def test_health() -> None:
    response = client.get("/health")

    assert response.status_code == 200
    assert response.json() == {
        "status": "ok",
        "service": "newtalk",
        "version": "0.1.0",
    }


def test_web_page_is_served() -> None:
    response = client.get("/")

    assert response.status_code == 200
    assert "Newtalk Signal Console" in response.text


def test_app_fails_fast_when_web_root_is_missing(tmp_path) -> None:
    config = AppConfig(web_root=tmp_path / "missing")

    with pytest.raises(RuntimeError, match="Web root does not exist"):
        create_app(config)
