from collections.abc import AsyncIterator
import math
from struct import pack

from newtalk.tts.model import AudioFormat


class FakeTTS:
    def __init__(self, *, sample_rate: int = 24000) -> None:
        self._audio_format = AudioFormat(
            codec="pcm_s16le",
            sample_rate=sample_rate,
            channels=1,
        )

    @property
    def audio_format(self) -> AudioFormat:
        return self._audio_format

    async def stream(
        self,
        text_chunks: AsyncIterator[str],
        *,
        turn_id: str,
    ) -> AsyncIterator[bytes]:
        del turn_id
        async for text in text_chunks:
            if not text.strip():
                continue
            yield _tone_frame(self._audio_format.sample_rate)

    async def aclose(self) -> None:
        return None


def _tone_frame(sample_rate: int) -> bytes:
    duration_seconds = 0.12
    sample_count = int(sample_rate * duration_seconds)
    amplitude = 2800
    frequency = 520
    return b"".join(
        pack(
            "<h",
            int(amplitude * math.sin(2 * math.pi * frequency * index / sample_rate)),
        )
        for index in range(sample_count)
    )
