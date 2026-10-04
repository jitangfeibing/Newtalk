import asyncio
from contextlib import aclosing, suppress
from dataclasses import dataclass, field
import logging
from time import perf_counter
from typing import Any

from fastapi import WebSocket

from newtalk.asr import AsrFinal, AsrPartial, SpeechRecognizer
from newtalk.audio import (
    INPUT_AUDIO_FORMAT,
    AudioInputSession,
    SpeechBoundary,
    VoiceActivityDetector,
    pcm_s16le_to_wav,
)
from newtalk.chat import (
    AudioCompleted,
    AudioFailed,
    AudioFrame,
    AudioStarted,
    ChatService,
    ChatMessage,
    DialogueSnapshot,
    DialogueStore,
    DialogueCacheCoordinator,
    DialogueSession,
    TextDelta,
    Turn,
    TurnCompleted,
)
from newtalk.identity import Identity, IdentityNotFoundError, IdentityService
from newtalk.memory import MemoryWriter
from newtalk.profile import SessionProfileCache
from newtalk.voiceprint import (
    VoicePrintClient,
    VoicePrintError,
    VoicePrintIdentification,
    VoicePrintUnavailableError,
)


logger = logging.getLogger(__name__)
_SEND_END = object()


@dataclass(slots=True)
class _OutboundFrame:
    payload: dict[str, Any] | bytes | object
    turn_id: str | None
    delivered: asyncio.Future[None]


@dataclass(slots=True)
class _VoiceJoinState:
    ready: asyncio.Event = field(default_factory=asyncio.Event)
    task: asyncio.Task[VoicePrintIdentification] | None = None
    failure_reason: str | None = None
    started_at: float = field(default_factory=perf_counter)


@dataclass(frozen=True, slots=True)
class _SpeakerResolution:
    identity: Identity | None
    score: float
    reason: str
    provider_elapsed_ms: float | None
    elapsed_ms: float


class ConnectionRuntime:
    """Owns the short-lived state and tasks for one WebSocket connection."""

    def __init__(
        self,
        websocket: WebSocket,
        *,
        session_id: str,
        device_id: str,
        chat_service: ChatService,
        identity_service: IdentityService,
        voiceprint_client: VoicePrintClient,
        vad: VoiceActivityDetector,
        recognizer: SpeechRecognizer,
        vad_pre_roll_ms: int,
        dialogue_max_turns: int,
        dialogue_max_chars: int,
        voiceprint_join_timeout_seconds: float,
        profile_cache: SessionProfileCache,
        profile_max_chars: int,
        memory_writer: MemoryWriter,
        dialogue_store: DialogueStore,
        dialogue_snapshot: DialogueSnapshot,
        dialogue_coordinator: DialogueCacheCoordinator,
    ) -> None:
        self.websocket = websocket
        self.session_id = session_id
        self.device_id = device_id
        self._chat_service = chat_service
        self._identity_service = identity_service
        self._voiceprint_client = voiceprint_client
        self._vad = vad
        self._recognizer = recognizer
        self._vad_pre_roll_ms = vad_pre_roll_ms
        self._family_dialogue = DialogueSession(
            session_id,
            max_turns=dialogue_max_turns,
            max_chars=dialogue_max_chars,
        )
        self._guest_dialogue = DialogueSession(
            session_id,
            max_turns=dialogue_max_turns,
            max_chars=dialogue_max_chars,
        )
        self._family_dialogue.restore(dialogue_snapshot.family)
        self._guest_dialogue.restore(dialogue_snapshot.guest)
        self._dialogue_store = dialogue_store
        self._dialogue_coordinator = dialogue_coordinator
        self._dialogue_coordinator.register(device_id, self._family_dialogue)
        self._voiceprint_join_timeout_seconds = voiceprint_join_timeout_seconds
        self._profile_cache = profile_cache
        self._profile_max_chars = profile_max_chars
        self._memory_writer = memory_writer
        self._voice_joins: dict[str, _VoiceJoinState] = {}
        self._seen_event_ids: set[str] = set()
        self._outbound: asyncio.Queue[_OutboundFrame] = asyncio.Queue(maxsize=256)
        self._sender_task: asyncio.Task[None] | None = None
        self._active_turn_task: asyncio.Task[None] | None = None
        self._active_turn_id: str | None = None
        self._active_stream_id: str | None = None
        self._audio_session: AudioInputSession | None = None
        self._capture_id: str | None = None
        self._audio_started_at: float | None = None
        self._closing = False

    @property
    def active_turn_id(self) -> str | None:
        return self._active_turn_id

    async def start(self) -> None:
        self._sender_task = asyncio.create_task(self._send_loop())
        self._profile_cache.start()

    async def send_json(
        self,
        payload: dict[str, Any],
        *,
        turn_id: str | None = None,
    ) -> None:
        await self._enqueue(payload, turn_id=turn_id)

    async def send_bytes(self, payload: bytes, *, turn_id: str) -> None:
        await self._enqueue(payload, turn_id=turn_id)

    async def send_error(
        self,
        *,
        code: str,
        message: str,
        event_id: str | None = None,
    ) -> None:
        event: dict[str, Any] = {
            "type": "error",
            "code": code,
            "message": message,
        }
        if event_id:
            event["event_id"] = event_id
        await self.send_json(event)

    def remember_event(self, event_id: str) -> bool:
        if event_id in self._seen_event_ids:
            return False
        self._seen_event_ids.add(event_id)
        return True

    async def start_text_turn(
        self,
        *,
        text: str,
        event_id: str,
        identity_id: str | None,
    ) -> None:
        identity = None
        if identity_id is not None:
            try:
                identity = await self._identity_service.get_identity(
                    device_id=self.device_id,
                    identity_id=identity_id,
                )
            except IdentityNotFoundError:
                await self.send_error(
                    code="identity_not_found",
                    message="Selected member does not belong to this device",
                    event_id=event_id,
                )
                return
        await self.start_turn(text=text, event_id=event_id, identity=identity)

    async def start_turn(
        self,
        *,
        text: str,
        event_id: str,
        identity: Identity | None,
    ) -> None:
        await self.cancel_turn(reason="superseded")
        context_started_at = perf_counter()
        dialogue = self._family_dialogue if identity is not None else self._guest_dialogue
        speaker_display_name = identity.display_name if identity is not None else "Guest"
        speaker_relationship = identity.relationship if identity is not None else None
        speaker_identity_id = identity.identity_id if identity is not None else None
        profile_snapshot = (
            self._profile_cache.snapshot_for(speaker_identity_id)
            if speaker_identity_id is not None
            else None
        )
        profile_prompt = (
            profile_snapshot.to_prompt(max_chars=self._profile_max_chars)
            if profile_snapshot is not None
            else None
        )
        context_messages = (
            (ChatMessage(role="system", content=profile_prompt),)
            if profile_prompt
            else ()
        )
        messages = dialogue.messages_for(
            text,
            speaker_identity_id=speaker_identity_id,
            speaker_display_name=speaker_display_name,
            speaker_relationship=speaker_relationship,
            context_messages=context_messages,
        )
        turn = self._chat_service.create_turn(
            session_id=self.session_id,
            user_text=text,
            messages=messages,
            device_id=self.device_id,
            speaker_identity_id=speaker_identity_id,
            speaker_display_name=speaker_display_name,
            speaker_relationship=speaker_relationship,
            profile_ready=profile_snapshot is not None,
            profile_field_count=(
                len(profile_snapshot.fields) if profile_snapshot is not None else 0
            ),
        )
        self._active_turn_id = turn.turn_id
        self._active_stream_id = None
        logger.info(
            "turn_started session_id=%s turn_id=%s event_id=%s",
            self.session_id,
            turn.turn_id,
            event_id,
        )
        logger.info(
            "context_ready session_id=%s turn_id=%s completed_turns=%s messages=%s chars=%s elapsed_ms=%.1f",
            self.session_id,
            turn.turn_id,
            len(dialogue.exchanges),
            len(messages),
            sum(len(message.content) for message in messages),
            (perf_counter() - context_started_at) * 1000,
        )
        await self.send_json(
            {
                "type": "turn_started",
                "session_id": self.session_id,
                "turn_id": turn.turn_id,
                "event_id": event_id,
                "speaker": {
                    "identity_id": speaker_identity_id,
                    "display_name": speaker_display_name,
                    "guest": identity is None,
                },
                "profile": {
                    "enabled": self._profile_cache.enabled,
                    "ready": turn.profile_ready,
                    "fields": turn.profile_field_count,
                },
            },
            turn_id=turn.turn_id,
        )
        self._active_turn_task = asyncio.create_task(
            self._stream_turn(turn, event_id=event_id, dialogue=dialogue)
        )

    async def cancel_turn(self, *, reason: str, notify: bool = True) -> None:
        task = self._active_turn_task
        turn_id = self._active_turn_id
        stream_id = self._active_stream_id
        if turn_id is None:
            return

        self._active_turn_id = None
        self._active_stream_id = None
        self._active_turn_task = None
        if task is not None:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        logger.info(
            "turn_cancelled session_id=%s turn_id=%s reason=%s",
            self.session_id,
            turn_id,
            reason,
        )
        if notify and not self._closing:
            await self.send_json(
                {
                    "type": "turn_cancelled",
                    "turn_id": turn_id,
                    "reason": reason,
                }
            )
            await self.send_json(
                {
                    "type": "audio_stop",
                    "turn_id": turn_id,
                    "stream_id": stream_id,
                    "reason": reason,
                }
            )

    async def start_audio_input(self, event: dict[str, Any]) -> None:
        event_id = _string_or_none(event.get("event_id"))
        capture_id = _string_or_none(event.get("capture_id"))
        if not capture_id:
            await self.send_error(
                code="invalid_capture_id",
                message="audio_input_start requires capture_id",
                event_id=event_id,
            )
            return
        if self._audio_session is not None:
            await self.send_error(
                code="audio_input_active",
                message="Only one audio input can be active per connection",
                event_id=event_id,
            )
            return
        if not _matches_input_format(event.get("format")):
            await self.send_error(
                code="unsupported_audio_format",
                message="Audio input must be 16kHz mono PCM S16LE with 20ms frames",
                event_id=event_id,
            )
            return

        self._capture_id = capture_id
        self._audio_started_at = perf_counter()
        self._audio_session = AudioInputSession(
            vad_stream=self._vad.create_stream(),
            recognizer=self._recognizer,
            on_boundary=self._on_speech_boundary,
            on_asr_event=self._on_asr_event,
            on_asr_error=self._on_asr_error,
            on_utterance_audio=self._on_utterance_audio,
            pre_roll_ms=self._vad_pre_roll_ms,
        )
        await self.send_json(
            {
                "type": "audio_input_ready",
                "capture_id": capture_id,
                "event_id": event_id,
            }
        )
        logger.info(
            "audio_input_started session_id=%s capture_id=%s",
            self.session_id,
            capture_id,
        )

    async def push_audio(self, pcm: bytes) -> None:
        if self._audio_session is None:
            await self.send_error(
                code="audio_input_not_started",
                message="Send audio_input_start before binary audio frames",
            )
            return
        try:
            await self._audio_session.push(pcm)
        except ValueError as exc:
            await self.send_error(code="invalid_audio_frame", message=str(exc))

    async def stop_audio_input(self, event: dict[str, Any]) -> None:
        event_id = _string_or_none(event.get("event_id"))
        capture_id = _string_or_none(event.get("capture_id"))
        if self._audio_session is None or capture_id != self._capture_id:
            await self.send_error(
                code="audio_input_not_found",
                message="capture_id does not match the active audio input",
                event_id=event_id,
            )
            return
        session = self._audio_session
        self._audio_session = None
        self._capture_id = None
        await session.close()
        await self.send_json(
            {
                "type": "audio_input_stopped",
                "capture_id": capture_id,
                "event_id": event_id,
            }
        )
        logger.info(
            "audio_input_stopped session_id=%s capture_id=%s",
            self.session_id,
            capture_id,
        )

    async def close(self) -> None:
        if self._closing:
            return
        self._closing = True
        self._dialogue_coordinator.unregister(self._family_dialogue)
        await self._profile_cache.close()
        if self._audio_session is not None:
            session = self._audio_session
            self._audio_session = None
            with suppress(Exception):
                await session.close()
        voiceprint_tasks = [
            state.task
            for state in self._voice_joins.values()
            if state.task is not None and not state.task.done()
        ]
        for task in voiceprint_tasks:
            task.cancel()
        if voiceprint_tasks:
            await asyncio.gather(*voiceprint_tasks, return_exceptions=True)
        self._voice_joins.clear()
        await self.cancel_turn(reason="connection_closed", notify=False)
        if self._sender_task is not None and not self._sender_task.done():
            with suppress(Exception):
                await self._enqueue(_SEND_END, turn_id=None)
        if self._sender_task is not None:
            with suppress(Exception):
                await self._sender_task

    async def _stream_turn(
        self,
        turn,
        *,
        event_id: str,
        dialogue: DialogueSession,
    ) -> None:
        try:
            async with aclosing(self._chat_service.stream_turn(turn)) as outputs:
                async for output in outputs:
                    if isinstance(output, TextDelta):
                        await self.send_json(
                            {
                                "type": "text_delta",
                                "turn_id": turn.turn_id,
                                "event_id": event_id,
                                "sequence": output.sequence,
                                "delta": output.text,
                            },
                            turn_id=turn.turn_id,
                        )
                    elif isinstance(output, AudioStarted):
                        if self._active_turn_id == turn.turn_id:
                            self._active_stream_id = output.stream_id
                        await self.send_json(
                            {
                                "type": "audio_start",
                                "turn_id": turn.turn_id,
                                "stream_id": output.stream_id,
                                "codec": output.audio_format.codec,
                                "sample_rate": output.audio_format.sample_rate,
                                "channels": output.audio_format.channels,
                            },
                            turn_id=turn.turn_id,
                        )
                    elif isinstance(output, AudioFrame):
                        await self.send_bytes(output.data, turn_id=turn.turn_id)
                    elif isinstance(output, AudioCompleted):
                        await self.send_json(
                            {
                                "type": "audio_end",
                                "turn_id": turn.turn_id,
                                "stream_id": output.stream_id,
                                "frames": output.frame_count,
                                "bytes": output.byte_count,
                            },
                            turn_id=turn.turn_id,
                        )
                    elif isinstance(output, AudioFailed):
                        await self.send_json(
                            {
                                "type": "audio_failed",
                                "turn_id": turn.turn_id,
                                "stream_id": output.stream_id,
                                "message": output.message,
                            },
                            turn_id=turn.turn_id,
                        )
                    elif isinstance(output, TurnCompleted):
                        committed = False
                        if (
                            self._active_turn_id == turn.turn_id
                            and await self._can_commit_turn(turn)
                        ):
                            dialogue.commit(turn, output.text)
                            committed = True
                            lane = (
                                "family"
                                if turn.speaker_identity_id is not None
                                else "guest"
                            )
                            try:
                                await self._dialogue_store.append(
                                    session_id=self.session_id,
                                    lane=lane,
                                    exchange=dialogue.exchanges[-1],
                                    max_turns=dialogue.max_turns,
                                )
                            except Exception:
                                logger.exception(
                                    "dialogue_persist_failed session_id=%s turn_id=%s",
                                    self.session_id,
                                    turn.turn_id,
                                )
                            logger.info(
                                "dialogue_committed session_id=%s turn_id=%s completed_turns=%s",
                                self.session_id,
                                turn.turn_id,
                                len(dialogue.exchanges),
                            )
                        if committed and turn.speaker_identity_id is not None:
                            try:
                                await self._memory_writer.enqueue_completed_turn(
                                    turn, output.text
                                )
                            except Exception:
                                logger.exception(
                                    "memory_job_enqueue_failed session_id=%s turn_id=%s",
                                    self.session_id,
                                    turn.turn_id,
                                )
                        await self.send_json(
                            {
                                "type": "turn_completed",
                                "turn_id": turn.turn_id,
                                "event_id": event_id,
                                "text": output.text,
                            },
                            turn_id=turn.turn_id,
                        )
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception(
                "turn_failed session_id=%s turn_id=%s event_id=%s",
                self.session_id,
                turn.turn_id,
                event_id,
            )
            await self.send_json(
                {
                    "type": "turn_failed",
                    "turn_id": turn.turn_id,
                    "event_id": event_id,
                    "code": "chat_failed",
                    "message": "Unable to generate a reply",
                },
                turn_id=turn.turn_id,
            )
        finally:
            if self._active_turn_id == turn.turn_id:
                self._active_turn_id = None
                self._active_stream_id = None
                self._active_turn_task = None
                logger.info(
                    "turn_completed session_id=%s turn_id=%s",
                    self.session_id,
                    turn.turn_id,
                )

    async def _can_commit_turn(self, turn: Turn) -> bool:
        if turn.speaker_identity_id is None:
            return True
        try:
            await self._identity_service.get_identity(
                device_id=turn.device_id,
                identity_id=turn.speaker_identity_id,
            )
            return True
        except IdentityNotFoundError:
            logger.info(
                "dialogue_commit_skipped_inactive_identity "
                "session_id=%s turn_id=%s identity_id=%s",
                self.session_id,
                turn.turn_id,
                turn.speaker_identity_id,
            )
            return False
        except Exception:
            logger.exception(
                "dialogue_commit_identity_check_failed "
                "session_id=%s turn_id=%s identity_id=%s",
                self.session_id,
                turn.turn_id,
                turn.speaker_identity_id,
            )
            # Keep this connection usable; PostgreSQL persistence has its own failure boundary.
            return True

    async def _on_speech_boundary(self, event: SpeechBoundary) -> None:
        if self._closing:
            return
        event_type = "vad_speech_start" if event.kind == "speech_start" else "vad_speech_end"
        elapsed_ms = (
            (perf_counter() - self._audio_started_at) * 1000
            if self._audio_started_at is not None
            else 0.0
        )
        await self.send_json(
            {
                "type": event_type,
                "capture_id": self._capture_id,
                "utterance_id": event.utterance_id,
                "probability": round(event.probability, 4),
                "audio_ms": round(event.audio_ms, 1),
            }
        )
        logger.info(
            "%s session_id=%s utterance_id=%s audio_ms=%.1f elapsed_ms=%.1f",
            event_type,
            self.session_id,
            event.utterance_id,
            event.audio_ms,
            elapsed_ms,
        )
        if event.kind == "speech_start":
            self._voice_joins[event.utterance_id] = _VoiceJoinState()
            await self.cancel_turn(reason="barge_in")

    async def _on_utterance_audio(self, utterance_id: str, pcm: bytes) -> None:
        if self._closing:
            return
        state = self._voice_joins.setdefault(utterance_id, _VoiceJoinState())
        try:
            wav = pcm_s16le_to_wav(pcm)
        except ValueError:
            state.failure_reason = "invalid_audio"
            state.ready.set()
            return
        state.task = asyncio.create_task(
            self._voiceprint_client.identify(device_id=self.device_id, sample=wav)
        )
        state.ready.set()

    async def _on_asr_event(self, utterance_id: str, event) -> None:
        if self._closing:
            return
        if isinstance(event, AsrPartial):
            await self.send_json(
                {
                    "type": "asr_partial",
                    "utterance_id": utterance_id,
                    "text": event.text,
                }
            )
            return
        if isinstance(event, AsrFinal):
            text = event.text.strip()
            await self.send_json(
                {
                    "type": "asr_final",
                    "utterance_id": utterance_id,
                    "text": text,
                }
            )
            logger.info(
                "asr_final session_id=%s utterance_id=%s recognizer=%s text_length=%s",
                self.session_id,
                utterance_id,
                type(self._recognizer).__name__,
                len(text),
            )
            if text:
                event_id = f"voice-{utterance_id}"
                self._seen_event_ids.add(event_id)
                resolution = await self._resolve_voice_speaker(utterance_id)
                await self.send_json(
                    {
                        "type": "speaker_resolved",
                        "utterance_id": utterance_id,
                        "identity_id": (
                            resolution.identity.identity_id
                            if resolution.identity is not None
                            else None
                        ),
                        "display_name": (
                            resolution.identity.display_name
                            if resolution.identity is not None
                            else "Guest"
                        ),
                        "matched": resolution.identity is not None,
                        "score": round(resolution.score, 4),
                        "reason": resolution.reason,
                        "provider_elapsed_ms": (
                            round(resolution.provider_elapsed_ms, 1)
                            if resolution.provider_elapsed_ms is not None
                            else None
                        ),
                        "elapsed_ms": round(resolution.elapsed_ms, 1),
                    }
                )
                await self.start_turn(
                    text=text,
                    event_id=event_id,
                    identity=resolution.identity,
                )
            else:
                await self._discard_voice_join(utterance_id)

    async def _resolve_voice_speaker(self, utterance_id: str) -> _SpeakerResolution:
        state = self._voice_joins.setdefault(utterance_id, _VoiceJoinState())
        deadline = asyncio.get_running_loop().time() + self._voiceprint_join_timeout_seconds
        identification: VoicePrintIdentification | None = None
        reason = state.failure_reason or "unavailable"
        try:
            remaining = max(0.0, deadline - asyncio.get_running_loop().time())
            await asyncio.wait_for(state.ready.wait(), timeout=remaining)
            if state.task is not None:
                remaining = max(0.0, deadline - asyncio.get_running_loop().time())
                identification = await asyncio.wait_for(state.task, timeout=remaining)
                reason = "matched" if identification.matched else "no_match"
        except TimeoutError:
            reason = "timeout"
            if state.task is not None:
                state.task.cancel()
        except VoicePrintUnavailableError:
            reason = "unavailable"
        except VoicePrintError:
            reason = "invalid_audio"
        except Exception:
            reason = "failed"
            logger.exception(
                "voiceprint_failed session_id=%s utterance_id=%s",
                self.session_id,
                utterance_id,
            )

        identity = None
        if identification is not None and identification.matched and identification.identity_id:
            try:
                identity = await self._identity_service.get_identity(
                    device_id=self.device_id,
                    identity_id=identification.identity_id,
                )
            except IdentityNotFoundError:
                reason = "identity_missing"
        elapsed_ms = (perf_counter() - state.started_at) * 1000
        await self._discard_voice_join(utterance_id, cancel=False)
        logger.info(
            "speaker_resolved session_id=%s utterance_id=%s identity_id=%s reason=%s score=%.4f elapsed_ms=%.1f",
            self.session_id,
            utterance_id,
            identity.identity_id if identity is not None else "guest",
            reason,
            identification.score if identification is not None else 0.0,
            elapsed_ms,
        )
        return _SpeakerResolution(
            identity=identity,
            score=identification.score if identification is not None else 0.0,
            reason=reason,
            provider_elapsed_ms=(
                identification.elapsed_ms if identification is not None else None
            ),
            elapsed_ms=elapsed_ms,
        )

    async def _discard_voice_join(
        self,
        utterance_id: str,
        *,
        cancel: bool = True,
    ) -> None:
        state = self._voice_joins.pop(utterance_id, None)
        if cancel and state is not None and state.task is not None and not state.task.done():
            state.task.cancel()
            await asyncio.gather(state.task, return_exceptions=True)

    async def _on_asr_error(self, utterance_id: str, error: Exception) -> None:
        if self._closing:
            return
        logger.warning(
            "asr_failed session_id=%s utterance_id=%s recognizer=%s error_type=%s",
            self.session_id,
            utterance_id,
            type(self._recognizer).__name__,
            type(error).__name__,
        )
        await self.send_json(
            {
                "type": "asr_failed",
                "utterance_id": utterance_id,
                "code": "recognition_failed",
                "message": "Unable to recognize speech",
            }
        )
        await self._discard_voice_join(utterance_id)

    async def _enqueue(self, payload: dict[str, Any] | bytes | object, *, turn_id: str | None) -> None:
        if self._sender_task is None:
            raise RuntimeError("Connection sender has not started")
        loop = asyncio.get_running_loop()
        delivered: asyncio.Future[None] = loop.create_future()
        await self._outbound.put(_OutboundFrame(payload, turn_id, delivered))
        await delivered

    async def _send_loop(self) -> None:
        failure: Exception | None = None
        try:
            while True:
                item = await self._outbound.get()
                try:
                    if item.payload is _SEND_END:
                        if not item.delivered.done():
                            item.delivered.set_result(None)
                        return
                    if item.turn_id is not None and item.turn_id != self._active_turn_id:
                        if not item.delivered.done():
                            item.delivered.set_result(None)
                        continue
                    if isinstance(item.payload, bytes):
                        await self.websocket.send_bytes(item.payload)
                    elif isinstance(item.payload, dict):
                        await self.websocket.send_json(item.payload)
                    if not item.delivered.done():
                        item.delivered.set_result(None)
                except Exception as exc:
                    if not item.delivered.done():
                        item.delivered.set_exception(exc)
                    raise
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            failure = exc
        finally:
            error = failure or RuntimeError("Connection sender stopped")
            while not self._outbound.empty():
                pending = self._outbound.get_nowait()
                if not pending.delivered.done():
                    pending.delivered.set_exception(error)


def _matches_input_format(value: Any) -> bool:
    return isinstance(value, dict) and value == {
        "codec": INPUT_AUDIO_FORMAT.codec,
        "sample_rate": INPUT_AUDIO_FORMAT.sample_rate,
        "channels": INPUT_AUDIO_FORMAT.channels,
        "frame_duration_ms": INPUT_AUDIO_FORMAT.frame_duration_ms,
    }


def _string_or_none(value: Any) -> str | None:
    return value if isinstance(value, str) and value else None
