import asyncio
import json

import pytest

from newtalk.asr import AsrFinal, FakeASR
from newtalk.chat import ChatService, ModelToolCall, TurnCompleted
from newtalk.chat.device_control import (
    DeviceControl,
    control_from_text,
    parse_device_control,
)
from newtalk.transport.websocket import _dispatch_event
from tests.test_memory import RecordingMemoryProvider
from tests.test_transport_runtime import RecordingWebSocket, make_runtime
from tests.test_websocket import ScriptedVad, audio_input_start, make_client


@pytest.mark.parametrize("text,action", [
    ("别说了！", "stop_reply"), ("请关闭麦克风。", "microphone_off"),
    ("关闭麦克风。", "microphone_off"),
    ("关闭摄像头", "camera_off"), ("断开连接", "disconnect"),
])
def test_explicit_control_commands(text, action):
    assert control_from_text(text) == action


@pytest.mark.parametrize("text", [
    "关闭", "不要关闭麦克风", "怎么断开连接", "他说‘断开连接’",
    "解释一下关闭摄像头的意思", "“别说了”", "关闭音乐",
])
def test_questions_negations_and_quotes_are_not_commands(text):
    assert control_from_text(text) is None


@pytest.mark.parametrize("arguments", [
    "{}", "[]", "null", "invalid", '{"action":"delete_all"}',
    '{"action":"disconnect","device_id":"other"}', '{"action":1}',
])
def test_device_tool_rejects_invalid_or_cross_device_arguments(arguments):
    with pytest.raises(ValueError):
        parse_device_control(ModelToolCall("c1", "device_control", arguments))


@pytest.mark.parametrize("mode", ["tool", "preload"])
def test_model_control_works_for_guest_without_text_tts_or_fake_completion(mode):
    class Model:
        async def stream(self, messages, *, tools=()):
            assert [tool["function"]["name"] for tool in tools] == ["device_control"]
            yield ModelToolCall("c1", "device_control", '{"action":"disconnect"}')

    class TTS:
        audio_format = ChatService().audio_format

        async def stream(self, chunks, *, turn_id):
            async for _ in chunks:
                pytest.fail("control commands must not be spoken")
            if False:
                yield b""

    async def scenario():
        service = ChatService(Model(), TTS(), memory_retrieval_mode=mode)
        turn = service.create_turn(session_id="s", user_text="不聊了，帮我断开吧")
        outputs = [item async for item in service.stream_turn(turn, device_control_enabled=True)]
        assert outputs == [DeviceControl("disconnect", "c1")]
        assert not any(isinstance(item, TurnCompleted) for item in outputs)
    asyncio.run(scenario())


def test_music_stop_in_history_does_not_authorize_stopping_a_new_chat_reply():
    class Model:
        async def stream(self, messages, *, tools=()):
            if tools:
                yield ModelToolCall('stale-stop', 'device_control', '{"action":"stop_reply"}')
            else:
                yield '你好，可以接着聊。'

        async def aclose(self):
            pass

    async def scenario():
        service = ChatService(Model())
        turn = service.create_turn(session_id='s', user_text='你好，我想你跟我聊天。')
        outputs = [item async for item in service.stream_turn(turn, device_control_enabled=True)]
        assert not any(isinstance(item, DeviceControl) for item in outputs)
        assert any(isinstance(item, TurnCompleted) and item.text == '你好，可以接着聊。' for item in outputs)
        await service.aclose()
    asyncio.run(scenario())


def test_invalid_model_control_returns_tool_error_without_executing():
    class Model:
        async def stream(self, messages, *, tools=()):
            if tools:
                yield ModelToolCall("c1", "device_control", '{"action":"delete_all"}')
            else:
                assert json.loads(messages[-1].content)["code"] == "invalid_arguments"
                yield "这个操作不支持。"

    async def scenario():
        service = ChatService(Model())
        turn = service.create_turn(session_id="s", user_text="关闭")
        outputs = [item async for item in service.stream_reply(turn, device_control_enabled=True)]
        assert outputs == ["这个操作不支持。"]
    asyncio.run(scenario())


@pytest.mark.parametrize("mode", ["tool", "preload"])
def test_device_tool_is_independent_of_member_memory_mode(mode):
    class Model:
        async def stream(self, messages, *, tools=()):
            names = [tool["function"]["name"] for tool in tools]
            assert "device_control" in names
            assert ("memory_search" in names) == (mode == "tool")
            yield ModelToolCall("c1", "device_control", '{"action":"microphone_off"}')

    async def scenario():
        provider = RecordingMemoryProvider()
        service = ChatService(Model(), memory_provider=provider, memory_retrieval_mode=mode)
        turn = service.create_turn(session_id="s", user_text="先关闭麦克风吧",
                                   speaker_identity_id="member", device_id="device")
        outputs = [item async for item in service.stream_reply(turn, device_control_enabled=True)]
        assert outputs == [DeviceControl("microphone_off", "c1")]
        assert len(provider.searches) == (1 if mode == "preload" else 0)
    asyncio.run(scenario())


def test_websocket_control_round_trip_duplicate_and_invalid_results():
    with make_client().websocket_connect("/ws") as ws:
        hello = ws.receive_json()
        command = {"type": "device_control", "event_id": "e1", "action": "camera_off"}
        ws.send_json(command)
        request = ws.receive_json()
        assert request["type"] == "device_control"
        ws.send_json({"type": "device_control_result", "request_id": request["request_id"],
                      "action": "camera_off", "status": "not_active"})
        assert ws.receive_json()["status"] == "not_active"
        ws.send_json(command)
        assert ws.receive_json()["code"] == "duplicate_event"
        ws.send_json({"type": "device_control", "event_id": "e2", "action": ["disconnect"]})
        assert ws.receive_json()["code"] == "invalid_device_action"
        ws.send_json({"type": "device_control_result", "request_id": "forged",
                      "action": "disconnect", "status": "accepted"})
        assert ws.receive_json()["code"] == "invalid_device_control_result"
        ws.send_json({"type": "ping"})
        assert ws.receive_json() == {"type": "pong", "session_id": hello["session_id"]}


def test_model_control_reaches_browser_without_completed_reply_and_connection_still_works():
    class Model:
        async def stream(self, messages, *, tools=()):
            assert any(tool["function"]["name"] == "device_control" for tool in tools)
            yield ModelToolCall("c1", "device_control", '{"action":"microphone_off"}')

        async def aclose(self):
            pass

    with make_client(chat_service=ChatService(Model())).websocket_connect("/ws") as ws:
        ws.receive_json()
        ws.send_json({"type": "text_input", "event_id": "e1", "text": "先别听我说话了"})
        started = ws.receive_json()
        assert started["type"] == "turn_started"
        request = ws.receive_json()
        assert request["type"] == "device_control"
        assert request["turn_id"] == started["turn_id"]
        assert ws.receive_json() == {"type": "turn_cancelled", "turn_id": started["turn_id"],
                                     "reason": "device_control"}
        ws.send_json({"type": "device_control_result", "request_id": request["request_id"],
                      "action": "microphone_off", "status": "completed"})
        assert ws.receive_json()["status"] == "completed"
        ws.send_json({"type": "ping"})
        assert ws.receive_json()["type"] == "pong"


@pytest.mark.parametrize("command,action", [
    ("关闭麦克风", "microphone_off"), ("断开连接", "disconnect"),
])
def test_voice_control_can_finish_its_own_capture_without_waiting_on_itself(command, action):
    client = make_client(vad=ScriptedVad(), recognizer=FakeASR(command))
    with client.websocket_connect("/ws") as ws:
        hello = ws.receive_json()
        ws.send_json(audio_input_start())
        assert ws.receive_json()["type"] == "audio_input_ready"
        ws.send_bytes(bytes(640))
        assert ws.receive_json()["type"] == "vad_speech_start"
        ws.send_bytes(bytes(640))
        assert ws.receive_json()["type"] == "vad_speech_end"
        assert ws.receive_json()["type"] == "asr_final"
        request = ws.receive_json()
        assert request["type"] == "device_control"
        assert request["action"] == action
        if action == "microphone_off":
            ws.send_json({"type": "audio_input_stop", "capture_id": "capture-1"})
            assert ws.receive_json()["type"] == "audio_input_stopped"
        ws.send_json({"type": "device_control_result", "request_id": request["request_id"],
                      "action": action, "status": "accepted" if action == "disconnect" else "completed"})
        assert ws.receive_json()["type"] == "device_control_status"
        ws.send_json({"type": "close"})
        assert ws.receive_json()["type"] == "closing"
        assert ws.receive()["type"] == "websocket.close"
    with client.websocket_connect("/ws") as ws:
        resumed = ws.receive_json()
        assert resumed["session_id"] == hello["session_id"]
        assert resumed["dialogue"]["items"] == []


def test_pending_device_controls_are_bounded_and_released_on_close():
    async def scenario():
        ws = RecordingWebSocket()
        runtime = make_runtime(ws)
        await runtime.start()
        for index in range(17):
            await runtime.request_device_control("camera_off", event_id=f"e{index}")
        assert len(runtime._pending_device_controls) == 16
        assert ws.json_events[-1]["code"] == "device_control_busy"
        tasks = [pending.task for pending in runtime._pending_device_controls.values()]
        await runtime.close()
        assert not runtime._pending_device_controls
        assert all(task.done() for task in tasks)
    asyncio.run(scenario())


def test_control_cleanup_does_not_block_receiving_ping():
    async def scenario():
        entered, cleaning, release = asyncio.Event(), asyncio.Event(), asyncio.Event()

        class Model:
            async def stream(self, messages, *, tools=()):
                entered.set()
                try:
                    await asyncio.Future()
                finally:
                    cleaning.set()
                    await release.wait()
                yield "unreachable"

        ws = RecordingWebSocket()
        runtime = make_runtime(ws)
        runtime._chat_service = ChatService(Model())
        await runtime.start()
        try:
            await runtime.start_text_turn(text="讲故事", event_id="text-1", identity_id=None)
            await asyncio.wait_for(entered.wait(), 1)
            await asyncio.wait_for(_dispatch_event(ws, runtime, event={
                "type": "device_control", "event_id": "stop-1", "action": "stop_reply",
            }, session_id=runtime.session_id), 1)
            await asyncio.wait_for(cleaning.wait(), 1)
            await asyncio.wait_for(_dispatch_event(ws, runtime, event={"type": "ping"},
                                                   session_id=runtime.session_id), 1)
            assert ws.json_events[-1]["type"] == "pong"
            assert runtime._device_control_tasks
            release.set()
            await asyncio.wait_for(asyncio.gather(*tuple(runtime._device_control_tasks)), 1)
            assert ws.json_events[-1]["action"] == "stop_reply"
        finally:
            release.set()
            await runtime.close()
            assert not runtime._device_control_tasks
    asyncio.run(scenario())


def test_voice_control_skips_model_and_speaker_wait_and_correlates_result():
    async def scenario():
        ws = RecordingWebSocket()
        runtime = make_runtime(ws)
        await runtime.start()
        try:
            await asyncio.wait_for(runtime._on_asr_event("u1", AsrFinal("关闭麦克风")), 1)
            await asyncio.wait_for(asyncio.gather(*tuple(runtime._device_control_tasks)), 1)
            assert [event["type"] for event in ws.json_events] == ["asr_final", "device_control"]
            request = ws.json_events[-1]
            assert request["action"] == "microphone_off"
            assert request["event_id"] == "voice-u1"
            assert runtime.active_turn_id is None
            assert not runtime._voice_joins
            pending = runtime._pending_device_controls[request["request_id"]]
            assert not runtime.device_control_result({
                "request_id": "forged", "action": "microphone_off", "status": "completed",
            })
            assert not runtime.device_control_result({
                "request_id": request["request_id"], "action": "disconnect", "status": "completed",
            })
            assert runtime.device_control_result({
                "request_id": request["request_id"], "action": "microphone_off", "status": "completed",
            })
            await asyncio.wait_for(pending.task, 1)
            assert ws.json_events[-1]["type"] == "device_control_status"
            assert ws.json_events[-1]["status"] == "completed"
            assert not runtime._pending_device_controls
        finally:
            await runtime.close()
    asyncio.run(scenario())


def test_stop_control_cancels_generation_but_retains_accepted_input():
    async def scenario():
        entered, cancelled = asyncio.Event(), asyncio.Event()

        class Model:
            async def stream(self, messages, *, tools=()):
                entered.set()
                try:
                    await asyncio.Future()
                finally:
                    cancelled.set()
                yield "unreachable"

        ws = RecordingWebSocket()
        runtime = make_runtime(ws)
        runtime._chat_service = ChatService(Model())
        await runtime.start()
        try:
            await runtime.start_text_turn(text="讲个故事", event_id="text-1", identity_id=None)
            await asyncio.wait_for(entered.wait(), 1)
            await runtime.start_text_turn(text="别说了", event_id="text-2", identity_id=None)
            await asyncio.wait_for(cancelled.wait(), 1)
            await asyncio.wait_for(asyncio.gather(*tuple(runtime._device_control_tasks)), 1)
            assert runtime.active_turn_id is None
            assert [item.user_text for item in runtime._dialogue.exchanges] == ["讲个故事"]
            assert ws.json_events[-1]["action"] == "stop_reply"
        finally:
            tasks = [pending.task for pending in runtime._pending_device_controls.values()]
            await runtime.close()
            assert all(task.done() for task in tasks)
            assert not runtime._pending_device_controls
    asyncio.run(scenario())


def test_control_timeout_reports_unknown_and_does_not_block_next_input(monkeypatch):
    import newtalk.transport.runtime as module
    monkeypatch.setattr(module, "_DEVICE_CONTROL_TIMEOUT_SECONDS", 0.01)

    async def scenario():
        ws = RecordingWebSocket()
        runtime = make_runtime(ws)
        await runtime.start()
        try:
            await runtime.start_text_turn(text="关闭摄像头", event_id="text-1", identity_id=None)
            await asyncio.wait_for(asyncio.gather(*tuple(runtime._device_control_tasks)), 1)
            request = ws.json_events[-1]
            pending = runtime._pending_device_controls[request["request_id"]]
            await asyncio.wait_for(pending.task, 1)
            assert ws.json_events[-1]["status"] == "unknown"
            assert not runtime.device_control_result({
                "request_id": request["request_id"], "action": "camera_off", "status": "completed",
            })
            await runtime.start_text_turn(text="你好", event_id="text-2", identity_id=None)
            assert runtime.active_turn_id is not None
        finally:
            await runtime.close()
    asyncio.run(scenario())


def test_cancelled_model_tool_cannot_control_a_new_turn():
    async def scenario():
        entered, release, cancelled = asyncio.Event(), asyncio.Event(), asyncio.Event()

        class Model:
            async def stream(self, messages, *, tools=()):
                if "旧请求" in messages[-1].content:
                    entered.set()
                    try:
                        await release.wait()
                    finally:
                        cancelled.set()
                    yield ModelToolCall("c1", "device_control", '{"action":"disconnect"}')
                else:
                    yield "新回答"

        ws = RecordingWebSocket()
        runtime = make_runtime(ws)
        runtime._chat_service = ChatService(Model())
        await runtime.start()
        try:
            await runtime.start_text_turn(text="旧请求", event_id="e1", identity_id=None)
            await asyncio.wait_for(entered.wait(), 1)
            await runtime.start_text_turn(text="新请求", event_id="e2", identity_id=None)
            await asyncio.wait_for(cancelled.wait(), 1)
            release.set()
            task = runtime._turn_state.task
            assert task is not None
            await asyncio.wait_for(task, 1)
            assert not any(event["type"] == "device_control" for event in ws.json_events)
            assert any(event["type"] == "turn_completed" for event in ws.json_events)
        finally:
            release.set()
            await runtime.close()
    asyncio.run(scenario())
