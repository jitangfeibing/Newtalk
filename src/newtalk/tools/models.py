from __future__ import annotations

import json
from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal, Protocol

from newtalk.media.models import MediaCommands, MediaControl

if TYPE_CHECKING:
    from newtalk.chat.models import ModelToolCall, Turn


DeviceAction = Literal["stop_reply", "microphone_off", "camera_off", "disconnect"]


@dataclass(frozen=True, slots=True)
class DeviceControl:
    action: DeviceAction
    call_id: str


ToolResult = str | DeviceControl | MediaControl
CLIENT_ACTION_TYPES = (DeviceControl, MediaControl)


@dataclass(frozen=True, slots=True)
class ToolContext:
    # 身份范围沿用已校验的 Turn；每轮独立创建，不存共享的“当前成员”。
    turn: Turn
    client_tools_enabled: bool = False
    memory_retrieval_mode: Literal["tool", "preload"] = "tool"
    media_commands: MediaCommands | None = None


class Tool(Protocol):
    @property
    def name(self) -> str: ...

    def definition(self, context: ToolContext) -> dict | None: ...

    async def execute(self, context: ToolContext, call: ModelToolCall) -> ToolResult: ...


def tool_error(code: str, message: str) -> str:
    return json.dumps(
        {"status": "error", "code": code, "message": message},
        ensure_ascii=False, separators=(",", ":"),
    )
