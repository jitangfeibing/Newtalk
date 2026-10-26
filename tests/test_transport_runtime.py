import asyncio
import json
from typing import Any

import pytest

from newtalk.asr import FakeASR
from newtalk.chat import (
    ChatService,
    DialogueCacheCoordinator,
    DialogueSnapshot,
    InMemoryDialogueStore,
)
from newtalk.identity import IdentityService, InMemoryIdentityStore
from newtalk.memory import DisabledMemoryWriteService
from newtalk.memory import DisabledMemoryProvider
from newtalk.profile import SessionProfileCache
from newtalk.transport.runtime import ConnectionRuntime
from newtalk.transport.runtime import _RECENT_EVENT_LIMIT
from newtalk.voiceprint import DisabledVoicePrintClient


class RecordingWebSocket:
    def __init__(self, *, block_first_send: bool = False, fail_sends: bool = False) -> None:
        self.block_first_send = block_first_send
        self.fail_sends = fail_sends
        self.first_send_started = asyncio.Event()
        self.release_first_send = asyncio.Event()
        self.json_events: list[dict[str, Any]] = []
        self.binary_frames: list[bytes] = []
        self._send_count = 0

    async def send_json(self, payload: dict[str, Any]) -> None:
        self._send_count += 1
        if self.fail_sends:
            raise RuntimeError("deterministic send failure")
        if self.block_first_send and self._send_count == 1:
            self.first_send_started.set()
            await self.release_first_send.wait()
        self.json_events.append(payload)

    async def send_bytes(self, payload: bytes) -> None:
        if self.fail_sends:
            raise RuntimeError("deterministic send failure")
        self.binary_frames.append(payload)


class TrackingRuntime(ConnectionRuntime):
    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.stream_tasks: dict[str, asyncio.Task[None]] = {}

    async def _stream_turn(self, turn, *, event_id, dialogue) -> None:
        del event_id, dialogue
        task = asyncio.current_task()
        assert task is not None
        self.stream_tasks[turn.user_text] = task
        await asyncio.Future()


def make_runtime(
    websocket: RecordingWebSocket,
    *,
    runtime_type: type[ConnectionRuntime] = ConnectionRuntime,
) -> ConnectionRuntime:
    identity_service = IdentityService(InMemoryIdentityStore())
    profile_cache = SessionProfileCache(
        device_id="device-1",
        identity_service=identity_service,
        provider=DisabledMemoryProvider(),
    )

    return runtime_type(
        websocket,
        session_id="session-1",
        device_id="device-1",
        chat_service=ChatService(),
        identity_service=identity_service,
        voiceprint_client=DisabledVoicePrintClient(),
        vad=object(),
        recognizer=FakeASR(),
        vad_pre_roll_ms=300,
        dialogue_max_turns=10,
        voiceprint_join_timeout_seconds=0.1,
        profile_cache=profile_cache,
        profile_max_chars=2_000,
        memory_writer=DisabledMemoryWriteService(),
        dialogue_store=InMemoryDialogueStore(),
        dialogue_snapshot=DialogueSnapshot(
            session_id="session-1",
            resumed=False,
            family=(),
            guest=(),
            history=(),
        ),
        dialogue_coordinator=DialogueCacheCoordinator(),
    )


def test_concurrent_turn_replacement_leaves_no_orphan_stream_task() -> None:
    async def scenario() -> None:
        websocket = RecordingWebSocket(block_first_send=True)
        runtime = make_runtime(websocket, runtime_type=TrackingRuntime)
        assert isinstance(runtime, TrackingRuntime)
        await runtime.start()

        first = asyncio.create_task(
            runtime.start_turn(text="first", event_id="event-1", identity=None)
        )
        await asyncio.wait_for(websocket.first_send_started.wait(), timeout=1)
        second = asyncio.create_task(
            runtime.start_turn(text="second", event_id="event-2", identity=None)
        )
        await asyncio.sleep(0)
        websocket.release_first_send.set()

        orphan_tasks: list[asyncio.Task[None]] = []
        try:
            await asyncio.wait_for(asyncio.gather(first, second), timeout=1)
            await asyncio.sleep(0)
            active_task = runtime._turn_state.task
            assert active_task is runtime.stream_tasks["second"]
            orphan_tasks = [
                task
                for task in runtime.stream_tasks.values()
                if task is not active_task and not task.done()
            ]
            assert orphan_tasks == []
            assert [event["type"] for event in websocket.json_events] == [
                "turn_started",
                "turn_cancelled",
                "audio_stop",
                "turn_started",
            ]
        finally:
            for task in orphan_tasks:
                task.cancel()
            if orphan_tasks:
                await asyncio.gather(*orphan_tasks, return_exceptions=True)
            await runtime.close()

    asyncio.run(scenario())


def test_enqueue_fails_immediately_after_sender_stops() -> None:
    async def scenario() -> None:
        runtime = make_runtime(RecordingWebSocket(fail_sends=True))
        await runtime.start()
        try:
            with pytest.raises(RuntimeError, match="deterministic send failure"):
                await runtime.send_json({"type": "first"})
            with pytest.raises(RuntimeError, match="deterministic send failure"):
                await asyncio.wait_for(
                    runtime.send_json({"type": "after-failure"}),
                    timeout=0.1,
                )
        finally:
            await runtime.close()

    asyncio.run(scenario())


def test_event_deduplication_window_is_bounded() -> None:
    runtime = make_runtime(RecordingWebSocket())

    assert runtime.remember_event("oldest") is True
    assert runtime.remember_event("oldest") is False
    for index in range(_RECENT_EVENT_LIMIT):
        assert runtime.remember_event(f"event-{index}") is True

    assert len(runtime._seen_event_ids) == _RECENT_EVENT_LIMIT
    assert runtime.remember_event(f"event-{_RECENT_EVENT_LIMIT - 1}") is False
    assert runtime.remember_event("oldest") is True


def test_reminder_receipts_do_not_block_receive_work_and_close_cancels_database_wait():
    async def scenario():
        entered, cancelled = asyncio.Event(), asyncio.Event()
        disconnected = []

        class Reminders:
            def connect(self, device_id, connection_id, send):
                self.connection_id = connection_id

            async def ack(self, *args):
                entered.set()
                try:
                    await asyncio.Future()
                finally:
                    cancelled.set()

            async def disconnect(self, device_id, connection_id):
                disconnected.append((device_id, connection_id))

        reminders = Reminders()

        class Runtime(ConnectionRuntime):
            def __init__(self, *args, **kwargs):
                super().__init__(*args, reminder_service=reminders, **kwargs)

        socket = RecordingWebSocket()
        runtime = make_runtime(socket, runtime_type=Runtime)
        await runtime.start()
        assert runtime.reminder_playback({'reminder_id': 'r', 'delivery_id': 'd', 'status': 'playing'})
        await asyncio.wait_for(entered.wait(), 1)
        await runtime.send_json({'type': 'pong'})
        assert socket.json_events == [{'type': 'pong'}]
        assert not runtime.reminder_playback({'reminder_id': 'r', 'delivery_id': 'd', 'status': 'invented'})
        await asyncio.wait_for(runtime.close(), 1)
        assert cancelled.is_set()
        assert disconnected == [('device-1', reminders.connection_id)]
        assert runtime._reminder_task.done()
        assert not runtime.reminder_playback({'reminder_id': 'r', 'delivery_id': 'd', 'status': 'completed'})
    asyncio.run(scenario())


@pytest.mark.parametrize('release', ['completed', 'cancelled', 'expired', 'disconnect', 'replacement'])
def test_next_reply_waits_for_reminder_without_blocking_receiver(release):
    from datetime import UTC, datetime, timedelta

    from newtalk.chat import TextDelta
    from newtalk.reminders.service import ReminderService
    from newtalk.reminders.store import InMemoryReminderStore

    async def scenario():
        now = datetime(2026, 10, 26, tzinfo=UTC)
        reminders = ReminderService(InMemoryReminderStore(), clock=lambda: now)
        waiting, model_called = asyncio.Event(), asyncio.Event()

        class Socket(RecordingWebSocket):
            async def send_json(self, payload):
                await super().send_json(payload)
                if payload['type'] == 'turn_waiting':
                    waiting.set()

        class Chat(ChatService):
            async def stream_turn(self, turn, *, device_control_enabled=False):
                model_called.set()
                yield TextDelta(1, '提醒之后回复')
                await asyncio.Future()

        class Runtime(ConnectionRuntime):
            def __init__(self, *args, **kwargs):
                super().__init__(*args, reminder_service=reminders, **kwargs)
                self._chat_service = Chat()

        socket = Socket()
        runtime = make_runtime(socket, runtime_type=Runtime)
        await runtime.start()
        try:
            item = await reminders.create('device-1', '取快递', now + timedelta(seconds=1), 'r')
            await reminders.tick(now + timedelta(seconds=1))
            due = await reminders.get('device-1', item.reminder_id)
            await runtime.start_turn(text='新的问题', event_id='question', identity=None)
            task = runtime._turn_state.task
            await asyncio.wait_for(waiting.wait(), 1)
            assert not model_called.is_set()
            await runtime.send_json({'type': 'pong'})
            assert socket.json_events[-1]['type'] == 'pong'
            if release == 'completed':
                for status in ('playing', 'completed'):
                    assert runtime.reminder_playback({'reminder_id': item.reminder_id,
                                                     'delivery_id': due.delivery_id, 'status': status})
                await asyncio.wait_for(runtime._reminder_receipts.join(), 1)
            elif release == 'cancelled':
                await reminders.cancel('device-1', item.reminder_id)
            elif release == 'expired':
                await reminders.tick(now + timedelta(minutes=3))
            elif release == 'replacement':
                runtime._release_reminder(due.delivery_id)
                await runtime._send_reminder_event({'type': 'reminder_due', 'reminder': {
                    'reminder_id': 'second', 'delivery_id': 'second-delivery', 'content': '另一条提醒',
                }})
                await runtime.send_json({'type': 'pong'})
                assert not model_called.is_set()
                await runtime._send_reminder_event({'type': 'reminder_delivery_ended',
                                                   'reminder_id': 'second', 'delivery_id': 'second-delivery',
                                                   'status': 'completed'})
            else:
                await runtime.close()
                assert task.done()
                assert not model_called.is_set()
                return
            await asyncio.wait_for(model_called.wait(), 1)
        finally:
            await runtime.close()
    asyncio.run(scenario())


def test_slow_music_lookup_keeps_receiver_available_and_new_command_cancels_stale_play():
    from newtalk.media.models import MediaControl

    async def scenario():
        entered, cancelled, delivered = asyncio.Event(), asyncio.Event(), asyncio.Event()

        class Music:
            enabled = True

            async def command(self, device, action, query, index, call_id):
                if action == 'play':
                    entered.set()
                    try:
                        await asyncio.Future()
                    finally:
                        cancelled.set()
                return MediaControl(action, call_id)

        class Runtime(ConnectionRuntime):
            def __init__(self, *args, **kwargs):
                super().__init__(*args, music_service=Music(), **kwargs)

        class Socket(RecordingWebSocket):
            async def send_json(self, payload):
                await super().send_json(payload)
                if payload['type'] == 'media_control':
                    delivered.set()

        socket = Socket()
        runtime = make_runtime(socket, runtime_type=Runtime)
        await runtime.start()
        try:
            await runtime.submit_music_control({'action': 'play', 'query': '歌手'})
            await asyncio.wait_for(entered.wait(), 1)
            await runtime.send_json({'type': 'pong'})
            assert socket.json_events[-1] == {'type': 'pong'}
            assert any(item['type'] == 'media_status' and item['operation_state'] == 'resolving' for item in socket.json_events)
            assert not any(item['type'] == 'media_control' for item in socket.json_events)
            await runtime.submit_music_control({'action': 'stop'})
            await asyncio.wait_for(delivered.wait(), 1)
            assert cancelled.is_set()
            commands = [event for event in socket.json_events if event['type'] == 'media_control']
            assert len(commands) == 1 and commands[0]['action'] == 'stop'
            result = {'request_id': commands[0]['request_id'], 'action': 'pause', 'status': 'paused'}
            assert not runtime.media_result(result)
            assert not runtime.media_result({'request_id': commands[0]['request_id'], 'action': 'stop', 'status': {}})
            result.update(action='stop', status='stopped')
            assert runtime.media_result(result)
            assert not runtime.media_result(result)
        finally:
            await runtime.close()
        assert not runtime._media_session.task_count and not runtime._media_session.pending_requests
    asyncio.run(scenario())


def test_disconnect_cancels_pending_music_lookup_and_no_late_command_is_sent():
    async def scenario():
        entered, closed = asyncio.Event(), asyncio.Event()

        class Music:
            enabled = True

            async def command(self, *args):
                entered.set()
                try:
                    await asyncio.Future()
                finally:
                    closed.set()

        class Runtime(ConnectionRuntime):
            def __init__(self, *args, **kwargs):
                super().__init__(*args, music_service=Music(), **kwargs)

        socket = RecordingWebSocket()
        runtime = make_runtime(socket, runtime_type=Runtime)
        await runtime.start()
        await runtime.submit_music_control({'action': 'play', 'query': '歌手'})
        await asyncio.wait_for(entered.wait(), 1)
        await asyncio.wait_for(runtime.close(), 1)
        assert closed.is_set()
        assert all(item['type'] == 'media_status' for item in socket.json_events)
        assert not runtime._media_session.task_count
    asyncio.run(scenario())


def test_confirmed_media_facts_reach_next_turn_without_late_feedback_overwriting_them():
    from newtalk.media.models import MediaControl, MediaTrack

    async def scenario():
        captured = asyncio.Event()
        turns = []

        class Runtime(ConnectionRuntime):
            async def _stream_turn(self, turn, **kwargs):
                turns.append(turn)
                captured.set()
                await asyncio.Future()

        runtime = make_runtime(RecordingWebSocket(), runtime_type=Runtime)
        await runtime.start()
        try:
            track = MediaTrack('11111111-1111-1111-1111-111111111111', '青花瓷', '周杰伦')
            await runtime.request_media_control(MediaControl('play', 'call-play', track))
            old_id = next(iter(runtime._media_session.pending_requests))
            assert runtime.media_result({'request_id': old_id, 'action': 'play', 'status': 'waiting'})
            await runtime.request_media_control(MediaControl('stop', 'call-stop'))
            current_id = next(key for key in runtime._media_session.pending_requests if key != old_id)
            assert runtime.media_result({'request_id': current_id, 'action': 'stop', 'status': 'stopped'})
            assert runtime.media_result({'request_id': old_id, 'action': 'play', 'status': 'playing'})
            await runtime.start_turn(text='你好，我想跟你聊天', event_id='chat', identity=None)
            await asyncio.wait_for(captured.wait(), 1)
            facts = [message for message in turns[0].messages if message.content.startswith('[网页媒体事实')]
            assert len(facts) == 1
            data = json.loads(facts[0].content.split('\n', 1)[1])
            assert facts[0].role == 'user'
            assert data['action'] == 'stop' and data['client_status'] == 'stopped'
            assert data['track'] == {'title': '青花瓷', 'artist': '周杰伦'}
            assert turns[0].messages[-1].content.endswith('你好，我想跟你聊天')
            await runtime.request_media_control(MediaControl('resume', 'call-resume'))
            assert json.loads(facts[0].content.split('\n', 1)[1])['client_status'] == 'stopped'
            captured.clear()
            await runtime.start_turn(text='现在播放了吗', event_id='next-chat', identity=None)
            await asyncio.wait_for(captured.wait(), 1)
            pending_facts = next(message for message in turns[1].messages if message.content.startswith('[网页媒体事实'))
            assert json.loads(pending_facts.content.split('\n', 1)[1])['client_status'] is None
        finally:
            await runtime.close()
        assert not runtime._media_session.pending_requests
    asyncio.run(scenario())


def test_voice_music_lookup_survives_chat_interruption_and_reaches_the_current_connection():
    from newtalk.chat import ModelToolCall
    from newtalk.media.models import MediaControl, MediaTrack
    from newtalk.tools import ToolRegistry
    from newtalk.tools.music import MusicTool

    async def scenario():
        entered, release, cancelled = asyncio.Event(), asyncio.Event(), asyncio.Event()
        delivered, answered = asyncio.Event(), asyncio.Event()

        class Music:
            enabled = True

            async def command(self, device, action, query, index, call_id):
                entered.set()
                try:
                    await release.wait()
                except asyncio.CancelledError:
                    cancelled.set()
                    raise
                return MediaControl('play', call_id, MediaTrack(
                    '11111111-1111-1111-1111-111111111111', '测试歌曲', '测试歌手'))

        class Model:
            async def stream(self, messages, *, tools=()):
                if tools:
                    yield ModelToolCall('music-call', 'music_control',
                                        '{"action":"play","query":"测试歌曲","index":null}')
                elif messages[-1].role == 'tool':
                    result = json.loads(messages[-1].content)
                    assert result['status'] == 'accepted'
                    yield '正在找歌。'
                else:
                    assert any('测试歌曲' in item.content and 'resolving' in item.content for item in messages)
                    answered.set()
                    yield '还在加载。'

            async def aclose(self):
                pass

        music = Music()
        chat = ChatService(Model(), tool_registry=ToolRegistry((MusicTool(music),)))

        class Runtime(ConnectionRuntime):
            def __init__(self, *args, **kwargs):
                kwargs['chat_service'] = chat
                super().__init__(*args, music_service=music, **kwargs)

        class Socket(RecordingWebSocket):
            async def send_json(self, payload):
                await super().send_json(payload)
                if payload['type'] == 'media_control' and payload['action'] == 'play':
                    delivered.set()

        socket = Socket()
        runtime = make_runtime(socket, runtime_type=Runtime)
        await runtime.start()
        try:
            await runtime.start_turn(text='播放测试歌曲', event_id='play', identity=None)
            await asyncio.wait_for(entered.wait(), 1)
            await runtime.cancel_turn(reason='barge_in')
            assert not cancelled.is_set()
            await runtime.start_turn(text='怎么还没声音', event_id='question', identity=None)
            await asyncio.wait_for(answered.wait(), 1)
            release.set()
            await asyncio.wait_for(delivered.wait(), 1)
            command = next(item for item in socket.json_events if item['type'] == 'media_control')
            assert command.get('turn_id') is None
            assert command['track']['title'] == '测试歌曲'
        finally:
            await runtime.close()
            await chat.aclose()
    asyncio.run(scenario())


def test_chat_model_receives_paused_playback_summary_after_resume_and_new_user_speech():
    from newtalk.media.models import MediaControl, MediaTrack

    async def scenario():
        captured = asyncio.Event()
        messages = []

        class Model:
            async def stream(self, current, *, tools=()):
                messages.extend(current)
                captured.set()
                await asyncio.Future()
                yield '我在。'

            async def aclose(self):
                pass

        chat = ChatService(Model())

        class Runtime(ConnectionRuntime):
            def __init__(self, *args, **kwargs):
                kwargs['chat_service'] = chat
                super().__init__(*args, **kwargs)

        socket = RecordingWebSocket()
        runtime = make_runtime(socket, runtime_type=Runtime)
        await runtime.start()
        try:
            track = MediaTrack('11111111-1111-1111-1111-111111111111', '演员', '薛之谦')
            await runtime.request_media_control(MediaControl('play', 'play', track))
            play = next(event for event in socket.json_events if event['type'] == 'media_control')
            assert runtime.media_result({**play, 'status': 'playing'})
            await runtime.request_media_control(MediaControl('resume', 'resume'))
            resume = [event for event in socket.json_events if event['type'] == 'media_control'][-1]
            assert runtime.media_result({**resume, 'status': 'playing'})
            assert runtime.media_state({**resume, 'status': 'paused'})
            await runtime.start_turn(text='哎，真的好累啊', event_id='tired', identity=None)
            await asyncio.wait_for(captured.wait(), 1)
            fact = next(message for message in messages if message.content.startswith('[网页媒体事实'))
            snapshot = json.loads(fact.content.split('\n', 1)[1])
            assert snapshot['client_status'] == 'paused'
            assert '《演员》已暂停' in snapshot['playback_summary']
            assert '明确继续' in snapshot['playback_summary']
            assert 'desired_playing' not in snapshot
            assert len([event for event in socket.json_events if event['type'] == 'media_control']) == 2
        finally:
            await runtime.close()
            await chat.aclose()
    asyncio.run(scenario())


def test_runtime_keeps_accepted_podcast_lookup_and_catalogue_across_chat_interruption():
    from newtalk.chat import ModelToolCall
    from newtalk.podcasts.models import Album, Episode, EpisodePage
    from newtalk.podcasts.service import PodcastService
    from newtalk.tools import ToolRegistry
    from newtalk.tools.podcasts import PodcastControlTool, PodcastSearchTool

    async def scenario():
        entered, release, delivered, answered = (asyncio.Event() for _ in range(4))
        class Provider:
            enabled = True
            async def search(self, query):
                return (Album(1, '足球评论', '主播'),)
            async def episodes(self, query, album, page=1):
                entered.set()
                await release.wait()
                return EpisodePage(album, 1, 1, (Episode('聊球第一期', 1, 600, 'https://audio.xmcdn.com/a.mp3'),))
            async def aclose(self):
                pass
        service = PodcastService(Provider())
        class Model:
            async def stream(self, messages, *, tools=()):
                names = [item['function']['name'] for item in tools]
                if 'podcast_search' in names:
                    yield ModelToolCall('search', 'podcast_search', '{"query":"足球","kind":"program"}')
                elif names:
                    result = json.loads(messages[-1].content)
                    yield ModelToolCall('play', 'podcast_control', json.dumps(
                        {'action': 'play', 'index': 1, 'selection_id': result['selection_id']}))
                elif messages[-1].role == 'tool':
                    await asyncio.Future()
                else:
                    facts = json.loads(next(item.content for item in messages if item.content.startswith('[网页媒体事实')).split('\n', 1)[1])
                    assert facts['source'] == 'podcast' and facts['operation_state'] == 'resolving'
                    assert facts['catalogues']['podcast']['albums'][0]['title'] == '足球评论'
                    answered.set()
                    yield '还在查找。'
            async def aclose(self):
                pass
        chat = ChatService(Model(), tool_registry=ToolRegistry((PodcastSearchTool(service), PodcastControlTool(service))))
        class Runtime(ConnectionRuntime):
            def __init__(self, *args, **kwargs):
                kwargs.update(chat_service=chat, podcast_service=service)
                super().__init__(*args, **kwargs)
        class Socket(RecordingWebSocket):
            async def send_json(self, payload):
                await super().send_json(payload)
                if payload['type'] == 'media_control' and payload['action'] == 'play':
                    delivered.set()
        socket = Socket()
        runtime = make_runtime(socket, runtime_type=Runtime)
        await runtime.start()
        try:
            await runtime.start_turn(text='放点足球节目听听', event_id='play', identity=None)
            await asyncio.wait_for(entered.wait(), 1)
            await runtime.cancel_turn(reason='barge_in')
            await runtime.start_turn(text='怎么还没声音', event_id='question', identity=None)
            await asyncio.wait_for(answered.wait(), 1)
            release.set()
            await asyncio.wait_for(delivered.wait(), 1)
            control = next(item for item in socket.json_events if item['type'] == 'media_control')
            assert control.get('turn_id') is None
            assert '/api/podcasts/tracks/' in control['track']['stream_url']
        finally:
            await runtime.close()
            await service.close()
            await chat.aclose()
    asyncio.run(scenario())


async def _make_playing_voice_runtime():
    from newtalk.media.models import MediaControl, MediaTrack

    class Source:
        enabled = True

    class Voiceprint(DisabledVoicePrintClient):
        calls = 0
        async def identify(self, **kwargs):
            self.calls += 1
            return await super().identify(**kwargs)

    class Runtime(TrackingRuntime):
        def __init__(self, *args, **kwargs):
            self.entered = asyncio.Event()
            self.turns = []
            super().__init__(*args, music_service=Source(), **kwargs)
            self._voiceprint_client = Voiceprint()
        async def _stream_turn(self, turn, *, event_id, dialogue):
            self.turns.append(turn)
            self.entered.set()
            await super()._stream_turn(turn, event_id=event_id, dialogue=dialogue)

    class Socket(RecordingWebSocket):
        def __init__(self):
            super().__init__()
            self.controls = asyncio.Queue()
        async def send_json(self, payload):
            await super().send_json(payload)
            if payload['type'] == 'media_control':
                self.controls.put_nowait(payload)

    socket = Socket()
    runtime = make_runtime(socket, runtime_type=Runtime)
    await runtime.start()
    await runtime.start_turn(text='之前的回复', event_id='old', identity=None)
    await asyncio.wait_for(runtime.entered.wait(), 1)
    await runtime.request_media_control(MediaControl('play', 'play', MediaTrack(
        '11111111-1111-1111-1111-111111111111', '测试曲', '歌手')))
    play = await socket.controls.get()
    assert runtime.media_result({**play, 'status': 'playing'})
    return runtime, socket, play


@pytest.mark.parametrize('text', ['今天吃什么', '不要暂停', '暂停音乐吗？', '下一集', '播放另一首歌', ''])
def test_playing_media_ignores_ordinary_voice_without_interrupting_or_creating_chat(text):
    from newtalk.asr import AsrFinal, AsrPartial
    from newtalk.audio import SpeechBoundary

    async def scenario():
        runtime, socket, _ = await _make_playing_voice_runtime()
        old_turn = runtime.active_turn_id
        try:
            await runtime._on_speech_boundary(SpeechBoundary('speech_start', 'background', 0.9, 10))
            assert runtime.active_turn_id == old_turn
            assert socket.json_events[-1]['media_only'] is True
            await runtime._on_utterance_audio('background', b'\0\0' * 2000)
            await runtime._on_asr_event('background', AsrPartial('暂停'))
            assert runtime._media_session.observation.client_status == 'playing'
            await runtime._on_asr_event('background', AsrFinal(text))
            assert len(runtime.turns) == 1
            assert runtime._voiceprint_client.calls == 0
            assert runtime._media_session.observation.client_status == 'playing'
            final = next(event for event in socket.json_events if event['type'] == 'asr_final')
            assert final['disposition'] == 'ignored_media' and final['text'] == ''
            assert not runtime._voice_joins
            assert socket.controls.empty()
        finally:
            await runtime.close()
    asyncio.run(scenario())


@pytest.mark.parametrize('text,action,status', [('暂停', 'pause', 'paused'), ('停止播放', 'stop', 'stopped')])
def test_recognized_media_stop_command_exits_playback_mode_and_next_utterance_can_chat(text, action, status):
    from newtalk.asr import AsrFinal
    from newtalk.audio import SpeechBoundary

    async def scenario():
        runtime, socket, _ = await _make_playing_voice_runtime()
        try:
            await runtime._on_speech_boundary(SpeechBoundary('speech_start', 'control', 0.9, 10))
            await runtime._on_asr_event('control', AsrFinal(text))
            control = await asyncio.wait_for(socket.controls.get(), 1)
            assert control['action'] == action
            assert len(runtime.turns) == 1 and runtime._voiceprint_client.calls == 0
            assert runtime.media_result({**control, 'status': status})
            runtime.entered.clear()
            await runtime._on_speech_boundary(SpeechBoundary('speech_start', 'chat', 0.9, 20))
            assert socket.json_events[-1]['media_only'] is False
            await runtime._on_utterance_audio('chat', b'\0\0' * 2000)
            await runtime._on_asr_event('chat', AsrFinal('换一首歌吧'))
            await asyncio.wait_for(runtime.entered.wait(), 1)
            assert runtime.turns[-1].user_text == '换一首歌吧'
            assert runtime._voiceprint_client.calls == 1
            await runtime.submit_music_control({'action': 'resume'})
            resume = await asyncio.wait_for(socket.controls.get(), 1)
            assert runtime.media_result({**resume, 'status': 'playing'})
            await runtime._on_speech_boundary(SpeechBoundary('speech_start', 'again', 0.9, 30))
            assert socket.json_events[-1]['media_only'] is True
        finally:
            await runtime.close()
    asyncio.run(scenario())


def test_speech_that_started_during_playback_stays_ignored_if_playback_ends_before_asr_final():
    from newtalk.asr import AsrFinal
    from newtalk.audio import SpeechBoundary

    async def scenario():
        runtime, socket, play = await _make_playing_voice_runtime()
        try:
            await runtime._on_speech_boundary(SpeechBoundary('speech_start', 'late', 0.9, 10))
            assert runtime.media_state({**play, 'status': 'ended'})
            await runtime._on_asr_event('late', AsrFinal('今天真热'))
            assert len(runtime.turns) == 1
            assert [event for event in socket.json_events if event['type'] == 'asr_final'][-1]['disposition'] == 'ignored_media'
        finally:
            await runtime.close()
    asyncio.run(scenario())


def test_late_stop_command_for_old_playback_cannot_stop_a_new_playback_request():
    from newtalk.asr import AsrFinal
    from newtalk.audio import SpeechBoundary
    from newtalk.media.models import MediaControl, MediaTrack

    async def scenario():
        runtime, socket, _ = await _make_playing_voice_runtime()
        try:
            await runtime._on_speech_boundary(SpeechBoundary('speech_start', 'late-stop', 0.9, 10))
            await runtime.request_media_control(MediaControl('play', 'new-play', MediaTrack(
                '22222222-2222-2222-2222-222222222222', '新曲目', '歌手')))
            new = await socket.controls.get()
            assert runtime.media_result({**new, 'status': 'playing'})
            await runtime._on_asr_event('late-stop', AsrFinal('停止播放'))
            assert runtime._media_session.observation.action == 'play'
            assert socket.controls.empty()
            final = [event for event in socket.json_events if event['type'] == 'asr_final'][-1]
            assert final['disposition'] == 'ignored_media'
        finally:
            await runtime.close()
    asyncio.run(scenario())


def test_asr_timeout_during_pure_playback_does_not_pause_media_or_start_chat():
    from newtalk.audio import SpeechBoundary

    async def scenario():
        runtime, socket, _ = await _make_playing_voice_runtime()
        old_turn = runtime.active_turn_id
        try:
            await runtime._on_speech_boundary(SpeechBoundary('speech_start', 'timeout', 0.9, 10))
            await runtime._on_utterance_audio('timeout', b'\0\0' * 2000)
            await runtime._on_asr_error('timeout', TimeoutError())
            assert runtime.active_turn_id == old_turn
            assert runtime._media_session.observation.client_status == 'playing'
            assert runtime._voiceprint_client.calls == 0 and not runtime._voice_joins
            assert socket.controls.empty()
        finally:
            await runtime.close()
    asyncio.run(scenario())
