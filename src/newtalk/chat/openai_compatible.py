from collections.abc import AsyncIterator, Sequence
import asyncio
from contextlib import aclosing
import logging
from time import perf_counter
from typing import Any

from openai import AsyncOpenAI, DefaultAsyncHttpxClient

from newtalk.chat.models import ChatMessage, ModelToolCall
from newtalk.context_debug import record_model_request
from newtalk.context_debug import record_context
from newtalk.chat.context_budget import ContextBudgetExceeded, fit_request
from newtalk.config import DEFAULT_CONTEXT_INPUT_TOKENS
from newtalk.chat.llm_timing import (
    LlmRequestTiming, active_request_timing, observe_http_request, observe_http_response,
)


logger = logging.getLogger(__name__)


class OpenAICompatibleChatModel:
    def __init__(
        self,
        *,
        api_key: str,
        model: str,
        base_url: str | None = None,
        system_prompt: str | None = None,
        timeout_seconds: float = 30.0,
        client: Any | None = None,
        input_token_budget: int = DEFAULT_CONTEXT_INPUT_TOKENS,
        thinking: str = "default",
    ) -> None:
        self.model = model
        self.system_prompt = system_prompt
        if thinking not in {"default", "enabled", "disabled"}:
            raise ValueError("Thinking mode must be default, enabled or disabled")
        self._thinking = thinking
        if input_token_budget <= 0:
            raise ValueError("Input token budget must be positive")
        self._input_token_budget = input_token_budget
        self._client = client or AsyncOpenAI(
            api_key=api_key,
            base_url=base_url,
            timeout=timeout_seconds,
            http_client=DefaultAsyncHttpxClient(event_hooks={
                'request': [observe_http_request], 'response': [observe_http_response],
            }),
        )
        # SDK 首次访问会延迟导入/初始化资源；移到启动阶段，不让首句承担这段本地等待。
        # 访问资源不会发送请求，不是付费 API 预热，也不代表云端模型已经预热。
        started_at = perf_counter()
        self._completions = self._client.chat.completions
        logger.info('llm_client_ready model=%s resource_init_ms=%.1f',
                    self.model, (perf_counter() - started_at) * 1000)

    async def stream(
        self,
        dialogue: Sequence[ChatMessage],
        *,
        tools: Sequence[dict[str, Any]] = (),
    ) -> AsyncIterator[str | ModelToolCall]:
        timing = LlmRequestTiming(self.model)
        token = active_request_timing.set(timing)
        outcome = 'closed'
        try:
            # 外层消费者提前停止时也必须立即关闭 SDK 流，不能等异步生成器被 GC。
            async with aclosing(self._stream_request(dialogue, tools=tools, timing=timing)) as events:
                async for event in events:
                    yield event
            outcome = 'completed'
        except asyncio.CancelledError:
            outcome = 'cancelled'
            raise
        except Exception:
            outcome = 'failed'
            raise
        finally:
            try:
                timing.finish(outcome)
            finally:
                active_request_timing.reset(token)

    async def _stream_request(
        self, dialogue: Sequence[ChatMessage], *, tools: Sequence[dict[str, Any]], timing: LlmRequestTiming,
    ) -> AsyncIterator[str | ModelToolCall]:
        messages: list[dict[str, Any]] = []
        if self.system_prompt:
            messages.append({"role": "system", "content": self.system_prompt})
        messages.extend(_message_payload(message) for message in dialogue)

        request: dict[str, Any] = {
            "model": self.model,
            "messages": messages,
        }
        if self._thinking != "default":
            request["thinking"] = {"type": self._thinking}
        if tools:
            request["tools"] = list(tools)
            request["tool_choice"] = "auto"

        source_turn_ids = ([None] if self.system_prompt else []) + [
            message.source_turn_id for message in dialogue
        ]
        try:
            request, budget = fit_request(
                request, limit=self._input_token_budget, source_turn_ids=source_turn_ids,
                memory_context_indices={
                    index + bool(self.system_prompt) for index, message in enumerate(dialogue)
                    if message.context_kind == "memory"
                },
            )
        except ContextBudgetExceeded as exc:
            record_context("context_budget_rejected", budget=exc.decision)
            raise
        timing.prepared(request, input_tokens_estimate=budget['after_tokens_estimate'])
        record_model_request(request, budget=budget, call_id=timing.call_id)
        timing.mark('sdk_start_ms')
        # 快照与预算记录线上实际 JSON；SDK 则要求供应商扩展字段从 extra_body 传入。
        sdk_request = dict(request)
        thinking = sdk_request.pop("thinking", None)
        if thinking is not None:
            sdk_request["extra_body"] = {"thinking": thinking}
        async with self._completions.stream(
            **sdk_request,
        ) as stream:
            timing.mark('stream_open_ms')
            tool_calls: dict[int, dict[str, str]] = {}
            reasoning_parts: list[str] = []
            text_emitted = False
            async for event in stream:
                timing.mark('first_event_ms')
                if event.type == "chunk":
                    timing.observe_usage(event.chunk)
                    for choice in getattr(event.chunk, "choices", ()):
                        reasoning = getattr(getattr(choice, "delta", None), "reasoning_content", None)
                        if isinstance(reasoning, str):
                            if reasoning:
                                timing.mark('first_reasoning_ms')
                            reasoning_parts.append(reasoning)
                    _collect_tool_call_chunks(event.chunk, tool_calls)
                    continue
                if event.type == "content.delta" and not tool_calls:
                    delta = event.delta
                    if isinstance(delta, str) and delta:
                        timing.mark('first_text_ms')
                        text_emitted = True
                        yield delta
            if tool_calls and text_emitted:
                raise ValueError("Model mixed text with a tool call")
            if tool_calls:
                if len(tool_calls) != 1:
                    raise ValueError("Newtalk supports one model tool call per round")
                call = next(iter(tool_calls.values()))
                call_id = call.get("id", "")
                name = call.get("name", "")
                if not call_id or not name:
                    raise ValueError("Model returned an incomplete tool call")
                yield ModelToolCall(
                    call_id=call_id,
                    name=name,
                    arguments=call.get("arguments", ""),
                    reasoning_content="".join(reasoning_parts) if reasoning_parts else None,
                )

    async def aclose(self) -> None:
        await self._client.close()


def _message_payload(message: ChatMessage) -> dict[str, Any]:
    payload: dict[str, Any] = {"role": message.role, "content": message.content}
    if message.tool_calls:
        payload["tool_calls"] = [
            {
                "id": call.call_id,
                "type": "function",
                "function": {"name": call.name, "arguments": call.arguments},
            }
            for call in message.tool_calls
        ]
        reasoning = message.tool_calls[0].reasoning_content
        if reasoning is not None:
            payload["reasoning_content"] = reasoning
    if message.tool_call_id:
        payload["tool_call_id"] = message.tool_call_id
    return payload


def _collect_tool_call_chunks(chunk: Any, calls: dict[int, dict[str, str]]) -> None:
    for choice in getattr(chunk, "choices", ()):
        delta = getattr(choice, "delta", None)
        for raw_call in getattr(delta, "tool_calls", None) or ():
            index = getattr(raw_call, "index", 0)
            call = calls.setdefault(index, {"id": "", "name": "", "arguments": ""})
            call_id = getattr(raw_call, "id", None)
            if isinstance(call_id, str) and call_id:
                call["id"] = call_id
            function = getattr(raw_call, "function", None)
            name = getattr(function, "name", None)
            if isinstance(name, str) and name:
                call["name"] = name
            arguments = getattr(function, "arguments", None)
            if isinstance(arguments, str) and arguments:
                call["arguments"] += arguments
