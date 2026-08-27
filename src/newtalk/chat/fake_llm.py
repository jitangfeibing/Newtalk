import asyncio
from collections.abc import AsyncIterator, Sequence

from newtalk.chat.models import ChatMessage


class FakeLLM:
    """Deterministic streaming response used to validate the P2 chat path."""

    def __init__(self, chunk_delay_seconds: float = 0.01) -> None:
        self.chunk_delay_seconds = chunk_delay_seconds

    async def stream(self, messages: Sequence[ChatMessage]) -> AsyncIterator[str]:
        if not messages or messages[-1].role != "user":
            raise ValueError("Chat messages must end with a user message")
        user_text = messages[-1].content
        for chunk in ("我收到了：", user_text):
            if self.chunk_delay_seconds:
                await asyncio.sleep(self.chunk_delay_seconds)
            yield chunk

    async def aclose(self) -> None:
        return None
