from __future__ import annotations

from typing import TYPE_CHECKING

from newtalk.tools.device_control import DeviceControlTool
from newtalk.tools.memory import MemorySearchTool
from newtalk.tools.registry import ToolRegistry
from newtalk.tools.reminder import ReminderTool

if TYPE_CHECKING:
    from newtalk.reminders.service import ReminderService


def create_tool_registry(
    memory: MemorySearchTool, reminder_service: ReminderService | None = None,
) -> ToolRegistry:
    tools = (memory, DeviceControlTool())
    if reminder_service is not None:
        tools = (*tools, ReminderTool(reminder_service))
    return ToolRegistry(tools)
