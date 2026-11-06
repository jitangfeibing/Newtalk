import asyncio
import base64
import io
import json
from dataclasses import replace

import httpx
import pytest
from PIL import Image

from newtalk.chat import ChatService, ModelToolCall
from newtalk.config import AppConfig, ConfigError
from newtalk.tools import ToolContext, ToolRegistry
from newtalk.tools.vision import VisionTool
from newtalk.vision.provider import CameraFrame, QwenVisionProvider, VisionUnavailable
from newtalk.vision.session import CameraSession, MAX_FRAME_BYTES


def jpeg():
    buffer = io.BytesIO()
    Image.new('RGB', (32, 24), 'blue').save(buffer, 'JPEG')
    return buffer.getvalue()


class Provider:
    enabled = True
    timeout_seconds = 1

    def __init__(self):
        self.entered = asyncio.Event()
        self.release = asyncio.Event()
        self.cancelled = asyncio.Event()
        self.frames = []

    async def analyze(self, frame, question):
        self.frames.append((frame, question))
        self.entered.set()
        try:
            await self.release.wait()
            return '画面里有蓝色物品。'
        except asyncio.CancelledError:
            self.cancelled.set()
            raise


def setup(provider=None):
    provider = provider or Provider()
    events = []
    capture_ready = asyncio.Event()

    async def emit(event, **kwargs):
        assert kwargs == {'turn_id': event['turn_id']}
        events.append(event)
        if event['type'] == 'camera_capture':
            capture_ready.set()

    session = CameraSession(provider, emit)
    session.state({'opened': True})
    return session, provider, events, capture_ready


def upload(capture, **overrides):
    return {'request_id': capture['request_id'], 'turn_id': capture['turn_id'],
            'mime_type': 'image/jpeg', 'image_base64': base64.b64encode(jpeg()).decode(), **overrides}


async def begin(session, ready, events, turn='turn'):
    ready.clear()
    task = asyncio.create_task(session.for_turn(turn, lambda: True).analyze('这是什么？'))
    await asyncio.wait_for(ready.wait(), 1)
    return task, next(event for event in reversed(events) if event['type'] == 'camera_capture')


def test_provider_request_and_observation_are_separate_from_main_llm():
    async def scenario():
        def handle(request):
            assert request.headers['Authorization'] == 'Bearer private-test-key'
            data = json.loads(request.content)
            assert data['model'] == 'qwen3-vl-plus'
            assert data['enable_thinking'] is False and data['stream'] is False
            assert len(data['messages']) == 1
            content = data['messages'][0]['content']
            assert base64.b64decode(content[0]['image_url']['url'].split(',')[1]) == jpeg()
            assert content[1]['text'].endswith('这是什么？')
            return httpx.Response(200, json={'choices': [{'message': {'content': '蓝色方块'}}]})

        provider = QwenVisionProvider('private-test-key', client=httpx.AsyncClient(transport=httpx.MockTransport(handle)))
        assert await provider.analyze(CameraFrame(jpeg(), 32, 24), '这是什么？') == '蓝色方块'
        assert 'private-test-key' not in repr(provider)
        assert base64.b64encode(jpeg()).decode() not in repr(CameraFrame(jpeg(), 32, 24))
        await provider.close()
    asyncio.run(scenario())


@pytest.mark.parametrize('status,body,code', [
    (401, {}, 'auth_failed'), (429, {}, 'provider_error'),
    (200, {}, 'invalid_response'), (200, {'choices': []}, 'invalid_response'),
    (200, {'choices': [{'message': {'content': ''}}]}, 'invalid_response'),
])
def test_provider_failures_do_not_expose_response_body(status, body, code):
    async def scenario():
        client = httpx.AsyncClient(transport=httpx.MockTransport(lambda _: httpx.Response(status, json=body)))
        provider = QwenVisionProvider('secret', client=client)
        with pytest.raises(VisionUnavailable) as error:
            await provider.analyze(CameraFrame(jpeg(), 32, 24), '看一下')
        assert str(error.value) == code
        await provider.close()
    asyncio.run(scenario())


def test_single_frame_success_then_new_question_requires_fresh_capture(caplog):
    async def scenario():
        session, provider, events, ready = setup()
        provider.release.set()
        for turn in ('a', 'b'):
            task, capture = await begin(session, ready, events, turn)
            assert session.frame(upload(capture, turn_id='old-turn')) is False
            assert session.frame(upload(capture, request_id='other-connection')) is False
            assert session.frame(upload(capture)) is True
            assert session.frame(upload(capture)) is False
            result = await task
            assert result['observed'] is True and result['observation'] == '画面里有蓝色物品。'
            assert session.frame(upload(capture)) is False
            assert session.opened
        assert len(provider.frames) == 2
        assert len({event['request_id'] for event in events}) == 2
        assert 'image_base64' not in caplog.text and '画面里有蓝色物品' not in caplog.text
        await session.close()
    asyncio.run(scenario())


@pytest.mark.parametrize('bad', [
    {'mime_type': 'image/png'}, {'image_base64': 'bad%%%'} ,
    {'image_base64': base64.b64encode(b'not a jpeg').decode()},
    {'image_base64': 'a' * (((MAX_FRAME_BYTES + 2) // 3) * 4 + 4)},
    {'status': 'error'},
])
def test_invalid_frames_are_unavailable_and_never_sent_to_provider(bad):
    async def scenario():
        session, provider, events, ready = setup()
        task, capture = await begin(session, ready, events)
        assert session.frame(upload(capture, **bad))
        result = await task
        assert result['observed'] is False and result['status'] == 'unavailable'
        assert provider.frames == [] and session.opened
        await session.close()
    asyncio.run(scenario())


@pytest.mark.parametrize('stage', ['capture', 'provider'])
def test_barge_in_cancels_visual_work_and_discards_late_frames(stage):
    async def scenario():
        session, provider, events, ready = setup()
        task, capture = await begin(session, ready, events)
        if stage == 'provider':
            session.frame(upload(capture))
            await asyncio.wait_for(provider.entered.wait(), 1)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert session._operation is None and session._frame is None
        assert session.frame(upload(capture)) is False
        assert session.opened
        if stage == 'provider':
            assert provider.cancelled.is_set()
        assert not any(event.get('status') == 'completed' for event in events)
        await session.close()
    asyncio.run(scenario())


@pytest.mark.parametrize('stage', ['capture', 'provider'])
def test_camera_off_cancels_request_but_returns_failure_to_current_chat(stage):
    async def scenario():
        session, provider, events, ready = setup()
        task, capture = await begin(session, ready, events)
        if stage == 'provider':
            session.frame(upload(capture))
            await asyncio.wait_for(provider.entered.wait(), 1)
        session.state({'opened': False})
        result = await task
        assert result['code'] == 'camera_off' and not result['observed']
        assert not session.opened and session.frame(upload(capture)) is False
        await session.close()
    asyncio.run(scenario())


@pytest.mark.parametrize('stage', ['capture', 'provider'])
def test_timeout_skips_vision_without_closing_preview(stage):
    async def scenario():
        session, provider, events, ready = setup()
        provider.timeout_seconds = .02
        task, capture = await begin(session, ready, events)
        if stage == 'provider':
            session.frame(upload(capture))
            await asyncio.wait_for(provider.entered.wait(), 1)
        result = await asyncio.wait_for(task, 1)
        assert result['code'] == 'timeout' and result['observed'] is False
        assert session.opened and session._operation is None
        assert session.frame(upload(capture)) is False
        await session.close()
    asyncio.run(scenario())


def test_disconnect_cancels_inflight_provider_and_prevents_new_capture():
    async def scenario():
        session, provider, events, ready = setup()
        task, capture = await begin(session, ready, events)
        session.frame(upload(capture))
        await asyncio.wait_for(provider.entered.wait(), 1)
        await session.close()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert provider.cancelled.is_set() and not session.opened
        with pytest.raises(asyncio.CancelledError):
            await session.for_turn('next', lambda: True).analyze('再看看')
    asyncio.run(scenario())


@pytest.mark.parametrize('use_vision', [False, True])
def test_chat_only_captures_on_actual_tool_call_and_returns_observation_to_main_llm(use_vision):
    async def scenario():
        session, provider, events, ready = setup()
        provider.release.set()

        class Model:
            async def stream(self, messages, *, tools=()):
                if not use_vision:
                    yield '我们聊聊天。'
                elif tools:
                    yield ModelToolCall('visual-call', 'camera_snapshot', '{"question":"手里是什么？"}')
                else:
                    result = json.loads(messages[-1].content)
                    assert result['observed'] and result['observation'] == '画面里有蓝色物品。'
                    assert 'image_base64' not in messages[-1].content
                    yield '你手里有蓝色物品。'

            async def aclose(self):
                pass

        service = ChatService(Model(), tool_registry=ToolRegistry((VisionTool(),)))
        turn = service.create_turn(session_id='s', user_text='帮我看看手里是什么' if use_vision else '聊聊天')

        async def reply():
            return [text async for text in service.stream_reply(
                turn, device_control_enabled=True, vision_commands=session.for_turn(turn.turn_id, lambda: True))]

        task = asyncio.create_task(reply())
        if use_vision:
            await asyncio.wait_for(ready.wait(), 1)
            capture = next(event for event in events if event['type'] == 'camera_capture')
            session.frame(upload(capture))
        result = await task
        assert result == (['你手里有蓝色物品。'] if use_vision else ['我们聊聊天。'])
        assert len(provider.frames) == int(use_vision)
        assert bool(events) == use_vision
        await service.aclose()
        await session.close()
    asyncio.run(scenario())


def test_tool_is_not_offered_to_non_camera_clients_and_validates_arguments():
    async def scenario():
        service = ChatService()
        context = ToolContext(service.create_turn(session_id='s', user_text='看看'))
        tool = VisionTool()
        assert tool.definition(context) is None
        context = replace(context, client_tools_enabled=True)
        assert tool.definition(context) is None
        for args in ('{}', '[]', '{"question":""}', '{"question":1}', '{"question":"看", "extra":1}'):
            result = json.loads(await tool.execute(context, ModelToolCall('id', tool.name, args)))
            assert result['code'] == 'invalid_arguments'
        await service.aclose()
    asyncio.run(scenario())


def test_vision_config_key_is_private_and_timeout_is_bounded():
    config = AppConfig.from_mapping({'VERSION_API_KEY': 'private-vision-key'})
    assert config.vision_api_key == 'private-vision-key' and config.vision_model == 'qwen3-vl-plus'
    assert 'private-vision-key' not in repr(config)
    for value in ('0', '-1', 'NaN', 'inf', '61', 'bad'):
        with pytest.raises(ConfigError):
            AppConfig.from_mapping({'NEWTALK_VISION_TIMEOUT_SECONDS': value})
