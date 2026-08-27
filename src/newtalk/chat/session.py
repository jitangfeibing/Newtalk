from collections import deque
from dataclasses import dataclass

from newtalk.chat.models import ChatMessage, Turn


@dataclass(frozen=True, slots=True)
class DialogueExchange:
    turn_id: str
    user_text: str
    assistant_text: str


class DialogueSession:
    """Completed dialogue history owned by one WebSocket session."""

    def __init__(
        self,
        session_id: str,
        *,
        max_turns: int,
        max_chars: int,
    ) -> None:
        if max_turns <= 0 or max_chars <= 0:
            raise ValueError("Dialogue window limits must be positive")
        self.session_id = session_id
        self.max_turns = max_turns
        self.max_chars = max_chars
        self._exchanges: deque[DialogueExchange] = deque(maxlen=max_turns)

    @property
    def exchanges(self) -> tuple[DialogueExchange, ...]:
        return tuple(self._exchanges)

    def messages_for(self, user_text: str) -> tuple[ChatMessage, ...]:
        current = ChatMessage(role="user", content=user_text)
        remaining_chars = max(0, self.max_chars - len(user_text))
        selected: list[DialogueExchange] = []

        for exchange in reversed(self._exchanges):
            exchange_chars = len(exchange.user_text) + len(exchange.assistant_text)
            if exchange_chars > remaining_chars:
                break
            selected.append(exchange)
            remaining_chars -= exchange_chars

        messages: list[ChatMessage] = []
        for exchange in reversed(selected):
            messages.extend(
                (
                    ChatMessage(role="user", content=exchange.user_text),
                    ChatMessage(role="assistant", content=exchange.assistant_text),
                )
            )
        messages.append(current)
        return tuple(messages)

    def commit(self, turn: Turn, assistant_text: str) -> None:
        if turn.session_id != self.session_id:
            raise ValueError("Turn belongs to a different session")
        if not assistant_text:
            raise ValueError("Completed assistant text must not be empty")
        if any(exchange.turn_id == turn.turn_id for exchange in self._exchanges):
            raise ValueError("Turn has already been committed")
        self._exchanges.append(
            DialogueExchange(
                turn_id=turn.turn_id,
                user_text=turn.user_text,
                assistant_text=assistant_text,
            )
        )
