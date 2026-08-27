import json
import logging
from uuid import uuid4

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from newtalk.audio import INPUT_AUDIO_FORMAT
from newtalk.transport.protocol import PROTOCOL_VERSION
from newtalk.transport.runtime import ConnectionRuntime
from newtalk.transport.text_chat import handle_text_input


router = APIRouter()
logger = logging.getLogger(__name__)


@router.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket) -> None:
    await websocket.accept()
    session_id = str(uuid4())
    logger.info("websocket_connected session_id=%s", session_id)
    runtime = ConnectionRuntime(
        websocket,
        session_id=session_id,
        chat_service=websocket.app.state.chat_service,
        vad=websocket.app.state.vad,
        recognizer=websocket.app.state.recognizer,
        vad_pre_roll_ms=websocket.app.state.config.vad_pre_roll_ms,
        dialogue_max_turns=websocket.app.state.config.dialogue_max_turns,
        dialogue_max_chars=websocket.app.state.config.dialogue_max_chars,
    )
    await runtime.start()
    await runtime.send_json(
        {
            "type": "hello",
            "protocol_version": PROTOCOL_VERSION,
            "session_id": session_id,
            "audio": {
                "input": {
                    "codec": INPUT_AUDIO_FORMAT.codec,
                    "sample_rate": INPUT_AUDIO_FORMAT.sample_rate,
                    "channels": INPUT_AUDIO_FORMAT.channels,
                    "frame_duration_ms": INPUT_AUDIO_FORMAT.frame_duration_ms,
                },
                "output": {
                    "codec": websocket.app.state.chat_service.audio_format.codec,
                    "sample_rate": websocket.app.state.chat_service.audio_format.sample_rate,
                    "channels": websocket.app.state.chat_service.audio_format.channels,
                },
            },
        }
    )

    try:
        while True:
            frame = await websocket.receive()
            if frame["type"] == "websocket.disconnect":
                logger.info("websocket_disconnected session_id=%s", session_id)
                break

            raw_bytes = frame.get("bytes")
            if raw_bytes is not None:
                await runtime.push_audio(raw_bytes)
                continue

            raw_message = frame.get("text")
            if raw_message is None:
                await runtime.send_error(
                    code="unsupported_frame",
                    message="Client messages must be JSON text frames",
                )
                continue

            try:
                event = json.loads(raw_message)
            except json.JSONDecodeError:
                await runtime.send_error(
                    code="invalid_json",
                    message="Message must be valid JSON",
                )
                continue

            if not isinstance(event, dict) or not isinstance(event.get("type"), str):
                await runtime.send_error(
                    code="invalid_event",
                    message="Event must be an object with a string type",
                )
                continue

            event_id = event.get("event_id")
            if not isinstance(event_id, str):
                event_id = None

            if event["type"] == "ping":
                response = {
                    "type": "pong",
                    "session_id": session_id,
                }
                if event_id:
                    response["event_id"] = event_id
                await runtime.send_json(response)
                continue

            if event["type"] == "close":
                response = {
                    "type": "closing",
                    "session_id": session_id,
                }
                if event_id:
                    response["event_id"] = event_id
                await runtime.send_json(response)
                await websocket.close(code=1000, reason="client requested close")
                logger.info("websocket_closed session_id=%s reason=client_request", session_id)
                return

            if event["type"] == "text_input":
                await handle_text_input(
                    runtime,
                    event=event,
                    event_id=event_id,
                )
                continue

            if event["type"] == "audio_input_start":
                await runtime.start_audio_input(event)
                continue

            if event["type"] == "audio_input_stop":
                await runtime.stop_audio_input(event)
                continue

            if event["type"] == "playback_started":
                turn_id = event.get("turn_id")
                stream_id = event.get("stream_id")
                elapsed_ms = event.get("elapsed_ms")
                if (
                    isinstance(turn_id, str)
                    and isinstance(stream_id, str)
                    and isinstance(elapsed_ms, (int, float))
                    and not isinstance(elapsed_ms, bool)
                    and elapsed_ms >= 0
                ):
                    logger.info(
                        "browser_playback_started session_id=%s turn_id=%s stream_id=%s elapsed_ms=%.1f",
                        session_id,
                        turn_id,
                        stream_id,
                        elapsed_ms,
                    )
                else:
                    await runtime.send_error(
                        code="invalid_playback_metric",
                        message="playback_started contains invalid fields",
                        event_id=event_id,
                    )
                continue

            await runtime.send_error(
                code="unsupported_event",
                message=f"Unsupported event type: {event['type']}",
                event_id=event_id,
            )
    except WebSocketDisconnect as exc:
        logger.info(
            "websocket_disconnected session_id=%s code=%s",
            session_id,
            exc.code,
        )
        return
    finally:
        await runtime.close()
