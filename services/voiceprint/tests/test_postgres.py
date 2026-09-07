import asyncio
import os
from uuid import uuid4

import pytest
from sqlalchemy.exc import IntegrityError
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from newtalk_voiceprint.embedder import DeterministicEmbedder
from newtalk_voiceprint.service import VoicePrintService
from newtalk_voiceprint.store import VoicePrintStore
from test_service import wav_tone


DATABASE_URL = os.getenv("NEWTALK_TEST_DATABASE_URL")


@pytest.mark.integration
@pytest.mark.skipif(not DATABASE_URL, reason="PostgreSQL integration URL is not configured")
def test_real_postgres_template_lifecycle() -> None:
    async def scenario() -> None:
        identity_id = uuid4()
        device_id = "02:00:00:00:00:42"
        engine = create_async_engine(DATABASE_URL)
        async with engine.begin() as connection:
            await connection.execute(
                text(
                    "INSERT INTO devices (device_id, credential_digest, recovery_digest) "
                    "VALUES (:device_id, :credential, :recovery)"
                ),
                {
                    "device_id": device_id,
                    "credential": uuid4().hex + uuid4().hex,
                    "recovery": uuid4().hex + uuid4().hex,
                },
            )
            await connection.execute(
                text(
                    "INSERT INTO identities (identity_id, device_id, display_name, status) "
                    "VALUES (:identity_id, :device_id, 'Voice Member', 'active')"
                ),
                {"identity_id": identity_id, "device_id": device_id},
            )

        service = VoicePrintService(
            VoicePrintStore(DATABASE_URL),
            DeterministicEmbedder(),
            threshold=0.9,
            min_sample_seconds=2,
            max_sample_seconds=10,
            max_audio_bytes=500_000,
        )
        await service.start()
        try:
            sample = wav_tone(260)
            await service.register(
                device_id=device_id,
                identity_id=str(identity_id),
                samples=[sample] * 3,
            )
            result = await service.identify(device_id=device_id, sample=sample)
            assert result.identity_id == str(identity_id)

            with pytest.raises(IntegrityError):
                async with engine.begin() as connection:
                    await connection.execute(
                        text(
                            "UPDATE identities "
                            "SET voiceprint_embedding_dimension = "
                            "voiceprint_embedding_dimension + 1 "
                            "WHERE identity_id = :identity_id"
                        ),
                        {"identity_id": identity_id},
                    )

            async with engine.begin() as connection:
                await connection.execute(
                    text(
                        "UPDATE identities SET voiceprint_model = 'obsolete-model' "
                        "WHERE identity_id = :identity_id"
                    ),
                    {"identity_id": identity_id},
                )
            assert (
                await service.identify(device_id=device_id, sample=sample)
            ).matched is False
            assert await service.delete(device_id=device_id, identity_id=str(identity_id))
        finally:
            await service.close()
            async with engine.begin() as connection:
                await connection.execute(
                    text("DELETE FROM devices WHERE device_id = :device_id"),
                    {"device_id": device_id},
                )
            await engine.dispose()

    asyncio.run(scenario())
