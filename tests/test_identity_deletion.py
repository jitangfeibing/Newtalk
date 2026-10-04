import asyncio

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
            max_attempts=3,
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
