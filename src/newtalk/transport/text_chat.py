from contextlib import aclosing
import logging

from fastapi import WebSocket, WebSocketDisconnect

from newtalk.chat import (
    AudioCompleted,
    AudioFailed,
    AudioFrame,
    AudioStarted,
    TextDelta,
    TurnCompleted,
)
from newtalk.transport.protocol import send_protocol_error


MAX_TEXT_LENGTH = 4000
logger = logging.getLogger(__name__)


async def handle_text_input(
    websocket: WebSocket,
    *,
    session_id: str,
    event: dict,
    event_id: str | None,
    seen_event_ids: set[str],
) -> None:
    if not event_id:
        await send_protocol_error(
            websocket,
            code="invalid_event_id",
            message="text_input requires a non-empty string event_id",
        )
        return

    raw_text = event.get("text")
    if not isinstance(raw_text, str) or not raw_text.strip():
        await send_protocol_error(
            websocket,
            code="invalid_text",
            message="text_input requires non-empty text",
            event_id=event_id,
        )
        return

    text = raw_text.strip()
    if len(text) > MAX_TEXT_LENGTH:
        await send_protocol_error(
            websocket,
            code="text_too_long",
            message=f"text must not exceed {MAX_TEXT_LENGTH} characters",
            event_id=event_id,
        )
        return

    if event_id in seen_event_ids:
        await send_protocol_error(
            websocket,
            code="duplicate_event",
            message="event_id has already been processed on this connection",
            event_id=event_id,
        )
        return

    seen_event_ids.add(event_id)
    chat_service = websocket.app.state.chat_service
    turn = chat_service.create_turn(session_id=session_id, user_text=text)
    logger.info(
        "turn_started session_id=%s turn_id=%s event_id=%s",
        session_id,
        turn.turn_id,
        event_id,
    )
    await websocket.send_json(
        {
            "type": "turn_started",
            "session_id": session_id,
            "turn_id": turn.turn_id,
            "event_id": event_id,
        }
    )

    try:
        async with aclosing(chat_service.stream_turn(turn)) as output_stream:
            async for output in output_stream:
                if isinstance(output, TextDelta):
                    await websocket.send_json(
                        {
                            "type": "text_delta",
                            "turn_id": turn.turn_id,
                            "event_id": event_id,
                            "sequence": output.sequence,
                            "delta": output.text,
                        }
                    )
                    continue
                if isinstance(output, AudioStarted):
                    await websocket.send_json(
                        {
                            "type": "audio_start",
                            "turn_id": turn.turn_id,
                            "stream_id": output.stream_id,
                            "codec": output.audio_format.codec,
                            "sample_rate": output.audio_format.sample_rate,
                            "channels": output.audio_format.channels,
                        }
                    )
                    continue
                if isinstance(output, AudioFrame):
                    await websocket.send_bytes(output.data)
                    continue
                if isinstance(output, AudioCompleted):
                    await websocket.send_json(
                        {
                            "type": "audio_end",
                            "turn_id": turn.turn_id,
                            "stream_id": output.stream_id,
                            "frames": output.frame_count,
                            "bytes": output.byte_count,
                        }
                    )
                    continue
                if isinstance(output, AudioFailed):
                    await websocket.send_json(
                        {
                            "type": "audio_failed",
                            "turn_id": turn.turn_id,
                            "stream_id": output.stream_id,
                            "message": output.message,
                        }
                    )
                    continue
                if isinstance(output, TurnCompleted):
                    await websocket.send_json(
                        {
                            "type": "turn_completed",
                            "turn_id": turn.turn_id,
                            "event_id": event_id,
                            "text": output.text,
                        }
                    )
    except WebSocketDisconnect:
        raise
    except Exception:
        logger.warning(
            "turn_failed session_id=%s turn_id=%s event_id=%s",
            session_id,
            turn.turn_id,
            event_id,
        )
        await websocket.send_json(
            {
                "type": "turn_failed",
                "turn_id": turn.turn_id,
                "event_id": event_id,
                "code": "chat_failed",
                "message": "Unable to generate a reply",
            }
        )
        return

    logger.info(
        "turn_completed session_id=%s turn_id=%s",
        session_id,
        turn.turn_id,
    )
