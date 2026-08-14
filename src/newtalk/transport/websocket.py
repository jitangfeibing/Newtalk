import json
import logging
from uuid import uuid4

from fastapi import APIRouter, WebSocket, WebSocketDisconnect


router = APIRouter()
PROTOCOL_VERSION = "0.1"
logger = logging.getLogger(__name__)


async def send_protocol_error(
    websocket: WebSocket,
    *,
    code: str,
    message: str,
    event_id: str | None = None,
) -> None:
    event: dict[str, str] = {
        "type": "error",
        "code": code,
        "message": message,
    }
    if event_id:
        event["event_id"] = event_id
    await websocket.send_json(event)


@router.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket) -> None:
    await websocket.accept()
    session_id = str(uuid4())
    logger.info("websocket_connected session_id=%s", session_id)
    await websocket.send_json(
        {
            "type": "hello",
            "protocol_version": PROTOCOL_VERSION,
            "session_id": session_id,
        }
    )

    try:
        while True:
            frame = await websocket.receive()
            if frame["type"] == "websocket.disconnect":
                logger.info("websocket_disconnected session_id=%s", session_id)
                break

            raw_message = frame.get("text")
            if raw_message is None:
                await send_protocol_error(
                    websocket,
                    code="unsupported_frame",
                    message="P1 only accepts JSON text frames",
                )
                continue

            try:
                event = json.loads(raw_message)
            except json.JSONDecodeError:
                await send_protocol_error(
                    websocket,
                    code="invalid_json",
                    message="Message must be valid JSON",
                )
                continue

            if not isinstance(event, dict) or not isinstance(event.get("type"), str):
                await send_protocol_error(
                    websocket,
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
                await websocket.send_json(response)
                continue

            if event["type"] == "close":
                response = {
                    "type": "closing",
                    "session_id": session_id,
                }
                if event_id:
                    response["event_id"] = event_id
                await websocket.send_json(response)
                await websocket.close(code=1000, reason="client requested close")
                logger.info("websocket_closed session_id=%s reason=client_request", session_id)
                return

            await send_protocol_error(
                websocket,
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
