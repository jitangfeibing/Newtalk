import asyncio
from dataclasses import replace
import os
from uuid import uuid4

import pytest

from newtalk.identity import IdentityService, SqlAlchemyIdentityStore
from newtalk.memory import MemoryWriteJob, SqlAlchemyMemoryJobStore


DATABASE_URL = os.getenv("NEWTALK_TEST_DATABASE_URL")


@pytest.mark.integration
@pytest.mark.skipif(not DATABASE_URL, reason="PostgreSQL integration URL is not configured")
def test_postgres_memory_job_claim_is_idempotent() -> None:
    async def scenario() -> None:
        identities = IdentityService(SqlAlchemyIdentityStore(DATABASE_URL))
        store = SqlAlchemyMemoryJobStore(DATABASE_URL, lease_seconds=0)
        await identities.start()
        await store.start()
        try:
            family = await identities.register_device()
            member = await identities.create_identity(
                device_id=family.device.device_id,
                display_name="Memory Member",
            )
            job = MemoryWriteJob(
                job_id=str(uuid4()),
                turn_id=str(uuid4()),
                device_id=family.device.device_id,
                identity_id=member.identity_id,
                session_id=str(uuid4()),
                speaker_name=member.display_name,
                user_text="需要持久化",
                assistant_text="已经完成",
            )

            assert await store.enqueue(job)
            assert not await store.enqueue(replace(job, job_id=str(uuid4())))
            claimed = await store.claim()
            assert claimed is not None
            assert claimed.turn_id == job.turn_id
            assert claimed.attempts == 1
            reclaimed = await store.claim()
            assert reclaimed is not None
            assert reclaimed.job_id == claimed.job_id
            assert reclaimed.attempts == 2
            await store.complete(reclaimed.job_id, "provider-task")
            assert await store.claim() is None
        finally:
            await store.close()
            await identities.close()

    asyncio.run(scenario())
