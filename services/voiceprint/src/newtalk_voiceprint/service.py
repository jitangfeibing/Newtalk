import asyncio
from dataclasses import dataclass
from datetime import datetime
import logging
import time

import numpy as np

from newtalk_voiceprint.audio import validate_pcm_wav
from newtalk_voiceprint.embedder import SpeakerEmbedder, normalize_embedding
from newtalk_voiceprint.store import VoicePrintStore


logger = logging.getLogger(__name__)


class IdentityNotFoundError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class Identification:
    identity_id: str | None
    score: float
    matched: bool
    elapsed_ms: float


class VoicePrintService:
    def __init__(
        self,
        store: VoicePrintStore,
        embedder: SpeakerEmbedder,
        *,
        threshold: float,
        min_sample_seconds: float,
        identify_min_sample_seconds: float,
        max_sample_seconds: float,
        max_audio_bytes: int,
    ) -> None:
        self._store = store
        self._embedder = embedder
        self._threshold = threshold
        self._enrollment_min_sample_seconds = min_sample_seconds
        self._identify_min_sample_seconds = identify_min_sample_seconds
        self._max_sample_seconds = max_sample_seconds
        self._max_audio_bytes = max_audio_bytes

    @property
    def model_name(self) -> str:
        return self._embedder.model_name

    async def start(self) -> None:
        await self._store.start()
        await asyncio.to_thread(self._embedder.start)

    async def close(self) -> None:
        await self._store.close()

    async def ping(self) -> None:
        await self._store.ping()

    def _validate(self, wav_bytes: bytes, *, min_seconds: float) -> bytes:
        return validate_pcm_wav(
            wav_bytes,
            min_seconds=min_seconds,
            max_seconds=self._max_sample_seconds,
            max_bytes=self._max_audio_bytes,
        ).wav_bytes

    async def register(
        self,
        *,
        device_id: str,
        identity_id: str,
        samples: list[bytes],
    ) -> datetime:
        if len(samples) != 3:
            raise ValueError("exactly three voice samples are required")
        embeddings = []
        for sample in samples:
            wav_bytes = self._validate(
                sample,
                min_seconds=self._enrollment_min_sample_seconds,
            )
            embeddings.append(await asyncio.to_thread(self._embedder.embed, wav_bytes))
        template = normalize_embedding(np.mean(embeddings, axis=0))
        enrolled_at = await self._store.save_template(
            device_id=device_id,
            identity_id=identity_id,
            embedding=template,
            model_name=self.model_name,
        )
        if enrolled_at is None:
            raise IdentityNotFoundError(identity_id)
        return enrolled_at

    async def identify(self, *, device_id: str, sample: bytes) -> Identification:
        started = time.perf_counter()
        probe = await asyncio.to_thread(
            self._embedder.embed,
            self._validate(
                sample,
                min_seconds=self._identify_min_sample_seconds,
            ),
        )
        templates = await self._store.list_templates(
            device_id,
            model_name=self.model_name,
        )
        if not templates:
            elapsed_ms = (time.perf_counter() - started) * 1000
            logger.info(
                "voiceprint_identify_completed model=%s templates=0 score=0.0000 "
                "threshold=%.4f matched=False elapsed_ms=%.1f",
                self.model_name,
                self._threshold,
                elapsed_ms,
            )
            return Identification(None, 0.0, False, elapsed_ms)
        scored = [
            (template.identity_id, float(np.dot(probe, template.embedding)))
            for template in templates
        ]
        identity_id, score = max(scored, key=lambda item: item[1])
        matched = score >= self._threshold
        elapsed_ms = (time.perf_counter() - started) * 1000
        logger.info(
            "voiceprint_identify_completed model=%s templates=%s score=%.4f "
            "threshold=%.4f matched=%s elapsed_ms=%.1f",
            self.model_name,
            len(templates),
            score,
            self._threshold,
            matched,
            elapsed_ms,
        )
        return Identification(
            identity_id if matched else None,
            score,
            matched,
            elapsed_ms,
        )

    async def delete(self, *, device_id: str, identity_id: str) -> bool:
        return await self._store.delete_template(
            device_id=device_id,
            identity_id=identity_id,
        )
