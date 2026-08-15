from collections.abc import AsyncIterator
from datetime import datetime, timezone
from uuid import uuid4

from newtalk.chat.fake_llm import FakeLLM
from newtalk.chat.models import Turn


class ChatService:
    def __init__(self, model: FakeLLM | None = None) -> None:
        self._model = model or FakeLLM()

    def create_turn(self, *, session_id: str, user_text: str) -> Turn:
        return Turn(
            turn_id=str(uuid4()),
            session_id=session_id,
            user_text=user_text,
            created_at=datetime.now(timezone.utc),
        )

    async def stream_reply(self, turn: Turn) -> AsyncIterator[str]:
        async for chunk in self._model.stream(turn.user_text):
            if not isinstance(chunk, str) or not chunk:
                raise ValueError("Chat model chunks must be non-empty strings")
            yield chunk
