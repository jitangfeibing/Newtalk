import asyncio
from collections.abc import AsyncIterator, Sequence
from typing import Any

from newtalk.chat.models import ChatMessage, ModelToolCall


class FakeLLM:
    """Deterministic streaming response used to validate the P2 chat path."""

    def __init__(self, chunk_delay_seconds: float = 0.01) -> None:
        self.chunk_delay_seconds = chunk_delay_seconds

    async def stream(
        self,
        messages: Sequence[ChatMessage],
        *,
        tools: Sequence[dict[str, Any]] = (),
    ) -> AsyncIterator[str | ModelToolCall]:
        if not messages or messages[-1].role not in {"user", "tool"}:
            raise ValueError("Chat messages must end with a user or tool message")
        user_message = next(
            (message for message in reversed(messages) if message.role == "user"),
            None,
        )
        if user_message is None:
            raise ValueError("Chat messages must contain a user message")
        user_text = user_message.content.split("\n", 1)[-1]
        for chunk in ("我收到了：", user_text):
            if self.chunk_delay_seconds:
                await asyncio.sleep(self.chunk_delay_seconds)
            yield chunk

    async def aclose(self) -> None:
        return None
