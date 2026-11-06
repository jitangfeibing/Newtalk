import asyncio
from datetime import UTC, datetime, timedelta

from newtalk.chat import ChatService
from newtalk.memory import InMemoryMemoryJobStore, MemoryWriteService
from newtalk.memory.batch_store import (
    InMemoryMemoryBatchStore,
    MemoryBatch,
    MemorySource,
)
from newtalk.memory.batching import MemoryBatchService, _prepared_payload
from tests.test_memory import RecordingMemoryProvider


class Curator:
    def __init__(self):
        self.calls = []
        self.started = asyncio.Event()
        self.release = asyncio.Event()
        self.release.set()
        self.closed = False

    async def curate(self, batch):
        self.calls.append(batch)
        self.started.set()
        await self.release.wait()
        source = next(item for item in batch.sources if not item.context_only)
        return {"memories": [{"kind": "event", "content": "用户准备生日礼物，是否送出未知。",
            "source_turn_ids": [source.turn_id], "evidence": [{"turn_id": source.turn_id,
            "quote": source.user_text}], "event_time": None, "state": "unknown",
            "observed_at": source.chat_time.isoformat()}]}

    async def close(self):
        self.closed = True


def make_turn(text="我给妈妈挑了一条项链", *, identity="member"):
    return ChatService().create_turn(session_id="session", device_id="device",
        speaker_identity_id=identity, speaker_display_name="Member", user_text=text)


def make_service(*, store=None, jobs=None, curator=None, clock=None):
    store = store or InMemoryMemoryBatchStore(**({"clock": clock} if clock else {}))
    jobs = jobs or InMemoryMemoryJobStore()
    curator = curator or Curator()
    writer = MemoryWriteService(jobs, RecordingMemoryProvider())
    values = {"clock": clock} if clock else {}
    return MemoryBatchService(store, curator, writer, **values), store, jobs, curator


def test_batch_waits_for_segment_end_and_does_not_submit_each_turn():
    async def scenario():
        service, _store, jobs, curator = make_service()
        turn = make_turn()
        assert await service.record_input(turn, segment_id="segment")
        await service.finish_input(turn, assistant_text="听起来不错。")
        assert not await service._tick()
        assert not curator.calls and not jobs.jobs
        assert await service.flush_segment(device_id="device", segment_id="segment", reason="disconnect") == 1
        assert await service._tick()
        assert len(curator.calls) == 1 and len(jobs.jobs) == 1
        job = next(iter(jobs.jobs.values()))
        assert job.user_text == job.assistant_text == ""
        assert job.prepared_payload["info"]["batch_id"] == job.turn_id
        assert job.prepared_payload["info"]["source_turn_ids"] == [turn.turn_id]
        assert job.prepared_payload["messages"][0]["role"] != "user"
        assert not await service._tick()
        assert await service.flush_segment(device_id="device", segment_id="segment", reason="disconnect") == 0
        await service.close()

    asyncio.run(scenario())


def test_time_and_count_seal_without_disconnect_and_guest_is_not_collected():
    async def scenario():
        now = datetime.now(UTC)
        current = [now]
        service, _store, _jobs, curator = make_service(clock=lambda: current[0])
        assert not await service.record_input(make_turn(identity=None), segment_id="segment")
        for index in range(8):
            turn = make_turn(f"第{index}条个人经历")
            await service.record_input(turn, segment_id="segment")
            await service.finish_input(turn)
        assert await service._tick()
        assert len(curator.calls) == 1
        turn = make_turn("我还想练习口语")
        await service.record_input(turn, segment_id="later")
        await service.finish_input(turn)
        assert not await service._tick()
        current[0] += timedelta(minutes=6)
        assert await service._tick()
        assert len(curator.calls) == 2
        await service.close()

    asyncio.run(scenario())


def test_retry_reuses_saved_curation_after_memory_enqueue_failure():
    async def scenario():
        now = [datetime.now(UTC)]
        service, _store, jobs, curator = make_service(clock=lambda: now[0])
        original = service.writer.enqueue_prepared_batch
        calls = []

        async def failing_once(**values):
            calls.append(values)
            if len(calls) == 1:
                raise RuntimeError("temporary local storage failure")
            return await original(**values)

        service.writer.enqueue_prepared_batch = failing_once
        turn = make_turn()
        await service.record_input(turn, segment_id="segment")
        await service.finish_input(turn)
        await service.flush_segment(device_id="device", segment_id="segment", reason="new_dialogue")
        assert await service._tick()
        now[0] += timedelta(minutes=1)
        assert await service._tick()
        assert len(curator.calls) == 1
        assert len(jobs.jobs) == 1
        await service.close()

    asyncio.run(scenario())


def test_empty_curation_creates_no_memory_job():
    async def scenario():
        class Empty(Curator):
            async def curate(self, batch):
                return {"memories": []}
        service, _store, jobs, _curator = make_service(curator=Empty())
        turn = make_turn("今天随便聊聊")
        await service.record_input(turn, segment_id="segment")
        await service.finish_input(turn)
        await service.flush_segment(device_id="device", segment_id="segment", reason="disconnect")
        assert await service._tick()
        assert not jobs.jobs
        assert not await service._tick()
        await service.close()

    asyncio.run(scenario())


def test_cancelled_curation_never_submits_and_can_be_recovered():
    async def scenario():
        curator = Curator()
        curator.release.clear()
        service, _store, jobs, _ = make_service(curator=curator)
        turn = make_turn()
        await service.record_input(turn, segment_id="segment")
        await service.finish_input(turn)
        await service.flush_segment(device_id="device", segment_id="segment", reason="disconnect")
        task = asyncio.create_task(service._tick())
        await asyncio.wait_for(curator.started.wait(), 1)
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        assert not jobs.jobs
        await service.close()

    asyncio.run(scenario())


def test_prepared_payload_keeps_only_referenced_originals_with_their_original_time():
    now = datetime(2026, 10, 27, 6, tzinfo=UTC)
    sources = tuple(MemorySource(str(i), "device", "member", "session", "segment",
        "Member", text, now + timedelta(minutes=i), None, True)
        for i, text in enumerate(("妈妈生日我准备补礼物", "我买了一条项链", "今天聊得开心")))
    batch = MemoryBatch("batch", "device", "member", "session", "segment", "Member", sources)
    result = {"memories": [{"kind": "event", "content": "用户给妈妈买项链作生日礼物",
        "source_turn_ids": ["0", "1"], "evidence": [
            {"turn_id": "0", "quote": sources[0].user_text},
            {"turn_id": "1", "quote": sources[1].user_text}],
        "event_time": None, "state": "completed"}]}
    payload = _prepared_payload(batch, result)
    assert payload["allow_memory_view"] == ["event"]
    messages = payload["messages"]
    assert messages[0]["role"] == "system"
    assert [(m["role"], m["content"], m["chat_time"]) for m in messages[1:]] == [
        ("user", sources[0].user_text, "2026-10-27 14:00:00"),
        ("user", sources[1].user_text, "2026-10-27 14:01:00"),
    ]


def test_deleting_member_during_curation_cannot_enqueue_late_result():
    async def scenario():
        curator = Curator()
        curator.release.clear()
        service, store, jobs, _ = make_service(curator=curator)
        turn = make_turn()
        await service.record_input(turn, segment_id="segment")
        await service.finish_input(turn)
        await service.flush_segment(device_id="device", segment_id="segment", reason="disconnect")
        task = asyncio.create_task(service._tick())
        await asyncio.wait_for(curator.started.wait(), 1)
        await service.delete_identity(device_id="device", identity_id="member")
        curator.release.set()
        await asyncio.wait_for(task, 1)
        assert not jobs.jobs
        assert not store.sources and not store.batches
        await service.close()

    asyncio.run(scenario())
