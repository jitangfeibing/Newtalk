from __future__ import annotations

import asyncio
import logging
from collections import OrderedDict
from dataclasses import dataclass, field
from time import monotonic
from uuid import uuid4

from newtalk.media.models import CLIENT_MEDIA_ACTIONS, MediaControl, MediaTrack
from newtalk.music.models import MusicUnavailable, Song
from newtalk.music.provider import MusicProvider

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class _Resource:
    device_id: str
    track: MediaTrack
    url: str = field(repr=False)
    expires: float


class MusicService:
    def __init__(self, provider: MusicProvider) -> None:
        self.provider = provider
        self._searches: OrderedDict[str, tuple[str, tuple[Song, ...], float]] = OrderedDict()
        self._resources: OrderedDict[str, _Resource] = OrderedDict()

    @property
    def enabled(self) -> bool:
        return self.provider.enabled

    async def search(self, device_id: str, query: str) -> tuple[Song, ...]:
        query = self._query(query)
        songs = await self.provider.search(query)
        self._searches[device_id] = (query, songs, monotonic() + 600)
        self._searches.move_to_end(device_id)
        while len(self._searches) > 128:
            self._searches.popitem(last=False)
        return songs

    @staticmethod
    def _query(value: str | None) -> str:
        if value is None or not value.strip():
            return "轻音乐"
        if len(value.strip()) > 80:
            raise ValueError("请将歌手或歌名控制在 80 字以内。")
        return value.strip()

    async def command(self, device_id: str, action: str, query: str | None, index: int | None,
                      call_id: str) -> MediaControl:
        if action not in CLIENT_MEDIA_ACTIONS:
            raise ValueError("不支持这个音乐操作。")
        if action != "play":
            if query is not None or index is not None:
                raise ValueError("暂停、继续和停止无需歌曲参数。")
            return MediaControl(action, call_id)
        if index is not None and (type(index) is not int or not 1 <= index <= 100):
            raise ValueError("歌曲序号无效。")
        async with asyncio.timeout(25):
            if query is None and index is not None:
                previous = self._searches.get(device_id)
                if previous is None or previous[2] <= monotonic():
                    raise MusicUnavailable("请先说要听的歌手或歌名，再选择序号。")
                resolved_query, songs, _ = previous
            else:
                resolved_query = self._query(query)
                songs = await self.search(device_id, resolved_query)
            candidates = tuple(song for song in songs if index is None or song.index == index)
            if not candidates:
                raise MusicUnavailable("搜索结果里没有这个序号，请重新选择。")
            failures = []
            for song in candidates[:3]:
                try:
                    audio = await self.provider.resolve(resolved_query, song)
                except MusicUnavailable as exc:
                    failures.append(exc)
                    logger.warning('music_candidate_failed operation_id=%s index=%s title=%r code=%s',
                                   call_id, song.index, song.title, exc.code)
                    if index is not None:
                        raise
                    continue
                track = MediaTrack(str(uuid4()), song.title, song.artist, audio.cover)
                self._resources[track.track_id] = _Resource(device_id, track, audio.url, monotonic() + 1800)
                while len(self._resources) > 256:
                    self._resources.popitem(last=False)
                return MediaControl("play", call_id, track)
        for failure in failures:
            if failure.code != 'no_resource':
                raise failure
        raise MusicUnavailable("前几个匹配版本均没有可播放地址，请换一首试试。", code="no_resource")

    def resource(self, device_id: str, track_id: str) -> _Resource:
        item = self._resources.get(track_id)
        if item is None or item.device_id != device_id or item.expires <= monotonic():
            raise LookupError("播放资源不存在或已过期，请重新点播。")
        return item

    async def close(self) -> None:
        self._searches.clear()
        self._resources.clear()
        await self.provider.aclose()
