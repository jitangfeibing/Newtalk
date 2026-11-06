from __future__ import annotations

import json
import asyncio
import logging
import re
from collections.abc import Iterable
from copy import deepcopy
from dataclasses import dataclass, field
from typing import TYPE_CHECKING
from time import perf_counter

from newtalk.tools.models import (
    CLIENT_ACTION_TYPES,
    Tool,
    ToolContext,
    ToolResult,
    tool_error,
)

if TYPE_CHECKING:
    from newtalk.chat.models import ModelToolCall

logger = logging.getLogger(__name__)


def _log_label(value: object) -> str:
    return value if isinstance(value, str) and re.fullmatch(r"[A-Za-z0-9_:-]{1,128}", value) else "unknown"


def _payload(result: ToolResult) -> dict:
    if isinstance(result, str):
        try:
            value = json.loads(result)
            if isinstance(value, dict):
                return value
        except ValueError:
            pass
    return {}


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

    def error_reply(self, name: str, result: ToolResult) -> str | None:
        if _payload(result).get("status") != "error":
            return None
        tool = next((tool for tool in self.handlers if tool.name == name), None)
        reply = getattr(tool, "error_reply", None)
        return reply if isinstance(reply, str) and reply else None

    async def execute(self, call: ModelToolCall, *, round_index: int = 0) -> ToolResult:
        # 只执行本轮实际提供过的工具；定义副本被修改也不能扩大执行范围。
        started = perf_counter()
        status, code, action = "failed", "exception", "none"
        try:
            try:
                arguments = json.loads(call.arguments)
                if isinstance(arguments, dict) and "action" in arguments:
                    action = _log_label(arguments["action"])
            except ValueError:
                pass
            tool = next((tool for tool in self.handlers if tool.name == call.name), None)
            result = (await tool.execute(self.context, call) if tool is not None
                      else tool_error("unsupported_tool", "不支持这个工具。"))
            if not isinstance(result, (str, *CLIENT_ACTION_TYPES)):
                raise TypeError("Tool results must be text or a client action")
            payload = _payload(result)
            status = _log_label(payload.get("status", "client_action" if isinstance(result, CLIENT_ACTION_TYPES) else "returned"))
            code = _log_label(payload.get("code", payload.get("reason", "none")))
            return result
        except asyncio.CancelledError:
            status, code = "cancelled", "cancelled"
            raise
        finally:
            logger.info(
                "tool_execution_result turn_id=%s call_id=%s tool=%s round=%s phase=execute "
                "action=%s status=%s code=%s elapsed_ms=%.1f",
                self.context.turn.turn_id, _log_label(call.call_id), _log_label(call.name), round_index,
                action, status, code, (perf_counter() - started) * 1000,
            )


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
