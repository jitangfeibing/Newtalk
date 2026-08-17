from collections.abc import AsyncIterator
from contextlib import aclosing
from datetime import datetime, timezone
import logging
from time import perf_counter
from uuid import uuid4

from newtalk.chat.fake_llm import FakeLLM
from newtalk.chat.model import ChatModel
from newtalk.chat.models import Turn


logger = logging.getLogger(__name__)


class ChatService:
    def __init__(self, model: ChatModel | None = None) -> None:
        self._model = model or FakeLLM()

    def create_turn(self, *, session_id: str, user_text: str) -> Turn:
        return Turn(
            turn_id=str(uuid4()),
            session_id=session_id,
            user_text=user_text,
            created_at=datetime.now(timezone.utc),
        )

    async def stream_reply(self, turn: Turn) -> AsyncIterator[str]:
        started_at = perf_counter()
        chunk_count = 0
        model_name = type(self._model).__name__
        try:
            async with aclosing(self._model.stream(turn.user_text)) as model_stream:
                async for chunk in model_stream:
                    if not isinstance(chunk, str) or not chunk:
                        raise ValueError("Chat model chunks must be non-empty strings")
                    chunk_count += 1
                    if chunk_count == 1:
                        logger.info(
                            "llm_first_token turn_id=%s model=%s elapsed_ms=%.1f",
                            turn.turn_id,
                            model_name,
                            (perf_counter() - started_at) * 1000,
                        )
                    yield chunk
            if chunk_count == 0:
                raise ValueError("Chat model returned no text")
        except Exception:
            logger.exception(
                "llm_stream_failed turn_id=%s model=%s elapsed_ms=%.1f",
                turn.turn_id,
                model_name,
                (perf_counter() - started_at) * 1000,
            )
            raise
        logger.info(
            "llm_stream_completed turn_id=%s model=%s chunks=%s elapsed_ms=%.1f",
            turn.turn_id,
            model_name,
            chunk_count,
            (perf_counter() - started_at) * 1000,
        )

    async def aclose(self) -> None:
        await self._model.aclose()
