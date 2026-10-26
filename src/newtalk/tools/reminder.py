from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import TYPE_CHECKING
from zoneinfo import ZoneInfo

from newtalk.context_debug import record_context
from newtalk.tools.models import ToolContext

if TYPE_CHECKING:
    from newtalk.chat.models import ModelToolCall, Turn
    from newtalk.reminders.service import ReminderService


def reminder_tool(now: datetime) -> dict:
    return {"type": "function", "function": {
        "name": "reminder_manage", "strict": True,
        "description": (
            "仅在当前用户明确要求时新增、查询、修改或取消当前家庭设备共享的一次性提醒。"
            "提醒正文将公开播报，Guest 也可管理本设备提醒。不要从闲聊、记忆或引用自动创建。"
            "时间或目标不明确先询问；修改/取消先通过查询取得真实 reminder_id，不编造。"
            "相对时间使用 delay_seconds，绝对时间使用含时区的 due_at ISO8601；二者只填一个。"
            "无需使用的字段填 null。不支持循环闹钟。"
            f"当前上海时间 {now.astimezone(ZoneInfo('Asia/Shanghai')).isoformat()}。"
        ),
        "parameters": {"type": "object", "properties": {
            "action": {"type": "string", "enum": ["create", "list", "update", "cancel"]},
            "reminder_id": {"type": ["string", "null"]},
            "content": {"type": ["string", "null"]},
            "due_at": {"type": ["string", "null"]},
            "delay_seconds": {"type": ["integer", "null"]},
        }, "required": ["action", "reminder_id", "content", "due_at", "delay_seconds"],
            "additionalProperties": False},
    }}


async def execute_reminder_tool(service: ReminderService, turn: Turn, call: ModelToolCall) -> str:
    try:
        arguments = json.loads(call.arguments)
        if not isinstance(arguments, dict) or set(arguments) != {"action", "reminder_id", "content", "due_at", "delay_seconds"}:
            raise ValueError("提醒工具参数不完整或包含不支持的字段")
        action = arguments["action"]
        reminder_id = arguments["reminder_id"]
        if action == "list":
            if any(arguments[key] is not None for key in ("reminder_id", "content", "due_at", "delay_seconds")):
                raise ValueError("查询无需提供其他参数")
            items = await service.list(turn.device_id)
            result = {"status": "ok", "scope": "device", "items": [item.payload() for item in items[:30]]}
        elif action == "cancel":
            if any(arguments[key] is not None for key in ("content", "due_at", "delay_seconds")):
                raise ValueError("取消无需提供内容或时间")
            if not isinstance(reminder_id, str) or not reminder_id:
                raise ValueError("取消前需要查询提醒编号")
            item = await service.cancel(turn.device_id, reminder_id)
            result = {"status": "ok", "reminder": item.payload()}
        elif action in {"create", "update"}:
            delay = arguments["delay_seconds"]
            absolute = arguments["due_at"]
            if (delay is None) == (absolute is None):
                raise ValueError("相对时间和绝对时间必须且只能提供一个")
            if delay is not None:
                if type(delay) is not int or not 1 <= delay <= 366 * 86400:
                    raise ValueError("相对时间无效")
                due = service.clock() + timedelta(seconds=delay)
            elif isinstance(absolute, str):
                due = datetime.fromisoformat(absolute)
            else:
                raise ValueError("提醒时间无效")
            content = arguments["content"]
            if not isinstance(content, str):
                raise ValueError("缺少提醒内容")
            if action == "create":
                if reminder_id is not None:
                    raise ValueError("新增提醒不能指定编号")
                item = await service.create(turn.device_id, content, due, f"turn-{turn.turn_id}")
            else:
                if not isinstance(reminder_id, str) or not reminder_id:
                    raise ValueError("修改前需要查询提醒编号")
                item = await service.update(turn.device_id, reminder_id, content, due)
            result = {"status": "ok", "scope": "device", "reminder": item.payload()}
        else:
            raise ValueError("不支持这个提醒操作")
    except (ValueError, LookupError, TypeError) as exc:
        result = {"status": "error", "code": "invalid_reminder", "message": str(exc)}
    return json.dumps(result, ensure_ascii=False)


@dataclass(frozen=True, slots=True)
class ReminderTool:
    service: ReminderService
    name = "reminder_manage"

    def definition(self, context: ToolContext) -> dict | None:
        return reminder_tool(self.service.clock()) if context.client_tools_enabled else None

    async def execute(self, context: ToolContext, call: ModelToolCall) -> str:
        result = await execute_reminder_tool(self.service, context.turn, call)
        record_context("reminder_tool_result", call_id=call.call_id,
                       tool=call.name, arguments=call.arguments, result=result)
        return result
