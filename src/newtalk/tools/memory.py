from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass
from time import perf_counter
from typing import TYPE_CHECKING

from newtalk.context_debug import record_context
from newtalk.tools.models import ToolContext, tool_error

if TYPE_CHECKING:
    from newtalk.chat.models import ModelToolCall, Turn
    from newtalk.memory.provider import MemoryProvider

logger = logging.getLogger(__name__)

_MEMORY_SEARCH_TOOL = {
    "type": "function",
    "function": {
        "name": "memory_search",
        "strict": True,
        "description": (
            "仅当当前对话和用户画像不足以回答、且问题依赖这位成员更早的个人经历时，"
            "检索当前成员的长期记忆。普通知识问题不要调用。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "需要回忆的过去信息的简洁描述。",
                }
            },
            "required": ["query"],
            "additionalProperties": False,
        },
    },
}

@dataclass(frozen=True, slots=True)
class MemorySearchTool:
    provider: MemoryProvider
    search_limit: int = 5
    search_relativity: float = 0.55
    result_max_chars: int = 4000
    query_max_chars: int = 500
    name = "memory_search"

    def enabled_for(self, turn: Turn) -> bool:
        return self.provider.enabled and turn.speaker_identity_id is not None

    def definition(self, context: ToolContext) -> dict | None:
        if context.memory_retrieval_mode == "tool" and self.enabled_for(context.turn):
            return _MEMORY_SEARCH_TOOL
        return None

    async def execute(self, context: ToolContext, call: ModelToolCall) -> str:
        result = await self.search(context, call)
        record_context("memory_search_result", call_id=call.call_id,
                       tool=call.name, arguments=call.arguments, result=result)
        return result

    async def search(
        self,
        context: ToolContext,
        tool_call: ModelToolCall,
    ) -> str:
        from newtalk.profile.models import ProfileScope

        turn = context.turn
        started_at = perf_counter()
        if tool_call.name != "memory_search":
            return tool_error("unsupported_tool", "不支持这个工具。")
        try:
            arguments = json.loads(tool_call.arguments)
        except json.JSONDecodeError:
            return tool_error("invalid_arguments", "记忆查询参数不是有效 JSON。")
        query = arguments.get("query") if isinstance(arguments, dict) else None
        if not isinstance(query, str) or not query.strip():
            return tool_error("invalid_arguments", "记忆查询缺少 query。")
        query = query.strip()
        if len(query) > self.query_max_chars:
            return tool_error("invalid_arguments", "记忆查询超过长度限制。")
        if turn.speaker_identity_id is None:
            return tool_error("guest_forbidden", "Guest 不能读取长期记忆。")
        record_context(
            "memory_search_started", call_id=tool_call.call_id, query=query,
            mode=context.memory_retrieval_mode,
        )
        try:
            result = await self.provider.search(
                ProfileScope(
                    device_id=turn.device_id,
                    identity_id=turn.speaker_identity_id,
                ),
                query=query,
                conversation_id=turn.session_id,
                limit=self.search_limit,
                relativity=self.search_relativity,
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - Provider 故障沿用降级结果，取消单独传播。
            logger.warning(
                "memory_search_failed turn_id=%s identity_id=%s error_type=%s elapsed_ms=%.1f",
                turn.turn_id,
                turn.speaker_identity_id,
                type(exc).__name__,
                (perf_counter() - started_at) * 1000,
            )
            return tool_error("unavailable", "长期记忆暂时不可用。")
        logger.info(
            "memory_search_completed turn_id=%s identity_id=%s results=%s elapsed_ms=%.1f",
            turn.turn_id,
            turn.speaker_identity_id,
            len(result.items),
            (perf_counter() - started_at) * 1000,
        )
        record_context(
            "memory_search_completed", call_id=tool_call.call_id,
            mode=context.memory_retrieval_mode, results=len(result.items),
            elapsed_ms=(perf_counter() - started_at) * 1000,
        )
        return result.to_tool_content(max_chars=self.result_max_chars)
