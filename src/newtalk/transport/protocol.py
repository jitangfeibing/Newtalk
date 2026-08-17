from fastapi import WebSocket


PROTOCOL_VERSION = "0.3"


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
