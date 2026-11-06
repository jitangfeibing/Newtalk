import asyncio
import os
from contextlib import asynccontextmanager
from dataclasses import FrozenInstanceError, replace
from datetime import UTC, datetime, timedelta, timezone
from uuid import UUID, uuid4

import pytest
from sqlalchemy import delete, func, select

from newtalk.identity import (
    IdentityService,
    InMemoryIdentityStore,
    SqlAlchemyIdentityStore,
)
from newtalk.identity.sqlalchemy_store import DeviceRow
from newtalk.memory.batch_store import (
    InMemoryMemoryBatchStore,
    MemoryBatchRow,
    MemorySource,
    MemorySourceRow,
    SqlAlchemyMemoryBatchStore,
)

DATABASE_URL = os.getenv("NEWTALK_TEST_DATABASE_URL")
BACKENDS = [
    "memory",
    pytest.param(
        "postgres",
        marks=[
            pytest.mark.integration,
            pytest.mark.skipif(
                not DATABASE_URL, reason="PostgreSQL integration URL is not configured"
            ),
        ],
    ),
]


class Clock:
    def __init__(self):
        self.now = datetime(2026, 10, 27, tzinfo=UTC)

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += timedelta(seconds=seconds)


class Scenario:
    def source(self, **changes):
        return replace(
            MemorySource(
                turn_id=str(uuid4()),
                device_id=self.device_id,
                identity_id=self.member.identity_id,
                session_id=self.session_id,
                segment_id="segment-a",
                speaker_name="Member",
                user_text="user input",
                chat_time=self.clock(),
                settled=True,
            ),
            **changes,
        )

    async def seal(self, segment_id="segment-a"):
        return await self.store.seal(
            device_id=self.device_id, segment_id=segment_id, reason="boundary"
        )

    async def restart(self):
        await self.store.close()
        if self.backend == "postgres":
            self.store = SqlAlchemyMemoryBatchStore(DATABASE_URL, clock=self.clock)
        await self.store.start()


@asynccontextmanager
async def scenario(backend):
    h = Scenario()
    h.backend = backend
    h.clock = Clock()
    h.session_id = str(uuid4())
    h.identity_store = (
        InMemoryIdentityStore()
        if backend == "memory"
        else SqlAlchemyIdentityStore(DATABASE_URL)
    )
    h.identities = IdentityService(h.identity_store)
    await h.identities.start()
    device = await h.identities.register_device()
    h.device_id = device.device.device_id
    h.member = await h.identities.create_identity(
        device_id=h.device_id, display_name="Member"
    )
    h.store = (
        InMemoryMemoryBatchStore(h.identities, clock=h.clock)
        if backend == "memory"
        else SqlAlchemyMemoryBatchStore(DATABASE_URL, clock=h.clock)
    )
    await h.store.start()
    try:
        yield h
    finally:
        await h.store.close()
        if backend == "postgres":
            async with h.identity_store._sessions.begin() as session:
                await session.execute(
                    delete(DeviceRow).where(DeviceRow.device_id == h.device_id)
                )
        await h.identities.close()


@pytest.mark.parametrize("backend", BACKENDS)
def test_batch_isolation_idempotency_and_active_members(backend):
    async def run():
        async with scenario(backend) as h:
            source = h.source()
            assert await h.store.append(source)
            assert not await h.store.append(replace(source, user_text="duplicate"))
            assert not await h.store.append(h.source(device_id="00:00:00:00:00:00"))
            other = await h.identities.create_identity(
                device_id=h.device_id, display_name="Other"
            )
            for item in (
                h.source(identity_id=other.identity_id),
                h.source(segment_id="segment-b"),
                h.source(session_id=str(uuid4())),
            ):
                assert await h.store.append(item)
            assert await h.seal() == 3
            batches = []
            while (batch := await h.store.claim()) is not None:
                batches.append(batch)
                assert (
                    len(
                        {
                            (s.device_id, s.identity_id, s.session_id, s.segment_id)
                            for s in batch.sources
                        }
                    )
                    == 1
                )
                assert await h.store.mark_queued(batch)
            assert len(batches) == 3
            assert all(b.segment_id == "segment-a" for b in batches)
            assert await h.seal("segment-b") == 1
            await h.identities.mark_identity_deletion_pending(
                device_id=h.device_id, identity_id=h.member.identity_id
            )
            assert not await h.store.append(h.source())
            assert await h.store.claim() is None

    asyncio.run(run())


@pytest.mark.parametrize("backend", BACKENDS)
def test_due_threshold_timeout_and_unsettled_group(backend):
    async def run():
        async with scenario(backend) as h:
            pending = h.source(settled=False)
            assert await h.store.append(pending)
            for _ in range(7):
                assert await h.store.append(h.source())
            assert await h.store.seal_due() == 0
            h.clock.advance(301)
            assert await h.store.seal_due() == 0
            await h.store.finish(pending.turn_id, "answer")
            assert await h.store.seal_due() == 1
            batch = await h.store.claim()
            assert len(batch.sources) == 8
            assert batch.sources[0].assistant_text == "answer"
            assert await h.store.mark_queued(batch)
            assert await h.store.append(h.source())
            assert await h.store.seal_due() == 0
            h.clock.advance(299)
            assert await h.store.seal_due() == 0
            h.clock.advance(1)
            assert await h.store.seal_due() == 1
            assert await h.store.append(h.source(segment_id="short"))
            assert await h.store.seal_due(max_sources=1) == 1

    asyncio.run(run())


@pytest.mark.parametrize("backend", BACKENDS)
def test_context_snapshot_and_new_source_limit(backend):
    async def run():
        async with scenario(backend) as h:
            sources = [h.source(user_text=str(i)) for i in range(19)]
            for item in sources:
                assert await h.store.append(item)
            assert await h.seal() == 3
            batches = []
            for _ in range(3):
                batch = await h.store.claim()
                batches.append(batch)
                assert await h.store.mark_queued(batch)
            assert [
                [s.turn_id for s in b.sources if not s.context_only] for b in batches
            ] == [
                [s.turn_id for s in sources[:8]],
                [s.turn_id for s in sources[8:16]],
                [s.turn_id for s in sources[16:]],
            ]
            assert [s.turn_id for s in batches[1].sources if s.context_only] == [
                s.turn_id for s in sources[6:8]
            ]
            assert [s.turn_id for s in batches[2].sources if s.context_only] == [
                s.turn_id for s in sources[14:16]
            ]
            await h.store.finish(sources[0].turn_id, "too late", discard=True)
            assert await h.seal() == 0
            assert batches[0].sources[0].user_text == "0"
            assert batches[0].sources[0].assistant_text is None
            with pytest.raises(FrozenInstanceError):
                batches[0].sources[0].user_text = "mutated"
            assert await h.store.append(h.source(segment_id="other"))
            assert await h.seal("other") == 1
            assert not any(s.context_only for s in (await h.store.claim()).sources)

    asyncio.run(run())


@pytest.mark.parametrize("backend", BACKENDS)
def test_restart_recovers_user_only_and_preserves_utc(backend):
    async def run():
        async with scenario(backend) as h:
            original = h.source(
                settled=False,
                assistant_text="uncommitted partial answer",
                chat_time=h.clock().astimezone(timezone(timedelta(hours=8))),
            )
            discarded = h.source(settled=False)
            assert await h.store.append(original)
            assert await h.store.append(discarded)
            await h.store.finish(discarded.turn_id, None, discard=True)
            await h.restart()
            assert await h.store.seal_due() == 0
            h.clock.advance(300)
            assert await h.store.seal_due() == 1
            batch = await h.store.claim()
            assert len(batch.sources) == 1
            item = batch.sources[0]
            assert item.turn_id == original.turn_id and item.settled
            assert item.assistant_text is None
            assert item.chat_time == original.chat_time
            assert item.chat_time.utcoffset() == timedelta(0)
            if backend == "postgres":
                async with h.store._sessions() as session:
                    row = await session.get(MemoryBatchRow, UUID(batch.batch_id))
                    assert row.sources[0]["chat_time"].endswith("+00:00")

    asyncio.run(run())


@pytest.mark.parametrize("backend", BACKENDS)
def test_lease_fencing_prepared_copy_and_retry_reuse(backend):
    async def run():
        async with scenario(backend) as h:
            assert await h.store.append(h.source())
            await h.seal()
            first = await h.store.claim()
            assert first.attempts == 1 and first.lease_token
            payload = {"messages": [{"text": "prepared"}]}
            assert await h.store.save_prepared(first, payload)
            payload["messages"][0]["text"] = "mutated"
            assert await h.store.claim() is None
            await h.restart()
            assert await h.store.claim() is None
            h.clock.advance(120)
            second = await h.store.claim()
            assert second.batch_id == first.batch_id
            assert second.lease_token != first.lease_token and second.attempts == 2
            assert second.prepared_payload == {"messages": [{"text": "prepared"}]}
            assert not await h.store.save_prepared(first, {"stale": True})
            assert not await h.store.mark_queued(first)
            assert not await h.store.skip(first)
            await h.store.fail(first, error="late", retry_at=None)
            second.prepared_payload["messages"][0]["text"] = "another mutation"
            retry = h.clock() + timedelta(seconds=60)
            await h.store.fail(second, error="temporary", retry_at=retry)
            assert await h.store.claim() is None
            h.clock.advance(60)
            third = await h.store.claim()
            assert third.attempts == 3
            assert third.prepared_payload == {"messages": [{"text": "prepared"}]}
            assert await h.store.mark_queued(third)
            assert not await h.store.mark_queued(third)
            assert await h.store.claim() is None

    asyncio.run(run())


@pytest.mark.parametrize("backend", BACKENDS)
def test_deletion_fences_preparation_queue_and_cleans_rows(backend):
    async def run():
        async with scenario(backend) as h:
            assert await h.store.append(h.source())
            await h.seal()
            claimed = await h.store.claim()
            assert await h.store.append(h.source(segment_id="pending"))
            await h.seal("pending")
            await h.identities.mark_identity_deletion_pending(
                device_id=h.device_id, identity_id=h.member.identity_id
            )
            assert not await h.store.save_prepared(claimed, {"deleted": True})
            assert not await h.store.mark_queued(claimed)
            assert await h.store.claim() is None
            await h.identities.delete_identity(
                device_id=h.device_id, identity_id=h.member.identity_id
            )
            if backend == "memory":
                await h.store.delete_identity(
                    device_id=h.device_id, identity_id=h.member.identity_id
                )
                assert not h.store.sources and not h.store.batches
            else:
                async with h.store._sessions() as session:
                    for model in (MemorySourceRow, MemoryBatchRow):
                        assert (
                            await session.scalar(
                                select(func.count())
                                .select_from(model)
                                .where(model.device_id == h.device_id)
                            )
                            == 0
                        )
            assert not await h.store.save_prepared(claimed, {})
            assert not await h.store.mark_queued(claimed)

    asyncio.run(run())


@pytest.mark.parametrize("backend", BACKENDS)
def test_concurrent_appends_and_seals_assign_each_turn_once(backend):
    async def run():
        async with scenario(backend) as h:
            sources = [h.source() for _ in range(17)]
            gate = asyncio.Event()

            async def append(item):
                await gate.wait()
                return await h.store.append(item)

            tasks = [
                asyncio.create_task(append(item)) for item in sources for _ in range(2)
            ]
            gate.set()
            assert sum(await asyncio.gather(*tasks)) == 17
            assert sum(await asyncio.gather(h.seal(), h.seal())) == 3
            claims = await asyncio.gather(*(h.store.claim() for _ in range(4)))
            batches = [batch for batch in claims if batch is not None]
            assert len(batches) == 3
            assert {
                s.turn_id for b in batches for s in b.sources if not s.context_only
            } == {s.turn_id for s in sources}
            assert sum(not s.context_only for b in batches for s in b.sources) == 17

    asyncio.run(run())


@pytest.mark.parametrize("backend", BACKENDS)
def test_skip_and_permanent_failure_are_terminal(backend):
    async def run():
        async with scenario(backend) as h:
            for segment in ("a", "b"):
                assert await h.store.append(h.source(segment_id=segment))
                assert await h.seal(segment) == 1
            first = await h.store.claim()
            assert await h.store.skip(first)
            second = await h.store.claim()
            await h.store.fail(second, error="permanent", retry_at=None)
            h.clock.advance(1000)
            assert await h.store.claim() is None
            assert not await h.store.save_prepared(second, {})

    asyncio.run(run())


@pytest.mark.parametrize("backend", BACKENDS)
def test_explicit_seal_freezes_only_settled_sources_and_finish_cannot_rewrite(backend):
    async def run():
        async with scenario(backend) as h:
            first, waiting = h.source(settled=False), h.source(settled=False)
            assert await h.store.append(first)
            assert await h.store.append(waiting)
            await h.store.finish(first.turn_id, "original answer")
            assert await h.seal() == 1
            batch = await h.store.claim()
            assert len(batch.sources) == 1
            await h.store.finish(first.turn_id, "late answer")
            await h.store.finish(first.turn_id, None, discard=True)
            await h.store.finish(waiting.turn_id, None, discard=True)
            assert await h.seal() == 0
            await h.store.fail(batch, error="retry snapshot", retry_at=h.clock())
            reloaded = await h.store.claim()
            assert reloaded.sources == batch.sources
            assert reloaded.sources[0].assistant_text == "original answer"
            assert not await h.store.append(first)
            if backend == "postgres":
                async with h.store._sessions() as session:
                    original = await session.scalar(
                        select(MemorySourceRow).where(
                            MemorySourceRow.turn_id == first.turn_id
                        )
                    )
                    assert str(original.batch_id) == batch.batch_id
                    assert original.assistant_text == "original answer"

    asyncio.run(run())


@pytest.mark.parametrize("backend", BACKENDS)
def test_context_after_restart_keeps_original_batch_and_member_scope(backend):
    async def run():
        async with scenario(backend) as h:
            old = [h.source() for _ in range(3)]
            for item in old:
                assert await h.store.append(item)
            assert await h.seal() == 1
            first = await h.store.claim()
            assert await h.store.mark_queued(first)
            await h.restart()
            new = h.source()
            other = await h.identities.create_identity(
                device_id=h.device_id, display_name="Other"
            )
            assert await h.store.append(new)
            assert await h.store.append(h.source(identity_id=other.identity_id))
            assert await h.seal() == 2
            batches = []
            while (batch := await h.store.claim()) is not None:
                batches.append(batch)
                assert await h.store.mark_queued(batch)
            own = next(b for b in batches if b.identity_id == h.member.identity_id)
            foreign = next(b for b in batches if b.identity_id == other.identity_id)
            assert [s.turn_id for s in own.sources] == [
                old[1].turn_id,
                old[2].turn_id,
                new.turn_id,
            ]
            assert [s.context_only for s in own.sources] == [True, True, False]
            assert len(foreign.sources) == 1 and not foreign.sources[0].context_only
            if backend == "postgres":
                async with h.store._sessions() as session:
                    assigned = (
                        await session.scalars(
                            select(MemorySourceRow.batch_id).where(
                                MemorySourceRow.turn_id.in_([s.turn_id for s in old])
                            )
                        )
                    ).all()
                    assert {str(value) for value in assigned} == {first.batch_id}
            else:
                assert {h.store._assignments[s.turn_id] for s in old} == {
                    first.batch_id
                }

    asyncio.run(run())


@pytest.mark.parametrize("backend", BACKENDS)
def test_deletion_during_inflight_work_rejects_queue_of_saved_preparation(backend):
    async def run():
        async with scenario(backend) as h:
            assert await h.store.append(h.source())
            await h.seal()
            batch = await h.store.claim()
            prepared, release = asyncio.Event(), asyncio.Event()

            async def worker():
                assert await h.store.save_prepared(batch, {"messages": ["safe result"]})
                prepared.set()
                await release.wait()
                return await h.store.mark_queued(batch)

            task = asyncio.create_task(worker())
            try:
                await asyncio.wait_for(prepared.wait(), 5)
                await h.identities.mark_identity_deletion_pending(
                    device_id=h.device_id, identity_id=h.member.identity_id
                )
            finally:
                release.set()
            assert not await task
            h.clock.advance(120)
            assert await h.store.claim() is None

    asyncio.run(run())


@pytest.mark.parametrize("backend", BACKENDS)
def test_explicit_identity_cleanup_is_scoped_and_idempotent(backend):
    async def run():
        async with scenario(backend) as h:
            other = await h.identities.create_identity(
                device_id=h.device_id, display_name="Other"
            )
            assert await h.store.append(h.source())
            assert await h.store.append(h.source(identity_id=other.identity_id))
            assert await h.seal() == 2
            assert await h.store.append(h.source(settled=False))
            await h.store.delete_identity(
                device_id="00:00:00:00:00:00", identity_id=h.member.identity_id
            )
            for _ in range(2):
                await h.store.delete_identity(
                    device_id=h.device_id, identity_id=h.member.identity_id
                )
            remaining = await h.store.claim()
            assert remaining.identity_id == other.identity_id
            assert await h.store.mark_queued(remaining)
            assert await h.store.claim() is None
            if backend == "postgres":
                async with h.store._sessions() as session:
                    for model in (MemorySourceRow, MemoryBatchRow):
                        assert (
                            await session.scalar(
                                select(func.count())
                                .select_from(model)
                                .where(model.identity_id == UUID(h.member.identity_id))
                            )
                            == 0
                        )
            else:
                assert all(
                    s.identity_id == other.identity_id for s in h.store.sources.values()
                )

    asyncio.run(run())


def test_sources_reject_naive_time():
    with pytest.raises(ValueError, match="timezone-aware"):
        MemorySource(
            "turn",
            "device",
            "member",
            "session",
            "segment",
            "speaker",
            "text",
            datetime(2026, 1, 1),
        )


@pytest.mark.parametrize("backend", BACKENDS)
def test_display_name_change_does_not_split_member_threshold_or_context(backend):
    async def run():
        async with scenario(backend) as h:
            for index in range(8):
                assert await h.store.append(h.source(speaker_name=f"name-{index}"))
            assert await h.store.seal_due() == 1
            first = await h.store.claim()
            assert len(first.sources) == 8
            assert await h.store.mark_queued(first)
            assert await h.store.append(h.source(speaker_name="renamed"))
            assert await h.seal() == 1
            second = await h.store.claim()
            assert [s.speaker_name for s in second.sources] == [
                "name-6",
                "name-7",
                "renamed",
            ]
            assert [s.context_only for s in second.sources] == [True, True, False]

    asyncio.run(run())
