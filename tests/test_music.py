import asyncio
import json
import logging
from dataclasses import replace
from types import SimpleNamespace

import httpx
import pytest
from fastapi.testclient import TestClient

from newtalk.app import create_app
from newtalk.chat import ChatMessage, ChatService, ModelToolCall, TurnCompleted
from newtalk.config import AppConfig, load_config
from newtalk.identity import IdentityService, InMemoryIdentityStore
from newtalk.media.models import MediaControl, MediaIntent, MediaObservation
from newtalk.media.session import MediaSession
from newtalk.music.api import stream_music
from newtalk.music.models import MusicUnavailable, Song, SongAudio
from newtalk.music.provider import YaohuMusicProvider, audio_url
from newtalk.music.service import MusicService
from newtalk.tools import ToolContext, ToolRegistry
from newtalk.tools.music import MusicTool

SONG = Song(1, '测试曲', '歌手', 'mid-one')

class RecordingCommands:
    def __init__(self):
        self.intents = []

    def submit(self, intent):
        self.intents.append(intent)
        return {'status': 'accepted', 'operation_id': 'test-operation', 'action': intent.action}



class Provider:
    enabled = True

    def __init__(self):
        self.queries = []
        self.closed = False

    async def search(self, query):
        self.queries.append(query)
        return (SONG,)

    async def resolve(self, query, song):
        return SongAudio(song, 'https://aqqmusic.tc.qq.com/demo.m4a')

    async def open_audio(self, url, range_header):
        assert range_header == 'bytes=0-1'
        return httpx.Response(206, content=b'AB', headers={
            'content-type': 'audio/mp4', 'content-range': 'bytes 0-1/100', 'accept-ranges': 'bytes',
        })

    async def aclose(self):
        self.closed = True


@pytest.mark.parametrize('codes,expected', [
    (['no_resource', 'no_resource'], 'no_resource'),
    (['no_resource', 'upstream_timeout'], 'upstream_timeout'),
])
def test_candidate_failures_preserve_upstream_cause_instead_of_guessing_membership(codes, expected):
    async def scenario():
        class FailingProvider(Provider):
            async def search(self, query):
                return tuple(replace(SONG, index=index + 1) for index in range(len(codes)))

            async def resolve(self, query, song):
                raise MusicUnavailable('候选没有资源或请求超时', code=codes[song.index - 1])

        service = MusicService(FailingProvider())
        try:
            with pytest.raises(MusicUnavailable) as caught:
                await service.command('device', 'play', '歌曲', None, 'call')
            assert caught.value.code == expected
            assert '会员' not in str(caught.value)
        finally:
            await service.close()
    asyncio.run(scenario())


def test_music_key_is_loaded_but_never_in_config_repr():
    config = load_config({'NEWTALK_MUSIC_API_KEY': 'test-secret'})
    assert config.music_api_key == 'test-secret'
    assert 'test-secret' not in repr(config)
    assert load_config({}).music_api_key is None


@pytest.mark.parametrize('text,offered', [('继续播放', False), ('暂停音乐', True), ('播放周杰伦晴天', True)])
def test_music_can_be_explicitly_requested_while_a_podcast_is_active(text, offered):
    service = MusicService(Provider())
    turn = ChatService(SimpleNamespace()).create_turn(session_id='s', user_text=text)
    context = ToolContext(turn, True, media_commands=SimpleNamespace(active_source='podcast'))
    assert (MusicTool(service).definition(context) is not None) is offered


def test_provider_parses_actual_contract_validates_mid_and_redacts_key(caplog):
    async def scenario():
        seen = []

        async def handler(request):
            seen.append(request)
            assert request.url.params['key'] == 'test-secret'
            assert request.url.params['size'] == '128'
            if 'n' not in request.url.params:
                data = {'songs': [{'n': 1, 'name': SONG.title, 'singer': SONG.artist, 'mid': SONG.mid}]}
            else:
                data = {'name': SONG.title, 'songname': SONG.artist,
                        'html': 'https://y.qq.com/n/ryqq/songDetail/mid-one',
                        'musicurl': 'http://aqqmusic.tc.qq.com/song.m4a?vkey=ticket',
                        'picture': 'https://y.qq.com/cover.jpg'}
            return httpx.Response(200, json={'code': 200, 'data': data})

        provider = YaohuMusicProvider('test-secret', client=httpx.AsyncClient(transport=httpx.MockTransport(handler)))
        songs = await provider.search('测试歌手')
        audio = await provider.resolve('测试歌手', songs[0])
        assert audio.url.startswith('https://aqqmusic.tc.qq.com/')
        assert len(seen) == 2
        assert seen[1].url.params['n'] == '1'
        assert 'application/x-www-form-urlencoded' in seen[0].headers['content-type']
        await provider.aclose()
    with caplog.at_level(logging.INFO, logger='httpx'):
        asyncio.run(scenario())
    assert 'test-secret' not in caplog.text


@pytest.mark.parametrize('response', [
    {'code': 403, 'msg': 'test-secret'},
    {'code': 200, 'data': {'songs': []}},
    {'code': 200, 'data': None},
])
def test_provider_errors_are_explicit_and_do_not_echo_secret(response):
    async def scenario():
        provider = YaohuMusicProvider('test-secret', client=httpx.AsyncClient(
            transport=httpx.MockTransport(lambda request: httpx.Response(200, json=response))))
        try:
            with pytest.raises(MusicUnavailable) as caught:
                await provider.search('歌手')
            assert 'test-secret' not in str(caught.value)
        finally:
            await provider.aclose()
    asyncio.run(scenario())


def test_song_identity_and_audio_hosts_are_verified_and_vip_url_is_not_a_fallback():
    async def scenario():
        response = {'code': 200, 'data': {'html': 'https://y.qq.com/song/mid-other',
                                       'musicurl': 'http://aqqmusic.tc.qq.com/song.m4a'}}
        provider = YaohuMusicProvider('key', client=httpx.AsyncClient(
            transport=httpx.MockTransport(lambda request: httpx.Response(200, json=response))))
        with pytest.raises(MusicUnavailable, match='变化'):
            await provider.resolve('歌手', SONG)
        response['data']['html'] = 'https://y.qq.com/song/mid-one'
        response['data']['musicurl'] = ''
        response['data']['vipmusicurl'] = 'https://paid.example/secret'
        with pytest.raises(MusicUnavailable, match='没有可播放'):
            await provider.resolve('歌手', SONG)
        await provider.aclose()
        for url in ('http://127.0.0.1/audio', 'http://aqqmusic.tc.qq.com.evil.test/a', 'file:///a',
                    'http://user:pass@aqqmusic.tc.qq.com/a'):
            with pytest.raises(MusicUnavailable):
                audio_url(url)
    asyncio.run(scenario())


def test_redirect_to_an_untrusted_audio_host_is_closed_and_rejected():
    async def scenario():
        requests = []

        def handler(request):
            requests.append(request)
            return httpx.Response(302, headers={'location': 'http://127.0.0.1/private'})

        provider = YaohuMusicProvider('key', client=httpx.AsyncClient(transport=httpx.MockTransport(handler)))
        with pytest.raises(MusicUnavailable):
            await provider.open_audio('https://aqqmusic.tc.qq.com/song', None)
        assert len(requests) == 1
        await provider.aclose()
    asyncio.run(scenario())


def test_service_tries_only_bounded_candidates_and_scopes_cached_resources():
    async def scenario():
        attempted = []

        class Source(Provider):
            async def search(self, query):
                return tuple(replace(SONG, index=index, mid=f'mid-{index}') for index in range(1, 6))

            async def resolve(self, query, song):
                attempted.append(song.index)
                if song.index != 2:
                    raise MusicUnavailable('无地址')
                return await super().resolve(query, song)

        service = MusicService(Source())
        result = await service.command('device', 'play', '歌手', None, 'call')
        assert result.track.title == SONG.title and attempted == [1, 2]
        assert service.resource('device', result.track.track_id).url.startswith('https:')
        with pytest.raises(LookupError):
            service.resource('other', result.track.track_id)
        with pytest.raises(MusicUnavailable, match='先说'):
            await service.command('other', 'play', None, 2, 'call')
        attempted.clear()
        with pytest.raises(MusicUnavailable):
            await service.command('device', 'play', None, 1, 'call')
        assert attempted == [1]
        assert await service.command('device', 'pause', None, None, 'call') == MediaControl('pause', 'call')
        await service.close()
    asyncio.run(scenario())


@pytest.mark.parametrize('text', ['播放歌曲', '播放音乐', '我想听周杰伦的歌', '放一首青花瓷', '来一首周杰伦', '换一首歌'])
def test_music_tool_is_registered_for_guest_and_acknowledges_an_independent_operation(text):
    async def scenario():
        service = MusicService(Provider())

        delivered = asyncio.Event()
        emitted = []

        async def emit(event):
            emitted.append(event)
            if event['type'] == 'media_control':
                delivered.set()

        media = MediaSession(device_id='device', resolvers={'music': service}, emit=emit)

        class Model:
            async def stream(self, messages, *, tools=()):
                if tools:
                    assert [item['function']['name'] for item in tools] == ['music_control']
                    yield ModelToolCall('call', 'music_control', '{"action":"play","query":"歌手","index":null}')
                else:
                    result = json.loads(messages[-1].content)
                    assert result['status'] == 'accepted' and result['operation_id']
                    yield '正在查找歌曲。'

            async def aclose(self):
                pass

        chat = ChatService(Model(), tool_registry=ToolRegistry((MusicTool(service),)))
        turn = chat.create_turn(session_id='s', device_id='device', user_text=text)
        outputs = [item async for item in chat.stream_turn(turn, device_control_enabled=True, media_commands=media)]
        assert not any(isinstance(item, MediaControl) for item in outputs)
        assert any(isinstance(item, TurnCompleted) and item.text == '正在查找歌曲。' for item in outputs)
        await asyncio.wait_for(delivered.wait(), 1)
        assert any(item['type'] == 'media_control' and item['action'] == 'play' for item in emitted)
        assert MusicTool(service).definition(ToolContext(turn)) is None
        await media.close()
        await chat.aclose()
        await service.close()
    asyncio.run(scenario())


def test_cancelled_music_resolution_does_not_emit_a_late_client_action():
    async def scenario():
        entered, closed = asyncio.Event(), asyncio.Event()

        class Source(Provider):
            async def resolve(self, query, song):
                entered.set()
                try:
                    await asyncio.Future()
                finally:
                    closed.set()

        service = MusicService(Source())
        task = asyncio.create_task(service.command('device', 'play', '歌手', None, 'call'))
        await asyncio.wait_for(entered.wait(), 1)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert closed.is_set() and not service._resources
        await service.close()
    asyncio.run(scenario())


@pytest.mark.parametrize(('text', 'action'), [('暂停音乐。', 'pause'), ('继续播放音乐', 'resume'), ('停止音乐', 'stop')])
def test_explicit_music_control_cannot_be_changed_to_play_by_the_model(text, action):
    async def scenario():
        source = Provider()
        service = MusicService(source)
        turn = ChatService(SimpleNamespace()).create_turn(session_id='s', device_id='device', user_text=text)
        commands = RecordingCommands()
        result = await MusicTool(service).execute(ToolContext(turn, True, media_commands=commands), ModelToolCall(
            'call', 'music_control', '{"action":"play","query":"歌手","index":null}'))
        assert json.loads(result)['status'] == 'accepted'
        assert commands.intents == [MediaIntent(action, 'call')]
        assert source.queries == []
        await service.close()
    asyncio.run(scenario())


@pytest.mark.parametrize(('text', 'action'), [('呃，我想继续听。', 'resume'), ('暂停一下吧。', 'pause')])
def test_short_natural_music_controls_remain_available(text, action):
    async def scenario():
        service = MusicService(Provider())
        turn = ChatService(SimpleNamespace()).create_turn(session_id='s', device_id='device', user_text=text)
        tool = MusicTool(service)
        assert tool.definition(ToolContext(turn, True)) is not None
        commands = RecordingCommands()
        result = await tool.execute(ToolContext(turn, True, media_commands=commands), ModelToolCall(
            'call', 'music_control', f'{{"action":"{action}","query":null,"index":null}}'))
        assert json.loads(result)['status'] == 'accepted'
        assert commands.intents == [MediaIntent(action, 'call')]
        await service.close()
    asyncio.run(scenario())


@pytest.mark.parametrize('text', [
    '我喜欢听这首歌哎。',
    '嗯，我挺喜欢听青花瓷周杰伦的这首歌。',
    '你好，我想你跟我聊天。',
    '继续聊聊刚才的话题。',
    '我想听你说话。',
])
def test_music_history_does_not_turn_current_chatter_into_a_media_action(text):
    async def scenario():
        source = Provider()
        service = MusicService(source)

        class Model:
            async def stream(self, messages, *, tools=()):
                if any(item['function']['name'] == 'music_control' for item in tools):
                    yield ModelToolCall('wrong-resume', 'music_control',
                                        '{"action":"resume","query":null,"index":null}')
                elif not any(message.role == 'tool' or message.content.startswith('[网页媒体事实') for message in messages):
                    yield '我不能播放音乐。'
                else:
                    fact = next(message for message in messages if message.content.startswith('[网页媒体事实'))
                    assert json.loads(fact.content.split('\n', 1)[1])['enabled'] is True
                    yield '我在，可以接着聊。'

            async def aclose(self):
                pass

        chat = ChatService(Model(), tool_registry=ToolRegistry((MusicTool(service),)))
        turn = chat.create_turn(session_id='s', device_id='device', user_text=text, messages=(
            ChatMessage('user', MediaObservation('previous-play', 'play', 'playing', None).prompt()),
            ChatMessage('user', '播放周杰伦的青花瓷'), ChatMessage('user', '继续。'),
            ChatMessage('user', text),
        ))
        outputs = [item async for item in chat.stream_turn(turn, device_control_enabled=True)]
        assert not any(isinstance(item, MediaControl) for item in outputs)
        assert any(isinstance(item, TurnCompleted) and item.text == '我在，可以接着聊。' for item in outputs)
        assert source.queries == []
        await chat.aclose()
        await service.close()
    asyncio.run(scenario())


def test_unrequested_direct_music_call_is_a_successful_no_op_not_a_playback_failure():
    async def scenario():
        source = Provider()
        service = MusicService(source)
        turn = ChatService(SimpleNamespace()).create_turn(session_id='s', device_id='device', user_text='我喜欢这首歌')
        result = await MusicTool(service).execute(ToolContext(turn, True), ModelToolCall(
            'wrong-resume', 'music_control', '{"action":"resume","query":null,"index":null}'))
        payload = json.loads(result)
        assert payload['status'] == 'ok' and payload['action'] == 'none'
        assert payload['reason'] == 'not_requested' and payload['enabled'] is True
        assert source.queries == []
        await service.close()
    asyncio.run(scenario())


def test_music_http_stream_auth_range_and_resource_scope():
    service = MusicService(Provider())
    app = create_app(AppConfig(), persistence_backend='memory',
                     identity_service=IdentityService(InMemoryIdentityStore()), music_service=service)
    with TestClient(app) as client:
        assert client.get('/api/music/status').status_code == 401
        device = client.post('/api/device').json()['device_id']
        assert client.get('/api/music/status').json()['enabled']
        result = client.portal.call(service.command, device, 'play', '歌手', None, 'call')
        path = result.track.payload()['stream_url']
        response = client.get(path, headers={'Range': 'bytes=0-1'})
        assert response.status_code == 206 and response.content == b'AB'
        assert response.headers['content-range'] == 'bytes 0-1/100'
        assert response.headers['cache-control'] == 'no-store'
        assert client.get(path, headers={'Range': 'bytes=0-1,3-5'}).status_code == 416
        client.cookies.clear()
        client.post('/api/device')
        assert client.get(path).status_code == 404
    assert service.provider.closed


def test_cancelled_http_stream_closes_upstream_audio():
    async def scenario():
        entered, closed = asyncio.Event(), asyncio.Event()

        class Stream(httpx.AsyncByteStream):
            async def __aiter__(self):
                yield b'A'
                entered.set()
                await asyncio.Future()

            async def aclose(self):
                closed.set()

        class Source(Provider):
            async def open_audio(self, url, range_header):
                return httpx.Response(200, stream=Stream(), headers={'content-type': 'audio/mp4'})

        service = MusicService(Source())
        command = await service.command('device', 'play', '歌手', None, 'call')
        request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(music_service=service)), headers={})
        response = await stream_music(command.track.track_id, request, SimpleNamespace(device_id='device'))
        assert await anext(response.body_iterator) == b'A'
        next_chunk = asyncio.create_task(anext(response.body_iterator))
        await asyncio.wait_for(entered.wait(), 1)
        next_chunk.cancel()
        with pytest.raises(asyncio.CancelledError):
            await next_chunk
        assert closed.is_set()
        await service.close()
    asyncio.run(scenario())
