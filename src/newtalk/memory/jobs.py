from __future__ import annotations

import asyncio
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
import logging
from typing import TYPE_CHECKING, Protocol
from uuid import UUID, uuid4

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    Integer,
    String,
    Text,
    and_,
    func,
    or_,
    select,
    update,
)
from sqlalchemy.dialects.postgresql import UUID as PostgreSQLUUID
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import Mapped, mapped_column

from newtalk.identity.sqlalchemy_store import Base
from newtalk.memory.provider import MemoryProvider
from newtalk.profile.models import ProfileScope


if TYPE_CHECKING:
    from newtalk.chat.models import Turn


logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class MemoryWriteJob:
    job_id: str
    turn_id: str
    device_id: str
    identity_id: str
    session_id: str
    speaker_name: str
    user_text: str
    assistant_text: str
    attempts: int = 0
    provider_task_id: str | None = None


class MemoryJobStore(Protocol):
    async def start(self) -> None: ...

    async def enqueue(self, job: MemoryWriteJob) -> bool: ...

    async def claim(self) -> MemoryWriteJob | None: ...

    async def submit(
        self,
        job_id: str,
        provider_task_id: str,
        *,
        check_at: datetime,
        error: str | None = None,
    ) -> None: ...

    async def complete(self, job_id: str, provider_task_id: str | None) -> None: ...

    async def fail(
        self,
        job_id: str,
        *,
        error: str,
        retry_at: datetime | None,
    ) -> None: ...

    async def close(self) -> None: ...


class MemoryWriter(Protocol):
    async def start(self) -> None: ...

    async def enqueue_completed_turn(self, turn: Turn, assistant_text: str) -> bool: ...

    async def close(self) -> None: ...


class MemoryJobRow(Base):
    __tablename__ = "memory_jobs"
    __table_args__ = (
        CheckConstraint(
            "status IN ('pending', 'processing', 'submitted', 'completed', 'failed')",
            name="ck_memory_jobs_status",
        ),
    )

    job_id: Mapped[UUID] = mapped_column(
        PostgreSQLUUID(as_uuid=True), primary_key=True, default=uuid4
    )
    turn_id: Mapped[str] = mapped_column(String(36), unique=True, nullable=False)
    device_id: Mapped[str] = mapped_column(
        ForeignKey("devices.device_id", ondelete="CASCADE"),
        index=True,
        nullable=False,
    )
    identity_id: Mapped[UUID] = mapped_column(
        ForeignKey("identities.identity_id", ondelete="CASCADE"),
        index=True,
        nullable=False,
    )
    session_id: Mapped[str] = mapped_column(String(36), nullable=False)
    speaker_name: Mapped[str] = mapped_column(String(80), nullable=False)
    user_text: Mapped[str] = mapped_column(Text, nullable=False)
    assistant_text: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(String(20), default="pending", nullable=False)
    attempts: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    provider_task_id: Mapped[str | None] = mapped_column(String(160))
    last_error: Mapped[str | None] = mapped_column(Text)
    available_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


def _job(row: MemoryJobRow) -> MemoryWriteJob:
    return MemoryWriteJob(
        job_id=str(row.job_id),
        turn_id=row.turn_id,
        device_id=row.device_id,
        identity_id=str(row.identity_id),
        session_id=row.session_id,
        speaker_name=row.speaker_name,
        user_text=row.user_text,
        assistant_text=row.assistant_text,
        attempts=row.attempts,
        provider_task_id=row.provider_task_id,
    )


class SqlAlchemyMemoryJobStore:
    def __init__(self, database_url: str, *, lease_seconds: float = 120.0) -> None:
        self._engine: AsyncEngine = create_async_engine(
            database_url,
            pool_pre_ping=True,
        )
        self._lease_seconds = lease_seconds
        self._sessions = async_sessionmaker(
            self._engine,
            class_=AsyncSession,
            expire_on_commit=False,
        )

    async def start(self) -> None:
        async with self._sessions.begin() as session:
            await session.execute(select(MemoryJobRow.job_id).limit(1))

    async def enqueue(self, job: MemoryWriteJob) -> bool:
        async with self._sessions() as session:
            session.add(
                MemoryJobRow(
                    job_id=UUID(job.job_id),
                    turn_id=job.turn_id,
                    device_id=job.device_id,
                    identity_id=UUID(job.identity_id),
                    session_id=job.session_id,
                    speaker_name=job.speaker_name,
                    user_text=job.user_text,
                    assistant_text=job.assistant_text,
                )
            )
            try:
                await session.commit()
                return True
            except IntegrityError:
                await session.rollback()
                return False

    async def claim(self) -> MemoryWriteJob | None:
        now = datetime.now(timezone.utc)
        lease_until = now + timedelta(seconds=self._lease_seconds)
        async with self._sessions.begin() as session:
            row = await session.scalar(
                select(MemoryJobRow)
                .where(
                    or_(
                        and_(
                            MemoryJobRow.status == "pending",
                            MemoryJobRow.available_at <= now,
                        ),
                        and_(
                            MemoryJobRow.status == "submitted",
                            MemoryJobRow.available_at <= now,
                        ),
                        and_(
                            MemoryJobRow.status == "processing",
                            MemoryJobRow.available_at <= now,
                        ),
                    )
                )
                .order_by(MemoryJobRow.created_at)
                .with_for_update(skip_locked=True)
                .limit(1)
            )
            if row is None:
                return None
            row.status = "processing"
            if row.provider_task_id is None:
                row.attempts += 1
            row.available_at = lease_until
            row.updated_at = now
            await session.flush()
            return _job(row)

    async def submit(
        self,
        job_id: str,
        provider_task_id: str,
        *,
        check_at: datetime,
        error: str | None = None,
    ) -> None:
        await self._set_result(
            job_id,
            status="submitted",
            provider_task_id=provider_task_id,
            last_error=error[:2000] if error else None,
            available_at=check_at,
        )

    async def complete(self, job_id: str, provider_task_id: str | None) -> None:
        await self._set_result(
            job_id,
            status="completed",
            provider_task_id=provider_task_id,
            last_error=None,
            available_at=datetime.now(timezone.utc),
        )

    async def fail(
        self,
        job_id: str,
        *,
        error: str,
        retry_at: datetime | None,
    ) -> None:
        await self._set_result(
            job_id,
            status="pending" if retry_at is not None else "failed",
            provider_task_id=None,
            last_error=error[:2000],
            available_at=retry_at or datetime.now(timezone.utc),
        )

    async def _set_result(self, job_id: str, **values: object) -> None:
        async with self._sessions.begin() as session:
            await session.execute(
                update(MemoryJobRow)
                .where(MemoryJobRow.job_id == UUID(job_id))
                .values(**values, updated_at=datetime.now(timezone.utc))
            )

    async def close(self) -> None:
        await self._engine.dispose()


class InMemoryMemoryJobStore:
    def __init__(self) -> None:
        self.jobs: dict[str, MemoryWriteJob] = {}
        self.statuses: dict[str, str] = {}
        self.retry_at: dict[str, datetime] = {}
        self.provider_task_ids: dict[str, str | None] = {}
        self.errors: dict[str, str] = {}

    async def start(self) -> None:
        for job_id, status in tuple(self.statuses.items()):
            if status == "processing":
                self.statuses[job_id] = (
                    "submitted" if self.provider_task_ids.get(job_id) else "pending"
                )

    async def enqueue(self, job: MemoryWriteJob) -> bool:
        if any(existing.turn_id == job.turn_id for existing in self.jobs.values()):
            return False
        self.jobs[job.job_id] = job
        self.statuses[job.job_id] = "pending"
        self.retry_at[job.job_id] = datetime.now(timezone.utc)
        return True

    async def claim(self) -> MemoryWriteJob | None:
        now = datetime.now(timezone.utc)
        for job_id, job in self.jobs.items():
            if self.statuses[job_id] not in {"pending", "submitted"}:
                continue
            if self.retry_at[job_id] > now:
                continue
            provider_task_id = self.provider_task_ids.get(job_id)
            claimed = replace(
                job,
                attempts=job.attempts + (provider_task_id is None),
                provider_task_id=provider_task_id,
            )
            self.jobs[job_id] = claimed
            self.statuses[job_id] = "processing"
            return claimed
        return None

    async def submit(
        self,
        job_id: str,
        provider_task_id: str,
        *,
        check_at: datetime,
        error: str | None = None,
    ) -> None:
        self.statuses[job_id] = "submitted"
        self.provider_task_ids[job_id] = provider_task_id
        self.retry_at[job_id] = check_at
        if error:
            self.errors[job_id] = error
        else:
            self.errors.pop(job_id, None)

    async def complete(self, job_id: str, provider_task_id: str | None) -> None:
        self.statuses[job_id] = "completed"
        self.provider_task_ids[job_id] = provider_task_id
        self.errors.pop(job_id, None)

    async def fail(
        self,
        job_id: str,
        *,
        error: str,
        retry_at: datetime | None,
    ) -> None:
        self.errors[job_id] = error
        self.statuses[job_id] = "pending" if retry_at is not None else "failed"
        self.provider_task_ids[job_id] = None
        if retry_at is not None:
            self.retry_at[job_id] = retry_at

    async def close(self) -> None:
        return None


class MemoryWriteService:
    def __init__(
        self,
        store: MemoryJobStore,
        provider: MemoryProvider,
        *,
        poll_seconds: float = 1.0,
        max_attempts: int = 3,
    ) -> None:
        self._store = store
        self._provider = provider
        self._poll_seconds = poll_seconds
        self._max_attempts = max_attempts
        self._worker: asyncio.Task[None] | None = None

    async def start(self) -> None:
        await self._store.start()
        self._worker = asyncio.create_task(self._run())

    async def enqueue_completed_turn(self, turn: Turn, assistant_text: str) -> bool:
        if not self._provider.enabled or turn.speaker_identity_id is None:
            return False
        job = MemoryWriteJob(
            job_id=str(uuid4()),
            turn_id=turn.turn_id,
            device_id=turn.device_id,
            identity_id=turn.speaker_identity_id,
            session_id=turn.session_id,
            speaker_name=turn.speaker_display_name,
            user_text=turn.user_text,
            assistant_text=assistant_text,
        )
        inserted = await self._store.enqueue(job)
        logger.info(
            "memory_job_enqueued turn_id=%s identity_id=%s inserted=%s",
            turn.turn_id,
            turn.speaker_identity_id,
            inserted,
        )
        return inserted

    async def close(self) -> None:
        if self._worker is not None:
            self._worker.cancel()
            await asyncio.gather(self._worker, return_exceptions=True)
            self._worker = None
        await self._store.close()

    async def _run(self) -> None:
        while True:
            try:
                job = await self._store.claim()
                if job is not None:
                    await self._process(job)
                    continue
            except asyncio.CancelledError:
                raise
            except Exception:
                # Worker 与单个 Job 的生命周期不同：一次数据库抖动不能让整个
                # 后台记忆管道永久停止。Job 的 lease 负责后续重新领取。
                logger.exception("memory_worker_iteration_failed")

            # 空队列和异常都需要退避，避免数据库不可用时形成忙循环。
            if self._poll_seconds:
                await asyncio.sleep(self._poll_seconds)

    async def _process(self, job: MemoryWriteJob) -> None:
        if job.provider_task_id is not None:
            await self._poll_provider(job)
            return
        await self._submit_to_provider(job)

    async def _submit_to_provider(self, job: MemoryWriteJob) -> None:
        try:
            receipt = await self._provider.add_completed_turn(
                ProfileScope(job.device_id, job.identity_id),
                conversation_id=job.session_id,
                turn_id=job.turn_id,
                speaker_name=job.speaker_name,
                user_text=job.user_text,
                assistant_text=job.assistant_text,
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            await self._fail_submission(job, exc)
            return
        if receipt.status == "failed":
            await self._fail_submission(job, RuntimeError("MemOS task failed"))
            return
        if receipt.status == "completed":
            await self._complete(job, receipt.provider_task_id)
            return
        if receipt.provider_task_id is None:
            await self._fail_submission(
                job,
                RuntimeError("Memory provider returned running without a task ID"),
            )
            return
        await self._store.submit(
            job.job_id,
            receipt.provider_task_id,
            check_at=self._next_poll_at(),
        )
        logger.info(
            "memory_job_submitted job_id=%s turn_id=%s attempts=%s provider_task_id=%s",
            job.job_id,
            job.turn_id,
            job.attempts,
            receipt.provider_task_id,
        )

    async def _poll_provider(self, job: MemoryWriteJob) -> None:
        provider_task_id = job.provider_task_id
        assert provider_task_id is not None
        try:
            receipt = await self._provider.get_write_status(provider_task_id)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            await self._store.submit(
                job.job_id,
                provider_task_id,
                check_at=self._next_poll_at(),
                error=f"{type(exc).__name__}: {exc}",
            )
            logger.warning(
                "memory_job_status_check_failed job_id=%s turn_id=%s "
                "provider_task_id=%s error_type=%s",
                job.job_id,
                job.turn_id,
                provider_task_id,
                type(exc).__name__,
            )
            return
        if receipt.status == "running":
            await self._store.submit(
                job.job_id,
                provider_task_id,
                check_at=self._next_poll_at(),
            )
            return
        if receipt.status == "failed":
            await self._fail_submission(job, RuntimeError("MemOS task failed"))
            return
        await self._complete(job, provider_task_id)

    async def _complete(self, job: MemoryWriteJob, provider_task_id: str | None) -> None:
        await self._store.complete(job.job_id, provider_task_id)
        logger.info(
            "memory_job_completed job_id=%s turn_id=%s attempts=%s provider_task_id=%s",
            job.job_id,
            job.turn_id,
            job.attempts,
            provider_task_id or "none",
        )

    async def _fail_submission(self, job: MemoryWriteJob, exc: Exception) -> None:
        retry_at = None
        if job.attempts < self._max_attempts:
            retry_at = datetime.now(timezone.utc) + timedelta(
                seconds=min(2 ** job.attempts, 60)
            )
        await self._store.fail(
            job.job_id,
            error=f"{type(exc).__name__}: {exc}",
            retry_at=retry_at,
        )
        logger.warning(
            "memory_job_failed job_id=%s turn_id=%s attempts=%s retry=%s error_type=%s",
            job.job_id,
            job.turn_id,
            job.attempts,
            retry_at is not None,
            type(exc).__name__,
        )

    def _next_poll_at(self) -> datetime:
        return datetime.now(timezone.utc) + timedelta(seconds=self._poll_seconds)


class DisabledMemoryWriteService:
    async def start(self) -> None:
        return None

    async def enqueue_completed_turn(self, turn: Turn, assistant_text: str) -> bool:
        return False

    async def close(self) -> None:
        return None
