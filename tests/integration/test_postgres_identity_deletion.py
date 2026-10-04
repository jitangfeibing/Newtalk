import asyncio
import os

import pytest

from newtalk.identity import IdentityNotFoundError, IdentityService, SqlAlchemyIdentityStore
from newtalk.identity.deletion import SqlAlchemyIdentityDeletionStore


DATABASE_URL = os.getenv("NEWTALK_TEST_DATABASE_URL")


@pytest.mark.integration
@pytest.mark.skipif(not DATABASE_URL, reason="PostgreSQL integration URL is not configured")
def test_postgres_identity_deletion_job_survives_schedule_and_completes() -> None:
    async def scenario() -> None:
        identities = IdentityService(SqlAlchemyIdentityStore(DATABASE_URL))
        jobs = SqlAlchemyIdentityDeletionStore(DATABASE_URL, lease_seconds=0)
        await identities.start()
        await jobs.start()
        try:
            family = await identities.register_device()
            member = await identities.create_identity(
                device_id=family.device.device_id,
                display_name="Deletion Member",
            )

            assert await jobs.schedule(
                device_id=family.device.device_id,
                identity_id=member.identity_id,
            )
            assert await identities.list_identities(family.device.device_id) == []
            with pytest.raises(IdentityNotFoundError):
                await identities.get_identity(
                    device_id=family.device.device_id,
                    identity_id=member.identity_id,
                )

            claimed = await jobs.claim()
            assert claimed is not None
            assert claimed.identity_id == member.identity_id
            assert claimed.attempts == 1
            await jobs.complete(claimed.job_id)

            assert await jobs.claim() is None
        finally:
            await jobs.close()
            await identities.close()

    asyncio.run(scenario())
