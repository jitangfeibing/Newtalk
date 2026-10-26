from __future__ import annotations

import json
import logging
from typing import Protocol
from urllib.parse import urlsplit, urlunsplit

import httpx

from newtalk.media.models import MediaUnavailable
from newtalk.podcasts.models import Album, Episode, EpisodePage

API_URL = 'https://api.yaohud.cn/api/music/xima'
logger = logging.getLogger(__name__)


def audio_url(value: str) -> str:
    parsed = urlsplit(value)
    host = (parsed.hostname or '').lower()
    if (not value or len(value) > 8192 or parsed.scheme not in {'http', 'https'}
            or parsed.username or parsed.password or parsed.port not in {None, 80, 443}
            or not host.endswith('.xmcdn.com')):
        raise MediaUnavailable('听书源未返回受支持的音频地址。', code='invalid_response')
    return urlunsplit(('https', host, parsed.path, parsed.query, ''))


class _HideCredentials(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        if isinstance(record.args, tuple):
            record.args = tuple(arg.copy_with(query=b'[redacted]')
                                if isinstance(arg, httpx.URL) and
                                (arg.host == 'api.yaohud.cn' or arg.host.endswith('.xmcdn.com'))
                                else arg for arg in record.args)
        return True


class PodcastProvider(Protocol):
    @property
    def enabled(self) -> bool: ...
    async def search(self, query: str) -> tuple[Album, ...]: ...
    async def episodes(self, query: str, album: Album, page: int = 1) -> EpisodePage: ...
    async def open_audio(self, url: str, range_header: str | None) -> httpx.Response: ...
    async def aclose(self) -> None: ...


def _integer(value: object, default: int = 0) -> int:
    return value if type(value) is int and value >= 0 else default


class YaohuPodcastProvider:
    def __init__(self, api_key: str | None, *, client: httpx.AsyncClient | None = None) -> None:
        self._key = api_key
        self._client = client or httpx.AsyncClient(timeout=httpx.Timeout(12, connect=5), follow_redirects=False)
        log = logging.getLogger('httpx')
        if not any(isinstance(item, _HideCredentials) for item in log.filters):
            log.addFilter(_HideCredentials())

    @property
    def enabled(self) -> bool:
        return bool(self._key)

    async def _request(self, query: str, *, index: int | None = None, page: int = 1) -> dict:
        if not self.enabled:
            raise MediaUnavailable('听书服务尚未配置密钥。', code='unavailable')
        params = {'key': self._key, 'msg': query, 'g': 10}
        if index is not None:
            params.update(n=index, pageId=page)
        try:
            async with self._client.stream('GET', API_URL, params=params,
                    headers={'Content-Type': 'application/x-www-form-urlencoded;charset:utf-8;'}) as response:
                response.raise_for_status()
                body = bytearray()
                async for chunk in response.aiter_bytes():
                    body.extend(chunk)
                    if len(body) > 1_000_000:
                        raise MediaUnavailable('听书接口响应过大。', code='invalid_response')
                result = json.loads(body)
        except httpx.TimeoutException:
            raise MediaUnavailable('听书源请求超时，请稍后重试。', code='upstream_timeout') from None
        except httpx.HTTPStatusError as exc:
            logger.warning('podcast_source_failed stage=%s album_index=%s page=%s http_status=%s',
                           'episodes' if index is not None else 'search', index, page, exc.response.status_code)
            raise MediaUnavailable('听书源暂不可用。', code='upstream_error') from None
        except httpx.HTTPError:
            raise MediaUnavailable('听书源暂不可用。', code='upstream_error') from None
        except ValueError:
            raise MediaUnavailable('听书源返回无效数据。', code='invalid_response') from None
        if not isinstance(result, dict) or result.get('code') != 200 or not isinstance(result.get('data'), dict):
            raise MediaUnavailable('听书接口请求失败，请稍后重试。', code='upstream_error')
        return result['data']

    async def search(self, query: str) -> tuple[Album, ...]:
        data = await self._request(query)
        raw = data.get('albums')
        if not isinstance(raw, list):
            raise MediaUnavailable('听书源没有返回有效专辑列表。', code='invalid_response')
        albums = []
        for item in raw[:10]:
            if not isinstance(item, dict):
                continue
            if (type(item.get('n')) is int and item['n'] > 0
                    and isinstance(item.get('title'), str) and item['title']
                    and isinstance(item.get('anchorName'), str)):
                albums.append(Album(item['n'], item['title'][:160], item['anchorName'][:100],
                                    item.get('isPaid') in (True, '收费', '付费'), _integer(item.get('tracksCount'))))
        if not albums:
            raise MediaUnavailable('没有找到匹配的专辑，请换个名称或主题。', code='no_resource')
        return tuple(albums)

    async def episodes(self, query: str, album: Album, page: int = 1) -> EpisodePage:
        data = await self._request(query, index=album.index, page=page)
        meta = data.get('albumMeta', {})
        # 接口只提供关键词与序号，必须核对专辑信息，防止缓存重排后播错内容。
        if (not isinstance(meta, dict) or meta.get('albumTitle') != album.title
                or meta.get('anchorName') != album.anchor or meta.get('n') != album.index):
            raise MediaUnavailable('专辑搜索结果已变化，请重新搜索后选择。', code='selection_changed')
        raw = data.get('audioList')
        if not isinstance(raw, list):
            raise MediaUnavailable('专辑没有返回有效的单集列表。', code='invalid_response')
        episodes = []
        for item in raw[:30]:
            if not isinstance(item, dict) or not isinstance(item.get('title'), str):
                continue
            value = item.get('musicurl')
            url = audio_url(value) if isinstance(value, str) and value else ''
            episodes.append(Episode(item['title'][:200], _integer(item.get('orderNo')) or None,
                                    _integer(item.get('duration')), url,
                                    item.get('isPaid') in (True, 'true', 1) or item.get('pay') in ('收费', '付费')))
        info = data.get('apiInfo', {})
        maximum = _integer(info.get('maxPageId'), page) if isinstance(info, dict) else page
        return EpisodePage(album, page, max(page, min(maximum, 100_000)), tuple(episodes))

    async def open_audio(self, url: str, range_header: str | None) -> httpx.Response:
        headers = {'Accept-Encoding': 'identity'}
        if range_header:
            headers['Range'] = range_header
        try:
            for _ in range(4):
                request = self._client.build_request('GET', audio_url(url), headers=headers)
                response = await self._client.send(request, stream=True)
                if response.status_code in {301, 302, 303, 307, 308}:
                    target = response.headers.get('location', '')
                    await response.aclose()
                    url = str(httpx.URL(url).join(target))
                    continue
                kind = response.headers.get('content-type', '').split(';', 1)[0].lower()
                if (response.status_code not in {200, 206, 416}
                        or (response.status_code != 416 and not (kind.startswith('audio/') or kind in
                            {'application/octet-stream', 'video/mp4'}))):
                    await response.aclose()
                    raise MediaUnavailable('听书音频暂不可播放，请重新点播。', code='no_resource')
                return response
        except httpx.HTTPError:
            raise MediaUnavailable('暂时无法连接听书音频源。', code='upstream_error') from None
        raise MediaUnavailable('听书音频重定向过多。', code='invalid_response')

    async def aclose(self) -> None:
        await self._client.aclose()
