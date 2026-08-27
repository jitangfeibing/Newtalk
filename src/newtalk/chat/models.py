from dataclasses import dataclass
from datetime import datetime
from typing import Literal

from newtalk.tts import AudioFormat


@dataclass(frozen=True, slots=True)
class ChatMessage:
    role: Literal["user", "assistant"]
    content: str


@dataclass(frozen=True, slots=True)
class Turn:
    turn_id: str
    session_id: str
    user_text: str
    messages: tuple[ChatMessage, ...]
    created_at: datetime


@dataclass(frozen=True, slots=True)
class TextDelta:
    sequence: int
    text: str


@dataclass(frozen=True, slots=True)
class AudioStarted:
    stream_id: str
    audio_format: AudioFormat


@dataclass(frozen=True, slots=True)
class AudioFrame:
    stream_id: str
    sequence: int
    data: bytes


@dataclass(frozen=True, slots=True)
class AudioCompleted:
    stream_id: str
    frame_count: int
    byte_count: int


@dataclass(frozen=True, slots=True)
class AudioFailed:
    stream_id: str
    message: str


@dataclass(frozen=True, slots=True)
class TurnCompleted:
    text: str


TurnOutput = (
    TextDelta
    | AudioStarted
    | AudioFrame
    | AudioCompleted
    | AudioFailed
    | TurnCompleted
)
