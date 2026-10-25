import asyncio
from collections import deque
from collections.abc import Sequence
from dataclasses import dataclass, field, replace
from weakref import WeakKeyDictionary, WeakValueDictionary

from newtalk.chat.models import ChatMessage, Turn, format_user_message


@dataclass(frozen=True, slots=True)
class DialogueExchange:
    turn_id: str
    user_text: str
    assistant_text: str  # 空串表示只接受了用户输入，不表示助手给过空回答。
    speaker_identity_id: str | None
    speaker_display_name: str
    speaker_relationship: str | None


@dataclass(slots=True, weakref_slot=True)
class DialogueWriteScope:
    """同进程同设备的写入顺序；清理后旧输入任务不得重新落库。"""

    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    revision: int = 0
    removed_identities: set[str] = field(default_factory=set)

    def permits(self, revision: int, identity_id: str | None) -> bool:
        return self.revision == revision and identity_id not in self.removed_identities


class DialogueSession:
    """Accepted user inputs and complete replies in a bounded session window."""

    def __init__(
        self,
        session_id: str,
        *,
        max_turns: int,
    ) -> None:
        if max_turns <= 0:
            raise ValueError("Dialogue window limits must be positive")
        self.session_id = session_id
        self.max_turns = max_turns
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

    def clear(self) -> None:
        self._exchanges.clear()

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
        messages: list[ChatMessage] = list(context_messages)
        for exchange in self._exchanges:
            messages.append(ChatMessage(
                role="user",
                content=format_user_message(
                    exchange.user_text,
                    speaker_identity_id=exchange.speaker_identity_id,
                    speaker_display_name=exchange.speaker_display_name,
                    speaker_relationship=exchange.speaker_relationship,
                ),
                source_turn_id=exchange.turn_id,
            ))
            # 保留连续的 user 消息，而不是伪造一个空助手回答或半截已交付回答。
            if exchange.assistant_text:
                messages.append(ChatMessage(
                    role="assistant", content=exchange.assistant_text,
                    source_turn_id=exchange.turn_id,
                ))
        messages.append(current)
        return tuple(messages)

    def accept_user_input(self, turn: Turn) -> bool:
        if turn.session_id != self.session_id:
            raise ValueError("Turn belongs to a different session")
        if any(exchange.turn_id == turn.turn_id for exchange in self._exchanges):
            return False
        self._exchanges.append(DialogueExchange(
            turn_id=turn.turn_id, user_text=turn.user_text, assistant_text="",
            speaker_identity_id=turn.speaker_identity_id,
            speaker_display_name=turn.speaker_display_name,
            speaker_relationship=turn.speaker_relationship,
        ))
        return True

    def commit(self, turn: Turn, assistant_text: str) -> None:
        if turn.session_id != self.session_id:
            raise ValueError("Turn belongs to a different session")
        if not assistant_text:
            raise ValueError("Completed assistant text must not be empty")
        for index, exchange in enumerate(self._exchanges):
            if exchange.turn_id == turn.turn_id:
                if exchange.assistant_text:
                    raise ValueError("Turn has already been committed")
                self._exchanges[index] = replace(exchange, assistant_text=assistant_text)
                return
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
    """Keeps active Dialogue windows consistent with persistent mutations."""

    def __init__(self) -> None:
        self._sessions: WeakKeyDictionary[
            DialogueSession, tuple[str, str]
        ] = WeakKeyDictionary()
        self._write_scopes: WeakValueDictionary[str, DialogueWriteScope] = WeakValueDictionary()

    def write_scope(self, device_id: str) -> DialogueWriteScope:
        scope = self._write_scopes.get(device_id)
        if scope is None:
            scope = DialogueWriteScope()
            self._write_scopes[device_id] = scope
        return scope

    def register(self, device_id: str, lane: str, dialogue: DialogueSession) -> None:
        # 新连接可能早于独立落库任务完成；先继承同进程活跃窗口，避免瞬时漏掉输入。
        for active, (owner_device_id, _) in tuple(self._sessions.items()):
            if owner_device_id == device_id:
                dialogue.clear()
                dialogue.restore(active.exchanges)
                break
        self._sessions[dialogue] = (device_id, lane)

    def unregister(self, dialogue: DialogueSession) -> None:
        self._sessions.pop(dialogue, None)

    def accept_user_input(self, device_id: str, turn: Turn) -> None:
        for dialogue, (owner_device_id, _) in tuple(self._sessions.items()):
            if owner_device_id == device_id:
                dialogue.accept_user_input(turn)

    def commit(self, device_id: str, turn: Turn, assistant_text: str) -> bool:
        committed = False
        for dialogue, (owner_device_id, _) in tuple(self._sessions.items()):
            if owner_device_id == device_id and any(
                item.turn_id == turn.turn_id for item in dialogue.exchanges
            ):
                dialogue.commit(turn, assistant_text)
                committed = True
        return committed

    def remove_identity(self, device_id: str, identity_id: str) -> None:
        self.write_scope(device_id).removed_identities.add(identity_id)
        for dialogue, (owner_device_id, lane) in tuple(self._sessions.items()):
            if owner_device_id == device_id and lane == "family":
                dialogue.remove_identity(identity_id)

    def clear(self, device_id: str, lane: str | None = None) -> None:
        self.write_scope(device_id).revision += 1
        for dialogue, (owner_device_id, owner_lane) in tuple(self._sessions.items()):
            if owner_device_id == device_id and (
                lane is None or owner_lane == lane
            ):
                dialogue.clear()
