from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

import numpy as np
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine


@dataclass(frozen=True, slots=True)
class StoredTemplate:
    identity_id: str
    embedding: np.ndarray


class VoicePrintStore:
    def __init__(self, database_url: str) -> None:
        self._engine: AsyncEngine = create_async_engine(database_url, pool_pre_ping=True)

    async def start(self) -> None:
        async with self._engine.connect() as connection:
            await connection.execute(
                text("SELECT voiceprint_embedding FROM identities LIMIT 1")
            )

    async def close(self) -> None:
        await self._engine.dispose()

    async def ping(self) -> None:
        async with self._engine.connect() as connection:
            await connection.execute(text("SELECT 1"))

    async def save_template(
        self,
        *,
        device_id: str,
        identity_id: str,
        embedding: np.ndarray,
        model_name: str,
    ) -> datetime | None:
        try:
            parsed_id = UUID(identity_id)
        except ValueError:
            return None
        payload = np.asarray(embedding, dtype="<f4").tobytes()
        async with self._engine.begin() as connection:
            row = (
                await connection.execute(
                    text(
                        """
                        UPDATE identities
                           SET voiceprint_embedding = :embedding,
                               voiceprint_embedding_dimension = :dimension,
                               voiceprint_model = :model_name,
                               voiceprint_enrolled_at = now(),
                               updated_at = now()
                         WHERE identity_id = :identity_id
                           AND device_id = :device_id
                           AND status = 'active'
                     RETURNING voiceprint_enrolled_at
                        """
                    ),
                    {
                        "embedding": payload,
                        "dimension": int(embedding.size),
                        "model_name": model_name,
                        "identity_id": parsed_id,
                        "device_id": device_id,
                    },
                )
            ).first()
            return row[0] if row else None

    async def list_templates(
        self,
        device_id: str,
        *,
        model_name: str,
    ) -> list[StoredTemplate]:
        async with self._engine.connect() as connection:
            rows = (
                await connection.execute(
                    text(
                        """
                        SELECT identity_id, voiceprint_embedding,
                               voiceprint_embedding_dimension
                          FROM identities
                         WHERE device_id = :device_id
                           AND status = 'active'
                           AND voiceprint_embedding IS NOT NULL
                           AND voiceprint_model = :model_name
                        """
                    ),
                    {"device_id": device_id, "model_name": model_name},
                )
            ).all()
        templates: list[StoredTemplate] = []
        for identity_id, payload, dimension in rows:
            embedding = np.frombuffer(payload, dtype="<f4", count=dimension).copy()
            templates.append(StoredTemplate(str(identity_id), embedding))
        return templates

    async def delete_template(self, *, device_id: str, identity_id: str) -> bool:
        try:
            parsed_id = UUID(identity_id)
        except ValueError:
            return False
        async with self._engine.begin() as connection:
            result = await connection.execute(
                text(
                    """
                    UPDATE identities
                       SET voiceprint_embedding = NULL,
                           voiceprint_embedding_dimension = NULL,
                           voiceprint_model = NULL,
                           voiceprint_enrolled_at = NULL,
                           updated_at = now()
                     WHERE identity_id = :identity_id
                       AND device_id = :device_id
                       AND voiceprint_embedding IS NOT NULL
                    """
                ),
                {"identity_id": parsed_id, "device_id": device_id},
            )
            return result.rowcount == 1
