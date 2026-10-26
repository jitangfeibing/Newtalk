import logging
from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel, ConfigDict, Field

from newtalk.identity.api import require_device
from newtalk.identity.models import Device

router = APIRouter(prefix="/api/reminders", tags=["reminders"])
logger = logging.getLogger(__name__)
CurrentDevice = Annotated[Device, Depends(require_device)]


class ReminderEdit(BaseModel):
    model_config = ConfigDict(extra="forbid")
    content: str = Field(min_length=1, max_length=160)
    due_at: datetime


class ReminderCreate(ReminderEdit):
    request_key: str = Field(min_length=1, max_length=128)


def _error(exc: Exception) -> HTTPException:
    return HTTPException(status_code=404 if isinstance(exc, LookupError) else 409, detail=str(exc))


@router.get("")
async def list_reminders(request: Request, response: Response, device: CurrentDevice):
    response.headers["Cache-Control"] = "no-store"
    items = await request.app.state.reminder_service.list(device.device_id)
    return {"scope": "device", "timezone": "Asia/Shanghai", "items": [item.payload() for item in items]}


@router.post("", status_code=201)
async def create_reminder(body: ReminderCreate, request: Request, device: CurrentDevice):
    try:
        item = await request.app.state.reminder_service.create(
            device.device_id, body.content, body.due_at, body.request_key,
        )
    except ValueError as exc:
        raise _error(exc) from exc
    return item.payload()


@router.patch("/{reminder_id}")
async def update_reminder(reminder_id: str, body: ReminderEdit, request: Request,
                          device: CurrentDevice):
    try:
        item = await request.app.state.reminder_service.update(
            device.device_id, reminder_id, body.content, body.due_at,
        )
    except (ValueError, LookupError) as exc:
        raise _error(exc) from exc
    return item.payload()


@router.delete("/{reminder_id}")
async def cancel_reminder(reminder_id: str, request: Request, device: CurrentDevice):
    try:
        item = await request.app.state.reminder_service.cancel(device.device_id, reminder_id)
    except LookupError as exc:
        raise _error(exc) from exc
    return item.payload()


@router.post("/{reminder_id}/replay")
async def replay_reminder(reminder_id: str, request: Request, device: CurrentDevice):
    try:
        item = await request.app.state.reminder_service.replay(device.device_id, reminder_id)
    except (ValueError, LookupError) as exc:
        raise _error(exc) from exc
    return item.payload()


@router.get("/{reminder_id}/audio")
async def reminder_audio(reminder_id: str, delivery_id: str, request: Request,
                          device: CurrentDevice):
    service = request.app.state.reminder_service
    try:
        item = await service.get(device.device_id, reminder_id)
        if item.status != "triggered" or item.delivery_id != delivery_id or item.delivery_status not in {"pending", "playing"}:
            raise LookupError("本次提醒播报已结束或不存在")
        audio = await request.app.state.reminder_audio.render(item)
        current = await service.get(device.device_id, reminder_id)
        if current.status != "triggered" or current.delivery_id != delivery_id or current.delivery_status not in {"pending", "playing"}:
            raise LookupError("本次提醒播报已取消或结束")
    except LookupError as exc:
        raise _error(exc) from exc
    except (TimeoutError, RuntimeError, ValueError) as exc:
        logger.warning("reminder_audio_failed reminder_id=%s error_type=%s", reminder_id, type(exc).__name__)
        raise HTTPException(status_code=503, detail="提醒语音暂时无法生成，请查看提醒卡片") from exc
    return Response(audio, media_type="audio/wav", headers={"Cache-Control": "no-store"})
