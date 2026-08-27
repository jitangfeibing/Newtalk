import asyncio
import gzip
import json
from typing import Any

import pytest

from newtalk.asr import AsrFinal, AsrPartial
from newtalk.asr.doubao import (
    CLIENT_AUDIO_REQUEST,
    CLIENT_FULL_REQUEST,
    DoubaoASRError,
    DoubaoStreamingASR,
    build_audio_request,
    build_full_request,
    extract_text,
    parse_response,
)


def _server_response(*, sequence: int, text: str, is_last: bool) -> bytes:
    payload = gzip.compress(
        json.dumps({"result": {"text": text}}).encode("utf-8")
    )
    flags = 0x1 | (0x2 if is_last else 0)
    wire_sequence = -sequence if is_last else sequence
    return b"".join(
        (
            bytes((0x11, (0x9 << 4) | flags, 0x11, 0x00)),
            wire_sequence.to_bytes(4, "big", signed=True),
            len(payload).to_bytes(4, "big"),
            payload,
        )
    )


class FakeWebSocket:
    def __init__(self, responses: list[bytes]) -> None:
        self.responses = list(responses)
        self.sent: list[bytes] = []

    async def send(self, payload: bytes) -> None:
        self.sent.append(payload)

    async def recv(self) -> bytes:
        await asyncio.sleep(0)
        return self.responses.pop(0)


class FakeConnection:
    def __init__(self, websocket: FakeWebSocket) -> None:
        self.websocket = websocket

    async def __aenter__(self) -> FakeWebSocket:
        return self.websocket

    async def __aexit__(self, *args: Any) -> None:
        return None


def test_request_builders_use_gzip_json_and_negative_final_sequence() -> None:
    full = build_full_request(
        sequence=1,
        payload={"audio": {"format": "pcm"}},
    )
    assert full[1] >> 4 == CLIENT_FULL_REQUEST
    assert int.from_bytes(full[4:8], "big", signed=True) == 1
    full_size = int.from_bytes(full[8:12], "big")
    assert json.loads(gzip.decompress(full[12 : 12 + full_size])) == {
        "audio": {"format": "pcm"}
    }

    final = build_audio_request(7, b"pcm-data", is_last=True)
    assert final[1] >> 4 == CLIENT_AUDIO_REQUEST
    assert final[1] & 0x0F == 0x3
    assert int.from_bytes(final[4:8], "big", signed=True) == -7
    final_size = int.from_bytes(final[8:12], "big")
    assert gzip.decompress(final[12 : 12 + final_size]) == b"pcm-data"


def test_response_parser_and_text_extraction() -> None:
    response = parse_response(
        _server_response(sequence=3, text="你好", is_last=True)
    )

    assert response.sequence == -3
    assert response.is_last is True
    assert extract_text(response.payload) == "你好"
    assert extract_text({"result": [{"text": "你"}, {"text": "好"}]}) == "你好"


def test_response_parser_rejects_truncated_payload() -> None:
    with pytest.raises(DoubaoASRError, match="payload size"):
        parse_response(bytes((0x11, 0x91, 0x11, 0x00, 0, 0, 0, 1)))


def test_doubao_asr_streams_partial_and_final_results() -> None:
    async def run() -> None:
        websocket = FakeWebSocket(
            [
                _server_response(sequence=1, text="", is_last=False),
                _server_response(sequence=2, text="你好", is_last=False),
                _server_response(sequence=3, text="你好世界", is_last=True),
            ]
        )
        connection_kwargs: dict[str, Any] = {}

        def connector(url: str, **kwargs: Any) -> FakeConnection:
            connection_kwargs["url"] = url
            connection_kwargs.update(kwargs)
            return FakeConnection(websocket)

        recognizer = DoubaoStreamingASR(
            api_key="secret",
            resource_id="volc.seedasr.sauc.duration",
            ws_url="wss://example.test/asr",
            packet_duration_ms=100,
            connector=connector,
        )

        async def audio_chunks():
            for _ in range(10):
                yield bytes(640)

        events = [
            event
            async for event in recognizer.stream(
                audio_chunks(), utterance_id="utterance-1"
            )
        ]

        assert events == [AsrPartial("你好"), AsrFinal("你好世界")]
        assert connection_kwargs["url"] == "wss://example.test/asr"
        assert connection_kwargs["additional_headers"] == {
            "X-Api-Key": "secret",
            "X-Api-Resource-Id": "volc.seedasr.sauc.duration",
            "X-Api-Request-Id": connection_kwargs["additional_headers"][
                "X-Api-Request-Id"
            ],
            "X-Api-Sequence": "-1",
        }
        assert connection_kwargs["proxy"] is None
        assert [message[1] >> 4 for message in websocket.sent] == [
            CLIENT_FULL_REQUEST,
            CLIENT_AUDIO_REQUEST,
            CLIENT_AUDIO_REQUEST,
        ]
        assert int.from_bytes(websocket.sent[-1][4:8], "big", signed=True) < 0

    asyncio.run(run())
