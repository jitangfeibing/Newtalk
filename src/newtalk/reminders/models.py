from dataclasses import asdict, dataclass, replace
from datetime import datetime, timedelta
from typing import Literal
from uuid import uuid4

ReminderStatus = Literal["scheduled", "triggered", "missed", "cancelled"]
DeliveryStatus = Literal["pending", "playing", "completed", "interrupted", "failed", "expired", "unknown"]
PENDING_DELIVERIES = ("pending", "playing")


@dataclass(frozen=True, slots=True)
class Reminder:
    reminder_id: str
    device_id: str
    content: str
    due_at: datetime
    created_at: datetime
    request_key: str
    status: ReminderStatus = "scheduled"
    delivery_id: str | None = None
    delivery_status: DeliveryStatus | None = None
    connection_id: str | None = None
    delivery_deadline: datetime | None = None

    def payload(self) -> dict:
        values = asdict(self)
        for key in ("device_id", "request_key", "connection_id"):
            values.pop(key)
        for key in ("due_at", "created_at", "delivery_deadline"):
            values[key] = values[key].isoformat() if values[key] is not None else None
        return values


def claim(item: Reminder, now: datetime, connections: dict[str, str]) -> Reminder:
    connection_id = connections.get(item.device_id)
    if connection_id is None:
        return replace(item, status="missed")
    return replace(item, status="triggered", delivery_id=str(uuid4()), delivery_status="pending",
                   connection_id=connection_id, delivery_deadline=now + timedelta(seconds=90))
