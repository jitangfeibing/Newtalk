from __future__ import annotations

import asyncio
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
import logging
from typing import Protocol
from uuid import UUID, uuid4

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    Integer,
    String,
    Text,
    and_,
    delete,
    func,
    or_,
    select,
    update,
)
from sqlalchemy.dialects.postgresql import UUID as PostgreSQLUUID
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Mapped, mapped_column

from newtalk.identity.models import IdentityStatus
from newtalk.identity.service import IdentityNotFoundError, IdentityService
from newtalk.identity.sqlalchemy_store import Base, IdentityRow
from newtalk.memory.provider import MemoryProvider
from newtalk.profile.models import ProfileScope
from newtalk.profile.session import ProfileCacheCoordinator
from newtalk.voiceprint.client import VoicePrintClient


logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class IdentityDeletionJob:
    job_id: str
    device_id: str
    identity_id: str
    attempts: int = 0


class IdentityDeletionStore(Protocol):
    async def start(self) -> None: ...

    async def schedule(self, *, device_id: str, identity_id: str) -> bool: ...

    async def claim(self) -> IdentityDeletionJob | None: ...

    async def complete(self, job_id: str) -> None: ...

    async def fail(
        self,
        job_id: str,
        *,
        error: str,
        retry_at: datetime | None,
    ) -> None: ...

    async def close(self) -> None: ...


class IdentityDeletionScheduler(Protocol):
    async def start(self) -> None: ...

    async def request(self, *, device_id: str, identity_id: str) -> bool: ...

    async def close(self) -> None: ...


class IdentityDeletionJobRow(Base):
    __tablename__ = "identity_deletion_jobs"
    __table_args__ = (
        CheckConstraint(
            "status IN ('pending', 'processing', 'completed', 'failed')",
            name="ck_identity_deletion_jobs_status",
        ),
    )

    job_id: Mapped[UUID] = mapped_column(
        PostgreSQLUUID(as_uuid=True), primary_key=True, default=uuid4
    )
    device_id: Mapped[str] = mapped_column(String(17), index=True, nullable=False)
    identity_id: Mapped[UUID] = mapped_column(
        PostgreSQLUUID(as_uuid=True), unique=True, index=True, nullable=False
    )
    status: Mapped[str] = mapped_column(String(20), default="pending", nullable=False)
    attempts: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
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


def _job(row: IdentityDeletionJobRow) -> IdentityDeletionJob:
    return IdentityDeletionJob(
        job_id=str(row.job_id),
        device_id=row.device_id,
        identity_id=str(row.identity_id),
        attempts=row.attempts,
    )


class SqlAlchemyIdentityDeletionStore:
    def __init__(self, database_url: str, *, lease_seconds: float = 120.0) -> None:
        self._engine: AsyncEngine = create_async_engine(database_url, pool_pre_ping=True)
        self._sessions = async_sessionmaker(
            self._engine,
            class_=AsyncSession,
            expire_on_commit=False,
        )
        self._lease_seconds = lease_seconds

    async def start(self) -> None:
        async with self._sessions() as session:
            await session.execute(select(IdentityDeletionJobRow.job_id).limit(1))

    async def schedule(self, *, device_id: str, identity_id: str) -> bool:
        try:
            parsed_id = UUID(identity_id)
        except ValueError:
            return False
        try:
            async with self._sessions.begin() as session:
                identity = await session.scalar(
                    select(IdentityRow)
                    .where(
                        IdentityRow.identity_id == parsed_id,
                        IdentityRow.device_id == device_id,
                        IdentityRow.status == IdentityStatus.ACTIVE.value,
                    )
                    .with_for_update()
                )
                if identity is None:
                    return False
                identity.status = IdentityStatus.DELETION_PENDING.value
                identity.updated_at = func.now()
                session.add(
                    IdentityDeletionJobRow(
                        device_id=device_id,
                        identity_id=parsed_id,
                    )
                )
                await session.flush()
                return True
        except IntegrityError:
            return False

    async def claim(self) -> IdentityDeletionJob | None:
        now = datetime.now(timezone.utc)
        lease_until = now + timedelta(seconds=self._lease_seconds)
        async with self._sessions.begin() as session:
            row = await session.scalar(
                select(IdentityDeletionJobRow)
                .where(
                    or_(
                        and_(
                            IdentityDeletionJobRow.status == "pending",
                            IdentityDeletionJobRow.available_at <= now,
                        ),
                        and_(
                            IdentityDeletionJobRow.status == "processing",
                            IdentityDeletionJobRow.available_at <= now,
                        ),
                    )
                )
                .order_by(IdentityDeletionJobRow.created_at)
                .with_for_update(skip_locked=True)
                .limit(1)
            )
            if row is None:
                return None
            row.status = "processing"
            row.attempts += 1
            row.available_at = lease_until
            row.updated_at = now
            await session.flush()
            return _job(row)

    async def complete(self, job_id: str) -> None:
        async with self._sessions.begin() as session:
            row = await session.get(
                IdentityDeletionJobRow,
                UUID(job_id),
                with_for_update=True,
            )
            if row is None:
                return
            await session.execute(
                delete(IdentityRow).where(
                    IdentityRow.identity_id == row.identity_id,
                    IdentityRow.device_id == row.device_id,
                    IdentityRow.status == IdentityStatus.DELETION_PENDING.value,
                )
            )
            row.status = "completed"
            row.last_error = None
            row.updated_at = datetime.now(timezone.utc)

    async def fail(
        self,
        job_id: str,
        *,
        error: str,
        retry_at: datetime | None,
    ) -> None:
        async with self._sessions.begin() as session:
            await session.execute(
                update(IdentityDeletionJobRow)
                .where(IdentityDeletionJobRow.job_id == UUID(job_id))
                .values(
                    status="pending" if retry_at is not None else "failed",
                    last_error=error[:2000],
                    available_at=retry_at or datetime.now(timezone.utc),
                    updated_at=datetime.now(timezone.utc),
                )
            )

    async def close(self) -> None:
        await self._engine.dispose()


class InMemoryIdentityDeletionStore:
    def __init__(self, identity_service: IdentityService) -> None:
        self._identity_service = identity_service
        self.jobs: dict[str, IdentityDeletionJob] = {}
        self.statuses: dict[str, str] = {}
        self.retry_at: dict[str, datetime] = {}
        self.errors: dict[str, str] = {}

    async def start(self) -> None:
        return None

    async def schedule(self, *, device_id: str, identity_id: str) -> bool:
        try:
            await self._identity_service.mark_identity_deletion_pending(
                device_id=device_id,
                identity_id=identity_id,
            )
        except IdentityNotFoundError:
            return False
        job = IdentityDeletionJob(str(uuid4()), device_id, identity_id)
        self.jobs[job.job_id] = job
        self.statuses[job.job_id] = "pending"
        self.retry_at[job.job_id] = datetime.now(timezone.utc)
        return True

    async def claim(self) -> IdentityDeletionJob | None:
        now = datetime.now(timezone.utc)
        for job_id, job in self.jobs.items():
            if self.statuses[job_id] != "pending" or self.retry_at[job_id] > now:
                continue
            claimed = replace(job, attempts=job.attempts + 1)
            self.jobs[job_id] = claimed
            self.statuses[job_id] = "processing"
            return claimed
        return None

    async def complete(self, job_id: str) -> None:
        job = self.jobs[job_id]
        await self._identity_service.delete_identity(
            device_id=job.device_id,
            identity_id=job.identity_id,
        )
        self.statuses[job_id] = "completed"

    async def fail(
        self,
        job_id: str,
        *,
        error: str,
        retry_at: datetime | None,
    ) -> None:
        self.errors[job_id] = error
        self.statuses[job_id] = "pending" if retry_at is not None else "failed"
        if retry_at is not None:
            self.retry_at[job_id] = retry_at

    async def close(self) -> None:
        return None


class IdentityDeletionService:
    def __init__(
        self,
        store: IdentityDeletionStore,
        voiceprint: VoicePrintClient,
        memory: MemoryProvider,
        coordinator: ProfileCacheCoordinator,
        *,
        poll_seconds: float = 1.0,
        max_attempts: int = 3,
    ) -> None:
        self._store = store
        self._voiceprint = voiceprint
        self._memory = memory
        self._coordinator = coordinator
        self._poll_seconds = poll_seconds
        self._max_attempts = max_attempts
        self._worker: asyncio.Task[None] | None = None

    async def start(self) -> None:
        await self._store.start()
        self._worker = asyncio.create_task(self._run())

    async def request(self, *, device_id: str, identity_id: str) -> bool:
        scheduled = await self._store.schedule(
            device_id=device_id,
            identity_id=identity_id,
        )
        if scheduled:
            self._coordinator.remove(device_id, identity_id)
        return scheduled

    async def close(self) -> None:
        if self._worker is not None:
            self._worker.cancel()
            await asyncio.gather(self._worker, return_exceptions=True)
            self._worker = None
        await self._store.close()

    async def _run(self) -> None:
        while True:
            job = None
            try:
                job = await self._store.claim()
                if job is not None:
                    await self._process(job)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("identity_deletion_worker_failed")
            if job is None:
                await asyncio.sleep(self._poll_seconds)

    async def _process(self, job: IdentityDeletionJob) -> None:
        scope = ProfileScope(job.device_id, job.identity_id)
        try:
            if getattr(self._voiceprint, "enabled", True):
                await self._voiceprint.delete(
                    device_id=job.device_id,
                    identity_id=job.identity_id,
                )
            if self._memory.enabled:
                await self._memory.delete_all_memories(scope)
                await self._memory.delete_profile(scope)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
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
                "identity_deletion_failed job_id=%s identity_id=%s attempts=%s retry=%s error_type=%s",
                job.job_id,
                job.identity_id,
                job.attempts,
                retry_at is not None,
                type(exc).__name__,
            )
            return
        await self._store.complete(job.job_id)
        logger.info(
            "identity_deletion_completed job_id=%s identity_id=%s attempts=%s",
            job.job_id,
            job.identity_id,
            job.attempts,
        )
