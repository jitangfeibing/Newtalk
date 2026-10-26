from dataclasses import dataclass, field
from datetime import datetime
from typing import Literal

from newtalk.chat.device_control import DeviceControl
from newtalk.tts import AudioFormat
from newtalk.media.models import MediaControl


@dataclass(frozen=True, slots=True)
class ModelToolCall:
    call_id: str
    name: str
    arguments: str
    # 部分思考模型要求工具后续请求回传原推理字段；不用于播报或对话历史。
    reasoning_content: str | None = field(default=None, repr=False)


@dataclass(frozen=True, slots=True)
class ChatMessage:
    role: Literal["system", "user", "assistant", "tool"]
    content: str
    tool_calls: tuple[ModelToolCall, ...] = ()
    tool_call_id: str | None = None
    # 仅用于解释历史取舍，不发送给 Provider，也不改变消息内容的相等语义。
    source_turn_id: str | None = field(default=None, compare=False, repr=False)
    context_kind: Literal["memory"] | None = field(default=None, compare=False, repr=False)


@dataclass(frozen=True, slots=True)
class Turn:
    turn_id: str
    session_id: str
    user_text: str
    messages: tuple[ChatMessage, ...]
    created_at: datetime
    device_id: str = "unknown"
    speaker_identity_id: str | None = None
    speaker_display_name: str = "Guest"
    speaker_relationship: str | None = None
    profile_ready: bool = False
    profile_field_count: int = 0


def format_user_message(
    text: str,
    *,
    speaker_identity_id: str | None,
    speaker_display_name: str,
    speaker_relationship: str | None,
) -> str:
    if speaker_identity_id is None:
        return f"[说话人：Guest（未识别访客）]\n{text}"
    relationship = (
        f"；家庭关系：{speaker_relationship}" if speaker_relationship else ""
    )
    return f"[说话人：{speaker_display_name}{relationship}]\n{text}"


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
    DeviceControl
    | MediaControl
    | TextDelta
    | AudioStarted
    | AudioFrame
    | AudioCompleted
    | AudioFailed
    | TurnCompleted
)
