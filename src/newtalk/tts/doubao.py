import asyncio
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass
import json
from typing import Any
from uuid import uuid4

from websockets.asyncio.client import connect

from newtalk.tts.model import AudioFormat


FULL_CLIENT_REQUEST = 0x1
FULL_SERVER_RESPONSE = 0x9
AUDIO_ONLY_RESPONSE = 0xB
ERROR_INFORMATION = 0xF
FLAG_WITH_EVENT = 0x4
NO_SERIALIZATION = 0x0
JSON_SERIALIZATION = 0x1

EVENT_START_CONNECTION = 1
EVENT_FINISH_CONNECTION = 2
EVENT_CONNECTION_STARTED = 50
EVENT_CONNECTION_FAILED = 51
EVENT_CONNECTION_FINISHED = 52
EVENT_START_SESSION = 100
EVENT_CANCEL_SESSION = 101
EVENT_FINISH_SESSION = 102
EVENT_SESSION_STARTED = 150
EVENT_SESSION_CANCELED = 151
EVENT_SESSION_FINISHED = 152
EVENT_SESSION_FAILED = 153
EVENT_TASK_REQUEST = 200
EVENT_TTS_RESPONSE = 352


class DoubaoTTSError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class DoubaoResponse:
    message_type: int
    event: int | None = None
    session_id: str | None = None
    payload: bytes | None = None
    metadata: str | None = None
    error_code: int | None = None


class DoubaoTTS:
    def __init__(
        self,
        *,
        app_id: str,
        access_token: str,
        resource_id: str,
        voice_type: str,
        ws_url: str,
        sample_rate: int = 24000,
        timeout_seconds: float = 30.0,
        use_system_proxy: bool = False,
        connector: Callable[..., Any] = connect,
    ) -> None:
        self._app_id = app_id
        self._access_token = access_token
        self._resource_id = resource_id
        self._voice_type = voice_type
        self._ws_url = ws_url
        self._timeout_seconds = timeout_seconds
        self._proxy = True if use_system_proxy else None
        self._connector = connector
        self._audio_format = AudioFormat(
            codec="pcm_s16le",
            sample_rate=sample_rate,
            channels=1,
        )

    @property
    def audio_format(self) -> AudioFormat:
        return self._audio_format

    async def stream(
        self,
        text_chunks: AsyncIterator[str],
        *,
        turn_id: str,
    ) -> AsyncIterator[bytes]:
        session_id = uuid4().hex
        headers = {
            "X-Api-App-Key": self._app_id,
            "X-Api-Access-Key": self._access_token,
            "X-Api-Resource-Id": self._resource_id,
            "X-Api-Connect-Id": str(uuid4()),
        }

        async with self._connector(
            self._ws_url,
            additional_headers=headers,
            max_size=64 * 1024 * 1024,
            open_timeout=self._timeout_seconds,
            proxy=self._proxy,
        ) as websocket:
            session_started = False
            session_finished = False
            sender: asyncio.Task[None] | None = None
            try:
                await websocket.send(_event_message(EVENT_START_CONNECTION))
                response = await self._receive(websocket)
                _expect_event(response, EVENT_CONNECTION_STARTED, "connection")

                await websocket.send(
                    _event_message(
                        EVENT_START_SESSION,
                        session_id=session_id,
                        payload=self._request_payload(
                            event=EVENT_START_SESSION,
                            uid=turn_id,
                        ),
                        serialized=True,
                    )
                )
                response = await self._receive(websocket)
                _expect_event(response, EVENT_SESSION_STARTED, "session")
                session_started = True

                sender = asyncio.create_task(
                    self._send_text(websocket, text_chunks, session_id, turn_id)
                )
                while True:
                    response = await self._receive(websocket)
                    if response.message_type == ERROR_INFORMATION:
                        raise _response_error(response)
                    if response.event == EVENT_SESSION_FAILED:
                        raise _response_error(response, "Doubao TTS session failed")
                    if response.event == EVENT_TTS_RESPONSE:
                        if response.payload:
                            yield response.payload
                        continue
                    if response.event == EVENT_SESSION_FINISHED:
                        session_finished = True
                        break

                await sender
                sender = None
                await websocket.send(_event_message(EVENT_FINISH_CONNECTION))
            finally:
                if sender is not None:
                    sender.cancel()
                    await asyncio.gather(sender, return_exceptions=True)
                if session_started and not session_finished:
                    try:
                        await websocket.send(
                            _event_message(
                                EVENT_CANCEL_SESSION,
                                session_id=session_id,
                                serialized=True,
                            )
                        )
                    except Exception:
                        pass

    async def _send_text(
        self,
        websocket: Any,
        text_chunks: AsyncIterator[str],
        session_id: str,
        turn_id: str,
    ) -> None:
        async for text in text_chunks:
            normalized = text.strip()
            if not normalized:
                continue
            await websocket.send(
                _event_message(
                    EVENT_TASK_REQUEST,
                    session_id=session_id,
                    payload=self._request_payload(
                        event=EVENT_TASK_REQUEST,
                        uid=turn_id,
                        text=normalized,
                    ),
                    serialized=True,
                )
            )
        await websocket.send(
            _event_message(
                EVENT_FINISH_SESSION,
                session_id=session_id,
                serialized=True,
            )
        )

    async def _receive(self, websocket: Any) -> DoubaoResponse:
        try:
            raw = await asyncio.wait_for(
                websocket.recv(), timeout=self._timeout_seconds
            )
        except TimeoutError as exc:
            raise DoubaoTTSError("Timed out waiting for Doubao TTS") from exc
        if not isinstance(raw, bytes):
            raise DoubaoTTSError("Doubao TTS returned a non-binary message")
        return parse_response(raw)

    def _request_payload(self, *, event: int, uid: str, text: str = "") -> bytes:
        body = {
            "user": {"uid": uid},
            "event": event,
            "namespace": "BidirectionalTTS",
            "req_params": {
                "text": text,
                "speaker": self._voice_type,
                "audio_params": {
                    "format": "pcm",
                    "sample_rate": self._audio_format.sample_rate,
                },
                "additions": json.dumps({}, ensure_ascii=False),
            },
        }
        return json.dumps(body, ensure_ascii=False).encode("utf-8")

    async def aclose(self) -> None:
        return None


def _event_message(
    event: int,
    *,
    session_id: str | None = None,
    payload: bytes = b"{}",
    serialized: bool = False,
) -> bytes:
    serialization = JSON_SERIALIZATION if serialized else NO_SERIALIZATION
    message = bytearray(
        [
            0x11,
            (FULL_CLIENT_REQUEST << 4) | FLAG_WITH_EVENT,
            serialization << 4,
            0x00,
        ]
    )
    message.extend(event.to_bytes(4, "big", signed=True))
    if session_id is not None:
        encoded_session = session_id.encode("utf-8")
        message.extend(len(encoded_session).to_bytes(4, "big", signed=True))
        message.extend(encoded_session)
    message.extend(len(payload).to_bytes(4, "big", signed=True))
    message.extend(payload)
    return bytes(message)


def parse_response(data: bytes) -> DoubaoResponse:
    if len(data) < 4:
        raise DoubaoTTSError("Doubao response is shorter than its header")

    message_type = data[1] >> 4
    flags = data[1] & 0x0F
    offset = (data[0] & 0x0F) * 4
    if offset < 4 or offset > len(data):
        raise DoubaoTTSError("Doubao response has an invalid header size")

    if message_type == ERROR_INFORMATION:
        error_code, offset = _read_int(data, offset)
        payload, _ = _read_bytes(data, offset)
        return DoubaoResponse(
            message_type=message_type,
            payload=payload,
            error_code=error_code,
        )

    if message_type not in {FULL_SERVER_RESPONSE, AUDIO_ONLY_RESPONSE}:
        return DoubaoResponse(message_type=message_type)
    if not flags & FLAG_WITH_EVENT:
        return DoubaoResponse(message_type=message_type)

    event, offset = _read_int(data, offset)
    if event == EVENT_CONNECTION_STARTED:
        _, offset = _read_bytes(data, offset)
        return DoubaoResponse(message_type=message_type, event=event)
    if event == EVENT_CONNECTION_FAILED:
        metadata, _ = _read_text(data, offset)
        return DoubaoResponse(
            message_type=message_type,
            event=event,
            metadata=metadata,
        )
    if event in {
        EVENT_SESSION_STARTED,
        EVENT_SESSION_CANCELED,
        EVENT_SESSION_FINISHED,
        EVENT_SESSION_FAILED,
    }:
        session_id, offset = _read_text(data, offset)
        metadata, _ = _read_text(data, offset)
        return DoubaoResponse(
            message_type=message_type,
            event=event,
            session_id=session_id,
            metadata=metadata,
        )

    session_id, offset = _read_text(data, offset)
    payload, _ = _read_bytes(data, offset)
    return DoubaoResponse(
        message_type=message_type,
        event=event,
        session_id=session_id,
        payload=payload,
    )


def _expect_event(response: DoubaoResponse, expected: int, stage: str) -> None:
    if response.message_type == ERROR_INFORMATION or response.event != expected:
        raise _response_error(response, f"Doubao TTS {stage} failed")


def _response_error(
    response: DoubaoResponse,
    fallback: str = "Doubao TTS request failed",
) -> DoubaoTTSError:
    detail = response.metadata
    if not detail and response.payload:
        detail = response.payload.decode("utf-8", errors="replace")
    suffix = f": {detail}" if detail else ""
    code = f" code={response.error_code}" if response.error_code is not None else ""
    return DoubaoTTSError(f"{fallback}{code}{suffix}")


def _read_int(data: bytes, offset: int) -> tuple[int, int]:
    if offset + 4 > len(data):
        raise DoubaoTTSError("Doubao response ended while reading an integer")
    return int.from_bytes(data[offset : offset + 4], "big", signed=True), offset + 4


def _read_bytes(data: bytes, offset: int) -> tuple[bytes, int]:
    size, offset = _read_int(data, offset)
    if size < 0 or offset + size > len(data):
        raise DoubaoTTSError("Doubao response contains an invalid field length")
    return data[offset : offset + size], offset + size


def _read_text(data: bytes, offset: int) -> tuple[str, int]:
    content, offset = _read_bytes(data, offset)
    try:
        return content.decode("utf-8"), offset
    except UnicodeDecodeError as exc:
        raise DoubaoTTSError("Doubao response contains invalid UTF-8") from exc
