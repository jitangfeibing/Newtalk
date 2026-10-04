from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Literal, Protocol
from uuid import UUID, uuid4

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    String,
    Text,
    delete,
    func,
    select,
    update,
)
from sqlalchemy.dialects.postgresql import UUID as PostgreSQLUUID
from sqlalchemy.dialects.postgresql import insert as postgresql_insert
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import Mapped, mapped_column

from newtalk.chat.session import DialogueExchange
from newtalk.identity.sqlalchemy_store import Base


DialogueLane = Literal["family", "guest"]


@dataclass(frozen=True, slots=True)
class PersistedDialogueExchange:
    lane: DialogueLane
    exchange: DialogueExchange
    created_at: datetime


@dataclass(frozen=True, slots=True)
class DialogueSnapshot:
    session_id: str
    resumed: bool
    family: tuple[DialogueExchange, ...]
    guest: tuple[DialogueExchange, ...]
    history: tuple[PersistedDialogueExchange, ...]


class DialogueStore(Protocol):
    async def start(self) -> None: ...

    async def open(self, *, device_id: str, max_turns: int) -> DialogueSnapshot: ...

    async def append(
        self,
        *,
        session_id: str,
        lane: DialogueLane,
        exchange: DialogueExchange,
        max_turns: int,
    ) -> bool: ...

    async def delete_identity(self, *, device_id: str, identity_id: str) -> None: ...

    async def close(self) -> None: ...


class DialogueSessionRow(Base):
    __tablename__ = "dialogue_sessions"

    session_id: Mapped[UUID] = mapped_column(
        PostgreSQLUUID(as_uuid=True), primary_key=True, default=uuid4
    )
    device_id: Mapped[str] = mapped_column(
        ForeignKey("devices.device_id", ondelete="CASCADE"),
        unique=True,
        index=True,
        nullable=False,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class DialogueExchangeRow(Base):
    __tablename__ = "dialogue_exchanges"
    __table_args__ = (
        CheckConstraint("lane IN ('family', 'guest')", name="ck_dialogue_exchanges_lane"),
    )

    exchange_id: Mapped[UUID] = mapped_column(
        PostgreSQLUUID(as_uuid=True), primary_key=True, default=uuid4
    )
    session_id: Mapped[UUID] = mapped_column(
        ForeignKey("dialogue_sessions.session_id", ondelete="CASCADE"),
        index=True,
        nullable=False,
    )
    turn_id: Mapped[str] = mapped_column(String(36), unique=True, nullable=False)
    lane: Mapped[str] = mapped_column(String(12), nullable=False)
    user_text: Mapped[str] = mapped_column(Text, nullable=False)
    assistant_text: Mapped[str] = mapped_column(Text, nullable=False)
    speaker_identity_id: Mapped[str | None] = mapped_column(String(36))
    speaker_display_name: Mapped[str] = mapped_column(String(80), nullable=False)
    speaker_relationship: Mapped[str | None] = mapped_column(String(80))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


def _exchange(row: DialogueExchangeRow) -> DialogueExchange:
    return DialogueExchange(
        turn_id=row.turn_id,
        user_text=row.user_text,
        assistant_text=row.assistant_text,
        speaker_identity_id=row.speaker_identity_id,
        speaker_display_name=row.speaker_display_name,
        speaker_relationship=row.speaker_relationship,
    )


class SqlAlchemyDialogueStore:
    def __init__(self, database_url: str) -> None:
        self._engine: AsyncEngine = create_async_engine(database_url, pool_pre_ping=True)
        self._sessions = async_sessionmaker(
            self._engine,
            class_=AsyncSession,
            expire_on_commit=False,
        )

    async def start(self) -> None:
        async with self._sessions() as session:
            await session.execute(select(DialogueSessionRow.session_id).limit(1))

    async def open(self, *, device_id: str, max_turns: int) -> DialogueSnapshot:
        async with self._sessions.begin() as session:
            await session.execute(
                postgresql_insert(DialogueSessionRow)
                .values(device_id=device_id)
                .on_conflict_do_nothing(index_elements=[DialogueSessionRow.device_id])
            )
            dialogue = await session.scalar(
                select(DialogueSessionRow).where(DialogueSessionRow.device_id == device_id)
            )
            if dialogue is None:
                raise RuntimeError("Unable to open Dialogue Session")
            family = await self._load_lane(session, dialogue.session_id, "family", max_turns)
            guest = await self._load_lane(session, dialogue.session_id, "guest", max_turns)
            history = tuple(sorted((*family, *guest), key=lambda item: item.created_at))
            return DialogueSnapshot(
                session_id=str(dialogue.session_id),
                resumed=bool(history),
                family=tuple(item.exchange for item in family),
                guest=tuple(item.exchange for item in guest),
                history=history,
            )

    async def _load_lane(
        self,
        session: AsyncSession,
        session_id: UUID,
        lane: DialogueLane,
        max_turns: int,
    ) -> tuple[PersistedDialogueExchange, ...]:
        rows = (
            await session.scalars(
                select(DialogueExchangeRow)
                .where(
                    DialogueExchangeRow.session_id == session_id,
                    DialogueExchangeRow.lane == lane,
                )
                .order_by(DialogueExchangeRow.created_at.desc())
                .limit(max_turns)
            )
        ).all()
        return tuple(
            PersistedDialogueExchange(lane, _exchange(row), row.created_at)
            for row in reversed(rows)
        )

    async def append(
        self,
        *,
        session_id: str,
        lane: DialogueLane,
        exchange: DialogueExchange,
        max_turns: int,
    ) -> bool:
        parsed_session_id = UUID(session_id)
        async with self._sessions.begin() as session:
            dialogue = await session.scalar(
                select(DialogueSessionRow)
                .where(DialogueSessionRow.session_id == parsed_session_id)
                .with_for_update()
            )
            if dialogue is None:
                raise RuntimeError("Dialogue Session does not exist")
            result = await session.execute(
                postgresql_insert(DialogueExchangeRow)
                .values(
                    session_id=parsed_session_id,
                    turn_id=exchange.turn_id,
                    lane=lane,
                    user_text=exchange.user_text,
                    assistant_text=exchange.assistant_text,
                    speaker_identity_id=exchange.speaker_identity_id,
                    speaker_display_name=exchange.speaker_display_name,
                    speaker_relationship=exchange.speaker_relationship,
                )
                .on_conflict_do_nothing(index_elements=[DialogueExchangeRow.turn_id])
                .returning(DialogueExchangeRow.exchange_id)
            )
            inserted = result.scalar_one_or_none() is not None
            if inserted:
                stale = (
                    select(DialogueExchangeRow.exchange_id)
                    .where(
                        DialogueExchangeRow.session_id == parsed_session_id,
                        DialogueExchangeRow.lane == lane,
                    )
                    .order_by(DialogueExchangeRow.created_at.desc())
                    .offset(max_turns)
                )
                await session.execute(
                    delete(DialogueExchangeRow).where(
                        DialogueExchangeRow.exchange_id.in_(stale)
                    )
                )
                await session.execute(
                    update(DialogueSessionRow)
                    .where(DialogueSessionRow.session_id == parsed_session_id)
                    .values(updated_at=func.now())
                )
            return inserted

    async def close(self) -> None:
        await self._engine.dispose()

    async def delete_identity(self, *, device_id: str, identity_id: str) -> None:
        session_id = select(DialogueSessionRow.session_id).where(
            DialogueSessionRow.device_id == device_id
        )
        async with self._sessions.begin() as session:
            await session.execute(
                delete(DialogueExchangeRow).where(
                    DialogueExchangeRow.session_id.in_(session_id),
                    DialogueExchangeRow.speaker_identity_id == identity_id,
                )
            )


class InMemoryDialogueStore:
    def __init__(self) -> None:
        self._session_by_device: dict[str, str] = {}
        self._history: dict[str, list[PersistedDialogueExchange]] = {}

    async def start(self) -> None:
        return None

    async def open(self, *, device_id: str, max_turns: int) -> DialogueSnapshot:
        session_id = self._session_by_device.setdefault(device_id, str(uuid4()))
        history = self._history.setdefault(session_id, [])
        family = [item for item in history if item.lane == "family"][-max_turns:]
        guest = [item for item in history if item.lane == "guest"][-max_turns:]
        selected_ids = {id(item) for item in (*family, *guest)}
        visible = tuple(item for item in history if id(item) in selected_ids)
        return DialogueSnapshot(
            session_id=session_id,
            resumed=bool(visible),
            family=tuple(item.exchange for item in family),
            guest=tuple(item.exchange for item in guest),
            history=visible,
        )

    async def append(
        self,
        *,
        session_id: str,
        lane: DialogueLane,
        exchange: DialogueExchange,
        max_turns: int,
    ) -> bool:
        history = self._history.setdefault(session_id, [])
        if any(item.exchange.turn_id == exchange.turn_id for item in history):
            return False
        history.append(
            PersistedDialogueExchange(lane, exchange, datetime.now(timezone.utc))
        )
        lane_items = [item for item in history if item.lane == lane]
        stale_ids = {id(item) for item in lane_items[:-max_turns]}
        if stale_ids:
            self._history[session_id] = [
                item for item in history if id(item) not in stale_ids
            ]
        return True

    async def close(self) -> None:
        return None

    async def delete_identity(self, *, device_id: str, identity_id: str) -> None:
        session_id = self._session_by_device.get(device_id)
        if session_id is None:
            return
        self._history[session_id] = [
            item
            for item in self._history.get(session_id, [])
            if item.exchange.speaker_identity_id != identity_id
        ]
