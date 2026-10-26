"""旧导入路径兼容；提醒模型工具由 tools 层维护。"""

from newtalk.tools.reminder import execute_reminder_tool, reminder_tool

__all__ = ["execute_reminder_tool", "reminder_tool"]
