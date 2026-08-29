import asyncio
from dataclasses import replace
from datetime import UTC, datetime
from uuid import uuid4

from newtalk.identity.models import Device, Identity, IdentityStatus


class InMemoryIdentityStore:
    def __init__(self) -> None:
        self._devices: dict[str, Device] = {}
        self._identities: dict[str, Identity] = {}
        self._lock = asyncio.Lock()

    async def start(self) -> None:
        return None

    async def close(self) -> None:
        return None

    async def ping(self) -> None:
        return None

    async def create_device(
        self,
        *,
        device_id: str,
        credential_digest: str,
        recovery_digest: str,
    ) -> Device:
        async with self._lock:
            if device_id in self._devices:
                raise ValueError("device_id already exists")
            if any(
                device.credential_digest == credential_digest
                or device.recovery_digest == recovery_digest
                for device in self._devices.values()
            ):
                raise ValueError("device secret already exists")
            now = datetime.now(UTC)
            device = Device(
                device_id=device_id,
                credential_digest=credential_digest,
                recovery_digest=recovery_digest,
                created_at=now,
                updated_at=now,
            )
            self._devices[device_id] = device
            return device

    async def get_device_by_credential(self, credential_digest: str) -> Device | None:
        async with self._lock:
            return next(
                (
                    device
                    for device in self._devices.values()
                    if device.credential_digest == credential_digest
                ),
                None,
            )

    async def recover_device(
        self,
        *,
        recovery_digest: str,
        new_credential_digest: str,
    ) -> Device | None:
        async with self._lock:
            if any(
                candidate.credential_digest == new_credential_digest
                for candidate in self._devices.values()
            ):
                raise ValueError("device credential already exists")
            device = next(
                (
                    candidate
                    for candidate in self._devices.values()
                    if candidate.recovery_digest == recovery_digest
                ),
                None,
            )
            if device is None:
                return None
            updated = replace(
                device,
                credential_digest=new_credential_digest,
                updated_at=datetime.now(UTC),
            )
            self._devices[device.device_id] = updated
            return updated

    async def rotate_recovery_code(
        self,
        *,
        device_id: str,
        recovery_digest: str,
    ) -> Device:
        async with self._lock:
            if any(
                candidate.recovery_digest == recovery_digest
                for candidate in self._devices.values()
            ):
                raise ValueError("recovery code already exists")
            device = self._devices[device_id]
            updated = replace(
                device,
                recovery_digest=recovery_digest,
                updated_at=datetime.now(UTC),
            )
            self._devices[device_id] = updated
            return updated

    async def list_identities(self, device_id: str) -> list[Identity]:
        async with self._lock:
            return sorted(
                (
                    identity
                    for identity in self._identities.values()
                    if identity.device_id == device_id
                ),
                key=lambda identity: identity.created_at,
            )

    async def create_identity(
        self,
        *,
        device_id: str,
        display_name: str,
        nickname: str | None,
        relationship: str | None,
        avatar: str | None,
    ) -> Identity:
        async with self._lock:
            if device_id not in self._devices:
                raise KeyError(device_id)
            now = datetime.now(UTC)
            identity = Identity(
                identity_id=str(uuid4()),
                device_id=device_id,
                display_name=display_name,
                nickname=nickname,
                relationship=relationship,
                avatar=avatar,
                status=IdentityStatus.ACTIVE,
                created_at=now,
                updated_at=now,
            )
            self._identities[identity.identity_id] = identity
            return identity

    async def update_identity(
        self,
        *,
        device_id: str,
        identity_id: str,
        changes: dict[str, str | None],
    ) -> Identity | None:
        async with self._lock:
            identity = self._identities.get(identity_id)
            if identity is None or identity.device_id != device_id:
                return None
            updated = replace(identity, **changes, updated_at=datetime.now(UTC))
            self._identities[identity_id] = updated
            return updated

    async def delete_identity(self, *, device_id: str, identity_id: str) -> bool:
        async with self._lock:
            identity = self._identities.get(identity_id)
            if identity is None or identity.device_id != device_id:
                return False
            del self._identities[identity_id]
            return True
