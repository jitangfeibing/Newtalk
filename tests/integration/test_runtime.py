import asyncio
from contextlib import contextmanager
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import time
from collections.abc import Iterator
from urllib.error import URLError
from urllib.request import urlopen

import pytest
import websockets
from websockets.exceptions import ConnectionClosedOK


PROJECT_ROOT = Path(__file__).resolve().parents[2]


def find_available_port() -> int:
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        return listener.getsockname()[1]


def wait_until_healthy(process: subprocess.Popen[str], port: int) -> None:
    deadline = time.monotonic() + 10
    health_url = f"http://127.0.0.1:{port}/health"
    last_error: Exception | None = None

    while time.monotonic() < deadline:
        if process.poll() is not None:
            output = process.stdout.read() if process.stdout else ""
            raise AssertionError(f"Newtalk exited during startup:\n{output}")
        try:
            with urlopen(health_url, timeout=0.25) as response:
                if response.status == 200:
                    return
        except (OSError, URLError) as exc:
            last_error = exc
            time.sleep(0.05)

    raise AssertionError(f"Newtalk did not become healthy: {last_error}")


@contextmanager
def running_newtalk(port: int) -> Iterator[None]:
    environment = os.environ.copy()
    environment.update(
        {
            "NEWTALK_HOST": "127.0.0.1",
            "NEWTALK_PORT": str(port),
            "NEWTALK_LOG_LEVEL": "INFO",
            "NEWTALK_WEB_ROOT": str(PROJECT_ROOT / "web"),
            "NEWTALK_LLM_BACKEND": "fake",
        }
    )
    process = subprocess.Popen(
        [sys.executable, "-m", "newtalk"],
        cwd=PROJECT_ROOT,
        env=environment,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    try:
        wait_until_healthy(process, port)
        yield
    finally:
        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)


async def assert_websocket_lifecycle(port: int) -> None:
    async with websockets.connect(f"ws://127.0.0.1:{port}/ws") as websocket:
        hello = json.loads(await websocket.recv())
        assert hello["type"] == "hello"
        assert hello["protocol_version"] == "0.2"
        assert hello["session_id"]

        await websocket.send(
            json.dumps(
                {"type": "text_input", "event_id": "integration-text", "text": "你好"}
            )
        )
        started = json.loads(await websocket.recv())
        first_delta = json.loads(await websocket.recv())
        second_delta = json.loads(await websocket.recv())
        completed = json.loads(await websocket.recv())

        assert started["type"] == "turn_started"
        assert first_delta["delta"] == "我收到了："
        assert second_delta["delta"] == "你好"
        assert completed == {
            "type": "turn_completed",
            "turn_id": started["turn_id"],
            "event_id": "integration-text",
            "text": "我收到了：你好",
        }

        await websocket.send(json.dumps({"type": "close", "event_id": "integration"}))
        closing = json.loads(await websocket.recv())
        assert closing == {
            "type": "closing",
            "session_id": hello["session_id"],
            "event_id": "integration",
        }
        with pytest.raises(ConnectionClosedOK) as closed:
            await websocket.recv()
        assert closed.value.rcvd is not None
        assert closed.value.rcvd.code == 1000


@pytest.mark.integration
def test_real_server_serves_web_and_websocket_hello() -> None:
    port = find_available_port()

    with running_newtalk(port):
        with urlopen(f"http://127.0.0.1:{port}/", timeout=2) as response:
            page = response.read().decode("utf-8")
            assert response.status == 200
            assert "Newtalk Text Console" in page
            assert 'id="chatForm"' in page

        asyncio.run(assert_websocket_lifecycle(port))
