import asyncio
from typing import Any

import pytest

from newtalk.asr import FakeASR
from newtalk.chat import (
    ChatService,
    DialogueCacheCoordinator,
    DialogueSnapshot,
    InMemoryDialogueStore,
)
from newtalk.identity import IdentityService, InMemoryIdentityStore
from newtalk.memory import DisabledMemoryWriteService
from newtalk.memory import DisabledMemoryProvider
from newtalk.profile import SessionProfileCache
from newtalk.transport.runtime import ConnectionRuntime
from newtalk.transport.runtime import _RECENT_EVENT_LIMIT
from newtalk.voiceprint import DisabledVoicePrintClient


class RecordingWebSocket:
    def __init__(self, *, block_first_send: bool = False, fail_sends: bool = False) -> None:
        self.block_first_send = block_first_send
        self.fail_sends = fail_sends
        self.first_send_started = asyncio.Event()
        self.release_first_send = asyncio.Event()
        self.json_events: list[dict[str, Any]] = []
        self.binary_frames: list[bytes] = []
        self._send_count = 0

    async def send_json(self, payload: dict[str, Any]) -> None:
        self._send_count += 1
        if self.fail_sends:
            raise RuntimeError("deterministic send failure")
        if self.block_first_send and self._send_count == 1:
            self.first_send_started.set()
            await self.release_first_send.wait()
        self.json_events.append(payload)

    async def send_bytes(self, payload: bytes) -> None:
        if self.fail_sends:
            raise RuntimeError("deterministic send failure")
        self.binary_frames.append(payload)


class TrackingRuntime(ConnectionRuntime):
    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.stream_tasks: dict[str, asyncio.Task[None]] = {}

    async def _stream_turn(self, turn, *, event_id, dialogue) -> None:
        del event_id, dialogue
        task = asyncio.current_task()
        assert task is not None
        self.stream_tasks[turn.user_text] = task
        await asyncio.Future()


def make_runtime(
    websocket: RecordingWebSocket,
    *,
    runtime_type: type[ConnectionRuntime] = ConnectionRuntime,
) -> ConnectionRuntime:
    identity_service = IdentityService(InMemoryIdentityStore())
    profile_cache = SessionProfileCache(
        device_id="device-1",
        identity_service=identity_service,
        provider=DisabledMemoryProvider(),
    )
    return runtime_type(
        websocket,
        session_id="session-1",
        device_id="device-1",
        chat_service=ChatService(),
        identity_service=identity_service,
        voiceprint_client=DisabledVoicePrintClient(),
        vad=object(),
        recognizer=FakeASR(),
        vad_pre_roll_ms=300,
        dialogue_max_turns=10,
        dialogue_max_chars=8_000,
        voiceprint_join_timeout_seconds=0.1,
        profile_cache=profile_cache,
        profile_max_chars=2_000,
        memory_writer=DisabledMemoryWriteService(),
        dialogue_store=InMemoryDialogueStore(),
        dialogue_snapshot=DialogueSnapshot(
            session_id="session-1",
            resumed=False,
            family=(),
            guest=(),
            history=(),
        ),
        dialogue_coordinator=DialogueCacheCoordinator(),
    )


def test_concurrent_turn_replacement_leaves_no_orphan_stream_task() -> None:
    async def scenario() -> None:
        websocket = RecordingWebSocket(block_first_send=True)
        runtime = make_runtime(websocket, runtime_type=TrackingRuntime)
        assert isinstance(runtime, TrackingRuntime)
        await runtime.start()

        first = asyncio.create_task(
            runtime.start_turn(text="first", event_id="event-1", identity=None)
        )
        await asyncio.wait_for(websocket.first_send_started.wait(), timeout=1)
        second = asyncio.create_task(
            runtime.start_turn(text="second", event_id="event-2", identity=None)
        )
        await asyncio.sleep(0)
        websocket.release_first_send.set()

        orphan_tasks: list[asyncio.Task[None]] = []
        try:
            await asyncio.wait_for(asyncio.gather(first, second), timeout=1)
            await asyncio.sleep(0)
            active_task = runtime._turn_state.task
            assert active_task is runtime.stream_tasks["second"]
            orphan_tasks = [
                task
                for task in runtime.stream_tasks.values()
                if task is not active_task and not task.done()
            ]
            assert orphan_tasks == []
            assert [event["type"] for event in websocket.json_events] == [
                "turn_started",
                "turn_cancelled",
                "audio_stop",
                "turn_started",
            ]
        finally:
            for task in orphan_tasks:
                task.cancel()
            if orphan_tasks:
                await asyncio.gather(*orphan_tasks, return_exceptions=True)
            await runtime.close()

    asyncio.run(scenario())


def test_enqueue_fails_immediately_after_sender_stops() -> None:
    async def scenario() -> None:
        runtime = make_runtime(RecordingWebSocket(fail_sends=True))
        await runtime.start()
        try:
            with pytest.raises(RuntimeError, match="deterministic send failure"):
                await runtime.send_json({"type": "first"})
            with pytest.raises(RuntimeError, match="deterministic send failure"):
                await asyncio.wait_for(
                    runtime.send_json({"type": "after-failure"}),
                    timeout=0.1,
                )
        finally:
            await runtime.close()

    asyncio.run(scenario())


def test_event_deduplication_window_is_bounded() -> None:
    runtime = make_runtime(RecordingWebSocket())

    assert runtime.remember_event("oldest") is True
    assert runtime.remember_event("oldest") is False
    for index in range(_RECENT_EVENT_LIMIT):
        assert runtime.remember_event(f"event-{index}") is True

    assert len(runtime._seen_event_ids) == _RECENT_EVENT_LIMIT
    assert runtime.remember_event(f"event-{_RECENT_EVENT_LIMIT - 1}") is False
    assert runtime.remember_event("oldest") is True
