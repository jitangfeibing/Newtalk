import asyncio
import logging

import pytest

from newtalk.identity import IdentityNotFoundError, IdentityService, InMemoryIdentityStore
from newtalk.identity.deletion import (
    IdentityDeletionService,
    InMemoryIdentityDeletionStore,
)
from newtalk.profile import ProfileCacheCoordinator, ProfileScope


class RecordingVoicePrint:
    enabled = True

    def __init__(self) -> None:
        self.deleted: list[tuple[str, str]] = []

    async def delete(self, *, device_id: str, identity_id: str) -> None:
        self.deleted.append((device_id, identity_id))


class RecordingMemory:
    enabled = True

    def __init__(self, *, fail: bool = False) -> None:
        self.fail = fail
        self.deleted: list[tuple[str, str]] = []

    async def delete_all_memories(self, scope: ProfileScope) -> None:
        if self.fail:
            raise RuntimeError("MemOS unavailable")
        self.deleted.append(("memory", scope.identity_id))

    async def delete_profile(self, scope: ProfileScope) -> None:
        self.deleted.append(("profile", scope.identity_id))


def test_identity_deletion_hides_member_then_cleans_external_data() -> None:
    async def exercise() -> None:
        identity_store = InMemoryIdentityStore()
        identities = IdentityService(identity_store)
        await identities.start()
        registration = await identities.register_device()
        member = await identities.create_identity(
            device_id=registration.device.device_id,
            display_name="小明",
        )
        store = InMemoryIdentityDeletionStore(identities)
        voiceprint = RecordingVoicePrint()
        memory = RecordingMemory()
        deletion = IdentityDeletionService(
            store,
            voiceprint,
            memory,
            ProfileCacheCoordinator(),
        )

        assert await deletion.request(
            device_id=member.device_id,
            identity_id=member.identity_id,
        )
        assert await identities.list_identities(member.device_id) == []
        with pytest.raises(IdentityNotFoundError):
            await identities.get_identity(
                device_id=member.device_id,
                identity_id=member.identity_id,
            )

        job = await store.claim()
        assert job is not None
        await deletion._process(job)

        assert store.statuses[job.job_id] == "completed"
        assert voiceprint.deleted == [(member.device_id, member.identity_id)]
        assert memory.deleted == [
            ("memory", member.identity_id),
            ("profile", member.identity_id),
        ]
        await identities.close()

    asyncio.run(exercise())


def test_identity_deletion_failure_is_retried_without_local_purge() -> None:
    async def exercise() -> None:
        identity_store = InMemoryIdentityStore()
        identities = IdentityService(identity_store)
        await identities.start()
        registration = await identities.register_device()
        member = await identities.create_identity(
            device_id=registration.device.device_id,
            display_name="小明",
        )
        store = InMemoryIdentityDeletionStore(identities)
        deletion = IdentityDeletionService(
            store,
            RecordingVoicePrint(),
            RecordingMemory(fail=True),
            ProfileCacheCoordinator(),
        )
        assert await deletion.request(
            device_id=member.device_id,
            identity_id=member.identity_id,
        )
        job = await store.claim()
        assert job is not None

        await deletion._process(job)

        assert store.statuses[job.job_id] == "pending"
        assert "MemOS unavailable" in store.errors[job.job_id]
        pending = await identity_store.get_identity(
            device_id=member.device_id,
            identity_id=member.identity_id,
        )
        assert pending is not None
        assert pending.status.value == "deletion_pending"
        await identities.close()

    asyncio.run(exercise())


def test_identity_deletion_recovers_after_six_provider_failures(caplog) -> None:
    class FlakyMemory(RecordingMemory):
        def __init__(self) -> None:
            super().__init__()
            self.calls = 0

        async def delete_all_memories(self, scope: ProfileScope) -> None:
            self.calls += 1
            if self.calls <= 6:
                raise RuntimeError("temporary MemOS outage")
            await super().delete_all_memories(scope)

    class CompletionStore(InMemoryIdentityDeletionStore):
        def __init__(self, identities: IdentityService) -> None:
            super().__init__(identities)
            self.completed = asyncio.Event()

        async def complete(self, job_id: str) -> None:
            await super().complete(job_id)
            self.completed.set()

    async def exercise() -> None:
        identity_store = InMemoryIdentityStore()
        identities = IdentityService(identity_store)
        await identities.start()
        registration = await identities.register_device()
        member = await identities.create_identity(
            device_id=registration.device.device_id, display_name="Retry Member"
        )
        store = CompletionStore(identities)
        memory = FlakyMemory()
        delays: list[int] = []

        def backoff(attempt: int) -> float:
            delays.append(attempt)
            return 0

        deletion = IdentityDeletionService(
            store, RecordingVoicePrint(), memory, ProfileCacheCoordinator(),
            backoff=backoff,
        )
        try:
            assert await deletion.request(
                device_id=member.device_id, identity_id=member.identity_id
            )
            await deletion.start()
            await asyncio.wait_for(store.completed.wait(), timeout=1)
            assert memory.calls == 7
            assert delays == [1, 2, 3, 4, 5, 6]
            assert list(store.statuses.values()) == ["completed"]
            assert await identity_store.get_identity(
                device_id=member.device_id, identity_id=member.identity_id
            ) is None
            assert any(
                record.levelno == logging.ERROR and "attempts=6" in record.message
                for record in caplog.records
            )
        finally:
            await deletion.close()
            await identities.close()

    asyncio.run(exercise())


def test_failed_identity_deletion_job_is_recovered_on_store_start() -> None:
    async def exercise() -> None:
        identities = IdentityService(InMemoryIdentityStore())
        await identities.start()
        registration = await identities.register_device()
        member = await identities.create_identity(
            device_id=registration.device.device_id, display_name="Pending Member"
        )
        store = InMemoryIdentityDeletionStore(identities)
        deletion = IdentityDeletionService(
            store, RecordingVoicePrint(), RecordingMemory(), ProfileCacheCoordinator()
        )
        try:
            assert await deletion.request(
                device_id=member.device_id, identity_id=member.identity_id
            )
            claimed = await store.claim()
            assert claimed is not None
            await store.fail(claimed.job_id, error="old retry budget exhausted", retry_at=None)
            await store.start()
            recovered = await store.claim()
            assert recovered is not None
            assert recovered.job_id == claimed.job_id
            await deletion._process(recovered)
            assert store.statuses[claimed.job_id] == "completed"
        finally:
            await store.close()
            await identities.close()

    asyncio.run(exercise())
