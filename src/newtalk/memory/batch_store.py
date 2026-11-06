from __future__ import annotations

import asyncio
from collections import defaultdict
from collections.abc import Callable
from copy import deepcopy
from dataclasses import asdict, dataclass, replace
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Integer,
    String,
    Text,
    delete,
    select,
    update,
)
from sqlalchemy.dialects.postgresql import JSONB, insert
from sqlalchemy.dialects.postgresql import UUID as PostgreSQLUUID
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.orm import Mapped, mapped_column

from newtalk.identity.models import IdentityStatus
from newtalk.identity.service import IdentityNotFoundError, IdentityService
from newtalk.identity.sqlalchemy_store import Base, IdentityRow


def UTCnow() -> datetime:
    return datetime.now(UTC)


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("Memory timestamps must be timezone-aware")
    return value.astimezone(UTC)


@dataclass(frozen=True, slots=True)
class MemorySource:
    turn_id: str
    device_id: str
    identity_id: str
    session_id: str
    segment_id: str
    speaker_name: str
    user_text: str
    chat_time: datetime
    assistant_text: str | None = None
    settled: bool = False
    context_only: bool = False

    def __post_init__(self) -> None:
        _utc(self.chat_time)


@dataclass(frozen=True, slots=True)
class MemoryBatch:
    batch_id: str
    device_id: str
    identity_id: str
    session_id: str
    segment_id: str
    speaker_name: str
    sources: tuple[MemorySource, ...]
    attempts: int = 0
    lease_token: str = ""
    prepared_payload: dict | None = None


class MemoryBatchRow(Base):
    __tablename__ = "memory_batches"
    __table_args__ = (
        CheckConstraint(
            "status IN ('pending', 'processing', 'queued', 'skipped', 'failed')",
            name="ck_memory_batches_status",
        ),
        CheckConstraint("attempts >= 0", name="ck_memory_batches_attempts"),
    )

    batch_id: Mapped[UUID] = mapped_column(
        PostgreSQLUUID(as_uuid=True), primary_key=True
    )
    device_id: Mapped[str] = mapped_column(
        ForeignKey("devices.device_id", ondelete="CASCADE"), index=True, nullable=False
    )
    identity_id: Mapped[UUID] = mapped_column(
        ForeignKey("identities.identity_id", ondelete="CASCADE"),
        index=True,
        nullable=False,
    )
    session_id: Mapped[str] = mapped_column(String(36), nullable=False)
    segment_id: Mapped[str] = mapped_column(String(160), nullable=False)
    speaker_name: Mapped[str] = mapped_column(String(80), nullable=False)
    sources: Mapped[list[dict]] = mapped_column(JSONB, nullable=False)
    prepared_payload: Mapped[dict | None] = mapped_column(JSONB)
    first_source_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    reason: Mapped[str] = mapped_column(String(160), nullable=False)
    status: Mapped[str] = mapped_column(String(20), default="pending", nullable=False)
    attempts: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    lease_token: Mapped[str] = mapped_column(String(36), default="", nullable=False)
    last_error: Mapped[str | None] = mapped_column(Text)
    available_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )


class MemorySourceRow(Base):
    __tablename__ = "memory_sources"

    source_id: Mapped[int] = mapped_column(
        BigInteger, primary_key=True, autoincrement=True
    )
    turn_id: Mapped[str] = mapped_column(String(36), unique=True, nullable=False)
    device_id: Mapped[str] = mapped_column(
        ForeignKey("devices.device_id", ondelete="CASCADE"), index=True, nullable=False
    )
    identity_id: Mapped[UUID] = mapped_column(
        ForeignKey("identities.identity_id", ondelete="CASCADE"),
        index=True,
        nullable=False,
    )
    session_id: Mapped[str] = mapped_column(String(36), nullable=False)
    segment_id: Mapped[str] = mapped_column(String(160), nullable=False)
    speaker_name: Mapped[str] = mapped_column(String(80), nullable=False)
    user_text: Mapped[str] = mapped_column(Text, nullable=False)
    assistant_text: Mapped[str | None] = mapped_column(Text)
    chat_time: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    settled: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    batch_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("memory_batches.batch_id", ondelete="CASCADE"), index=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )


def _source(row: MemorySourceRow) -> MemorySource:
    return MemorySource(
        **{
            name: str(getattr(row, name))
            if name == "identity_id"
            else getattr(row, name)
            for name in MemorySource.__dataclass_fields__
            if name != "context_only"
        }
    )


def _snapshot(source: MemorySource) -> dict:
    value = asdict(source)
    value["chat_time"] = _utc(source.chat_time).isoformat()
    return value


def _restore(value: dict) -> MemorySource:
    return MemorySource(
        **{**value, "chat_time": _utc(datetime.fromisoformat(value["chat_time"]))}
    )


def _batch(row: MemoryBatchRow) -> MemoryBatch:
    return MemoryBatch(
        batch_id=str(row.batch_id),
        device_id=row.device_id,
        identity_id=str(row.identity_id),
        session_id=row.session_id,
        segment_id=row.segment_id,
        speaker_name=row.speaker_name,
        sources=tuple(_restore(s) for s in row.sources),
        attempts=row.attempts,
        lease_token=row.lease_token,
        prepared_payload=deepcopy(row.prepared_payload),
    )


def _group(source: MemorySource | MemorySourceRow) -> tuple:
    return (
        source.device_id,
        source.identity_id,
        source.session_id,
        source.segment_id,
    )


def _due(sources, created_at, now, max_sources, max_age_seconds) -> bool:
    return all(s.settled for s in sources) and (
        len(sources) >= max_sources
        or now - min(created_at(s) for s in sources)
        >= timedelta(seconds=max_age_seconds)
    )


def _validate_due(max_sources: int, max_age_seconds: float) -> None:
    if max_sources < 1 or max_age_seconds < 0:
        raise ValueError("Batch threshold must be positive and age nonnegative")


class SqlAlchemyMemoryBatchStore:
    def __init__(
        self,
        database_url: str,
        lease_seconds: float = 120,
        clock: Callable[[], datetime] = UTCnow,
    ) -> None:
        self._engine = create_async_engine(database_url, pool_pre_ping=True)
        self._sessions = async_sessionmaker(self._engine, expire_on_commit=False)
        self._lease_seconds = lease_seconds
        self._clock = clock
        self._started = False

    async def start(self) -> None:
        if self._started:
            return
        async with self._sessions.begin() as session:
            # 仅单进程启动恢复未完成输入，不补造崩溃前未落库的助手回答。
            await session.execute(
                update(MemorySourceRow)
                .where(
                    MemorySourceRow.batch_id.is_(None),
                    MemorySourceRow.settled.is_(False),
                )
                .values(settled=True, assistant_text=None)
            )
        self._started = True

    async def _identity(self, session, device_id, identity_id, *, active=True):
        try:
            parsed = UUID(str(identity_id))
        except ValueError:
            return None
        query = select(IdentityRow).where(
            IdentityRow.device_id == device_id,
            IdentityRow.identity_id == parsed,
        )
        if active:
            query = query.where(IdentityRow.status == IdentityStatus.ACTIVE.value)
        # 所有双表写入先锁成员再锁素材/批次，与现有删除服务保持同序。
        return await session.scalar(query.with_for_update())

    async def append(self, source: MemorySource) -> bool:
        now = _utc(self._clock())
        async with self._sessions.begin() as session:
            if (
                await self._identity(session, source.device_id, source.identity_id)
                is None
            ):
                return False
            values = asdict(source)
            values.pop("context_only")
            values.update(
                identity_id=UUID(source.identity_id),
                chat_time=_utc(source.chat_time),
                created_at=now,
            )
            result = await session.execute(
                insert(MemorySourceRow)
                .values(**values)
                .on_conflict_do_nothing(index_elements=[MemorySourceRow.turn_id])
                .returning(MemorySourceRow.source_id)
            )
            return result.scalar_one_or_none() is not None

    async def finish(
        self, turn_id: str, assistant_text: str | None, discard: bool = False
    ) -> None:
        async with self._sessions.begin() as session:
            scope = (
                await session.execute(
                    select(
                        MemorySourceRow.device_id,
                        MemorySourceRow.identity_id,
                    ).where(MemorySourceRow.turn_id == turn_id)
                )
            ).first()
            if (
                scope is None
                or await self._identity(session, *scope, active=False) is None
            ):
                return
            statement = (
                delete(MemorySourceRow)
                if discard
                else update(MemorySourceRow).values(
                    assistant_text=assistant_text,
                    settled=True,
                )
            )
            await session.execute(
                statement.where(
                    MemorySourceRow.turn_id == turn_id,
                    MemorySourceRow.batch_id.is_(None),
                )
            )

    async def seal(self, *, device_id: str, segment_id: str, reason: str) -> int:
        return await self._seal(
            device_id=device_id, segment_id=segment_id, reason=reason
        )

    async def seal_due(
        self, *, max_sources: int = 8, max_age_seconds: float = 300
    ) -> int:
        _validate_due(max_sources, max_age_seconds)
        return await self._seal(reason="due", due=(max_sources, max_age_seconds))

    async def _seal(self, *, reason, device_id=None, segment_id=None, due=None) -> int:
        filters = [MemorySourceRow.batch_id.is_(None)]
        if device_id is not None:
            filters.append(MemorySourceRow.device_id == device_id)
        if segment_id is not None:
            filters.append(MemorySourceRow.segment_id == segment_id)
        async with self._sessions() as session:
            identities = (
                await session.execute(
                    select(
                        MemorySourceRow.device_id,
                        MemorySourceRow.identity_id,
                    )
                    .where(*filters)
                    .distinct()
                    .order_by(
                        MemorySourceRow.device_id,
                        MemorySourceRow.identity_id,
                    )
                )
            ).all()
        count = 0
        for device, identity in identities:
            async with self._sessions.begin() as session:
                if await self._identity(session, device, identity) is None:
                    continue
                rows = (
                    await session.scalars(
                        select(MemorySourceRow)
                        .where(
                            *filters,
                            MemorySourceRow.device_id == device,
                            MemorySourceRow.identity_id == identity,
                        )
                        .order_by(MemorySourceRow.source_id)
                        .with_for_update()
                    )
                ).all()
                groups = defaultdict(list)
                for row in rows:
                    groups[_group(row)].append(row)
                now = _utc(self._clock())
                for group in groups.values():
                    if due is not None and not _due(
                        group, lambda s: s.created_at, now, *due
                    ):
                        continue
                    settled = [s for s in group if s.settled]
                    for offset in range(0, len(settled), 8):
                        chunk = settled[offset : offset + 8]
                        head = chunk[0]
                        context = (
                            await session.scalars(
                                select(MemorySourceRow)
                                .where(
                                    MemorySourceRow.device_id == device,
                                    MemorySourceRow.identity_id == identity,
                                    MemorySourceRow.session_id == head.session_id,
                                    MemorySourceRow.segment_id == head.segment_id,
                                    MemorySourceRow.batch_id.is_not(None),
                                    MemorySourceRow.source_id < head.source_id,
                                )
                                .order_by(MemorySourceRow.source_id.desc())
                                .limit(2)
                            )
                        ).all()
                        snapshot = [
                            replace(_source(s), context_only=True)
                            for s in reversed(context)
                        ]
                        snapshot.extend(_source(s) for s in chunk)
                        batch_id = uuid4()
                        session.add(
                            MemoryBatchRow(
                                batch_id=batch_id,
                                device_id=device,
                                identity_id=identity,
                                session_id=head.session_id,
                                segment_id=head.segment_id,
                                speaker_name=head.speaker_name,
                                sources=[_snapshot(s) for s in snapshot],
                                first_source_id=head.source_id,
                                reason=reason,
                                status="pending",
                                attempts=0,
                                lease_token="",
                                available_at=now,
                                created_at=now,
                                updated_at=now,
                            )
                        )
                        await session.flush()
                        for row in chunk:
                            row.batch_id = batch_id
                        # 下一块可引用刚分配的前文，但不重写前文的原始批次归属。
                        await session.flush()
                        count += 1
        return count

    async def claim(self) -> MemoryBatch | None:
        now = _utc(self._clock())
        eligible = (
            MemoryBatchRow.status.in_(("pending", "processing")),
            MemoryBatchRow.available_at <= now,
        )
        async with self._sessions() as session:
            candidates = (
                await session.execute(
                    select(
                        MemoryBatchRow.batch_id,
                        MemoryBatchRow.device_id,
                        MemoryBatchRow.identity_id,
                    )
                    .where(
                        *eligible,
                        select(IdentityRow.identity_id)
                        .where(
                            IdentityRow.identity_id == MemoryBatchRow.identity_id,
                            IdentityRow.device_id == MemoryBatchRow.device_id,
                            IdentityRow.status == IdentityStatus.ACTIVE.value,
                        )
                        .exists(),
                    )
                    .order_by(MemoryBatchRow.created_at, MemoryBatchRow.first_source_id)
                )
            ).all()
        for batch_id, device, identity in candidates:
            async with self._sessions.begin() as session:
                if await self._identity(session, device, identity) is None:
                    continue
                row = await session.scalar(
                    select(MemoryBatchRow)
                    .where(
                        MemoryBatchRow.batch_id == batch_id,
                        *eligible,
                    )
                    .with_for_update(skip_locked=True)
                )
                if row is None:
                    continue
                row.status = "processing"
                row.attempts += 1
                row.lease_token = str(uuid4())
                row.available_at = now + timedelta(seconds=self._lease_seconds)
                row.updated_at = now
                await session.flush()
                return _batch(row)
        return None

    async def _change(self, batch: MemoryBatch, *, active=False, **values) -> bool:
        async with self._sessions.begin() as session:
            if (
                active
                and await self._identity(session, batch.device_id, batch.identity_id)
                is None
            ):
                return False
            # 新领取会更换 token，迟到 worker 不能覆盖新 worker 的结果。
            result = await session.execute(
                update(MemoryBatchRow)
                .where(
                    MemoryBatchRow.batch_id == UUID(batch.batch_id),
                    MemoryBatchRow.device_id == batch.device_id,
                    MemoryBatchRow.identity_id == UUID(batch.identity_id),
                    MemoryBatchRow.status == "processing",
                    MemoryBatchRow.lease_token == batch.lease_token,
                )
                .values(**values, updated_at=_utc(self._clock()))
            )
            return result.rowcount == 1

    async def save_prepared(self, batch: MemoryBatch, payload: dict) -> bool:
        return await self._change(
            batch, active=True, prepared_payload=deepcopy(payload)
        )

    async def mark_queued(self, batch: MemoryBatch) -> bool:
        return await self._change(batch, active=True, status="queued")

    async def skip(self, batch: MemoryBatch) -> bool:
        return await self._change(batch, status="skipped")

    async def fail(
        self, batch: MemoryBatch, *, error: str, retry_at: datetime | None
    ) -> None:
        await self._change(
            batch,
            status="pending" if retry_at is not None else "failed",
            last_error=error[:2000],
            available_at=_utc(retry_at if retry_at is not None else self._clock()),
        )

    async def delete_identity(self, *, device_id: str, identity_id: str) -> None:
        async with self._sessions.begin() as session:
            await self._identity(session, device_id, identity_id, active=False)
            for model in (MemorySourceRow, MemoryBatchRow):
                await session.execute(
                    delete(model).where(
                        model.device_id == device_id,
                        model.identity_id == UUID(identity_id),
                    )
                )

    async def close(self) -> None:
        await self._engine.dispose()
        self._started = False


class InMemoryMemoryBatchStore:
    def __init__(
        self,
        identity_service: IdentityService | None = None,
        clock: Callable[[], datetime] = UTCnow,
    ) -> None:
        self._identity_service = identity_service
        self._clock = clock
        self._lock = asyncio.Lock()
        self._started = False
        self.sources: dict[str, MemorySource] = {}
        self.batches: dict[str, MemoryBatch] = {}
        self.statuses: dict[str, str] = {}
        self.errors: dict[str, str] = {}
        self._created_at: dict[str, datetime] = {}
        self._assignments: dict[str, str] = {}
        self._available_at: dict[str, datetime] = {}
        self._deleted: set[tuple[str, str]] = set()

    async def _active(self, device_id: str, identity_id: str) -> bool:
        if (device_id, identity_id) in self._deleted:
            return False
        if self._identity_service is None:
            return True
        try:
            await self._identity_service.get_identity(
                device_id=device_id, identity_id=identity_id
            )
            return True
        except IdentityNotFoundError:
            return False

    async def start(self) -> None:
        async with self._lock:
            if self._started:
                return
            for turn_id, source in self.sources.items():
                if turn_id not in self._assignments and not source.settled:
                    self.sources[turn_id] = replace(
                        source, settled=True, assistant_text=None
                    )
            self._started = True

    async def append(self, source: MemorySource) -> bool:
        async with self._lock:
            if source.turn_id in self.sources or not await self._active(
                source.device_id, source.identity_id
            ):
                return False
            self.sources[source.turn_id] = replace(
                source, chat_time=_utc(source.chat_time), context_only=False
            )
            self._created_at[source.turn_id] = _utc(self._clock())
            return True

    async def finish(
        self, turn_id: str, assistant_text: str | None, discard: bool = False
    ) -> None:
        async with self._lock:
            if turn_id in self._assignments or turn_id not in self.sources:
                return
            if discard:
                del self.sources[turn_id]
                del self._created_at[turn_id]
            else:
                self.sources[turn_id] = replace(
                    self.sources[turn_id], assistant_text=assistant_text, settled=True
                )

    async def seal(self, *, device_id: str, segment_id: str, reason: str) -> int:
        return await self._seal(device_id=device_id, segment_id=segment_id)

    async def seal_due(
        self, *, max_sources: int = 8, max_age_seconds: float = 300
    ) -> int:
        _validate_due(max_sources, max_age_seconds)
        return await self._seal(due=(max_sources, max_age_seconds))

    async def _seal(self, *, device_id=None, segment_id=None, due=None) -> int:
        async with self._lock:
            groups = defaultdict(list)
            for source in self.sources.values():
                if source.turn_id in self._assignments:
                    continue
                if device_id is not None and source.device_id != device_id:
                    continue
                if segment_id is not None and source.segment_id != segment_id:
                    continue
                groups[_group(source)].append(source)
            count = 0
            now = _utc(self._clock())
            for group in groups.values():
                head = group[0]
                if not await self._active(head.device_id, head.identity_id):
                    continue
                if due is not None and not _due(
                    group, lambda s: self._created_at[s.turn_id], now, *due
                ):
                    continue
                settled = [s for s in group if s.settled]
                for offset in range(0, len(settled), 8):
                    chunk = settled[offset : offset + 8]
                    head = chunk[0]
                    context = []
                    for source in self.sources.values():
                        if source.turn_id == head.turn_id:
                            break
                        if source.turn_id in self._assignments and _group(
                            source
                        ) == _group(head):
                            context.append(replace(source, context_only=True))
                    batch_id = str(uuid4())
                    self.batches[batch_id] = MemoryBatch(
                        batch_id=batch_id,
                        device_id=head.device_id,
                        identity_id=head.identity_id,
                        session_id=head.session_id,
                        segment_id=head.segment_id,
                        speaker_name=head.speaker_name,
                        sources=tuple(context[-2:] + chunk),
                    )
                    self.statuses[batch_id] = "pending"
                    self._available_at[batch_id] = now
                    for source in chunk:
                        self._assignments[source.turn_id] = batch_id
                    count += 1
            return count

    async def claim(self) -> MemoryBatch | None:
        async with self._lock:
            now = _utc(self._clock())
            for batch_id, batch in self.batches.items():
                if self.statuses[batch_id] not in {"pending", "processing"}:
                    continue
                if self._available_at[batch_id] > now or not await self._active(
                    batch.device_id, batch.identity_id
                ):
                    continue
                claimed = replace(
                    batch, attempts=batch.attempts + 1, lease_token=str(uuid4())
                )
                self.batches[batch_id] = claimed
                self.statuses[batch_id] = "processing"
                self._available_at[batch_id] = now + timedelta(seconds=120)
                return deepcopy(claimed)
            return None

    async def _change(
        self,
        batch,
        *,
        active=False,
        payload=None,
        status=None,
        error=None,
        retry_at=None,
    ):
        async with self._lock:
            current = self.batches.get(batch.batch_id)
            if (
                current is None
                or self.statuses[batch.batch_id] != "processing"
                or current.lease_token != batch.lease_token
                or (current.device_id, current.identity_id)
                != (batch.device_id, batch.identity_id)
            ):
                return False
            if active and not await self._active(batch.device_id, batch.identity_id):
                return False
            if payload is not None:
                self.batches[batch.batch_id] = replace(
                    current, prepared_payload=deepcopy(payload)
                )
            if status is not None:
                self.statuses[batch.batch_id] = status
            if error is not None:
                self.errors[batch.batch_id] = error[:2000]
            if retry_at is not None:
                self._available_at[batch.batch_id] = _utc(retry_at)
            return True

    async def save_prepared(self, batch: MemoryBatch, payload: dict) -> bool:
        return await self._change(batch, active=True, payload=payload)

    async def mark_queued(self, batch: MemoryBatch) -> bool:
        return await self._change(batch, active=True, status="queued")

    async def skip(self, batch: MemoryBatch) -> bool:
        return await self._change(batch, status="skipped")

    async def fail(
        self, batch: MemoryBatch, *, error: str, retry_at: datetime | None
    ) -> None:
        await self._change(
            batch,
            status="pending" if retry_at is not None else "failed",
            error=error,
            retry_at=_utc(retry_at) if retry_at is not None else None,
        )

    async def delete_identity(self, *, device_id: str, identity_id: str) -> None:
        async with self._lock:
            self._deleted.add((device_id, identity_id))
            for turn_id, source in tuple(self.sources.items()):
                if (source.device_id, source.identity_id) == (device_id, identity_id):
                    del self.sources[turn_id]
                    self._created_at.pop(turn_id, None)
                    self._assignments.pop(turn_id, None)
            for batch_id, batch in tuple(self.batches.items()):
                if (batch.device_id, batch.identity_id) == (device_id, identity_id):
                    del self.batches[batch_id]
                    self.statuses.pop(batch_id, None)
                    self.errors.pop(batch_id, None)
                    self._available_at.pop(batch_id, None)

    async def close(self) -> None:
        self._started = False
