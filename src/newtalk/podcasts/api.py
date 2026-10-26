from typing import Annotated

from fastapi import APIRouter, Depends, Request

from newtalk.identity.api import require_device
from newtalk.identity.models import Device
from newtalk.media.api import stream_resource

router = APIRouter(prefix='/api/podcasts', tags=['podcasts'])
CurrentDevice = Annotated[Device, Depends(require_device)]


@router.get('/status')
async def podcast_status(request: Request, device: CurrentDevice):
    return {'enabled': request.app.state.podcast_service.enabled, 'source': '喜马拉雅 · 演示'}


@router.get('/tracks/{track_id}/audio')
async def stream_podcast(track_id: str, request: Request, device: CurrentDevice):
    return await stream_resource(request.app.state.podcast_service, track_id, request, device.device_id)
