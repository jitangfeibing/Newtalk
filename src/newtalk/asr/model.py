from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True, slots=True)
class AsrPartial:
    text: str


@dataclass(frozen=True, slots=True)
class AsrFinal:
    text: str


AsrEvent = AsrPartial | AsrFinal


class SpeechRecognizer(Protocol):
    def stream(
        self,
        audio_chunks: AsyncIterator[bytes],
        *,
        utterance_id: str,
    ) -> AsyncIterator[AsrEvent]: ...

    async def aclose(self) -> None: ...
