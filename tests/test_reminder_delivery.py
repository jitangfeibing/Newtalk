import asyncio
import json
import wave
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from io import BytesIO

import pytest
from fastapi.testclient import TestClient

from newtalk.app import create_app
from newtalk.chat import ChatService
from newtalk.chat.models import ModelToolCall
from newtalk.config import AppConfig
from newtalk.identity import IdentityService, InMemoryIdentityStore
from newtalk.reminders.audio import ReminderAudio
from newtalk.reminders.service import ReminderService
from newtalk.reminders.store import InMemoryReminderStore
from newtalk.reminders.tools import execute_reminder_tool
from newtalk.tts import FakeTTS

NOW = datetime(2026, 10, 26, 1, tzinfo=UTC)


def test_manual_replay_has_a_new_delivery_and_restart_does_not_replay_unknown():
    async def scenario():
        store = InMemoryReminderStore()
        service = ReminderService(store, clock=lambda: NOW)
        events = []

        async def send(event):
            events.append(event)

        service.connect('device', 'page', send)
        item = await service.create('device', '喝水', NOW + timedelta(seconds=1), 'r1')
        await asyncio.gather(service.tick(NOW + timedelta(seconds=2)), service.tick(NOW + timedelta(seconds=2)))
        due = [event for event in events if event['type'] == 'reminder_due']
        assert len(due) == 1
        await service.disconnect('device', 'page')
        service.connect('device', 'new-page', send)
        replay = await service.replay('device', item.reminder_id)
        assert replay.delivery_id != due[0]['reminder']['delivery_id']
        assert not await service.ack('device', 'page', item.reminder_id, replay.delivery_id, 'playing')
        restarted = ReminderService(store, clock=lambda: NOW)
        await restarted.start()
        try:
            assert (await restarted.get('device', item.reminder_id)).delivery_status == 'unknown'
        finally:
            await restarted.close()
    asyncio.run(scenario())


def test_reminder_audio_uses_provider_sample_rate_fixed_text_and_cached_delivery():
    async def scenario():
        texts = []

        class Synth(FakeTTS):
            async def stream(self, chunks, *, turn_id):
                texts.extend([text async for text in chunks])
                yield bytes(2400 * 2)

        synth = Synth(sample_rate=24000)
        renderer = ReminderAudio(synth)
        service = ReminderService(InMemoryReminderStore(), clock=lambda: NOW)
        item = replace(await service.create('device', '取快递', NOW + timedelta(seconds=1), 'r'), delivery_id='delivery')
        audio = await renderer.render(item)
        assert await renderer.render(item) == audio
        assert texts == ['提醒你：取快递']
        with wave.open(BytesIO(audio)) as wav:
            assert wav.getframerate() == 24000
            assert wav.getnchannels() == 1
            assert wav.getsampwidth() == 2
            assert wav.getnframes() == 9600
        await renderer.close()
    asyncio.run(scenario())


def test_audio_synthesis_cancellation_closes_provider_stream_and_can_retry():
    async def scenario():
        entered, released, closed = asyncio.Event(), asyncio.Event(), asyncio.Event()

        class Synth(FakeTTS):
            async def stream(self, chunks, *, turn_id):
                try:
                    entered.set()
                    await released.wait()
                    yield b'\x01\x00'
                finally:
                    closed.set()

        renderer = ReminderAudio(Synth())
        service = ReminderService(InMemoryReminderStore(), clock=lambda: NOW)
        item = replace(await service.create('device', '取快递', NOW + timedelta(seconds=1), 'r'), delivery_id='delivery')
        task = asyncio.create_task(renderer.render(item))
        await asyncio.wait_for(entered.wait(), 1)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert closed.is_set()
        released.set()
        assert (await renderer.render(item)).startswith(b'RIFF')
        await renderer.close()
    asyncio.run(scenario())


def test_reminder_tool_is_available_to_guest_without_memory_and_uses_server_time():
    async def scenario():
        service = ReminderService(InMemoryReminderStore(), clock=lambda: NOW)
        calls = []

        class Model:
            async def stream(self, messages, *, tools=()):
                calls.append((messages, tools))
                if len(calls) == 1:
                    yield ModelToolCall(call_id='call', name='reminder_manage', arguments=json.dumps({
                        'action': 'create', 'reminder_id': None, 'content': '取快递',
                        'due_at': None, 'delay_seconds': 300,
                    }))
                else:
                    result = json.loads(messages[-1].content)
                    assert result['status'] == 'ok'
                    yield '已设置五分钟后的提醒。'

            async def aclose(self):
                pass

        chat = ChatService(model=Model(), reminder_service=service, memory_retrieval_mode='preload')
        turn = chat.create_turn(session_id='session', device_id='device', user_text='五分钟后提醒我取快递')
        chunks = [chunk async for chunk in chat.stream_reply(turn, device_control_enabled=True)]
        assert chunks == ['已设置五分钟后的提醒。']
        assert {tool['function']['name'] for tool in calls[0][1]} == {'device_control', 'reminder_manage'}
        assert calls[1][1] == ()
        assert (await service.list('device'))[0].due_at == NOW + timedelta(minutes=5)
        await chat.aclose()
    asyncio.run(scenario())


def test_tool_rejects_ambiguous_arguments_and_propagates_storage_failure():
    async def scenario():
        class Store(InMemoryReminderStore):
            async def create(self, item):
                raise RuntimeError('database unavailable')

        service = ReminderService(Store(), clock=lambda: NOW)
        turn = ChatService().create_turn(session_id='s', device_id='d', user_text='提醒我')
        arguments = {'action': 'create', 'reminder_id': None, 'content': '取快递',
                     'due_at': NOW.isoformat(), 'delay_seconds': 30}
        call = ModelToolCall(call_id='call', name='reminder_manage', arguments=json.dumps(arguments))
        assert json.loads(await execute_reminder_tool(service, turn, call))['status'] == 'error'
        arguments['due_at'] = None
        with pytest.raises(RuntimeError, match='database unavailable'):
            await execute_reminder_tool(service, turn, replace(call, arguments=json.dumps(arguments)))
    asyncio.run(scenario())


@pytest.mark.parametrize('commit_first', [False, True])
def test_cancelling_chat_during_reminder_creation_respects_the_commit_point(commit_first):
    async def scenario():
        entered, closed = asyncio.Event(), asyncio.Event()

        class Store(InMemoryReminderStore):
            async def create(self, item):
                if not commit_first:
                    entered.set()
                    try:
                        await asyncio.Future()
                    finally:
                        closed.set()
                return await super().create(item)

        async def blocked_notification(event):
            entered.set()
            try:
                await asyncio.Future()
            finally:
                closed.set()

        class Model:
            async def stream(self, messages, *, tools=()):
                assert tools, 'cancelled tool must not start a second model request'
                yield ModelToolCall(call_id='call', name='reminder_manage', arguments=json.dumps({
                    'action': 'create', 'reminder_id': None, 'content': '取快递',
                    'due_at': None, 'delay_seconds': 300,
                }))

            async def aclose(self):
                pass

        service = ReminderService(Store(), clock=lambda: NOW)
        service.connect('device', 'connection', blocked_notification)
        chat = ChatService(model=Model(), reminder_service=service)
        turn = chat.create_turn(session_id='s', device_id='device', user_text='五分钟后提醒我取快递')

        async def consume():
            return [event async for event in chat.stream_turn(turn, device_control_enabled=True)]

        task = asyncio.create_task(consume())
        await asyncio.wait_for(entered.wait(), 1)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert closed.is_set()
        saved = await service.list('device')
        assert len(saved) == (1 if commit_first else 0)
        if commit_first:
            assert saved[0].status == 'scheduled'
        await chat.aclose()
    asyncio.run(scenario())


def test_reminder_http_auth_scope_crud_and_playback_audio():
    app = create_app(AppConfig(), identity_service=IdentityService(InMemoryIdentityStore()), persistence_backend='memory')
    with TestClient(app) as client:
        assert client.get('/api/reminders').status_code == 401
        assert client.post('/api/device').status_code == 201
        body = {'content': '取快递', 'due_at': (datetime.now(UTC) + timedelta(minutes=5)).isoformat(),
                'request_key': 'request'}
        created = client.post('/api/reminders', json=body)
        assert created.status_code == 201
        item = created.json()
        assert client.post('/api/reminders', json=body).json() == item
        assert len(client.get('/api/reminders').json()['items']) == 1
        assert client.get(f"/api/reminders/{item['reminder_id']}/audio?delivery_id=forged").status_code == 404
        updated = client.patch(f"/api/reminders/{item['reminder_id']}", json={**{key: body[key] for key in ('content', 'due_at')}, 'content': '拿钥匙'})
        assert updated.json()['content'] == '拿钥匙'
        cookie = client.cookies.get(app.state.config.device_cookie_name)
        client.cookies.clear()
        client.post('/api/device')
        assert client.delete(f"/api/reminders/{item['reminder_id']}").status_code == 404
        client.cookies.clear()
        client.cookies.set(app.state.config.device_cookie_name, cookie)
        with client.websocket_connect('/ws') as ws:
            assert ws.receive_json()['type'] == 'hello'
            original_ack = app.state.reminder_service.ack
            completed = client.portal.call(asyncio.Event)

            async def ack(*args):
                result = await original_ack(*args)
                if args[-1] == 'completed' and result:
                    completed.set()
                return result

            app.state.reminder_service.ack = ack
            # 在应用事件循环中推进调度，不依赖真实时间或 sleep。
            due = datetime.fromisoformat(item['due_at'])
            client.portal.call(app.state.reminder_service.tick, due)
            event = ws.receive_json()
            assert event['type'] == 'reminder_due'
            delivery = event['reminder']['delivery_id']
            audio = client.get(f"/api/reminders/{item['reminder_id']}/audio?delivery_id={delivery}")
            assert audio.status_code == 200 and audio.content.startswith(b'RIFF')
            ws.send_json({'type': 'reminder_playback', 'reminder_id': item['reminder_id'], 'delivery_id': delivery, 'status': 'playing'})
            ws.send_json({'type': 'reminder_playback', 'reminder_id': item['reminder_id'], 'delivery_id': delivery, 'status': 'completed'})
            async def wait_completed():
                await asyncio.wait_for(completed.wait(), 1)

            client.portal.call(wait_completed)
        saved = client.get('/api/reminders').json()['items'][0]
        assert saved['delivery_status'] == 'completed'
        assert client.delete(f"/api/reminders/{item['reminder_id']}").json()['status'] == 'cancelled'
