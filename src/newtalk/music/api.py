from typing import Annotated

from fastapi import APIRouter, Depends, Request

from newtalk.identity.api import require_device
from newtalk.identity.models import Device
from newtalk.media.api import stream_resource

router = APIRouter(prefix="/api/music", tags=["music"])
CurrentDevice = Annotated[Device, Depends(require_device)]


@router.get("/status")
async def music_status(request: Request, device: CurrentDevice):
    return {"enabled": request.app.state.music_service.enabled, "source": "QQ音乐 · 演示"}


@router.get("/tracks/{track_id}/audio")
async def stream_music(track_id: str, request: Request, device: CurrentDevice):
    return await stream_resource(request.app.state.music_service, track_id, request, device.device_id)
