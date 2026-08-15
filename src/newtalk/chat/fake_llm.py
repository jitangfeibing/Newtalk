import asyncio
from collections.abc import AsyncIterator


class FakeLLM:
    """Deterministic streaming response used to validate the P2 chat path."""

    def __init__(self, chunk_delay_seconds: float = 0.01) -> None:
        self.chunk_delay_seconds = chunk_delay_seconds

    async def stream(self, user_text: str) -> AsyncIterator[str]:
        for chunk in ("我收到了：", user_text):
            if self.chunk_delay_seconds:
                await asyncio.sleep(self.chunk_delay_seconds)
            yield chunk
