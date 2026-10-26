import asyncio
from contextlib import asynccontextmanager
import os

import pytest
from sqlalchemy import select

from newtalk.chat import ChatService
from newtalk.identity import IdentityService, InMemoryIdentityStore, SqlAlchemyIdentityStore
from newtalk.identity.deletion import (
    IdentityDeletionService,
    InMemoryIdentityDeletionStore,
    SqlAlchemyIdentityDeletionStore,
)
from newtalk.memory import (
    InMemoryMemoryJobStore,
    MemoryWriteReceipt,
    MemoryWriteService,
    SqlAlchemyMemoryJobStore,
)
from newtalk.memory.jobs import MemoryJobRow
from newtalk.profile import ProfileCacheCoordinator
from newtalk.voiceprint import DisabledVoicePrintClient


DATABASE_URL = os.getenv("NEWTALK_TEST_DATABASE_URL")
BACKENDS = [
    "memory",
    pytest.param(
        "postgres", marks=[pytest.mark.integration, pytest.mark.skipif(
            not DATABASE_URL, reason="PostgreSQL integration URL is not configured"
        )],
    ),
]


class GatedMemoryProvider:
    enabled = True

    def __init__(self) -> None:
        self.submitting = asyncio.Event()
        self.release = asyncio.Event()
        self.release.set()
        self.status = "running"
        self.operations: list[str] = []

    async def add_completed_turn(self, scope, **kwargs):
        self.operations.append("add")
        self.submitting.set()
        await self.release.wait()
        return MemoryWriteReceipt("remote-task", "running")

    async def get_write_status(self, provider_task_id):
        return MemoryWriteReceipt(provider_task_id, self.status)

    async def delete_all_memories(self, scope):
        self.operations.append("delete")

    async def delete_profile(self, scope):
        self.operations.append("delete_profile")


class DeletionScenario:
    async def start(self, backend: str, *, max_polls: int) -> None:
        self.backend = backend
        self.identity_store = (
            InMemoryIdentityStore() if backend == "memory" else SqlAlchemyIdentityStore(DATABASE_URL)
        )
        self.identities = IdentityService(self.identity_store)
        await self.identities.start()
        device = await self.identities.register_device()
        self.member = await self.identities.create_identity(
            device_id=device.device.device_id, display_name="Deletion Member"
        )
        self.jobs = (
            InMemoryMemoryJobStore()
            if backend == "memory" else SqlAlchemyMemoryJobStore(DATABASE_URL, lease_seconds=0)
        )
        self.deletions = (
            InMemoryIdentityDeletionStore(self.identities, memory_jobs=self.jobs)
            if backend == "memory" else SqlAlchemyIdentityDeletionStore(DATABASE_URL, lease_seconds=0)
        )
        self.provider = GatedMemoryProvider()
        self.writer = MemoryWriteService(
            self.jobs, self.provider, poll_seconds=0,
            max_polls=max_polls, backoff=lambda attempt: 0,
        )
        self.deleter = IdentityDeletionService(
            self.deletions, DisabledVoicePrintClient(), self.provider,
            ProfileCacheCoordinator(), backoff=lambda attempt: 0,
        )
        await self.jobs.start()
        await self.deletions.start()

    async def enqueue(self) -> bool:
        turn = ChatService().create_turn(
            session_id="deletion-session", user_text="remember this",
            device_id=self.member.device_id, speaker_identity_id=self.member.identity_id,
        )
        return await self.writer.enqueue_completed_turn(turn, "completed reply")

    async def request_delete(self) -> bool:
        return await self.deleter.request(
            device_id=self.member.device_id, identity_id=self.member.identity_id
        )

    async def process_memory(self):
        job = await self.jobs.claim()
        assert job is not None
        await self.writer._process(job)
        return job

    async def process_deletion(self):
        job = await self.deletions.claim()
        assert job is not None
        await self.deleter._process(job)
        return job

    async def job_state(self, job_id):
        if self.backend == "memory":
            return self.jobs.statuses[job_id], self.jobs.errors.get(job_id)
        async with self.jobs._sessions() as session:
            row = (await session.execute(
                select(MemoryJobRow.status, MemoryJobRow.last_error)
                .where(MemoryJobRow.job_id == job_id)
            )).one()
            return tuple(row)

    async def close(self) -> None:
        await self.identity_store.delete_identity(
            device_id=self.member.device_id, identity_id=self.member.identity_id
        )
        await self.writer.close()
        await self.deleter.close()
        await self.identities.close()


@asynccontextmanager
async def deletion_scenario(backend: str, *, max_polls: int = 600):
    scenario = DeletionScenario()
    await scenario.start(backend, max_polls=max_polls)
    try:
        yield scenario
    finally:
        await scenario.close()


@pytest.mark.parametrize("backend", BACKENDS)
def test_deletion_cancels_pending_and_claimed_writes(backend) -> None:
    async def run():
        async with deletion_scenario(backend) as scenario:
            assert await scenario.enqueue()
            claimed = await scenario.jobs.claim()
            assert claimed is not None
            assert await scenario.enqueue()
            assert await scenario.request_delete()
            assert await scenario.job_state(claimed.job_id) == ("failed", "identity_deleted")
            assert not await scenario.enqueue()
            assert await scenario.jobs.claim() is None
            await scenario.writer._process(claimed)
            await scenario.process_deletion()
            assert scenario.provider.operations == ["delete", "delete_profile"]
            assert await scenario.identity_store.get_identity(
                device_id=scenario.member.device_id, identity_id=scenario.member.identity_id
            ) is None

    asyncio.run(run())


@pytest.mark.parametrize("backend", BACKENDS)
@pytest.mark.parametrize("remote_status", ["completed", "failed"])
def test_deletion_waits_for_submitted_write_to_finish(backend, remote_status) -> None:
    async def run():
        async with deletion_scenario(backend) as scenario:
            assert await scenario.enqueue()
            await scenario.process_memory()
            assert await scenario.request_delete()
            await scenario.process_deletion()
            assert scenario.provider.operations == ["add"]
            await scenario.process_memory()
            await scenario.process_deletion()
            assert scenario.provider.operations == ["add"]
            scenario.provider.status = remote_status
            await scenario.process_memory()
            await scenario.process_deletion()
            assert scenario.provider.operations == ["add", "delete", "delete_profile"]
            assert await scenario.deletions.claim() is None

    asyncio.run(run())


@pytest.mark.parametrize("backend", BACKENDS)
def test_deletion_waits_for_inflight_submission_receipt(backend) -> None:
    async def run():
        async with deletion_scenario(backend) as scenario:
            assert await scenario.enqueue()
            claimed = await scenario.jobs.claim()
            assert claimed is not None
            scenario.provider.release.clear()
            writing = asyncio.create_task(scenario.writer._process(claimed))
            deleting = None
            requested = asyncio.Event()

            async def request_delete():
                requested.set()
                return await scenario.request_delete()

            try:
                await asyncio.wait_for(scenario.provider.submitting.wait(), timeout=1)
                deleting = asyncio.create_task(request_delete())
                await asyncio.wait_for(requested.wait(), timeout=1)
                assert not deleting.done()
                scenario.provider.release.set()
                await asyncio.wait_for(writing, timeout=2)
                assert await asyncio.wait_for(deleting, timeout=2)
                await scenario.process_deletion()
                assert scenario.provider.operations == ["add"]
                scenario.provider.status = "completed"
                await scenario.process_memory()
                await scenario.process_deletion()
                assert scenario.provider.operations == ["add", "delete", "delete_profile"]
            finally:
                scenario.provider.release.set()
                for task in (writing, deleting):
                    if task is not None:
                        task.cancel()
                await asyncio.gather(
                    *(task for task in (writing, deleting) if task is not None),
                    return_exceptions=True,
                )

    asyncio.run(run())


@pytest.mark.parametrize("backend", BACKENDS)
def test_deleting_member_keeps_remote_task_after_poll_budget(backend) -> None:
    async def run():
        async with deletion_scenario(backend, max_polls=1) as scenario:
            assert await scenario.enqueue()
            await scenario.process_memory()
            assert await scenario.request_delete()
            for _ in range(3):
                await scenario.process_memory()
            await scenario.process_deletion()
            assert scenario.provider.operations == ["add"]
            scenario.provider.status = "completed"
            await scenario.process_memory()
            await scenario.process_deletion()
            assert scenario.provider.operations == ["add", "delete", "delete_profile"]

    asyncio.run(run())


@pytest.mark.parametrize("backend", BACKENDS)
def test_deletion_does_not_cancel_already_claimed_remote_poll(backend) -> None:
    async def run():
        async with deletion_scenario(backend) as scenario:
            assert await scenario.enqueue()
            await scenario.process_memory()
            polling = await scenario.jobs.claim()
            assert polling is not None
            assert await scenario.request_delete()
            assert (await scenario.job_state(polling.job_id))[0] == "submitted"
            scenario.provider.status = "completed"
            await scenario.writer._process(polling)
            await scenario.process_deletion()
            assert scenario.provider.operations == ["add", "delete", "delete_profile"]

    asyncio.run(run())


@pytest.mark.parametrize("backend", BACKENDS)
def test_identity_is_rechecked_before_claim_and_submission(backend) -> None:
    async def run():
        async with deletion_scenario(backend) as scenario:
            assert await scenario.enqueue()
            claimed = await scenario.jobs.claim()
            assert claimed is not None
            assert await scenario.enqueue()
            await scenario.identities.mark_identity_deletion_pending(
                device_id=scenario.member.device_id, identity_id=scenario.member.identity_id
            )
            assert await scenario.jobs.claim() is None
            await scenario.writer._process(claimed)
            assert scenario.provider.operations == []

    asyncio.run(run())


@pytest.mark.parametrize("backend", BACKENDS)
def test_deletion_does_not_cancel_or_wait_for_another_members_jobs(backend) -> None:
    async def run():
        async with deletion_scenario(backend) as scenario:
            device = await scenario.identities.register_device()
            other = await scenario.identities.create_identity(
                device_id=device.device.device_id, display_name="Other Member"
            )
            try:
                turn = ChatService().create_turn(
                    session_id="other-session", user_text="other memory",
                    device_id=other.device_id, speaker_identity_id=other.identity_id,
                )
                assert await scenario.writer.enqueue_completed_turn(turn, "other reply")
                assert await scenario.enqueue()
                assert await scenario.request_delete()
                claimed = await scenario.jobs.claim()
                assert claimed is not None
                assert claimed.identity_id == other.identity_id
                await scenario.writer._process(claimed)
                await scenario.process_deletion()
                assert (await scenario.job_state(claimed.job_id))[0] == "submitted"
                assert scenario.provider.operations == ["add", "delete", "delete_profile"]
                assert await scenario.identities.get_identity(
                    device_id=other.device_id, identity_id=other.identity_id
                ) == other
            finally:
                await scenario.identity_store.delete_identity(
                    device_id=other.device_id, identity_id=other.identity_id
                )

    asyncio.run(run())
