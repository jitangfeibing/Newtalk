from collections.abc import AsyncIterator, Sequence
from typing import Any

from openai import AsyncOpenAI

from newtalk.chat.models import ChatMessage, ModelToolCall


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
    ) -> None:
        self.model = model
        self.system_prompt = system_prompt
        self._client = client or AsyncOpenAI(
            api_key=api_key,
            base_url=base_url,
            timeout=timeout_seconds,
        )

    async def stream(
        self,
        dialogue: Sequence[ChatMessage],
        *,
        tools: Sequence[dict[str, Any]] = (),
    ) -> AsyncIterator[str | ModelToolCall]:
        messages: list[dict[str, Any]] = []
        if self.system_prompt:
            messages.append({"role": "system", "content": self.system_prompt})
        messages.extend(_message_payload(message) for message in dialogue)

        request: dict[str, Any] = {
            "model": self.model,
            "messages": messages,
        }
        if tools:
            request["tools"] = list(tools)
            request["tool_choice"] = "auto"

        async with self._client.chat.completions.stream(
            **request,
        ) as stream:
            tool_calls: dict[int, dict[str, str]] = {}
            text_emitted = False
            async for event in stream:
                if event.type == "chunk":
                    _collect_tool_call_chunks(event.chunk, tool_calls)
                    continue
                if event.type == "content.delta" and not tool_calls:
                    delta = event.delta
                    if isinstance(delta, str) and delta:
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
