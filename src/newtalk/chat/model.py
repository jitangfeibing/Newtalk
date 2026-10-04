from collections.abc import AsyncIterator, Sequence
from typing import Any, Protocol

from newtalk.chat.models import ChatMessage, ModelToolCall


class ChatModel(Protocol):
    def stream(
        self,
        messages: Sequence[ChatMessage],
        *,
        tools: Sequence[dict[str, Any]] = (),
    ) -> AsyncIterator[str | ModelToolCall]: ...

    async def aclose(self) -> None: ...
