from __future__ import annotations

import json
import logging
from typing import Protocol
from urllib.parse import urlsplit, urlunsplit

import httpx

from newtalk.music.models import MusicUnavailable, Song, SongAudio

MUSIC_API = "https://api.yaohud.cn/api/music/qq"


def audio_url(value: object) -> str:
    if not isinstance(value, str) or not value or len(value) > 8192:
        raise MusicUnavailable("这首歌曲没有可播放地址，请换一首试试。", code="no_resource")
    parsed = urlsplit(value)
    host = (parsed.hostname or "").lower()
    if (parsed.scheme not in {"http", "https"} or parsed.username or parsed.password
            or parsed.port not in {None, 80, 443}
            or not (host.endswith((".tc.qq.com", ".stream.qqmusic.qq.com"))
                    or host == "dl.stream.qqmusic.qq.com")):
        raise MusicUnavailable("音乐源返回了不受支持的播放地址。")
    return urlunsplit(("https", host, parsed.path, parsed.query, ""))


class _HideMusicCredentials(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        # 此接口强制把 key 放在 URL；避免 HTTPX 的请求日志泄漏密钥或播放票据。
        if isinstance(record.args, tuple):
            record.args = tuple(
                arg.copy_with(query=b"[redacted]")
                if isinstance(arg, httpx.URL) and (arg.host == "api.yaohud.cn" or arg.host.endswith(".qq.com"))
                else arg for arg in record.args
            )
        return True


class MusicProvider(Protocol):
    @property
    def enabled(self) -> bool: ...
    async def search(self, query: str) -> tuple[Song, ...]: ...
    async def resolve(self, query: str, song: Song) -> SongAudio: ...
    async def open_audio(self, url: str, range_header: str | None) -> httpx.Response: ...
    async def aclose(self) -> None: ...


class YaohuMusicProvider:
    def __init__(self, api_key: str | None, *, client: httpx.AsyncClient | None = None) -> None:
        self._api_key = api_key
        self._client = client or httpx.AsyncClient(timeout=httpx.Timeout(15, connect=5), follow_redirects=False)
        log = logging.getLogger("httpx")
        if not any(isinstance(item, _HideMusicCredentials) for item in log.filters):
            log.addFilter(_HideMusicCredentials())

    @property
    def enabled(self) -> bool:
        return bool(self._api_key)

    async def _request(self, query: str, index: int | None = None) -> dict:
        if not self.enabled:
            raise MusicUnavailable("音乐演示尚未配置密钥。")
        params = {"key": self._api_key, "msg": query, "size": "128"}
        if index is not None:
            params["n"] = str(index)
        try:
            async with self._client.stream("GET", MUSIC_API, params=params,
                    headers={"Content-Type": "application/x-www-form-urlencoded;charset:utf-8;"}) as response:
                response.raise_for_status()
                body = bytearray()
                async for chunk in response.aiter_bytes():
                    body.extend(chunk)
                    if len(body) > 1_000_000:
                        raise MusicUnavailable("音乐接口响应过大，请稍后再试。")
                result = json.loads(body)
        except httpx.TimeoutException:
            raise MusicUnavailable("音乐源请求超时，请稍后重试。", code="upstream_timeout") from None
        except httpx.HTTPError:
            raise MusicUnavailable("音乐接口暂不可用，请稍后再试。", code="upstream_error") from None
        except ValueError:
            raise MusicUnavailable("音乐接口返回了无效数据。", code="invalid_response") from None
        if not isinstance(result, dict) or result.get("code") != 200:
            raise MusicUnavailable("音乐接口请求失败，请检查密钥、调用额度或稍后重试。")
        data = result.get("data")
        if not isinstance(data, dict):
            raise MusicUnavailable("音乐接口没有返回有效结果。")
        return data

    async def search(self, query: str) -> tuple[Song, ...]:
        data = await self._request(query)
        raw = data.get("songs")
        if not isinstance(raw, list):
            raise MusicUnavailable("没有找到匹配的歌曲，请换个歌手或歌名。")
        songs = []
        for item in raw[:10]:
            if not isinstance(item, dict):
                continue
            index, title, artist, mid = (item.get(key) for key in ("n", "name", "singer", "mid"))
            if (type(index) is int and index > 0 and isinstance(title, str) and title
                    and isinstance(artist, str) and isinstance(mid, str) and mid and len(mid) <= 80):
                songs.append(Song(index, title[:160], artist[:160], mid, str(item.get("album", ""))[:160]))
        if not songs:
            raise MusicUnavailable("没有找到匹配的歌曲，请换个歌手或歌名。")
        return tuple(songs)

    async def resolve(self, query: str, song: Song) -> SongAudio:
        data = await self._request(query, song.index)
        # 供应商通过关键词+序号解析，必须核对真实 mid，避免列表变动后播错歌曲。
        detail = urlsplit(str(data.get("html", "")))
        if detail.path.rstrip("/").split("/")[-1] != song.mid:
            raise MusicUnavailable("搜索结果已变化，请重新搜索后播放。")
        url = audio_url(data.get("musicurl"))
        cover = data.get("picture", "")
        if not isinstance(cover, str) or urlsplit(cover).hostname != "y.qq.com":
            cover = ""
        return SongAudio(song, url, cover)

    async def open_audio(self, url: str, range_header: str | None) -> httpx.Response:
        headers = {"Referer": "https://y.qq.com/", "Accept-Encoding": "identity"}
        if range_header:
            headers["Range"] = range_header
        try:
            for _ in range(4):
                request = self._client.build_request("GET", audio_url(url), headers=headers)
                response = await self._client.send(request, stream=True)
                if response.status_code in {301, 302, 303, 307, 308}:
                    target = response.headers.get("location", "")
                    await response.aclose()
                    url = str(httpx.URL(url).join(target))
                    continue
                if response.status_code not in {200, 206, 416}:
                    await response.aclose()
                    raise MusicUnavailable("播放地址已失效，请重新点播。")
                content_type = response.headers.get("content-type", "").split(";", 1)[0].lower()
                if response.status_code != 416 and not (content_type.startswith("audio/")
                        or content_type in {"application/octet-stream", "video/mp4"}):
                    await response.aclose()
                    raise MusicUnavailable("音频源没有返回可播放的音频。")
                return response
        except httpx.HTTPError:
            raise MusicUnavailable("暂时无法连接音乐音频源。") from None
        raise MusicUnavailable("音乐源重定向过多，请重新点播。")

    async def aclose(self) -> None:
        await self._client.aclose()
