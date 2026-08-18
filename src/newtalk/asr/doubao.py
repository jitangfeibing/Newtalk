import asyncio
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass
import gzip
import json
import logging
from time import perf_counter
from typing import Any
from uuid import uuid4

from websockets.asyncio.client import connect
from websockets.exceptions import InvalidStatus

from newtalk.asr.model import AsrEvent, AsrFinal, AsrPartial


CLIENT_FULL_REQUEST = 0x1
CLIENT_AUDIO_REQUEST = 0x2
SERVER_FULL_RESPONSE = 0x9
SERVER_ERROR_RESPONSE = 0xF
FLAG_POSITIVE_SEQUENCE = 0x1
FLAG_LAST = 0x2
JSON_SERIALIZATION = 0x1
GZIP_COMPRESSION = 0x1

logger = logging.getLogger(__name__)


class DoubaoASRError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class DoubaoASRResponse:
    sequence: int | None
    is_last: bool
    payload: dict[str, Any] | None = None
    event: int | None = None
    error_code: int | None = None


class DoubaoStreamingASR:
    def __init__(
        self,
        *,
        api_key: str,
        resource_id: str,
        ws_url: str,
        packet_duration_ms: int = 100,
        timeout_seconds: float = 30.0,
        use_system_proxy: bool = False,
        connector: Callable[..., Any] = connect,
    ) -> None:
        self._api_key = api_key
        self._resource_id = resource_id
        self._ws_url = ws_url
        self._packet_bytes = 16000 * 2 * packet_duration_ms // 1000
        self._timeout_seconds = timeout_seconds
        self._proxy = True if use_system_proxy else None
        self._connector = connector

    async def stream(
        self,
        audio_chunks: AsyncIterator[bytes],
        *,
        utterance_id: str,
    ) -> AsyncIterator[AsrEvent]:
        request_id = str(uuid4())
        headers = {
            "X-Api-Key": self._api_key,
            "X-Api-Resource-Id": self._resource_id,
            "X-Api-Request-Id": request_id,
            "X-Api-Sequence": "-1",
        }
        started_at = perf_counter()
        logger.info(
            "asr_stream_started utterance_id=%s provider=doubao resource_id=%s",
            utterance_id,
            self._resource_id,
        )

        connection = self._connector(
            self._ws_url,
            additional_headers=headers,
            max_size=16 * 1024 * 1024,
            open_timeout=self._timeout_seconds,
            proxy=self._proxy,
        )
        async with _open_connection(connection) as websocket:
            await websocket.send(
                build_full_request(
                    sequence=1,
                    payload=_request_payload(utterance_id),
                )
            )
            sender = asyncio.create_task(self._send_audio(websocket, audio_chunks))
            latest_text = ""
            first_result_logged = False
            try:
                while True:
                    response = await self._receive(websocket)
                    if response.error_code is not None:
                        raise _response_error(response)

                    text = extract_text(response.payload)
                    if text and text != latest_text:
                        latest_text = text
                        if not first_result_logged:
                            first_result_logged = True
                            logger.info(
                                "asr_first_result utterance_id=%s elapsed_ms=%.1f",
                                utterance_id,
                                (perf_counter() - started_at) * 1000,
                            )
                        if not response.is_last:
                            yield AsrPartial(text)

                    if response.is_last:
                        await sender
                        logger.info(
                            "asr_stream_completed utterance_id=%s text_length=%s elapsed_ms=%.1f",
                            utterance_id,
                            len(latest_text),
                            (perf_counter() - started_at) * 1000,
                        )
                        yield AsrFinal(latest_text)
                        return
            finally:
                if not sender.done():
                    sender.cancel()
                await asyncio.gather(sender, return_exceptions=True)

    async def _send_audio(
        self,
        websocket: Any,
        audio_chunks: AsyncIterator[bytes],
    ) -> None:
        sequence = 2
        buffer = bytearray()
        pending: bytes | None = None

        async for chunk in audio_chunks:
            if not chunk:
                continue
            buffer.extend(chunk)
            while len(buffer) >= self._packet_bytes:
                segment = bytes(buffer[: self._packet_bytes])
                del buffer[: self._packet_bytes]
                if pending is not None:
                    await websocket.send(
                        build_audio_request(sequence, pending, is_last=False)
                    )
                    sequence += 1
                pending = segment

        if buffer:
            if pending is not None:
                await websocket.send(
                    build_audio_request(sequence, pending, is_last=False)
                )
                sequence += 1
            pending = bytes(buffer)

        await websocket.send(
            build_audio_request(sequence, pending or b"", is_last=True)
        )

    async def _receive(self, websocket: Any) -> DoubaoASRResponse:
        try:
            raw = await asyncio.wait_for(
                websocket.recv(), timeout=self._timeout_seconds
            )
        except TimeoutError as exc:
            raise DoubaoASRError("Timed out waiting for Doubao ASR") from exc
        if not isinstance(raw, bytes):
            raise DoubaoASRError("Doubao ASR returned a non-binary message")
        return parse_response(raw)

    async def aclose(self) -> None:
        return None


def build_full_request(*, sequence: int, payload: dict[str, Any]) -> bytes:
    encoded = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    compressed = gzip.compress(encoded)
    return b"".join(
        (
            _header(
                CLIENT_FULL_REQUEST,
                FLAG_POSITIVE_SEQUENCE,
                JSON_SERIALIZATION,
                GZIP_COMPRESSION,
            ),
            sequence.to_bytes(4, "big", signed=True),
            len(compressed).to_bytes(4, "big"),
            compressed,
        )
    )


def build_audio_request(sequence: int, audio: bytes, *, is_last: bool) -> bytes:
    compressed = gzip.compress(audio)
    flags = FLAG_POSITIVE_SEQUENCE | (FLAG_LAST if is_last else 0)
    wire_sequence = -sequence if is_last else sequence
    return b"".join(
        (
            _header(
                CLIENT_AUDIO_REQUEST,
                flags,
                0,
                GZIP_COMPRESSION,
            ),
            wire_sequence.to_bytes(4, "big", signed=True),
            len(compressed).to_bytes(4, "big"),
            compressed,
        )
    )


def parse_response(data: bytes) -> DoubaoASRResponse:
    if len(data) < 4:
        raise DoubaoASRError("Doubao ASR response is shorter than its header")

    header_size = (data[0] & 0x0F) * 4
    if header_size < 4 or header_size > len(data):
        raise DoubaoASRError("Doubao ASR response has an invalid header size")

    message_type = data[1] >> 4
    flags = data[1] & 0x0F
    serialization = data[2] >> 4
    compression = data[2] & 0x0F
    offset = header_size
    sequence = None
    event = None

    if flags & FLAG_POSITIVE_SEQUENCE:
        sequence, offset = _read_int(data, offset)
    if flags & 0x4:
        event, offset = _read_int(data, offset)
    is_last = bool(flags & FLAG_LAST)

    if message_type == SERVER_ERROR_RESPONSE:
        error_code, offset = _read_int(data, offset)
        raw_payload, _ = _read_payload(data, offset)
        payload = _decode_payload(raw_payload, serialization, compression)
        return DoubaoASRResponse(
            sequence=sequence,
            is_last=True,
            payload=payload,
            event=event,
            error_code=error_code,
        )

    if message_type != SERVER_FULL_RESPONSE:
        raise DoubaoASRError(
            f"Doubao ASR returned unsupported message type: {message_type}"
        )

    raw_payload, _ = _read_payload(data, offset)
    payload = _decode_payload(raw_payload, serialization, compression)
    return DoubaoASRResponse(
        sequence=sequence,
        is_last=is_last,
        payload=payload,
        event=event,
    )


def extract_text(payload: dict[str, Any] | None) -> str:
    if not payload:
        return ""
    result = payload.get("result")
    if isinstance(result, dict):
        text = result.get("text")
        return text if isinstance(text, str) else ""
    if isinstance(result, list):
        parts = [
            item.get("text", "")
            for item in result
            if isinstance(item, dict) and isinstance(item.get("text"), str)
        ]
        return "".join(parts)
    text = payload.get("text")
    return text if isinstance(text, str) else ""


def _request_payload(utterance_id: str) -> dict[str, Any]:
    return {
        "user": {"uid": utterance_id},
        "audio": {
            "format": "pcm",
            "codec": "raw",
            "rate": 16000,
            "bits": 16,
            "channel": 1,
        },
        "request": {
            "model_name": "bigmodel",
            "enable_nonstream": False,
            "enable_itn": True,
            "enable_punc": True,
            "enable_ddc": False,
            "show_utterances": True,
        },
    }


def _header(
    message_type: int,
    flags: int,
    serialization: int,
    compression: int,
) -> bytes:
    return bytes(
        (
            0x11,
            (message_type << 4) | flags,
            (serialization << 4) | compression,
            0x00,
        )
    )


def _read_int(data: bytes, offset: int) -> tuple[int, int]:
    if offset + 4 > len(data):
        raise DoubaoASRError("Doubao ASR response ended while reading an integer")
    return int.from_bytes(data[offset : offset + 4], "big", signed=True), offset + 4


def _read_payload(data: bytes, offset: int) -> tuple[bytes, int]:
    if offset + 4 > len(data):
        raise DoubaoASRError("Doubao ASR response is missing its payload size")
    size = int.from_bytes(data[offset : offset + 4], "big")
    offset += 4
    if offset + size > len(data):
        raise DoubaoASRError("Doubao ASR response contains an invalid payload size")
    return data[offset : offset + size], offset + size


def _decode_payload(
    payload: bytes,
    serialization: int,
    compression: int,
) -> dict[str, Any] | None:
    if not payload:
        return None
    if compression == GZIP_COMPRESSION:
        try:
            payload = gzip.decompress(payload)
        except gzip.BadGzipFile as exc:
            raise DoubaoASRError("Doubao ASR returned invalid gzip data") from exc
    elif compression != 0:
        raise DoubaoASRError("Doubao ASR returned unsupported compression")

    if serialization == 0:
        return {"message": payload.decode("utf-8", errors="replace")}
    if serialization != JSON_SERIALIZATION:
        raise DoubaoASRError("Doubao ASR returned unsupported serialization")
    try:
        decoded = json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise DoubaoASRError("Doubao ASR returned invalid JSON") from exc
    if not isinstance(decoded, dict):
        raise DoubaoASRError("Doubao ASR returned a non-object JSON payload")
    return decoded


def _response_error(response: DoubaoASRResponse) -> DoubaoASRError:
    detail = ""
    if response.payload:
        message = response.payload.get("message")
        if isinstance(message, str):
            detail = f": {message}"
        else:
            detail = f": {json.dumps(response.payload, ensure_ascii=False)}"
    return DoubaoASRError(
        f"Doubao ASR request failed code={response.error_code}{detail}"
    )


@asynccontextmanager
async def _open_connection(connection: Any):
    try:
        async with connection as websocket:
            yield websocket
    except InvalidStatus as exc:
        raise _handshake_error(exc) from exc


def _handshake_error(error: InvalidStatus) -> DoubaoASRError:
    response = error.response
    log_id = response.headers.get("x-tt-logid")
    body = (
        response.body.decode("utf-8", errors="replace").strip()
        if response.body
        else ""
    )
    details = [f"status={response.status_code}"]
    if log_id:
        details.append(f"logid={log_id}")
    if body:
        details.append(body)
    return DoubaoASRError(f"Doubao ASR handshake failed: {' '.join(details)}")
