import json
import logging
import math
from dataclasses import replace

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from newtalk.audio import INPUT_AUDIO_FORMAT
from newtalk.chat import DialogueSnapshot
from newtalk.transport.protocol import PROTOCOL_VERSION
from newtalk.transport.runtime import ConnectionRuntime
from newtalk.transport.text_chat import handle_text_input
from newtalk.identity import DeviceAuthenticationError
from newtalk.profile import SessionProfileCache


router = APIRouter()
logger = logging.getLogger(__name__)


@router.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket) -> None:
    config = websocket.app.state.config
    try:
        device = await websocket.app.state.identity_service.authenticate_device(
            websocket.cookies.get(config.device_cookie_name)
        )
    except DeviceAuthenticationError:
        logger.warning("websocket_rejected reason=device_authentication")
        await websocket.close(code=4401, reason="device registration required")
        return

    dialogue_snapshot = await websocket.app.state.dialogue_store.open(
        device_id=device.device_id,
        max_turns=config.dialogue_max_turns,
    )
    # user-only 输入可能早于成员删除已落库；删除任务尚未清理时也不能恢复其记录。
    members = await websocket.app.state.identity_service.list_identities(device.device_id)
    active_ids = {member.identity_id for member in members}
    visible = tuple(
        item for item in dialogue_snapshot.history
        if item.exchange.speaker_identity_id is None
        or item.exchange.speaker_identity_id in active_ids
    )
    for item in dialogue_snapshot.history:
        identity_id = item.exchange.speaker_identity_id
        if identity_id is not None and identity_id not in active_ids:
            websocket.app.state.dialogue_cache_coordinator.remove_identity(
                device.device_id, identity_id,
            )
    dialogue_snapshot = replace(
        dialogue_snapshot,
        history=visible,
        resumed=bool(visible),
        family=tuple(item.exchange for item in visible if item.lane == "family"),
        guest=tuple(item.exchange for item in visible if item.lane == "guest"),
    )
    await websocket.accept()
    session_id = dialogue_snapshot.session_id
    logger.info(
        "websocket_connected session_id=%s device_id=%s",
        session_id,
        device.device_id,
    )
    profile_cache = SessionProfileCache(
        device_id=device.device_id,
        identity_service=websocket.app.state.identity_service,
        provider=websocket.app.state.memory_provider,
    )
    websocket.app.state.profile_cache_coordinator.register(profile_cache)
    runtime = ConnectionRuntime(
        websocket,
        session_id=session_id,
        device_id=device.device_id,
        chat_service=websocket.app.state.chat_service,
        identity_service=websocket.app.state.identity_service,
        voiceprint_client=websocket.app.state.voiceprint_client,
        vad=websocket.app.state.vad,
        recognizer=websocket.app.state.recognizer,
        vad_pre_roll_ms=websocket.app.state.config.vad_pre_roll_ms,
        dialogue_max_turns=websocket.app.state.config.dialogue_max_turns,
        voiceprint_join_timeout_seconds=(
            websocket.app.state.config.voiceprint_join_timeout_seconds
        ),
        profile_cache=profile_cache,
        profile_max_chars=websocket.app.state.config.profile_max_chars,
        memory_writer=websocket.app.state.memory_writer,
        dialogue_store=websocket.app.state.dialogue_store,
        dialogue_snapshot=dialogue_snapshot,
        dialogue_coordinator=websocket.app.state.dialogue_cache_coordinator,
        reminder_service=websocket.app.state.reminder_service,
        music_service=websocket.app.state.music_service,
        podcast_service=websocket.app.state.podcast_service,
    )
    await runtime.start()
    await runtime.send_json(
        _hello_payload(
            websocket,
            device_id=device.device_id,
            dialogue_snapshot=dialogue_snapshot,
            profile_cache=profile_cache,
        )
    )
    try:
        await _receive_messages(websocket, runtime, session_id=session_id)
    except WebSocketDisconnect as exc:
        logger.info(
            "websocket_disconnected session_id=%s code=%s",
            session_id,
            exc.code,
        )
        return
    finally:
        await runtime.close()
        websocket.app.state.profile_cache_coordinator.unregister(profile_cache)


def _hello_payload(
    websocket: WebSocket,
    *,
    device_id: str,
    dialogue_snapshot: DialogueSnapshot,
    profile_cache: SessionProfileCache,
) -> dict:
    audio_format = websocket.app.state.chat_service.audio_format
    return {
        "type": "hello",
        "protocol_version": PROTOCOL_VERSION,
        "session_id": dialogue_snapshot.session_id,
        "device_id": device_id,
        "dialogue": {
            "resumed": dialogue_snapshot.resumed,
            "family_turns": len(dialogue_snapshot.family),
            "guest_turns": len(dialogue_snapshot.guest),
            "items": [
                {
                    "lane": item.lane,
                    "turn_id": item.exchange.turn_id,
                    "user_text": item.exchange.user_text,
                    "assistant_text": item.exchange.assistant_text,
                    "reply_status": "completed" if item.exchange.assistant_text else "user_only",
                    "speaker": {
                        "identity_id": item.exchange.speaker_identity_id,
                        "display_name": item.exchange.speaker_display_name,
                        "guest": item.exchange.speaker_identity_id is None,
                    },
                    "created_at": item.created_at.isoformat(),
                }
                for item in dialogue_snapshot.history
            ],
        },
        "profile": {"enabled": profile_cache.enabled},
        "music": {"enabled": websocket.app.state.music_service.enabled},
        "podcasts": {"enabled": websocket.app.state.podcast_service.enabled},
        "audio": {
            "input": {
                "codec": INPUT_AUDIO_FORMAT.codec,
                "sample_rate": INPUT_AUDIO_FORMAT.sample_rate,
                "channels": INPUT_AUDIO_FORMAT.channels,
                "frame_duration_ms": INPUT_AUDIO_FORMAT.frame_duration_ms,
            },
            "output": {
                "codec": audio_format.codec,
                "sample_rate": audio_format.sample_rate,
                "channels": audio_format.channels,
            },
        },
    }


async def _receive_messages(
    websocket: WebSocket,
    runtime: ConnectionRuntime,
    *,
    session_id: str,
) -> None:
    while True:
        frame = await websocket.receive()
        if frame["type"] == "websocket.disconnect":
            logger.info("websocket_disconnected session_id=%s", session_id)
            return

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

        event = await _parse_event(runtime, raw_message)
        if event is None:
            continue
        if await _dispatch_event(
            websocket,
            runtime,
            event=event,
            session_id=session_id,
        ):
            return


async def _parse_event(
    runtime: ConnectionRuntime,
    raw_message: str,
) -> dict | None:
    try:
        event = json.loads(raw_message)
    except json.JSONDecodeError:
        await runtime.send_error(
            code="invalid_json",
            message="Message must be valid JSON",
        )
        return None
    if not isinstance(event, dict) or not isinstance(event.get("type"), str):
        await runtime.send_error(
            code="invalid_event",
            message="Event must be an object with a string type",
        )
        return None
    return event


async def _dispatch_event(
    websocket: WebSocket,
    runtime: ConnectionRuntime,
    *,
    event: dict,
    session_id: str,
) -> bool:
    event_id = event.get("event_id")
    if not isinstance(event_id, str):
        event_id = None
    event_type = event["type"]

    if event_type == "ping":
        response = {"type": "pong", "session_id": session_id}
        if event_id:
            response["event_id"] = event_id
        await runtime.send_json(response)
        return False

    if event_type == "close":
        response = {"type": "closing", "session_id": session_id}
        if event_id:
            response["event_id"] = event_id
        await runtime.send_json(response)
        await websocket.close(code=1000, reason="client requested close")
        logger.info("websocket_closed session_id=%s reason=client_request", session_id)
        return True

    if event_type == "text_input":
        await handle_text_input(runtime, event=event, event_id=event_id)
        return False

    if event_type == "device_control":
        if not event_id or len(event_id) > 128:
            await runtime.send_error(code="invalid_event_id",
                                     message="device_control requires an event_id")
            return False
        if not runtime.remember_event(event_id):
            await runtime.send_error(code="duplicate_event", message="Duplicate device control",
                                     event_id=event_id)
            return False
        await runtime.submit_device_control(event.get("action"), event_id=event_id)
        return False

    if event_type == "device_control_result":
        if not runtime.device_control_result(event):
            await runtime.send_error(code="invalid_device_control_result",
                                     message="Unknown, expired or mismatched device control result")
        return False

    if event_type == "music_control":
        await runtime.submit_music_control(event)
        return False

    if event_type == "media_result":
        runtime.media_result(event)
        return False

    if event_type == "media_state":
        runtime.media_state(event)
        return False

    if event_type == "reminder_playback":
        if not runtime.reminder_playback(event):
            await runtime.send_error(code="invalid_reminder_playback", message="Invalid or busy reminder receipt")
        return False

    if event_type == "dialogue_clear":
        if not event_id:
            await runtime.send_error(
                code="invalid_event_id",
                message="dialogue_clear requires a non-empty string event_id",
            )
            return False
        if not runtime.remember_event(event_id):
            await runtime.send_error(
                code="duplicate_event",
                message="event_id has already been processed on this connection",
                event_id=event_id,
            )
            return False
        await runtime.clear_dialogue(event_id=event_id)
        return False

    if event_type == "audio_input_start":
        await runtime.start_audio_input(event)
        return False

    if event_type == "audio_input_stop":
        await runtime.stop_audio_input(event)
        return False

    if event_type == "playback_audible":
        metrics = event.get("metrics")
        allowed = {"speech_end_to_audible_ms", "vad_to_audible_ms", "turn_to_audible_ms",
                   "tts_to_audible_ms"}
        valid = (
            isinstance(event.get("turn_id"), str) and 0 < len(event["turn_id"]) <= 128
            and isinstance(event.get("stream_id"), str) and 0 < len(event["stream_id"]) <= 128
            and event.get("output_clock") in ("output_timestamp", "render_clock")
            and isinstance(metrics, dict) and "turn_to_audible_ms" in metrics
            and metrics.keys() <= allowed
            and all(isinstance(value, (int, float)) and not isinstance(value, bool)
                    and 0 <= value <= 3_600_000 and math.isfinite(value)
                    for value in metrics.values())
        )
        if valid:
            runtime.record_playback_timing(event)
        else:
            await runtime.send_error(code="invalid_playback_metric",
                                     message="playback_audible contains invalid fields", event_id=event_id)
        return False

    if event_type == "playback_started":
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
                "browser_playback_started session_id=%s turn_id=%s "
                "stream_id=%s elapsed_ms=%.1f",
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
        return False

    if event_type == "playback_drained":
        turn_id = event.get("turn_id")
        stream_id = event.get("stream_id")
        if isinstance(turn_id, str) and isinstance(stream_id, str):
            await runtime.playback_drained(turn_id=turn_id, stream_id=stream_id)
        else:
            await runtime.send_error(
                code="invalid_playback_metric",
                message="playback_drained contains invalid fields",
                event_id=event_id,
            )
        return False

    await runtime.send_error(
        code="unsupported_event",
        message=f"Unsupported event type: {event_type}",
        event_id=event_id,
    )
    return False
