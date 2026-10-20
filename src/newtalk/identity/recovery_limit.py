from collections import defaultdict, deque
from datetime import datetime, timedelta, timezone
from hashlib import sha256
import time
from typing import Protocol

from sqlalchemy import DateTime, Integer, String, delete, select
from sqlalchemy.dialects.postgresql import insert as postgresql_insert
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import Mapped, mapped_column

from newtalk.identity.sqlalchemy_store import Base


class RecoveryRateLimitExceeded(RuntimeError):
    pass


class RecoveryRateLimiter(Protocol):
    async def start(self) -> None: ...

    async def check(self, key: str) -> None: ...

    async def clear(self, key: str) -> None: ...

    async def close(self) -> None: ...


class InMemoryRecoveryRateLimiter:
    def __init__(self, *, max_attempts: int, window_seconds: int) -> None:
        self._max_attempts = max_attempts
        self._window_seconds = window_seconds
        self._attempts: dict[str, deque[float]] = defaultdict(deque)

    async def start(self) -> None:
        return None

    async def check(self, key: str) -> None:
        now = time.monotonic()
        if len(self._attempts) > 1024:
            cutoff = now - self._window_seconds
            self._attempts = defaultdict(
                deque,
                {
                    candidate: attempts
                    for candidate, attempts in self._attempts.items()
                    if attempts and attempts[-1] > cutoff
                },
            )
        attempts = self._attempts[key]
        while attempts and attempts[0] <= now - self._window_seconds:
            attempts.popleft()
        if len(attempts) >= self._max_attempts:
            raise RecoveryRateLimitExceeded
        attempts.append(now)

    async def clear(self, key: str) -> None:
        self._attempts.pop(key, None)

    async def close(self) -> None:
        return None


class RecoveryRateLimitRow(Base):
    __tablename__ = "recovery_rate_limits"

    client_key_digest: Mapped[str] = mapped_column(String(64), primary_key=True)
    window_started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    attempts: Mapped[int] = mapped_column(Integer, nullable=False)


class SqlAlchemyRecoveryRateLimiter:
    """Shared fixed-window limiter for deployments with multiple app workers."""

    def __init__(
        self,
        database_url: str,
        *,
        max_attempts: int,
        window_seconds: int,
    ) -> None:
        self._engine: AsyncEngine = create_async_engine(
            database_url,
            pool_pre_ping=True,
        )
        self._sessions = async_sessionmaker(
            self._engine,
            class_=AsyncSession,
            expire_on_commit=False,
        )
        self._max_attempts = max_attempts
        self._window = timedelta(seconds=window_seconds)

    async def start(self) -> None:
        async with self._sessions() as session:
            await session.execute(
                select(RecoveryRateLimitRow.client_key_digest).limit(1)
            )

    async def check(self, key: str) -> None:
        digest = _digest_client_key(key)
        now = datetime.now(timezone.utc)
        async with self._sessions.begin() as session:
            await session.execute(
                postgresql_insert(RecoveryRateLimitRow)
                .values(
                    client_key_digest=digest,
                    window_started_at=now,
                    attempts=0,
                )
                .on_conflict_do_nothing(
                    index_elements=[RecoveryRateLimitRow.client_key_digest]
                )
            )
            row = await session.scalar(
                select(RecoveryRateLimitRow)
                .where(RecoveryRateLimitRow.client_key_digest == digest)
                .with_for_update()
            )
            if row is None:
                raise RuntimeError("Unable to create recovery rate-limit state")
            if row.window_started_at <= now - self._window:
                row.window_started_at = now
                row.attempts = 0
            if row.attempts >= self._max_attempts:
                raise RecoveryRateLimitExceeded
            row.attempts += 1

    async def clear(self, key: str) -> None:
        async with self._sessions.begin() as session:
            await session.execute(
                delete(RecoveryRateLimitRow).where(
                    RecoveryRateLimitRow.client_key_digest
                    == _digest_client_key(key)
                )
            )

    async def close(self) -> None:
        await self._engine.dispose()


def _digest_client_key(key: str) -> str:
    return sha256(key.encode("utf-8")).hexdigest()
