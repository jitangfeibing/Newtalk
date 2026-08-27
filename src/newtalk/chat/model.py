from collections.abc import AsyncIterator, Sequence
from typing import Protocol

from newtalk.chat.models import ChatMessage


class ChatModel(Protocol):
    def stream(self, messages: Sequence[ChatMessage]) -> AsyncIterator[str]: ...

    async def aclose(self) -> None: ...
