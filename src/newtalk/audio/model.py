from dataclasses import dataclass
from typing import Literal, Protocol


@dataclass(frozen=True, slots=True)
class InputAudioFormat:
    codec: str
    sample_rate: int
    channels: int
    frame_duration_ms: int

    @property
    def bytes_per_second(self) -> int:
        return self.sample_rate * self.channels * 2


INPUT_AUDIO_FORMAT = InputAudioFormat(
    codec="pcm_s16le",
    sample_rate=16000,
    channels=1,
    frame_duration_ms=20,
)


@dataclass(frozen=True, slots=True)
class VadEvent:
    kind: Literal["speech_start", "speech_end"]
    probability: float
    audio_ms: float


@dataclass(frozen=True, slots=True)
class SpeechBoundary:
    kind: Literal["speech_start", "speech_end"]
    utterance_id: str
    probability: float
    audio_ms: float


class VoiceActivityStream(Protocol):
    def process(self, pcm: bytes) -> list[VadEvent]: ...

    def flush(self) -> list[VadEvent]: ...

    def reset(self) -> None: ...


class VoiceActivityDetector(Protocol):
    def create_stream(self) -> VoiceActivityStream: ...
