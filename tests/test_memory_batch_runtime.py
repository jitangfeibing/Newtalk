import asyncio
from datetime import UTC, datetime

from newtalk.chat import ChatService
from newtalk.chat.fake_llm import FakeLLM
from newtalk.identity.models import Identity, IdentityStatus
from tests.test_transport_runtime import RecordingWebSocket, make_runtime


class Batches:
    def __init__(self):
        self.inputs = []
        self.finished = []
        self.sealed = []
        self.recording = asyncio.Event()
        self.release = asyncio.Event()
        self.release.set()

    async def record_input(self, turn, *, segment_id):
        self.recording.set()
        await self.release.wait()
        self.inputs.append((turn, segment_id))
        return True

    async def finish_input(self, turn, *, assistant_text=None, discard=False):
        self.finished.append((turn, assistant_text, discard))

    async def flush_segment(self, *, device_id, segment_id, reason):
        self.sealed.append((segment_id, reason, tuple(self.finished)))
        return 1


def configured_runtime():
    runtime = make_runtime(RecordingWebSocket())
    runtime._chat_service = ChatService(FakeLLM(chunk_delay_seconds=0))
    batches = Batches()
    runtime._memory_batches = batches
    now = datetime.now(UTC)
    member = Identity("member", runtime.device_id, "Member", None, None, None,
                      IdentityStatus.ACTIVE, None, now, now)

    async def valid(turn):
        return True

    runtime._can_commit_turn = valid

    async def no_raw_upload(*args):
        raise AssertionError("Batch mode must not upload each completed turn")

    runtime._memory_writer.enqueue_completed_turn = no_raw_upload
    return runtime, batches, member


def test_completed_input_is_buffered_then_sealed_before_dialogue_clear():
    async def scenario():
        runtime, batches, member = configured_runtime()
        try:
            await runtime.start()
            await runtime.start_turn(text="我给妈妈准备了项链", event_id="one", identity=member)
            await asyncio.wait_for(runtime._turn_state.task, 2)
            await runtime.clear_dialogue(event_id="new")
            assert len(batches.inputs) == 1
            assert len(batches.finished) == 1
            assert batches.finished[0][1] is not None
            first_segment = batches.inputs[0][1]
            assert batches.sealed[0][0:2] == (first_segment, "new_dialogue")
            assert len(batches.sealed[0][2]) == 1
            assert runtime._dialogue.exchanges == ()
            await runtime.start_turn(text="我想学英语", event_id="two", identity=member)
            await asyncio.wait_for(runtime._turn_state.task, 2)
        finally:
            await runtime.close()
            await runtime._chat_service.aclose()
        assert batches.inputs[1][1] != first_segment
        assert batches.sealed[-1][1] == "disconnect"
        assert len(batches.finished) == 2

    asyncio.run(scenario())


def test_cancel_before_stream_start_keeps_user_only_material_without_waiting_for_capture():
    async def scenario():
        runtime, batches, member = configured_runtime()
        batches.release.clear()
        try:
            await runtime.start()
            await runtime.start_turn(text="补给妈妈的生日礼物，买了一条项链", event_id="one", identity=member)
            await runtime.cancel_turn(reason="barge_in")
            await asyncio.wait_for(batches.recording.wait(), 1)
            assert not batches.finished
            clearing = asyncio.create_task(runtime.clear_dialogue(event_id="new"))
            batches.release.set()
            await asyncio.wait_for(clearing, 2)
            assert len(batches.finished) == 1
            assert batches.finished[0][0].user_text == "补给妈妈的生日礼物，买了一条项链"
            assert batches.finished[0][1] is None
            assert len(batches.sealed[0][2]) == 1
        finally:
            batches.release.set()
            await runtime.close()
            await runtime._chat_service.aclose()

    asyncio.run(scenario())


def test_guest_uses_no_member_batch_and_close_never_waits_for_curator():
    async def scenario():
        runtime, batches, _member = configured_runtime()
        try:
            await runtime.start()
            await runtime.start_turn(text="访客随便聊聊", event_id="guest", identity=None)
            await asyncio.wait_for(runtime._turn_state.task, 2)
        finally:
            await asyncio.wait_for(runtime.close(), 2)
            await runtime._chat_service.aclose()
        assert not batches.finished

    asyncio.run(scenario())
