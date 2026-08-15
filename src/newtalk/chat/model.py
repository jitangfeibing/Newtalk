from collections.abc import AsyncIterator
from typing import Protocol


class ChatModel(Protocol):
    def stream(self, user_text: str) -> AsyncIterator[str]: ...

    async def aclose(self) -> None: ...
