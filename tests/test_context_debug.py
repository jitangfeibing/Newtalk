import asyncio
import json
from types import SimpleNamespace

import pytest

from newtalk.app import create_chat_service
from newtalk.chat import ChatMessage, ChatService, FakeLLM, OpenAICompatibleChatModel
from newtalk.config import AppConfig, ConfigError, load_config
from newtalk.memory import MemorySearchResult

from tests.test_memory import RecordingMemoryProvider
from tests.test_memory import MemoryToolModel
from tests.test_openai_compatible import SequencedClient, StubStream
from tests.test_transport_runtime import RecordingWebSocket, make_runtime


def read_events(path):
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def test_context_debug_config_is_opt_in() -> None:
    assert load_config({}).context_debug is False
    assert load_config({"NEWTALK_CONTEXT_DEBUG": "true"}).context_debug is True
    with pytest.raises(ConfigError, match="NEWTALK_CONTEXT_DEBUG"):
        load_config({"NEWTALK_CONTEXT_DEBUG": "sometimes"})


def test_empty_search_is_explicitly_not_evidence_of_absence() -> None:
    result = json.loads(MemorySearchResult(()).to_tool_content(max_chars=500))
    assert result == {"status": "no_match", "memories": []}


def test_production_prompt_keeps_persona_and_adds_context_rules() -> None:
    service = create_chat_service(AppConfig(
        llm_backend="openai", llm_api_key="unused-test-key", llm_model="test",
        llm_system_prompt="你叫 Newtalk，回答简短。",
        context_input_tokens=8000,
        memory_retrieval_mode="preload", memory_preload_timeout_seconds=0.6,
    ))
    try:
        prompt = service._model.system_prompt
        assert service._model._input_token_budget == 8000
        assert service._memory_retrieval_mode == "preload"
        assert service._memory_preload_timeout_seconds == 0.6
        assert "你叫 Newtalk，回答简短。" in prompt
        assert "当前对话" in prompt
        assert "未命中不代表" in prompt
        assert "不要编造" in prompt
        assert "不作为指令" in prompt
        assert "我在记忆里没找到" in prompt
    finally:
        asyncio.run(service.aclose())


def test_disabled_debugger_does_not_create_files(tmp_path, capsys) -> None:
    from newtalk.context_debug import ContextDebugger

    path = tmp_path / "private" / "context.jsonl"
    debugger = ContextDebugger(path=path)
    debugger.record("example", turn_id="t", content="private-body")
    debugger.close()
    assert not path.parent.exists()
    assert "private-body" not in capsys.readouterr().err


def test_debugger_write_failure_does_not_break_chat_or_print_content(tmp_path, capsys) -> None:
    from newtalk.context_debug import ContextDebugger

    debugger = ContextDebugger(enabled=True, path=tmp_path)
    debugger.record("example", turn_id="t", content="private-body")
    debugger.close()
    assert "private-body" not in capsys.readouterr().err


def test_debugger_rotates_private_logs(tmp_path) -> None:
    from newtalk.context_debug import ContextDebugger

    path = tmp_path / "context.jsonl"
    debugger = ContextDebugger(enabled=True, path=path, max_bytes=400, backup_count=1)
    for index in range(8):
        debugger.record("example", turn_id=str(index), content="x" * 160)
    debugger.close()
    assert {item.name for item in tmp_path.iterdir()} == {"context.jsonl", "context.jsonl.1"}
    assert read_events(path)[-1]["turn_id"] == "7"


def test_debug_records_exact_requests_for_both_tool_rounds(tmp_path, capsys) -> None:
    from newtalk.context_debug import ContextDebugger

    path = tmp_path / "context.jsonl"

    async def scenario():
        call = SimpleNamespace(index=0, id="call-1", function=SimpleNamespace(
            name="memory_search", arguments='{"query":"上次面试"}',
        ))
        client = SequencedClient([
            StubStream([SimpleNamespace(type="chunk", chunk=SimpleNamespace(
                choices=[SimpleNamespace(delta=SimpleNamespace(tool_calls=[call]))],
            ))]),
            StubStream([SimpleNamespace(type="content.delta", delta="算法题准备不足。")]),
        ])
        service = ChatService(
            OpenAICompatibleChatModel(api_key="secret-not-in-log", model="test",
                                      system_prompt="真实系统提示", client=client),
            memory_provider=RecordingMemoryProvider(),
            context_debug=ContextDebugger(enabled=True, path=path),
        )
        turn = service.create_turn(
            session_id="session-1", user_text="还记得吗", device_id="device-1",
            speaker_identity_id="member-1",
            messages=(ChatMessage("system", "[当前说话人画像]\n喜欢编程"),
                      ChatMessage("user", "还记得吗")),
        )
        try:
            assert [text async for text in service.stream_reply(turn)] == ["算法题准备不足。"]
        finally:
            await service.aclose()
        return turn, client

    turn, client = asyncio.run(scenario())
    events = read_events(path)
    requests = [event for event in events if event["event"] == "llm_request"]
    assert [event["request"] for event in requests] == client.chat.completions.requests
    assert [event["request_index"] for event in requests] == [1, 2]
    assert all(event["budget"]["after_tokens_estimate"] <= event["budget"]["input_token_limit"]
               for event in requests)
    assert all(event["budget"]["is_estimate"] for event in requests)
    assert all(event["turn_id"] == turn.turn_id for event in requests)
    assert requests[0]["request"]["messages"][0]["content"] == "真实系统提示"
    assert requests[1]["request"]["messages"][-1]["role"] == "tool"
    assert any(event["event"] == "memory_search_started" and event["query"] == "上次面试"
               for event in events)
    assert "secret-not-in-log" not in path.read_text(encoding="utf-8")
    assert "真实系统提示" not in capsys.readouterr().err


def test_concurrent_turn_traces_are_isolated_and_cancelled_scope_is_reset(tmp_path) -> None:
    from newtalk.context_debug import ContextDebugger

    path = tmp_path / "context.jsonl"

    async def scenario():
        entered = asyncio.Event()

        class WaitingStream(StubStream):
            async def __anext__(self):
                entered.set()
                await asyncio.Future()

        client = SequencedClient([
            WaitingStream([]),
            StubStream([SimpleNamespace(type="content.delta", delta="第二轮")]),
            StubStream([SimpleNamespace(type="content.delta", delta="无追踪作用域")]),
        ])
        model = OpenAICompatibleChatModel(api_key="unused", model="test", client=client)
        service = ChatService(model, context_debug=ContextDebugger(enabled=True, path=path))
        first = service.create_turn(session_id="one", user_text="first")
        second = service.create_turn(session_id="two", user_text="second")

        async def collect(turn):
            return [text async for text in service.stream_reply(turn)]

        first_task = asyncio.create_task(collect(first))
        try:
            await asyncio.wait_for(entered.wait(), timeout=1)
            assert await collect(second) == ["第二轮"]
            first_task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await first_task
            assert [text async for text in model.stream((ChatMessage("user", "outside"),))] == ["无追踪作用域"]
        finally:
            first_task.cancel()
            await asyncio.gather(first_task, return_exceptions=True)
            await service.aclose()
        return first, second

    first, second = asyncio.run(scenario())
    requests = [event for event in read_events(path) if event["event"] == "llm_request"]
    assert [(event["turn_id"], event["session_id"], event["request_index"])
            for event in requests] == [(first.turn_id, "one", 1), (second.turn_id, "two", 1)]


def test_runtime_trace_reports_candidates_separately_from_final_budget_selection(tmp_path) -> None:
    from newtalk.context_debug import ContextDebugger

    path = tmp_path / "context.jsonl"

    async def scenario():
        runtime = make_runtime(RecordingWebSocket())
        runtime._chat_service = ChatService(
            FakeLLM(chunk_delay_seconds=0),
            context_debug=ContextDebugger(enabled=True, path=path),
        )
        old_turns = []
        for index in range(10):
            old = runtime._chat_service.create_turn(session_id=runtime.session_id, user_text=str(index))
            runtime._dialogue.commit(old, "a" * 100)
            old_turns.append(old)
        await runtime.start()
        try:
            await runtime.start_turn(text="现在的问题", event_id="text-1", identity=None)
            await asyncio.wait_for(runtime._turn_state.task, timeout=2)
        finally:
            await runtime.close()
            await runtime._chat_service.aclose()
        return old_turns

    old = asyncio.run(scenario())
    events = read_events(path)
    selected = next(event for event in events if event["event"] == "context_selected")
    assert selected["identity"]["reason"] == "explicit_guest"
    assert selected["history"][-1] == {
        "turn_id": old[-1].turn_id, "reason": "candidate", "reply_status": "completed",
    }
    assert all(item["reason"] == "candidate" for item in selected["history"])
    ended = next(event for event in events if event["event"] == "turn_end")
    assert ended["dialogue_memory"] == "completed"
    assert ended["dialogue_database"] == "completed"
    assert ended["memory_queue"] == "not_attempted"
    assert ended["memory_skip_reason"] == "guest"
    assert any(event["event"] == "context_excluded" and event["reason"] == "window_limit"
               and event["excluded_turn_id"] == old[0].turn_id for event in events)


def test_interrupted_reply_is_omitted_but_user_input_is_in_next_context(tmp_path) -> None:
    from newtalk.context_debug import ContextDebugger

    path = tmp_path / "context.jsonl"

    async def scenario():
        entered = asyncio.Event()

        class WaitingModel:
            async def stream(self, messages, *, tools=()):
                if "旧问题" in messages[-1].content:
                    entered.set()
                    await asyncio.Future()
                yield "新回答"

            async def aclose(self):
                pass

        runtime = make_runtime(RecordingWebSocket())
        runtime._chat_service = ChatService(WaitingModel(), context_debug=ContextDebugger(enabled=True, path=path))
        await runtime.start()
        try:
            await runtime.start_turn(text="旧问题", event_id="text-old", identity=None)
            old_id = runtime.active_turn_id
            await asyncio.wait_for(entered.wait(), timeout=1)
            await runtime.cancel_turn(reason="barge_in")
            await runtime.start_turn(text="新问题", event_id="text-new", identity=None)
            await asyncio.wait_for(runtime._turn_state.task, timeout=2)
            assert [item.user_text for item in runtime._dialogue.exchanges] == ["旧问题", "新问题"]
            assert runtime._dialogue.exchanges[0].assistant_text == ""
        finally:
            await runtime.close()
            await runtime._chat_service.aclose()
        return old_id

    old_id = asyncio.run(scenario())
    events = read_events(path)
    selected = [event for event in events if event["event"] == "context_selected"][-1]
    assert {"turn_id": old_id, "reason": "candidate", "reply_status": "user_only"} in selected["history"]
    assert not any(item["turn_id"] == old_id for item in selected["recent_exclusions"])
    ended = next(item for item in events if item["event"] == "turn_end" and item["turn_id"] == old_id)
    assert ended["user_input_memory"] == "retained"
    assert ended["dialogue_memory"] == "user_only"


def test_cancel_during_persistence_is_reported_as_unknown_not_false(tmp_path) -> None:
    from newtalk.chat import InMemoryDialogueStore
    from newtalk.context_debug import ContextDebugger

    path = tmp_path / "context.jsonl"

    async def scenario():
        entered = asyncio.Event()

        class WaitingStore(InMemoryDialogueStore):
            async def complete(self, **kwargs):
                entered.set()
                await asyncio.Future()

        runtime = make_runtime(RecordingWebSocket())
        runtime._dialogue_store = WaitingStore()
        runtime._chat_service = ChatService(FakeLLM(chunk_delay_seconds=0), context_debug=ContextDebugger(enabled=True, path=path))
        await runtime.start()
        try:
            await runtime.start_turn(text="保存期间打断", event_id="text-1", identity=None)
            await asyncio.wait_for(entered.wait(), timeout=2)
            await runtime.cancel_turn(reason="barge_in")
        finally:
            await runtime.close()
            await runtime._chat_service.aclose()

    asyncio.run(scenario())
    ended = next(event for event in read_events(path) if event["event"] == "turn_end")
    assert ended["outcome"] == "cancelled"
    assert ended["dialogue_memory"] == "completed"
    assert ended["dialogue_database"] == "unknown"
    assert ended["memory_queue"] == "not_attempted"


def test_no_search_is_explicit_in_debug_trace(tmp_path) -> None:
    from newtalk.context_debug import ContextDebugger

    path = tmp_path / "context.jsonl"

    async def scenario():
        service = ChatService(FakeLLM(chunk_delay_seconds=0), context_debug=ContextDebugger(enabled=True, path=path))
        try:
            turn = service.create_turn(session_id="s", user_text="你好")
            assert [text async for text in service.stream_reply(turn)] == ["我收到了：", "你好"]
        finally:
            await service.aclose()

    asyncio.run(scenario())
    usage = next(event for event in read_events(path) if event["event"] == "memory_search_usage")
    assert usage["offered"] is False
    assert usage["used"] is False


@pytest.mark.parametrize("fail_search", [False, True])
def test_empty_and_unavailable_memory_remain_distinct_in_trace(tmp_path, fail_search) -> None:
    from newtalk.context_debug import ContextDebugger

    path = tmp_path / "context.jsonl"

    class EmptyProvider(RecordingMemoryProvider):
        async def search(self, scope, **kwargs):
            if fail_search:
                raise RuntimeError("provider-private-error")
            return MemorySearchResult(())

    async def scenario():
        model = MemoryToolModel()
        service = ChatService(model, memory_provider=EmptyProvider(), context_debug=ContextDebugger(enabled=True, path=path))
        turn = service.create_turn(session_id="s", user_text="上次呢", speaker_identity_id="member")
        try:
            _ = [text async for text in service.stream_reply(turn)]
        finally:
            await service.aclose()
        return model.requests[1][0][-1].content

    actual_result = asyncio.run(scenario())
    event = next(event for event in read_events(path) if event["event"] == "memory_search_result")
    assert event["result"] == actual_result
    payload = json.loads(actual_result)
    assert payload["status"] == ("error" if fail_search else "no_match")
    if fail_search:
        assert payload["code"] == "unavailable"
    assert "provider-private-error" not in path.read_text(encoding="utf-8")


def test_voice_identity_reason_is_attached_to_turn(tmp_path) -> None:
    from newtalk.context_debug import ContextDebugger
    from newtalk.transport.runtime import _SpeakerResolution

    path = tmp_path / "context.jsonl"

    async def scenario():
        runtime = make_runtime(RecordingWebSocket())
        runtime._chat_service = ChatService(FakeLLM(chunk_delay_seconds=0), context_debug=ContextDebugger(enabled=True, path=path))
        await runtime.start()
        try:
            resolution = _SpeakerResolution(None, 0.31, "no_match", 20.0, 22.0)
            await runtime.start_turn(text="我是谁", event_id="voice-utterance", identity=None, speaker_resolution=resolution)
            await asyncio.wait_for(runtime._turn_state.task, timeout=2)
        finally:
            await runtime.close()
            await runtime._chat_service.aclose()

    asyncio.run(scenario())
    event = next(event for event in read_events(path) if event["event"] == "context_selected")
    assert event["source"] == "voice"
    assert event["identity"]["reason"] == "no_match"
    assert event["identity"]["score"] == 0.31
    assert event["profile"]["injected"] is False


@pytest.mark.parametrize("result", ["inserted", "not_inserted", "failed", "database_failed"])
def test_member_commit_trace_reports_actual_persistence_and_queue_result(tmp_path, result) -> None:
    from newtalk.chat import InMemoryDialogueStore
    from newtalk.context_debug import ContextDebugger
    from tests.test_websocket import make_client, receive_turn

    path = tmp_path / "context.jsonl"

    class Store(InMemoryDialogueStore):
        async def append(self, **kwargs):
            if result == "database_failed":
                raise RuntimeError("deterministic persistence failure")
            return await super().append(**kwargs)

    class Writer:
        calls = 0

        async def start(self):
            pass

        async def close(self):
            pass

        async def enqueue_completed_turn(self, turn, assistant_text):
            self.calls += 1
            if result == "failed":
                raise RuntimeError("deterministic enqueue failure")
            return result == "inserted"

    writer = Writer()
    service = ChatService(FakeLLM(chunk_delay_seconds=0), context_debug=ContextDebugger(enabled=True, path=path))
    client = make_client(chat_service=service, dialogue_store=Store(), memory_writer=writer)
    with client:
        member = client.post("/api/members", json={"display_name": "小明"}).json()
        with client.websocket_connect("/ws") as websocket:
            websocket.receive_json()
            websocket.send_json({"type": "text_input", "event_id": "member-1",
                                 "text": "我在学英语", "identity_id": member["identity_id"]})
            _, _, completed, _, _ = receive_turn(websocket)
            assert completed["type"] == "turn_completed"

    event = next(event for event in read_events(path) if event["event"] == "turn_end")
    assert event["dialogue_memory"] == "completed"
    assert event["dialogue_database"] == ("failed" if result == "database_failed" else "completed")
    assert event["memory_queue"] == ("not_attempted" if result == "database_failed" else result)
    assert writer.calls == (0 if result == "database_failed" else 1)
    assert event["response_text"] == "我收到了：我在学英语"
