from newtalk.transport.runtime import ConnectionRuntime


MAX_TEXT_LENGTH = 4000
async def handle_text_input(
    runtime: ConnectionRuntime,
    *,
    event: dict,
    event_id: str | None,
) -> None:
    if not event_id:
        await runtime.send_error(
            code="invalid_event_id",
            message="text_input requires a non-empty string event_id",
        )
        return

    raw_text = event.get("text")
    if not isinstance(raw_text, str) or not raw_text.strip():
        await runtime.send_error(
            code="invalid_text",
            message="text_input requires non-empty text",
            event_id=event_id,
        )
        return

    text = raw_text.strip()
    if len(text) > MAX_TEXT_LENGTH:
        await runtime.send_error(
            code="text_too_long",
            message=f"text must not exceed {MAX_TEXT_LENGTH} characters",
            event_id=event_id,
        )
        return

    if not runtime.remember_event(event_id):
        await runtime.send_error(
            code="duplicate_event",
            message="event_id has already been processed on this connection",
            event_id=event_id,
        )
        return

    await runtime.start_turn(text=text, event_id=event_id)
