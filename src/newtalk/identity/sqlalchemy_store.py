from collections.abc import Sequence
from datetime import datetime
from uuid import UUID, uuid4

from sqlalchemy import DateTime, ForeignKey, String, delete, func, select
from sqlalchemy.dialects.postgresql import UUID as PostgreSQLUUID
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from newtalk.identity.models import Device, Identity, IdentityStatus


class Base(DeclarativeBase):
    pass


class DeviceRow(Base):
    __tablename__ = "devices"

    device_id: Mapped[str] = mapped_column(String(17), primary_key=True)
    credential_digest: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    recovery_digest: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class IdentityRow(Base):
    __tablename__ = "identities"

    identity_id: Mapped[UUID] = mapped_column(
        PostgreSQLUUID(as_uuid=True), primary_key=True, default=uuid4
    )
    device_id: Mapped[str] = mapped_column(
        ForeignKey("devices.device_id", ondelete="CASCADE"),
        index=True,
        nullable=False,
    )
    display_name: Mapped[str] = mapped_column(String(80), nullable=False)
    nickname: Mapped[str | None] = mapped_column(String(80))
    relationship: Mapped[str | None] = mapped_column(String(80))
    avatar: Mapped[str | None] = mapped_column(String(500))
    status: Mapped[str] = mapped_column(
        String(32), default=IdentityStatus.ACTIVE.value, nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


def _device(row: DeviceRow) -> Device:
    return Device(
        device_id=row.device_id,
        credential_digest=row.credential_digest,
        recovery_digest=row.recovery_digest,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


def _identity(row: IdentityRow) -> Identity:
    return Identity(
        identity_id=str(row.identity_id),
        device_id=row.device_id,
        display_name=row.display_name,
        nickname=row.nickname,
        relationship=row.relationship,
        avatar=row.avatar,
        status=IdentityStatus(row.status),
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


class SqlAlchemyIdentityStore:
    def __init__(self, database_url: str, *, echo: bool = False) -> None:
        self._engine: AsyncEngine = create_async_engine(
            database_url,
            echo=echo,
            pool_pre_ping=True,
        )
        self._sessions = async_sessionmaker(
            self._engine,
            class_=AsyncSession,
            expire_on_commit=False,
        )

    async def start(self) -> None:
        async with self._sessions() as session:
            await session.execute(select(DeviceRow.device_id).limit(1))

    async def close(self) -> None:
        await self._engine.dispose()

    async def ping(self) -> None:
        async with self._sessions() as session:
            await session.execute(select(1))

    async def create_device(
        self,
        *,
        device_id: str,
        credential_digest: str,
        recovery_digest: str,
    ) -> Device:
        async with self._sessions() as session:
            row = DeviceRow(
                device_id=device_id,
                credential_digest=credential_digest,
                recovery_digest=recovery_digest,
            )
            session.add(row)
            try:
                await session.commit()
            except IntegrityError as exc:
                await session.rollback()
                raise ValueError("device identity collision") from exc
            await session.refresh(row)
            return _device(row)

    async def get_device_by_credential(self, credential_digest: str) -> Device | None:
        async with self._sessions() as session:
            row = await session.scalar(
                select(DeviceRow).where(
                    DeviceRow.credential_digest == credential_digest
                )
            )
            return _device(row) if row else None

    async def recover_device(
        self,
        *,
        recovery_digest: str,
        new_credential_digest: str,
    ) -> Device | None:
        try:
            async with self._sessions.begin() as session:
                row = await session.scalar(
                    select(DeviceRow)
                    .where(DeviceRow.recovery_digest == recovery_digest)
                    .with_for_update()
                )
                if row is None:
                    return None
                row.credential_digest = new_credential_digest
                row.updated_at = func.now()
                await session.flush()
                await session.refresh(row)
                return _device(row)
        except IntegrityError as exc:
            raise ValueError("device credential collision") from exc

    async def rotate_recovery_code(
        self,
        *,
        device_id: str,
        recovery_digest: str,
    ) -> Device:
        try:
            async with self._sessions.begin() as session:
                row = await session.get(DeviceRow, device_id, with_for_update=True)
                if row is None:
                    raise KeyError(device_id)
                row.recovery_digest = recovery_digest
                row.updated_at = func.now()
                await session.flush()
                await session.refresh(row)
                return _device(row)
        except IntegrityError as exc:
            raise ValueError("recovery code collision") from exc

    async def list_identities(self, device_id: str) -> Sequence[Identity]:
        async with self._sessions() as session:
            rows = (
                await session.scalars(
                    select(IdentityRow)
                    .where(IdentityRow.device_id == device_id)
                    .order_by(IdentityRow.created_at)
                )
            ).all()
            return [_identity(row) for row in rows]

    async def create_identity(
        self,
        *,
        device_id: str,
        display_name: str,
        nickname: str | None,
        relationship: str | None,
        avatar: str | None,
    ) -> Identity:
        async with self._sessions() as session:
            row = IdentityRow(
                device_id=device_id,
                display_name=display_name,
                nickname=nickname,
                relationship=relationship,
                avatar=avatar,
            )
            session.add(row)
            await session.commit()
            await session.refresh(row)
            return _identity(row)

    async def update_identity(
        self,
        *,
        device_id: str,
        identity_id: str,
        changes: dict[str, str | None],
    ) -> Identity | None:
        try:
            parsed_id = UUID(identity_id)
        except ValueError:
            return None
        async with self._sessions.begin() as session:
            row = await session.scalar(
                select(IdentityRow)
                .where(
                    IdentityRow.identity_id == parsed_id,
                    IdentityRow.device_id == device_id,
                )
                .with_for_update()
            )
            if row is None:
                return None
            for name, value in changes.items():
                setattr(row, name, value)
            row.updated_at = func.now()
            await session.flush()
            await session.refresh(row)
            return _identity(row)

    async def delete_identity(self, *, device_id: str, identity_id: str) -> bool:
        try:
            parsed_id = UUID(identity_id)
        except ValueError:
            return False
        async with self._sessions.begin() as session:
            result = await session.execute(
                delete(IdentityRow).where(
                    IdentityRow.identity_id == parsed_id,
                    IdentityRow.device_id == device_id,
                )
            )
            return result.rowcount == 1
