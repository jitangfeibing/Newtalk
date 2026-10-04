from collections import deque
from collections.abc import Sequence
from dataclasses import dataclass
from weakref import WeakKeyDictionary

from newtalk.chat.models import ChatMessage, Turn, format_user_message


@dataclass(frozen=True, slots=True)
class DialogueExchange:
    turn_id: str
    user_text: str
    assistant_text: str
    speaker_identity_id: str | None
    speaker_display_name: str
    speaker_relationship: str | None


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

    def restore(self, exchanges: Sequence[DialogueExchange]) -> None:
        if self._exchanges:
            raise ValueError("Dialogue history has already been initialized")
        seen: set[str] = set()
        for exchange in exchanges[-self.max_turns :]:
            if exchange.turn_id in seen:
                raise ValueError("Restored Dialogue contains duplicate turns")
            seen.add(exchange.turn_id)
            self._exchanges.append(exchange)

    def remove_identity(self, identity_id: str) -> None:
        self._exchanges = deque(
            (
                exchange
                for exchange in self._exchanges
                if exchange.speaker_identity_id != identity_id
            ),
            maxlen=self.max_turns,
        )

    def messages_for(
        self,
        user_text: str,
        *,
        speaker_identity_id: str | None = None,
        speaker_display_name: str = "Guest",
        speaker_relationship: str | None = None,
        context_messages: Sequence[ChatMessage] = (),
    ) -> tuple[ChatMessage, ...]:
        current = ChatMessage(
            role="user",
            content=format_user_message(
                user_text,
                speaker_identity_id=speaker_identity_id,
                speaker_display_name=speaker_display_name,
                speaker_relationship=speaker_relationship,
            ),
        )
        remaining_chars = max(0, self.max_chars - len(user_text))
        selected: list[DialogueExchange] = []

        for exchange in reversed(self._exchanges):
            exchange_chars = len(exchange.user_text) + len(exchange.assistant_text)
            if exchange_chars > remaining_chars:
                break
            selected.append(exchange)
            remaining_chars -= exchange_chars

        messages: list[ChatMessage] = list(context_messages)
        for exchange in reversed(selected):
            messages.extend(
                (
                    ChatMessage(
                        role="user",
                        content=format_user_message(
                            exchange.user_text,
                            speaker_identity_id=exchange.speaker_identity_id,
                            speaker_display_name=exchange.speaker_display_name,
                            speaker_relationship=exchange.speaker_relationship,
                        ),
                    ),
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
                speaker_identity_id=turn.speaker_identity_id,
                speaker_display_name=turn.speaker_display_name,
                speaker_relationship=turn.speaker_relationship,
            )
        )


class DialogueCacheCoordinator:
    """Removes deleted member exchanges from active Family Dialogue windows."""

    def __init__(self) -> None:
        self._sessions: WeakKeyDictionary[DialogueSession, str] = WeakKeyDictionary()

    def register(self, device_id: str, dialogue: DialogueSession) -> None:
        self._sessions[dialogue] = device_id

    def unregister(self, dialogue: DialogueSession) -> None:
        self._sessions.pop(dialogue, None)

    def remove_identity(self, device_id: str, identity_id: str) -> None:
        for dialogue, owner_device_id in tuple(self._sessions.items()):
            if owner_device_id == device_id:
                dialogue.remove_identity(identity_id)
