from collections.abc import Sequence

from newtalk.identity.models import Device, DeviceRegistration, Identity
from newtalk.identity.security import (
    digest_recovery_code,
    digest_secret,
    generate_device_credential,
    generate_device_id,
    generate_recovery_code,
)
from newtalk.identity.store import IdentityStore


class DeviceAuthenticationError(RuntimeError):
    pass


class DeviceRecoveryError(RuntimeError):
    pass


class IdentityNotFoundError(RuntimeError):
    pass


class IdentityService:
    def __init__(self, store: IdentityStore) -> None:
        self._store = store

    async def start(self) -> None:
        await self._store.start()

    async def close(self) -> None:
        await self._store.close()

    async def ping(self) -> None:
        await self._store.ping()

    async def register_device(self) -> DeviceRegistration:
        for _ in range(5):
            credential = generate_device_credential()
            recovery_code = generate_recovery_code()
            try:
                device = await self._store.create_device(
                    device_id=generate_device_id(),
                    credential_digest=digest_secret(credential),
                    recovery_digest=digest_recovery_code(recovery_code),
                )
            except ValueError:
                continue
            return DeviceRegistration(
                device=device,
                credential=credential,
                recovery_code=recovery_code,
            )
        raise RuntimeError("Unable to allocate a unique device identity")

    async def authenticate_device(self, credential: str | None) -> Device:
        if not credential:
            raise DeviceAuthenticationError("Device credential is missing")
        device = await self._store.get_device_by_credential(digest_secret(credential))
        if device is None:
            raise DeviceAuthenticationError("Device credential is invalid")
        return device

    async def recover_device(self, recovery_code: str) -> DeviceRegistration:
        recovery_digest = digest_recovery_code(recovery_code)
        for _ in range(5):
            credential = generate_device_credential()
            try:
                device = await self._store.recover_device(
                    recovery_digest=recovery_digest,
                    new_credential_digest=digest_secret(credential),
                )
            except ValueError:
                continue
            if device is None:
                raise DeviceRecoveryError("Recovery code is invalid")
            return DeviceRegistration(
                device=device,
                credential=credential,
                recovery_code="",
            )
        raise RuntimeError("Unable to allocate a unique device credential")

    async def rotate_recovery_code(self, device_id: str) -> str:
        for _ in range(5):
            recovery_code = generate_recovery_code()
            try:
                await self._store.rotate_recovery_code(
                    device_id=device_id,
                    recovery_digest=digest_recovery_code(recovery_code),
                )
            except ValueError:
                continue
            return recovery_code
        raise RuntimeError("Unable to allocate a unique recovery code")

    async def list_identities(self, device_id: str) -> Sequence[Identity]:
        return await self._store.list_identities(device_id)

    async def create_identity(
        self,
        *,
        device_id: str,
        display_name: str,
        nickname: str | None = None,
        relationship: str | None = None,
        avatar: str | None = None,
    ) -> Identity:
        return await self._store.create_identity(
            device_id=device_id,
            display_name=display_name,
            nickname=nickname,
            relationship=relationship,
            avatar=avatar,
        )

    async def update_identity(
        self,
        *,
        device_id: str,
        identity_id: str,
        changes: dict[str, str | None],
    ) -> Identity:
        identity = await self._store.update_identity(
            device_id=device_id,
            identity_id=identity_id,
            changes=changes,
        )
        if identity is None:
            raise IdentityNotFoundError(identity_id)
        return identity

    async def delete_identity(self, *, device_id: str, identity_id: str) -> None:
        deleted = await self._store.delete_identity(
            device_id=device_id,
            identity_id=identity_id,
        )
        if not deleted:
            raise IdentityNotFoundError(identity_id)
