"""旧导入路径兼容；工具定义与口令解析由 tools 层维护。"""

from newtalk.tools.device_control import (
    DEVICE_ACTIONS,
    DEVICE_CONTROL_TOOL,
    control_from_text,
    parse_device_control,
)
from newtalk.tools.models import DeviceAction, DeviceControl

__all__ = [
    "DEVICE_ACTIONS", "DEVICE_CONTROL_TOOL", "DeviceAction", "DeviceControl",
    "control_from_text", "parse_device_control",
]
