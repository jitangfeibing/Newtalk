from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True, slots=True)
class AudioFormat:
    codec: str
    sample_rate: int
    channels: int


class TextToSpeech(Protocol):
    @property
    def audio_format(self) -> AudioFormat: ...

    def stream(
        self,
        text_chunks: AsyncIterator[str],
        *,
        turn_id: str,
    ) -> AsyncIterator[bytes]: ...

    async def aclose(self) -> None: ...
