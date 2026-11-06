import asyncio
from dataclasses import replace
from datetime import datetime, timezone
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
                chat_time=datetime(2026, 10, 25, 15, 59, tzinfo=timezone.utc),
            )

            assert await store.enqueue(job)
            assert not await store.enqueue(replace(job, job_id=str(uuid4())))
            claimed = await store.claim()
            assert claimed is not None
            assert claimed.turn_id == job.turn_id
            assert claimed.attempts == 1
            assert claimed.chat_time == job.chat_time
            await store.submit(
                claimed.job_id,
                "provider-task",
                check_at=datetime.now(timezone.utc),
            )
            reclaimed = await store.claim()
            assert reclaimed is not None
            assert reclaimed.job_id == claimed.job_id
            assert reclaimed.attempts == 1
            assert reclaimed.provider_task_id == "provider-task"
            assert reclaimed.poll_count == 1
            await store.submit(
                job.job_id, "provider-task", check_at=datetime.now(timezone.utc)
            )
            await store.close()
            store = SqlAlchemyMemoryJobStore(DATABASE_URL, lease_seconds=0)
            await store.start()
            reclaimed = await store.claim()
            assert reclaimed is not None
            assert reclaimed.poll_count == 2
            assert reclaimed.attempts == 1
            assert reclaimed.chat_time == job.chat_time
            await store.submit(
                job.job_id, "new-provider-task", check_at=datetime.now(timezone.utc)
            )
            reclaimed = await store.claim()
            assert reclaimed is not None
            assert reclaimed.poll_count == 1
            await store.fail(
                job.job_id, error="retry submission", retry_at=datetime.now(timezone.utc)
            )
            reclaimed = await store.claim()
            assert reclaimed is not None
            assert reclaimed.poll_count == 0
            assert reclaimed.provider_task_id is None
            assert reclaimed.attempts == 2
            assert reclaimed.chat_time == job.chat_time
            await store.complete(reclaimed.job_id, None)
            assert await store.claim() is None
            # 旧任务可无发言时间，不能在领取或重启时用当前时间补造。
            legacy = replace(job, job_id=str(uuid4()), turn_id=str(uuid4()), chat_time=None)
            assert await store.enqueue(legacy)
            reclaimed = await store.claim()
            assert reclaimed.chat_time is None
            await store.complete(reclaimed.job_id, None)
            # 整理结果必须随任务持久化，重启/重试不能回退成空的单轮问答。
            payload = {"messages": [{"role": "system", "content": "curated"}],
                       "info": {"source_turn_ids": [job.turn_id]}}
            prepared = replace(job, job_id=str(uuid4()), turn_id=str(uuid4()),
                               user_text="", assistant_text="", prepared_payload=payload)
            assert await store.enqueue(prepared)
            await store.close()
            store = SqlAlchemyMemoryJobStore(DATABASE_URL, lease_seconds=0)
            await store.start()
            reclaimed = await store.claim()
            assert reclaimed.prepared_payload == payload
            assert reclaimed.user_text == reclaimed.assistant_text == ""
            await store.complete(reclaimed.job_id, None)
        finally:
            await store.close()
            await identities.close()

    asyncio.run(scenario())
