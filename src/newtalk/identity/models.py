from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum


class IdentityStatus(StrEnum):
    ACTIVE = "active"
    DELETION_PENDING = "deletion_pending"


@dataclass(frozen=True, slots=True)
class Device:
    device_id: str
    credential_digest: str
    recovery_digest: str
    created_at: datetime
    updated_at: datetime


@dataclass(frozen=True, slots=True)
class DeviceRegistration:
    device: Device
    credential: str
    recovery_code: str


@dataclass(frozen=True, slots=True)
class Identity:
    identity_id: str
    device_id: str
    display_name: str
    nickname: str | None
    relationship: str | None
    avatar: str | None
    status: IdentityStatus
    created_at: datetime
    updated_at: datetime
