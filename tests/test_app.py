import asyncio
from html.parser import HTMLParser

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


def test_control_status_does_not_add_an_extra_chat_grid_row() -> None:
    class ChatLayout(HTMLParser):
        def __init__(self):
            super().__init__()
            self.stack = []
            self.panel_children = []
            self.status_parent = None

        def handle_starttag(self, tag, attrs):
            attributes = dict(attrs)
            if self.stack and self.stack[-1][1].get("class") == "chat-panel":
                self.panel_children.append(tag)
            if attributes.get("id") == "deviceControlStatus":
                self.status_parent = self.stack[-1][1].get("id")
            if tag not in {"area", "base", "br", "col", "embed", "hr", "img", "input",
                           "link", "meta", "param", "source", "track", "wbr"}:
                self.stack.append((tag, attributes))

        def handle_endtag(self, tag):
            if self.stack and self.stack[-1][0] == tag:
                self.stack.pop()

    layout = ChatLayout()
    layout.feed(client.get("/").text)
    # 聊天 Grid 固定为标题、消息、输入三行，操作提示不能占掉消息行。
    assert layout.panel_children == ["header", "ol", "form"]
    assert layout.status_parent == "chatForm"


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
