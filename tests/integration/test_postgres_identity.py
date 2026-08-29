import asyncio
import os

import pytest

from newtalk.identity import (
    DeviceAuthenticationError,
    IdentityService,
    SqlAlchemyIdentityStore,
)


DATABASE_URL = os.getenv("NEWTALK_TEST_DATABASE_URL")


@pytest.mark.integration
@pytest.mark.skipif(not DATABASE_URL, reason="PostgreSQL integration URL is not configured")
def test_postgres_device_recovery_and_member_isolation() -> None:
    async def scenario() -> None:
        service = IdentityService(SqlAlchemyIdentityStore(DATABASE_URL))
        await service.start()
        try:
            family_a = await service.register_device()
            family_b = await service.register_device()
            member = await service.create_identity(
                device_id=family_a.device.device_id,
                display_name="Postgres Member",
            )

            assert [
                identity.identity_id
                for identity in await service.list_identities(
                    family_a.device.device_id
                )
            ] == [member.identity_id]
            assert await service.list_identities(family_b.device.device_id) == []

            recovered = await service.recover_device(family_a.recovery_code)
            assert recovered.device.device_id == family_a.device.device_id
            with pytest.raises(DeviceAuthenticationError):
                await service.authenticate_device(family_a.credential)
            assert (
                await service.authenticate_device(recovered.credential)
            ).device_id == family_a.device.device_id
        finally:
            await service.close()

    asyncio.run(scenario())
