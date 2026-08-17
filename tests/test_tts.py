import asyncio
from collections.abc import AsyncIterator
import json

import pytest

from newtalk.tts import DoubaoTTS, FakeTTS, StreamingTextSegmenter
from newtalk.tts.doubao import (
    AUDIO_ONLY_RESPONSE,
    ERROR_INFORMATION,
    EVENT_CONNECTION_STARTED,
    EVENT_FINISH_CONNECTION,
    EVENT_FINISH_SESSION,
    EVENT_SESSION_FINISHED,
    EVENT_SESSION_STARTED,
    EVENT_START_CONNECTION,
    EVENT_START_SESSION,
    EVENT_TASK_REQUEST,
    EVENT_TTS_RESPONSE,
    FLAG_WITH_EVENT,
    FULL_SERVER_RESPONSE,
    DoubaoTTSError,
    parse_response,
)


async def chunks(*values: str) -> AsyncIterator[str]:
    for value in values:
        yield value


def collect_audio(synthesizer, *values: str) -> list[bytes]:
    async def collect() -> list[bytes]:
        return [
            frame
            async for frame in synthesizer.stream(
                chunks(*values), turn_id="test-turn"
            )
        ]

    return asyncio.run(collect())


def test_streaming_segmenter_preserves_text_and_boundaries() -> None:
    segmenter = StreamingTextSegmenter(min_chars=6, max_chars=12)

    assert segmenter.push("你好，这是一段") == []
    assert segmenter.push("流式文本。下一句") == ["你好，这是一段流式文本。"]
    assert segmenter.flush() == "下一句"


def test_streaming_segmenter_limits_unpunctuated_text() -> None:
    segmenter = StreamingTextSegmenter(min_chars=4, max_chars=8)

    assert segmenter.push("abcdefghijkl") == ["abcdefgh"]
    assert segmenter.flush() == "ijkl"


def test_fake_tts_returns_valid_pcm_bytes() -> None:
    synthesizer = FakeTTS(sample_rate=24000)

    frames = collect_audio(synthesizer, "你好")

    assert synthesizer.audio_format.codec == "pcm_s16le"
    assert synthesizer.audio_format.sample_rate == 24000
    assert len(frames) == 1
    assert len(frames[0]) > 0
    assert len(frames[0]) % 2 == 0


class FakeWebSocket:
    def __init__(self, responses: list[bytes]) -> None:
        self.responses = responses
        self.sent: list[bytes] = []

    async def send(self, message: bytes) -> None:
        self.sent.append(message)

    async def recv(self) -> bytes:
        await asyncio.sleep(0)
        return self.responses.pop(0)


class FakeConnectionContext:
    def __init__(self, websocket: FakeWebSocket) -> None:
        self.websocket = websocket

    async def __aenter__(self) -> FakeWebSocket:
        return self.websocket

    async def __aexit__(self, exc_type, exc, traceback) -> None:
        return None


def test_doubao_tts_performs_v3_session_and_yields_pcm() -> None:
    pcm = b"\x01\x00\x02\x00"
    websocket = FakeWebSocket(
        [
            _connection_response(),
            _session_response(EVENT_SESSION_STARTED),
            _payload_response(EVENT_TTS_RESPONSE, pcm),
            _session_response(EVENT_SESSION_FINISHED),
        ]
    )
    captured: dict = {}

    def connector(url, **kwargs):
        captured["url"] = url
        captured.update(kwargs)
        return FakeConnectionContext(websocket)

    synthesizer = DoubaoTTS(
        app_id="app-id",
        access_token="secret-token",
        resource_id="seed-tts-2.0",
        voice_type="test-voice",
        ws_url="wss://example.test/tts",
        connector=connector,
    )

    frames = collect_audio(synthesizer, "第一句。", "第二句。")

    assert frames == [pcm]
    assert captured["url"] == "wss://example.test/tts"
    assert captured["proxy"] is None
    assert captured["additional_headers"] == {
        "X-Api-App-Key": "app-id",
        "X-Api-Access-Key": "secret-token",
        "X-Api-Resource-Id": "seed-tts-2.0",
        "X-Api-Connect-Id": captured["additional_headers"]["X-Api-Connect-Id"],
    }
    assert [_event_number(message) for message in websocket.sent] == [
        EVENT_START_CONNECTION,
        EVENT_START_SESSION,
        EVENT_TASK_REQUEST,
        EVENT_TASK_REQUEST,
        EVENT_FINISH_SESSION,
        EVENT_FINISH_CONNECTION,
    ]
    task_payloads = [
        _request_json(message)
        for message in websocket.sent
        if _event_number(message) == EVENT_TASK_REQUEST
    ]
    assert [payload["req_params"]["text"] for payload in task_payloads] == [
        "第一句。",
        "第二句。",
    ]
    assert task_payloads[0]["req_params"]["audio_params"] == {
        "format": "pcm",
        "sample_rate": 24000,
    }


def test_doubao_error_response_is_rejected() -> None:
    response = parse_response(_error_response(45000000, b"invalid credentials"))

    assert response.message_type == ERROR_INFORMATION
    assert response.error_code == 45000000
    assert response.payload == b"invalid credentials"

    websocket = FakeWebSocket([_error_response(45000000, b"denied")])
    synthesizer = DoubaoTTS(
        app_id="app-id",
        access_token="token",
        resource_id="resource",
        voice_type="voice",
        ws_url="wss://example.test/tts",
        connector=lambda *args, **kwargs: FakeConnectionContext(websocket),
    )
    with pytest.raises(DoubaoTTSError, match="connection failed"):
        collect_audio(synthesizer, "你好")


def _event_number(message: bytes) -> int:
    return int.from_bytes(message[4:8], "big", signed=True)


def _request_json(message: bytes) -> dict:
    offset = 8
    event = _event_number(message)
    if event in {
        EVENT_START_SESSION,
        EVENT_TASK_REQUEST,
        EVENT_FINISH_SESSION,
    }:
        session_size = int.from_bytes(message[offset : offset + 4], "big", signed=True)
        offset += 4 + session_size
    payload_size = int.from_bytes(message[offset : offset + 4], "big", signed=True)
    offset += 4
    return json.loads(message[offset : offset + payload_size])


def _field(content: bytes) -> bytes:
    return len(content).to_bytes(4, "big", signed=True) + content


def _connection_response() -> bytes:
    return bytes([0x11, (FULL_SERVER_RESPONSE << 4) | FLAG_WITH_EVENT, 0, 0]) + (
        EVENT_CONNECTION_STARTED.to_bytes(4, "big", signed=True)
        + _field(b"connection-id")
    )


def _session_response(event: int) -> bytes:
    return bytes([0x11, (FULL_SERVER_RESPONSE << 4) | FLAG_WITH_EVENT, 0, 0]) + (
        event.to_bytes(4, "big", signed=True)
        + _field(b"session-id")
        + _field(b"{}")
    )


def _payload_response(event: int, payload: bytes) -> bytes:
    return bytes([0x11, (AUDIO_ONLY_RESPONSE << 4) | FLAG_WITH_EVENT, 0, 0]) + (
        event.to_bytes(4, "big", signed=True)
        + _field(b"session-id")
        + _field(payload)
    )


def _error_response(code: int, payload: bytes) -> bytes:
    return bytes([0x11, ERROR_INFORMATION << 4, 0, 0]) + (
        code.to_bytes(4, "big", signed=True) + _field(payload)
    )
