from collections.abc import Sequence
from typing import Protocol

from newtalk.identity.models import Device, Identity


class IdentityStore(Protocol):
    async def start(self) -> None: ...

    async def close(self) -> None: ...

    async def ping(self) -> None: ...

    async def create_device(
        self,
        *,
        device_id: str,
        credential_digest: str,
        recovery_digest: str,
    ) -> Device: ...

    async def get_device_by_credential(self, credential_digest: str) -> Device | None: ...

    async def recover_device(
        self,
        *,
        recovery_digest: str,
        new_credential_digest: str,
    ) -> Device | None: ...

    async def rotate_recovery_code(
        self,
        *,
        device_id: str,
        recovery_digest: str,
    ) -> Device: ...

    async def list_identities(self, device_id: str) -> Sequence[Identity]: ...

    async def create_identity(
        self,
        *,
        device_id: str,
        display_name: str,
        nickname: str | None,
        relationship: str | None,
        avatar: str | None,
    ) -> Identity: ...

    async def update_identity(
        self,
        *,
        device_id: str,
        identity_id: str,
        changes: dict[str, str | None],
    ) -> Identity | None: ...

    async def delete_identity(self, *, device_id: str, identity_id: str) -> bool: ...
