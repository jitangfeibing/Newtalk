import asyncio
import json

import pytest

from newtalk.chat import ChatService, ModelToolCall
from newtalk.tools import ToolRegistry
from newtalk.tools.vision import VisionTool
from newtalk.transport.runtime import ConnectionRuntime
from newtalk.transport.websocket import _dispatch_event
from tests.test_transport_runtime import RecordingWebSocket, make_runtime
from tests.test_vision import Provider, upload


class Socket(RecordingWebSocket):
    def __init__(self):
        super().__init__()
        self.captures = asyncio.Queue()
        self.completed = asyncio.Queue()

    async def send_json(self, payload):
        await super().send_json(payload)
        if payload['type'] == 'camera_capture':
            self.captures.put_nowait(payload)
        if payload['type'] == 'turn_completed':
            self.completed.put_nowait(payload)


def runtime_for(provider):
    observations = []

    class Model:
        async def stream(self, messages, *, tools=()):
            if messages[-1].role == 'tool':
                result = json.loads(messages[-1].content)
                observations.append(result)
                yield '我看到蓝色物品。' if result.get('observed') else '这次没能看到画面。'
            elif messages[-1].content.endswith('看看'):
                assert any(item['function']['name'] == 'camera_snapshot' for item in tools)
                yield ModelToolCall('look', 'camera_snapshot', '{"question":"看看眼前是什么"}')
            else:
                yield '可以继续聊天。'

        async def aclose(self):
            pass

    chat = ChatService(Model(), tool_registry=ToolRegistry((VisionTool(),)))

    class Runtime(ConnectionRuntime):
        def __init__(self, *args, **kwargs):
            kwargs['chat_service'] = chat
            super().__init__(*args, vision_provider=provider, **kwargs)

    socket = Socket()
    runtime = make_runtime(socket, runtime_type=Runtime)
    runtime.camera_state({'opened': True})
    return runtime, socket, chat, observations


@pytest.mark.parametrize('stage', ['capture', 'provider'])
def test_receiver_handles_ping_and_new_turn_cancels_only_current_vision(stage):
    async def scenario():
        provider = Provider()
        runtime, socket, chat, observations = runtime_for(provider)
        await runtime.start()
        try:
            await runtime.start_turn(text='看看', event_id='look', identity=None)
            capture = await asyncio.wait_for(socket.captures.get(), 1)
            if stage == 'provider':
                await _dispatch_event(socket, runtime, event={'type': 'camera_frame', **upload(capture)}, session_id='s')
                await asyncio.wait_for(provider.entered.wait(), 1)
            await asyncio.wait_for(_dispatch_event(
                socket, runtime, event={'type': 'ping', 'event_id': 'ping'}, session_id='s'), 1)
            assert any(event.get('type') == 'pong' for event in socket.json_events)
            await runtime.cancel_turn(reason='barge_in')
            assert runtime._camera._operation is None
            if stage == 'provider':
                assert provider.cancelled.is_set()
            assert runtime.camera_frame(upload(capture)) is False
            await runtime.start_turn(text='聊聊天', event_id='chat', identity=None)
            done = await asyncio.wait_for(socket.completed.get(), 1)
            assert done['text'] == '可以继续聊天。'
            assert observations == [] and runtime._camera.opened
            assert socket.captures.empty()
        finally:
            await runtime.close()
            await chat.aclose()
    asyncio.run(scenario())


def test_provider_timeout_reaches_main_llm_and_next_chat_is_usable():
    async def scenario():
        provider = Provider()
        provider.timeout_seconds = .1
        runtime, socket, chat, observations = runtime_for(provider)
        await runtime.start()
        try:
            await runtime.start_turn(text='看看', event_id='look', identity=None)
            capture = await asyncio.wait_for(socket.captures.get(), 1)
            assert runtime.camera_frame(upload(capture))
            await asyncio.wait_for(provider.entered.wait(), 1)
            done = await asyncio.wait_for(socket.completed.get(), 2)
            assert done['text'] == '这次没能看到画面。'
            assert observations[0]['code'] == 'timeout' and not observations[0]['observed']
            assert provider.cancelled.is_set() and runtime._camera.opened
            assert runtime.camera_frame(upload(capture)) is False
            await runtime.start_turn(text='继续聊天', event_id='chat', identity=None)
            assert (await asyncio.wait_for(socket.completed.get(), 1))['text'] == '可以继续聊天。'
        finally:
            await runtime.close()
            await chat.aclose()
    asyncio.run(scenario())


def test_visual_member_turn_preserves_dialogue_but_skips_automatic_long_term_memory():
    async def scenario():
        provider = Provider()
        provider.release.set()
        runtime, socket, chat, _ = runtime_for(provider)
        identity_service = runtime._identity_service
        await identity_service._store.create_device(
            device_id='device-1', credential_digest='credential', recovery_digest='recovery')
        identity = await identity_service.create_identity(device_id='device-1', display_name='测试成员')
        enqueued = []

        class Writer:
            async def enqueue_completed_turn(self, turn, text):
                enqueued.append((turn.user_text, text))
                return True

        runtime._memory_writer = Writer()
        await runtime.start()
        try:
            await runtime.start_turn(text='看看', event_id='look', identity=identity)
            capture = await asyncio.wait_for(socket.captures.get(), 1)
            assert runtime.camera_frame(upload(capture))
            assert (await asyncio.wait_for(socket.completed.get(), 1))['text'] == '我看到蓝色物品。'
            assert enqueued == []
            assert runtime._dialogue.exchanges[-1].assistant_text == '我看到蓝色物品。'
            await runtime.start_turn(text='聊聊天', event_id='chat', identity=identity)
            await asyncio.wait_for(socket.completed.get(), 1)
            assert enqueued == [('聊聊天', '可以继续聊天。')]
        finally:
            await runtime.close()
            await chat.aclose()
    asyncio.run(scenario())
