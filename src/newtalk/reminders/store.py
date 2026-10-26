import asyncio
from dataclasses import asdict, replace
from datetime import datetime
from typing import Protocol

from sqlalchemy import (
    DateTime,
    ForeignKey,
    Index,
    String,
    UniqueConstraint,
    or_,
    select,
    update,
)
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.orm import Mapped, mapped_column

from newtalk.identity.sqlalchemy_store import Base
from newtalk.reminders.models import PENDING_DELIVERIES, Reminder, claim


class ReminderRow(Base):
    __tablename__ = "reminders"
    __table_args__ = (
        UniqueConstraint("device_id", "request_key", name="uq_reminders_device_request"),
        Index("ix_reminders_status_due", "status", "due_at"),
        Index("ix_reminders_device_due", "device_id", "due_at"),
    )
    reminder_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    device_id: Mapped[str] = mapped_column(ForeignKey("devices.device_id", ondelete="CASCADE"))
    content: Mapped[str] = mapped_column(String(160))
    due_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    request_key: Mapped[str] = mapped_column(String(128))
    status: Mapped[str] = mapped_column(String(16))
    delivery_id: Mapped[str | None] = mapped_column(String(36))
    delivery_status: Mapped[str | None] = mapped_column(String(16))
    connection_id: Mapped[str | None] = mapped_column(String(36))
    delivery_deadline: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


def _item(row: ReminderRow) -> Reminder:
    return Reminder(**{key: getattr(row, key) for key in Reminder.__dataclass_fields__})


class ReminderStore(Protocol):
    async def start(self) -> None: ...
    async def close(self) -> None: ...
    async def create(self, item: Reminder) -> Reminder: ...
    async def get(self, device_id: str, reminder_id: str) -> Reminder | None: ...
    async def list(self, device_id: str) -> tuple[Reminder, ...]: ...
    async def change(self, device_id: str, reminder_id: str, expected: dict, changes: dict) -> Reminder | None: ...
    async def claim_due(self, now: datetime, connections: dict[str, str]) -> tuple[Reminder, ...]: ...
    async def expire(self, now: datetime, *, connection_id: str | None = None, recover: bool = False) -> tuple[Reminder, ...]: ...


class InMemoryReminderStore:
    def __init__(self) -> None:
        self.items: dict[str, Reminder] = {}
        self._lock = asyncio.Lock()

    async def start(self) -> None:
        pass

    async def close(self) -> None:
        pass

    async def create(self, item: Reminder) -> Reminder:
        async with self._lock:
            for previous in self.items.values():
                if (previous.device_id, previous.request_key) == (item.device_id, item.request_key):
                    return previous
            self.items[item.reminder_id] = item
            return item

    async def get(self, device_id: str, reminder_id: str) -> Reminder | None:
        item = self.items.get(reminder_id)
        return item if item and item.device_id == device_id else None

    async def list(self, device_id: str) -> tuple[Reminder, ...]:
        return tuple(sorted((item for item in self.items.values() if item.device_id == device_id),
                            key=lambda item: item.due_at, reverse=True)[:200])

    async def change(self, device_id: str, reminder_id: str, expected: dict, changes: dict) -> Reminder | None:
        async with self._lock:
            item = await self.get(device_id, reminder_id)
            if item is None or any(getattr(item, key) not in values for key, values in expected.items()):
                return None
            item = replace(item, **changes)
            self.items[reminder_id] = item
            return item

    async def claim_due(self, now: datetime, connections: dict[str, str]) -> tuple[Reminder, ...]:
        async with self._lock:
            due = sorted((item for item in self.items.values()
                          if item.status == "scheduled" and item.due_at <= now), key=lambda item: item.due_at)[:50]
            result = tuple(claim(item, now, connections) for item in due)
            self.items.update({item.reminder_id: item for item in result})
            return result

    async def expire(self, now: datetime, *, connection_id: str | None = None, recover: bool = False) -> tuple[Reminder, ...]:
        expired = []
        async with self._lock:
            for key, item in tuple(self.items.items()):
                if item.delivery_status in PENDING_DELIVERIES and (
                    recover or (connection_id is not None and item.connection_id == connection_id)
                    or (item.delivery_deadline is not None and item.delivery_deadline <= now)
                ):
                    self.items[key] = replace(item, delivery_status="unknown")
                    expired.append(self.items[key])
        return tuple(expired)


class SqlAlchemyReminderStore:
    def __init__(self, database_url: str) -> None:
        self.engine = create_async_engine(database_url)
        self.sessions = async_sessionmaker(self.engine, expire_on_commit=False)

    async def start(self) -> None:
        async with self.sessions() as session:
            await session.execute(select(ReminderRow.reminder_id).limit(1))

    async def close(self) -> None:
        await self.engine.dispose()

    async def create(self, item: Reminder) -> Reminder:
        async with self.sessions.begin() as session:
            await session.execute(insert(ReminderRow).values(**asdict(item)).on_conflict_do_nothing(
                constraint="uq_reminders_device_request",
            ))
            row = (await session.execute(select(ReminderRow).where(
                ReminderRow.device_id == item.device_id, ReminderRow.request_key == item.request_key,
            ))).scalar_one()
            return _item(row)

    async def get(self, device_id: str, reminder_id: str) -> Reminder | None:
        async with self.sessions() as session:
            row = (await session.execute(select(ReminderRow).where(
                ReminderRow.device_id == device_id, ReminderRow.reminder_id == reminder_id,
            ))).scalar_one_or_none()
            return _item(row) if row is not None else None

    async def list(self, device_id: str) -> tuple[Reminder, ...]:
        async with self.sessions() as session:
            rows = (await session.execute(select(ReminderRow).where(
                ReminderRow.device_id == device_id,
            ).order_by(ReminderRow.due_at.desc()).limit(200))).scalars()
            return tuple(_item(row) for row in rows)

    async def change(self, device_id: str, reminder_id: str, expected: dict, changes: dict) -> Reminder | None:
        async with self.sessions.begin() as session:
            statement = update(ReminderRow).where(
                ReminderRow.device_id == device_id, ReminderRow.reminder_id == reminder_id,
                *(getattr(ReminderRow, key).in_(values) for key, values in expected.items()),
            ).values(**changes).returning(ReminderRow)
            row = (await session.execute(statement)).scalar_one_or_none()
            return _item(row) if row is not None else None

    async def claim_due(self, now: datetime, connections: dict[str, str]) -> tuple[Reminder, ...]:
        async with self.sessions.begin() as session:
            rows = (await session.execute(select(ReminderRow).where(
                ReminderRow.status == "scheduled", ReminderRow.due_at <= now,
            ).order_by(ReminderRow.due_at).limit(50).with_for_update(skip_locked=True))).scalars()
            result = []
            for row in rows:
                item = claim(_item(row), now, connections)
                for key, value in asdict(item).items():
                    setattr(row, key, value)
                result.append(item)
            return tuple(result)

    async def expire(self, now: datetime, *, connection_id: str | None = None, recover: bool = False) -> tuple[Reminder, ...]:
        conditions = [ReminderRow.delivery_deadline <= now]
        if connection_id is not None:
            conditions.append(ReminderRow.connection_id == connection_id)
        async with self.sessions.begin() as session:
            statement = update(ReminderRow).where(ReminderRow.delivery_status.in_(PENDING_DELIVERIES))
            if not recover:
                statement = statement.where(or_(*conditions))
            rows = (await session.execute(statement.values(delivery_status="unknown").returning(ReminderRow))).scalars()
            return tuple(_item(row) for row in rows)
