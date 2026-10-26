import re

from fastapi import HTTPException, Request
from fastapi.responses import StreamingResponse
from starlette.background import BackgroundTask

from newtalk.media.models import MediaUnavailable


async def stream_resource(service, track_id: str, request: Request, device_id: str):
    try:
        item = service.resource(device_id, track_id)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    range_header = request.headers.get('range')
    if range_header is not None and (range_header == 'bytes=-' or not re.fullmatch(r'bytes=\d*-\d*', range_header)):
        raise HTTPException(status_code=416, detail='不支持这个音频范围请求')
    try:
        response = await service.provider.open_audio(item.url, range_header)
    except MediaUnavailable as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from None
    headers = {key: value for key, value in response.headers.items()
               if key in {'content-type', 'content-length', 'content-range', 'accept-ranges'}}
    headers['Cache-Control'] = 'no-store'

    async def body():
        try:
            async for chunk in response.aiter_bytes():
                yield chunk
        finally:
            await response.aclose()

    return StreamingResponse(body(), status_code=response.status_code, headers=headers,
                             background=BackgroundTask(response.aclose))
