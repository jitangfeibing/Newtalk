from __future__ import annotations

import json
from collections.abc import Iterable
from copy import deepcopy
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from newtalk.tools.models import (
    CLIENT_ACTION_TYPES,
    Tool,
    ToolContext,
    ToolResult,
    tool_error,
)

if TYPE_CHECKING:
    from newtalk.chat.models import ModelToolCall


@dataclass(frozen=True, slots=True)
class ToolSelection:
    context: ToolContext
    handlers: tuple[Tool, ...]
    _definitions: tuple[dict, ...] = field(repr=False)

    @property
    def names(self) -> tuple[str, ...]:
        return tuple(tool.name for tool in self.handlers)

    @property
    def definitions(self) -> tuple[dict, ...]:
        return deepcopy(self._definitions)

    def following(self, name: str, result: str = '') -> ToolSelection:
        names = next((getattr(tool, 'followup_tools', ()) for tool in self.handlers if tool.name == name), ())
        if names:
            try:
                payload = json.loads(result)
            except ValueError:
                payload = None
            if not isinstance(payload, dict) or payload.get('status') != 'ok':
                names = ()
        pairs = [(tool, definition) for tool, definition in zip(self.handlers, self._definitions, strict=True)
                 if tool.name in names]
        return ToolSelection(self.context, tuple(tool for tool, _ in pairs), tuple(definition for _, definition in pairs))

    async def execute(self, call: ModelToolCall) -> ToolResult:
        # 只执行本轮实际提供过的工具；定义副本被修改也不能扩大执行范围。
        for tool in self.handlers:
            if tool.name == call.name:
                result = await tool.execute(self.context, call)
                if not isinstance(result, (str, *CLIENT_ACTION_TYPES)):
                    raise TypeError("Tool results must be text or a client action")
                return result
        return tool_error("unsupported_tool", "不支持这个工具。")


class ToolRegistry:
    def __init__(self, tools: Iterable[Tool] = ()) -> None:
        self._tools = tuple(tools)
        names = [tool.name for tool in self._tools]
        if any(not isinstance(name, str) or not name for name in names) or len(set(names)) != len(names):
            raise ValueError("Tool names must be non-empty and unique")

    def select(self, context: ToolContext) -> ToolSelection:
        handlers, definitions = [], []
        for tool in self._tools:
            definition = tool.definition(context)
            if definition is None:
                continue
            if definition.get("function", {}).get("name") != tool.name:
                raise ValueError("Tool definition name must match its handler")
            handlers.append(tool)
            definitions.append(deepcopy(definition))
        return ToolSelection(context, tuple(handlers), tuple(definitions))

    def with_tool(self, tool: Tool) -> ToolRegistry:
        # 组合期替换返回新注册表，已经开始的会话继续使用自己的选择快照。
        tools = tuple(tool if item.name == tool.name else item for item in self._tools)
        if not any(item.name == tool.name for item in self._tools):
            tools = (*tools, tool)
        return ToolRegistry(tools)
