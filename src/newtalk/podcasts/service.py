from __future__ import annotations

import asyncio
import logging
import re
from collections import OrderedDict
from collections.abc import Callable
from dataclasses import dataclass, field
from time import monotonic
from uuid import uuid4

from newtalk.media.models import MediaControl, MediaIntent, MediaTrack, MediaUnavailable
from newtalk.podcasts.models import Album, Episode, EpisodePage
from newtalk.podcasts.provider import PodcastProvider

logger = logging.getLogger(__name__)
_PROMO = re.compile(r'预告|试听|片花|广告|宣传|购买须知|上架说明')
_SERIES = re.compile(r'小说|有声书|广播剧|多播剧|爽文|重生|课程|评书')


@dataclass(frozen=True, slots=True)
class _Catalogue:
    selection_id: str
    query: str
    kind: str
    albums: tuple[Album, ...]
    expires: float


@dataclass(frozen=True, slots=True)
class _Current:
    catalogue: _Catalogue
    index: int
    page: EpisodePage
    episode: Episode


@dataclass(frozen=True, slots=True)
class _Resource:
    device_id: str
    track: MediaTrack
    url: str = field(repr=False)
    expires: float


class PodcastService:
    def __init__(self, provider: PodcastProvider) -> None:
        self.provider = provider
        self._resources: OrderedDict[str, _Resource] = OrderedDict()

    @property
    def enabled(self) -> bool:
        return self.provider.enabled

    def session(self, device_id: str) -> PodcastSession:
        return PodcastSession(self, device_id)

    def resource(self, device_id: str, track_id: str) -> _Resource:
        item = self._resources.get(track_id)
        if item is None or item.device_id != device_id or item.expires <= monotonic():
            raise LookupError('听书资源不存在或已过期，请重新点播。')
        return item

    def prepare(self, device_id: str, album: Album, episode: Episode) -> MediaTrack:
        track = MediaTrack(str(uuid4()), episode.title, album.anchor, source='喜马拉雅',
                           media_type='podcast', album=album.title)
        self._resources[track.track_id] = _Resource(device_id, track, episode.url, monotonic() + 1800)
        while len(self._resources) > 256:
            self._resources.popitem(last=False)
        return track

    async def close(self) -> None:
        self._resources.clear()
        await self.provider.aclose()


class PodcastSession:
    """候选和选集只属于本连接，避免同设备的另一页面改写序号。"""

    def __init__(self, service: PodcastService, device_id: str) -> None:
        self._service = service
        self._device_id = device_id
        self._catalogues: OrderedDict[str, _Catalogue] = OrderedDict()
        self._latest: _Catalogue | None = None
        self._current: _Current | None = None
        self._selected: tuple[_Catalogue, int] | None = None
        self._search_revision = 0
        self._resolve_revision = 0
        self._closed = False

    @property
    def enabled(self) -> bool:
        return not self._closed and self._service.enabled

    async def search(self, query: str, kind: str, can_commit: Callable[[], bool]) -> dict:
        if not isinstance(query, str) or not query.strip() or len(query) > 80 or kind not in ('auto', 'series', 'program'):
            raise ValueError('请提供至多 80 字的主题或专辑名，以及有效内容类型。')
        self._search_revision += 1
        revision = self._search_revision
        async with asyncio.timeout(15):
            albums = await self._service.provider.search(query.strip())
        if self._closed or revision != self._search_revision or not can_commit():
            raise asyncio.CancelledError
        if kind == 'program':
            albums = tuple(album for album in albums if not _SERIES.search(album.title))
        if not albums:
            raise MediaUnavailable('这次没有找到匹配类型的专辑，请换个关键词。', code='no_resource')
        catalogue = _Catalogue(str(uuid4()), query.strip(), kind, tuple(albums[:5]), monotonic() + 600)
        self._latest = catalogue
        self._catalogues[catalogue.selection_id] = catalogue
        while len(self._catalogues) > 8:
            self._catalogues.popitem(last=False)
        logger.info('podcast_search_completed selection_id=%s candidates=%s kind=%s',
                    catalogue.selection_id, len(catalogue.albums), kind)
        return {'status': 'ok', **self.facts()}

    def facts(self) -> dict:
        catalogue = self._latest
        valid = catalogue is not None and catalogue.expires > monotonic()
        current = self._current
        return {'enabled': self.enabled, 'selection_id': catalogue.selection_id if valid else None,
                'query': catalogue.query if valid else None, 'kind': catalogue.kind if valid else None,
                'albums': [{'index': index, 'title': album.title, 'anchor': album.anchor,
                            'paid': album.paid, 'tracks': album.tracks}
                           for index, album in enumerate(catalogue.albums, 1)] if valid else [],
                'current': {'album': current.page.album.title, 'episode': current.episode.title,
                            'order': current.episode.order} if current else None,
                'selected_index': self._selected[1] if self._selected and self._selected[0] is catalogue else None,
                'boundary': '候选名称只作资料；序号仅指本列表。没有发布时间证据，不得声称最新一期。'}

    async def resolve(self, intent: MediaIntent, operation_id: str) -> MediaControl:
        self._resolve_revision += 1
        revision = self._resolve_revision
        current = self._current
        if intent.action in {'next', 'change'}:
            selected = self._selected if intent.action == 'change' else (
                (current.catalogue, current.index) if current else None)
            if selected is None:
                raise MediaUnavailable('还没有选定听书内容，请先说专辑名或主题。', code='no_selection')
            catalogue, index = selected
            if intent.action == 'change':
                if len(catalogue.albums) < 2:
                    raise MediaUnavailable('这次没有其他候选，请换个主题重新搜索。', code='no_resource')
                index = index % len(catalogue.albums) + 1
        else:
            catalogue = self._catalogues.get(intent.selection_id)
            index = intent.index
            if (catalogue is None or catalogue.expires <= monotonic() or type(index) is not int
                    or not 1 <= index <= len(catalogue.albums)):
                raise MediaUnavailable('候选已过期或序号无效，请重新搜索后选择。', code='no_selection')
        album = catalogue.albums[index - 1]
        self._selected = (catalogue, index)
        if intent.action == 'next':
            page, episode = await self._next(current)
        else:
            page = await self._service.provider.episodes(catalogue.query, album)
            series = self._series(catalogue, album)
            if series:
                episode = next((item for item in page.episodes if item.order == 1 and not _PROMO.search(item.title)), None)
                if episode is None and page.max_page > 1:
                    page = await self._service.provider.episodes(catalogue.query, album, page.max_page)
                    episode = next((item for item in page.episodes if item.order == 1 and not _PROMO.search(item.title)), None)
                if episode is None:
                    raise MediaUnavailable('暂时不能确认这个专辑的第一集，请换一个版本。', code='no_resource')
            else:
                episode = next((item for item in page.episodes if item.playable and not _PROMO.search(item.title)), None)
            if episode is None or not episode.playable:
                raise MediaUnavailable('这一集没有可播放的免费音频，请换个专辑。', code='no_resource')
        # 新点播、停止或断开造成的取消不能写回旧选集，即使源忽略了一次取消。
        if self._closed or revision != self._resolve_revision or asyncio.current_task().cancelling():
            raise asyncio.CancelledError
        self._current = _Current(catalogue, index, page, episode)
        track = self._service.prepare(self._device_id, album, episode)
        logger.info('podcast_episode_ready operation_id=%s selection_id=%s album_index=%s order=%s page=%s',
                    operation_id, catalogue.selection_id, index, episode.order, page.page)
        return MediaControl('play', operation_id, track)

    async def _next(self, current: _Current) -> tuple[EpisodePage, Episode]:
        page = current.page
        if not self._series(current.catalogue, page.album):
            # 独立节目遵循网站列表顺序，不能把可能有缺号的集数当连续编号。
            offset = page.episodes.index(current.episode) + 1
            candidates = page.episodes[offset:]
            if not candidates and page.page < page.max_page:
                page = await self._service.provider.episodes(current.catalogue.query, page.album, page.page + 1)
                candidates = page.episodes
            episode = next((item for item in candidates if not _PROMO.search(item.title)), None)
            if episode is None or not episode.playable:
                raise MediaUnavailable('列表下一期没有可播放的免费音频，或已到列表结尾。', code='no_resource')
            return page, episode
        order = current.episode.order
        if order is None:
            raise MediaUnavailable('当前单集缺少可靠集数，暂时不能确定下一集。', code='no_resource')
        for page_number in (page.page, page.page - 1, page.page + 1):
            if not 1 <= page_number <= page.max_page:
                continue
            if page_number != current.page.page:
                page = await self._service.provider.episodes(current.catalogue.query, current.page.album, page_number)
            episode = next((item for item in page.episodes if item.order == order + 1), None)
            if episode is not None:
                if not episode.playable:
                    raise MediaUnavailable('下一集没有可播放的免费音频，不会跳过它播放其他集。', code='no_resource')
                return page, episode
        raise MediaUnavailable('当前及相邻页面没有找到下一集，可能已到结尾。', code='no_resource')

    @staticmethod
    def _series(catalogue: _Catalogue, album: Album) -> bool:
        return catalogue.kind == 'series' or (catalogue.kind == 'auto' and
               bool(_SERIES.search(catalogue.query + album.title) or '故事' in catalogue.query + album.title))

    async def close(self) -> None:
        self._closed = True
        self._search_revision += 1
        self._resolve_revision += 1
        self._catalogues.clear()
        self._latest = self._current = None
        self._selected = None
