import asyncio
import json
import logging
from dataclasses import replace

import httpx
import pytest
from fastapi.testclient import TestClient

from newtalk.app import create_app
from newtalk.chat import ChatService, ModelToolCall
from newtalk.config import AppConfig, load_config
from newtalk.identity import IdentityService, InMemoryIdentityStore
from newtalk.media.models import MediaIntent, MediaUnavailable
from newtalk.media.session import MediaSession
from newtalk.podcasts.models import Album, Episode, EpisodePage
from newtalk.podcasts.provider import YaohuPodcastProvider, audio_url
from newtalk.podcasts.service import PodcastService
from newtalk.tools import ToolContext, ToolRegistry
from newtalk.tools.podcasts import PodcastControlTool, PodcastSearchTool

ALBUMS = (Album(3, '足球第一视角', '聊球主播', tracks=10), Album(8, '足球访谈', '访谈主播', tracks=20))
EPISODES = (Episode('预告', 0, 30, 'https://audio.xmcdn.com/preview.mp3'),
            Episode('第1集 开场', 1, 600, 'https://audio.xmcdn.com/first.mp3'),
            Episode('第2集', 2, 700, 'https://audio.xmcdn.com/second.mp3'))


class Provider:
    enabled = True

    def __init__(self):
        self.calls = []
        self.closed = False

    async def search(self, query):
        self.calls.append(('search', query))
        return ALBUMS

    async def episodes(self, query, album, page=1):
        self.calls.append(('episodes', album.index, page))
        return EpisodePage(album, page, 1, EPISODES)

    async def open_audio(self, url, range_header):
        assert range_header == 'bytes=0-1'
        return httpx.Response(206, content=b'AB', headers={'content-type': 'audio/mpeg',
                              'content-range': 'bytes 0-1/100', 'accept-ranges': 'bytes'})

    async def aclose(self):
        self.closed = True


class Harness:
    def __init__(self, provider=None):
        self.provider = provider or Provider()
        self.service = PodcastService(self.provider)
        self.resolver = self.service.session('device')
        self.events = []
        self.queue = asyncio.Queue()
        self.media = MediaSession(device_id='device', resolvers={'podcast': self.resolver}, emit=self.emit)
        self.commands = self.media.for_turn(lambda: True)

    async def emit(self, event):
        self.events.append(event)
        self.queue.put_nowait(event)

    async def next(self, event_type):
        async def receive():
            while True:
                event = await self.queue.get()
                if event['type'] == event_type:
                    return event
        return await asyncio.wait_for(receive(), 1)

    async def search(self, kind='program'):
        return await self.commands.discover('podcast', '足球', kind)

    async def play(self, result, index=1, call_id='play'):
        accepted = self.commands.submit(MediaIntent('play', call_id, index=index, source='podcast',
                                                  selection_id=result['selection_id']))
        return accepted, await self.next('media_control')

    async def close(self):
        await self.media.close()
        await self.service.close()


def test_provider_preserves_real_indices_validates_album_and_redacts_credentials(caplog):
    async def scenario():
        requests = []
        changed = False

        def handler(request):
            requests.append(request)
            assert request.url.params['key'] == 'private-key'
            if 'n' not in request.url.params:
                data = {'albums': [{'n': 3, 'title': ALBUMS[0].title, 'anchorName': ALBUMS[0].anchor,
                                     'isPaid': '免费', 'tracksCount': 10}]}
            else:
                assert request.url.params['n'] == '3' and request.url.params['pageId'] == '1'
                data = {'albumMeta': {'n': 3, 'albumTitle': '已变化' if changed else ALBUMS[0].title,
                                      'anchorName': ALBUMS[0].anchor},
                        'audioList': [{'title': '第一集', 'orderNo': 1, 'duration': 600, 'isPaid': False,
                                       'musicurl': 'http://audio.xmcdn.com/first.mp3?token=private-ticket'}],
                        'apiInfo': {'maxPageId': 3}}
            return httpx.Response(200, json={'code': 200, 'data': data})

        provider = YaohuPodcastProvider('private-key', client=httpx.AsyncClient(transport=httpx.MockTransport(handler)))
        try:
            albums = await provider.search('足球')
            assert albums[0].index == 3
            page = await provider.episodes('足球', albums[0])
            assert page.max_page == 3 and page.episodes[0].playable
            assert page.episodes[0].url.startswith('https://audio.xmcdn.com/')
            assert 'private-ticket' not in repr(page)
            changed = True
            with pytest.raises(MediaUnavailable) as caught:
                await provider.episodes('足球', albums[0])
            assert caught.value.code == 'selection_changed'
        finally:
            await provider.aclose()
    with caplog.at_level(logging.INFO, logger='httpx'):
        asyncio.run(scenario())
    assert 'private-key' not in caplog.text and 'private-ticket' not in caplog.text


@pytest.mark.parametrize('url', ['http://127.0.0.1/a', 'https://xmcdn.com.evil.test/a',
                                'file:///audio', 'https://user:pass@audio.xmcdn.com/a'])
def test_audio_urls_cannot_escape_the_allowed_source(url):
    with pytest.raises(MediaUnavailable):
        audio_url(url)


def test_audio_redirects_are_validated_and_closed():
    async def scenario():
        seen = []
        def handler(request):
            seen.append(request)
            return httpx.Response(302, headers={'location': 'http://127.0.0.1/private'})
        provider = YaohuPodcastProvider('key', client=httpx.AsyncClient(transport=httpx.MockTransport(handler)))
        try:
            with pytest.raises(MediaUnavailable):
                await provider.open_audio('https://audio.xmcdn.com/a', None)
            assert len(seen) == 1
        finally:
            await provider.aclose()
    asyncio.run(scenario())


def test_search_display_indices_and_current_episode_are_connection_local():
    async def scenario():
        h = Harness()
        other = h.service.session('device')
        try:
            result = await h.search()
            assert [item['index'] for item in result['albums']] == [1, 2]
            assert not other.facts()['albums']
            accepted, control = await h.play(result)
            assert accepted['status'] == 'accepted' and control['track']['media_type'] == 'podcast'
            assert control['track']['stream_url'].startswith('/api/podcasts/tracks/')
            assert control['track']['title'] == '第1集 开场'
            assert ('episodes', 3, 1) in h.provider.calls
            assert not other.facts()['current']
            assert h.media.report({**control, 'status': 'playing'})
            assert h.media.report_state({**control, 'status': 'paused'})
            assert '已暂停' in h.media.prompt()
            h.commands.submit(MediaIntent('resume', 'resume', source='podcast'))
            resume = await h.next('media_control')
            assert resume['action'] == 'resume'
            assert len([call for call in h.provider.calls if call[0] == 'episodes']) == 1
            await h.media.close()
            assert not h.provider.closed and other.enabled
            assert not h.resolver.facts()['albums']
        finally:
            await other.close()
            await h.close()
    asyncio.run(scenario())


def test_series_starts_at_verified_episode_one_even_when_pages_are_descending():
    async def scenario():
        class Descending(Provider):
            async def episodes(self, query, album, page=1):
                self.calls.append(('episodes', album.index, page))
                episodes = (replace(EPISODES[2], order=100),) if page == 1 else tuple(reversed(EPISODES))
                return EpisodePage(album, page, 4, episodes)
        h = Harness(Descending())
        try:
            result = await h.search('series')
            _, control = await h.play(result)
            assert control['track']['title'] == '第1集 开场'
            assert h.provider.calls[-1] == ('episodes', 3, 4)
        finally:
            await h.close()
    asyncio.run(scenario())


def test_next_episode_uses_order_across_pages_and_change_selects_another_album():
    async def scenario():
        class Paged(Provider):
            async def episodes(self, query, album, page=1):
                self.calls.append(('episodes', album.index, page))
                return EpisodePage(album, page, 2, (EPISODES[1 if page == 1 else 2],))
        h = Harness(Paged())
        try:
            await h.play(await h.search('series'))
            h.commands.submit(MediaIntent('next', 'next', source='podcast'))
            control = await h.next('media_control')
            assert control['track']['title'] == '第2集'
            h.commands.submit(MediaIntent('change', 'change', source='podcast'))
            changed = await h.next('media_control')
            assert changed['track']['album'] == ALBUMS[1].title
            assert changed['track']['title'] == '第1集 开场'
        finally:
            await h.close()
    asyncio.run(scenario())


@pytest.mark.parametrize('episodes', [(EPISODES[2],), (replace(EPISODES[1], paid=True), EPISODES[2])])
def test_missing_or_paid_first_episode_is_not_silently_replaced(episodes):
    async def scenario():
        class NoFirst(Provider):
            async def episodes(self, query, album, page=1):
                return EpisodePage(album, page, 1, episodes)
        h = Harness(NoFirst())
        try:
            result = await h.search('series')
            h.commands.submit(MediaIntent('play', 'play', index=1, source='podcast', selection_id=result['selection_id']))
            error = await h.next('media_error')
            assert error['code'] == 'no_resource'
            assert not any(event['type'] == 'media_control' for event in h.events)
        finally:
            await h.close()
    asyncio.run(scenario())


def test_accepted_selection_remains_bound_to_its_snapshot_after_a_new_search():
    async def scenario():
        h = Harness()
        try:
            original = await h.search()
            h.commands.submit(MediaIntent('play', 'play', index=2, source='podcast', selection_id=original['selection_id']))
            await h.search()
            control = await h.next('media_control')
            assert control['track']['album'] == ALBUMS[1].title
        finally:
            await h.close()
    asyncio.run(scenario())


def test_search_then_play_is_bounded_and_the_accepted_lookup_survives_chat_cancellation():
    async def scenario():
        entered, release, cancelled = asyncio.Event(), asyncio.Event(), asyncio.Event()
        acknowledged = asyncio.Event()
        class Slow(Provider):
            async def episodes(self, query, album, page=1):
                entered.set()
                try:
                    await release.wait()
                except asyncio.CancelledError:
                    cancelled.set()
                    raise
                return await super().episodes(query, album, page)
        h = Harness(Slow())
        class Model:
            def __init__(self):
                self.round = 0
            async def stream(self, messages, *, tools=()):
                self.round += 1
                if self.round == 1:
                    yield ModelToolCall('search', 'podcast_search', '{"query":"足球","kind":"program"}')
                elif self.round == 2:
                    assert [tool['function']['name'] for tool in tools] == ['podcast_control']
                    result = json.loads(messages[-1].content)
                    yield ModelToolCall('play', 'podcast_control', json.dumps(
                        {'action': 'play', 'index': 1, 'selection_id': result['selection_id']}))
                else:
                    assert not tools and json.loads(messages[-1].content)['status'] == 'accepted'
                    acknowledged.set()
                    await asyncio.Future()
                    yield '正在查找。'
            async def aclose(self):
                pass
        chat = ChatService(Model(), tool_registry=ToolRegistry((PodcastSearchTool(h.service), PodcastControlTool(h.service))))
        turn = chat.create_turn(session_id='s', user_text='放点足球节目听听', device_id='device')
        async def collect():
            return [item async for item in chat.stream_reply(turn, device_control_enabled=True, media_commands=h.commands)]
        task = asyncio.create_task(collect())
        try:
            await asyncio.wait_for(acknowledged.wait(), 1)
            await asyncio.wait_for(entered.wait(), 1)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            assert not cancelled.is_set()
            release.set()
            assert (await h.next('media_control'))['track']['media_type'] == 'podcast'
        finally:
            task.cancel()
            await h.close()
            await chat.aclose()
    asyncio.run(scenario())


def test_cancelled_search_cannot_publish_late_candidates():
    async def scenario():
        entered, cancelled, release = asyncio.Event(), asyncio.Event(), asyncio.Event()
        valid = True
        class Late(Provider):
            async def search(self, query):
                entered.set()
                try:
                    await asyncio.Future()
                except asyncio.CancelledError:
                    cancelled.set()
                    await release.wait()
                return ALBUMS
        h = Harness(Late())
        task = asyncio.create_task(h.media.for_turn(lambda: valid).discover('podcast', '足球', 'program'))
        try:
            await asyncio.wait_for(entered.wait(), 1)
            valid = False
            task.cancel()
            await asyncio.wait_for(cancelled.wait(), 1)
            release.set()
            with pytest.raises(asyncio.CancelledError):
                await task
            assert not h.resolver.facts()['albums']
        finally:
            release.set()
            await h.close()
    asyncio.run(scenario())


@pytest.mark.parametrize('text,allowed', [('找一下足球相关节目', False), ('那个聊球的', True),
                                         ('听第二个', True), ('聊球吧', True), ('你帮我选一个', True),
                                         ('随便吧', True), ('找一下听书节目', False)])
def test_search_only_does_not_play_but_natural_selection_can(text, allowed):
    async def scenario():
        h = Harness()
        try:
            result = await h.search()
            turn = ChatService().create_turn(session_id='s', device_id='device', user_text=text)
            response = await PodcastControlTool(h.service).execute(ToolContext(turn, True, media_commands=h.commands),
                ModelToolCall('choose', 'podcast_control', json.dumps(
                    {'action': 'play', 'index': 1, 'selection_id': result['selection_id']})))
            assert (json.loads(response)['status'] == 'accepted') is allowed
        finally:
            await h.close()
    asyncio.run(scenario())


def test_podcast_routes_authenticate_and_scope_audio_resources():
    service = PodcastService(Provider())
    app = create_app(AppConfig(), persistence_backend='memory', podcast_service=service,
                     identity_service=IdentityService(InMemoryIdentityStore()))
    with TestClient(app) as client:
        assert client.get('/api/podcasts/status').status_code == 401
        device = client.post('/api/device').json()['device_id']
        assert client.get('/api/podcasts/status').json()['enabled']
        track = service.prepare(device, ALBUMS[0], EPISODES[1])
        path = track.payload()['stream_url']
        response = client.get(path, headers={'Range': 'bytes=0-1'})
        assert response.status_code == 206 and response.content == b'AB'
        assert response.headers['cache-control'] == 'no-store'
        assert client.get(path, headers={'Range': 'bytes=0-1,4-5'}).status_code == 416
        with client.websocket_connect('/ws') as socket:
            assert socket.receive_json()['podcasts']['enabled'] is True
        client.cookies.clear()
        client.post('/api/device')
        assert client.get(path).status_code == 404
    assert service.provider.closed


def test_podcast_key_is_optional_and_hidden():
    config = load_config({'NEWTALK_PODCAST_API_KEY': 'private-key'})
    assert config.podcast_api_key == 'private-key' and 'private-key' not in repr(config)


def test_change_album_still_works_after_the_first_album_failed_to_load():
    async def scenario():
        class UnavailableFirst(Provider):
            async def episodes(self, query, album, page=1):
                if album == ALBUMS[0]:
                    raise MediaUnavailable('接口暂不可用', code='upstream_error')
                return await super().episodes(query, album, page)
        h = Harness(UnavailableFirst())
        try:
            result = await h.search()
            h.commands.submit(MediaIntent('play', 'play', index=1, source='podcast', selection_id=result['selection_id']))
            assert (await h.next('media_error'))['code'] == 'upstream_error'
            h.commands.submit(MediaIntent('change', 'change', source='podcast'))
            assert (await h.next('media_control'))['track']['album'] == ALBUMS[1].title
        finally:
            await h.close()
    asyncio.run(scenario())


def test_independent_programs_follow_the_returned_descending_episode_order():
    async def scenario():
        class Descending(Provider):
            async def episodes(self, query, album, page=1):
                return EpisodePage(album, 1, 1, tuple(reversed(EPISODES[1:])))
        h = Harness(Descending())
        try:
            await h.play(await h.search('program'))
            h.commands.submit(MediaIntent('next', 'next', source='podcast'))
            assert (await h.next('media_control'))['track']['title'] == '第1集 开场'
        finally:
            await h.close()
    asyncio.run(scenario())


def test_program_discovery_filters_obvious_novels_without_changing_provider_indices():
    async def scenario():
        class Mixed(Provider):
            async def search(self, query):
                return (Album(1, '足球有声小说|多播剧', '小说主播'), ALBUMS[0])
        h = Harness(Mixed())
        try:
            result = await h.search('program')
            assert len(result['albums']) == 1 and result['albums'][0]['index'] == 1
            await h.play(result)
            assert ('episodes', 3, 1) in h.provider.calls
        finally:
            await h.close()
    asyncio.run(scenario())


@pytest.mark.parametrize('attempt', ['other', 'third'])
def test_followup_query_cannot_execute_unrelated_tools_or_a_third_operation(attempt):
    async def scenario():
        calls = []
        class Handler:
            followup_tools = ('play',)
            def __init__(self, name):
                self.name = name
            def definition(self, context):
                return {'type': 'function', 'function': {'name': self.name, 'parameters': {'type': 'object'}}}
            async def execute(self, context, call):
                calls.append(self.name)
                return '{"status":"ok"}'
        class Model:
            def __init__(self):
                self.round = 0
            async def stream(self, messages, *, tools=()):
                self.round += 1
                if self.round == 1:
                    yield ModelToolCall('s', 'search', '{}')
                elif self.round == 2:
                    assert [item['function']['name'] for item in tools] == ['play']
                    yield ModelToolCall('p', 'other' if attempt == 'other' else 'play', '{}')
                elif attempt == 'third':
                    assert not tools
                    yield ModelToolCall('third', 'play', '{}')
                else:
                    assert json.loads(messages[-1].content)['code'] == 'unsupported_tool'
                    yield '无法执行这个动作。'
            async def aclose(self):
                pass
        registry = ToolRegistry(Handler(name) for name in ('search', 'play', 'other'))
        chat = ChatService(Model(), tool_registry=registry)
        turn = chat.create_turn(session_id='s', user_text='执行')
        assert registry.select(ToolContext(turn)).following('search', '{"status":"error"}').names == ()
        try:
            if attempt == 'third':
                with pytest.raises(ValueError, match='limit'):
                    _ = [item async for item in chat.stream_reply(turn)]
                assert calls == ['search', 'play']
            else:
                _ = [item async for item in chat.stream_reply(turn)]
                assert calls == ['search']
        finally:
            await chat.aclose()
    asyncio.run(scenario())


@pytest.mark.parametrize('orders', [(10, 8), (None, None)])
def test_program_next_follows_list_entries_even_with_gaps_or_missing_episode_numbers(orders):
    async def scenario():
        class Gapped(Provider):
            async def episodes(self, query, album, page=1):
                return EpisodePage(album, 1, 1, tuple(replace(item, order=number)
                                   for item, number in zip(EPISODES[1:], orders, strict=True)))
        h = Harness(Gapped())
        try:
            await h.play(await h.search('program'))
            h.commands.submit(MediaIntent('next', 'next', source='podcast'))
            assert (await h.next('media_control'))['track']['title'] == '第2集'
        finally:
            await h.close()
    asyncio.run(scenario())


@pytest.mark.parametrize('text', ['我喜欢这个节目', '继续聊聊刚才的节目'])
def test_ordinary_program_chat_cannot_replay_a_historical_resume(text):
    async def scenario():
        h = Harness()
        try:
            await h.play(await h.search())
            turn = ChatService().create_turn(session_id='s', device_id='device', user_text=text)
            response = await PodcastControlTool(h.service).execute(ToolContext(turn, True, media_commands=h.commands),
                ModelToolCall('old-resume', 'podcast_control', '{"action":"resume","index":null,"selection_id":null}'))
            assert json.loads(response)['reason'] == 'not_requested'
            assert len([event for event in h.events if event['type'] == 'media_control']) == 1
        finally:
            await h.close()
    asyncio.run(scenario())
