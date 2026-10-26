import asyncio
from dataclasses import replace

import pytest

from newtalk.chat import (
    ChatMessage, ChatService, DialogueCacheCoordinator, DialogueSession, InMemoryDialogueStore,
)
from newtalk.chat.models import format_user_message
from tests.test_transport_runtime import RecordingWebSocket, TrackingRuntime, make_runtime


def guest(text):
    return format_user_message(text, speaker_identity_id=None,
                               speaker_display_name="Guest", speaker_relationship=None)


def test_user_only_history_is_selected_without_an_empty_assistant_message():
    history = DialogueSession("session-1", max_turns=2)
    turn = ChatService().create_turn(session_id="session-1", user_text="我想学英语")
    history.accept_user_input(turn)
    assert history.messages_for("聊天练习吧") == (
        ChatMessage("user", guest("我想学英语")),
        ChatMessage("user", guest("聊天练习吧")),
    )
    history.commit(turn, "好，我们练英语")
    assert len(history.exchanges) == 1
    assert history.exchanges[0].assistant_text == "好，我们练英语"
    assert history.messages_for("旅行话题")[-2] == ChatMessage("assistant", "好，我们练英语")
    with pytest.raises(ValueError, match="already been committed"):
        history.commit(turn, "重复回答")


@pytest.mark.parametrize("reason", ["barge_in", "superseded"])
def test_interruption_preserves_final_input_without_partial_reply_or_memory(reason):
    async def scenario():
        entered = asyncio.Event()
        requests = []

        class Model:
            async def stream(self, messages, *, tools=()):
                requests.append(messages)
                if "我想学英语" in messages[-1].content:
                    yield "不应作为完整历史的半句"
                    entered.set()
                    await asyncio.Future()
                yield "我们继续英语练习"

            async def aclose(self):
                pass

        class Writer:
            async def enqueue_completed_turn(self, *args):
                raise AssertionError("Guest/interrupted turns must not enter long-term memory")

        runtime = make_runtime(RecordingWebSocket())
        runtime._chat_service = ChatService(Model())
        runtime._memory_writer = Writer()
        await runtime.start()
        try:
            await runtime.start_turn(text="我想学英语", event_id="first", identity=None)
            first_id = runtime.active_turn_id
            await asyncio.wait_for(entered.wait(), 1)
            if reason == "barge_in":
                await runtime.cancel_turn(reason=reason)
            await runtime.start_turn(text="聊天练习吧", event_id="second", identity=None)
            await asyncio.wait_for(runtime._turn_state.task, 2)
            assert requests[-1] == (
                ChatMessage("user", guest("我想学英语")),
                ChatMessage("user", guest("聊天练习吧")),
            )
            assert len(runtime._dialogue.exchanges) == 2
            assert runtime._dialogue.exchanges[0].turn_id == first_id
            assert runtime._dialogue.exchanges[0].assistant_text == ""
        finally:
            await runtime.close()
            await runtime._chat_service.aclose()
        stored = runtime._dialogue_store._history[runtime.session_id]
        assert [item.exchange.user_text for item in stored] == ["我想学英语", "聊天练习吧"]
        assert stored[0].exchange.assistant_text == ""
        assert stored[1].exchange.assistant_text == "我们继续英语练习"

    asyncio.run(scenario())


def test_pending_input_write_survives_turn_cancellation_and_cannot_resurrect_after_clear():
    async def scenario():
        writing = asyncio.Event()
        release = asyncio.Event()

        class Store(InMemoryDialogueStore):
            async def append(self, **kwargs):
                writing.set()
                await release.wait()
                return await super().append(**kwargs)

        runtime = make_runtime(RecordingWebSocket())
        runtime._dialogue_store = Store()
        await runtime.start()
        clearing = None
        try:
            await runtime.start_turn(text="需要清理的输入", event_id="first", identity=None)
            await asyncio.wait_for(writing.wait(), 1)
            await asyncio.wait_for(runtime.cancel_turn(reason="barge_in"), 1)
            clearing = asyncio.create_task(runtime.clear_dialogue(event_id="clear"))
            release.set()
            await asyncio.wait_for(clearing, 2)
            assert runtime._dialogue.exchanges == ()
            assert runtime._dialogue_store._history[runtime.session_id] == []
        finally:
            release.set()
            if clearing is not None:
                await asyncio.gather(clearing, return_exceptions=True)
            await runtime.close()

    asyncio.run(scenario())


def test_completing_a_cleared_or_missing_user_record_does_not_insert_it():
    async def scenario():
        store = InMemoryDialogueStore()
        snapshot = await store.open(device_id="device-1", max_turns=2)
        turn = ChatService().create_turn(session_id=snapshot.session_id, user_text="输入")
        history = DialogueSession(snapshot.session_id, max_turns=2)
        history.accept_user_input(turn)
        exchange = history.exchanges[0]
        assert await store.append(session_id=snapshot.session_id, lane="guest",
                                  exchange=exchange)
        assert await store.complete(session_id=snapshot.session_id,
                                    exchange=replace(exchange, assistant_text="完整回答"))
        assert not await store.complete(session_id=snapshot.session_id,
                                        exchange=replace(exchange, assistant_text="重复回答"))
        await store.clear(session_id=snapshot.session_id)
        assert not await store.complete(session_id=snapshot.session_id,
                                        exchange=replace(exchange, assistant_text="迟到回答"))
        assert (await store.open(device_id="device-1", max_turns=2)).history == ()

    asyncio.run(scenario())


def test_generation_failure_keeps_input_and_close_flushes_independent_write():
    async def scenario():
        entered = asyncio.Event()
        release = asyncio.Event()

        class FailingModel:
            async def stream(self, messages, *, tools=()):
                raise RuntimeError("provider failed")
                yield  # async generator, not a coroutine

            async def aclose(self):
                pass

        class Store(InMemoryDialogueStore):
            async def append(self, **kwargs):
                entered.set()
                await release.wait()
                return await super().append(**kwargs)

        websocket = RecordingWebSocket()
        runtime = make_runtime(websocket)
        runtime._dialogue_store = Store()
        runtime._chat_service = ChatService(FailingModel())
        await runtime.start()
        closing = None
        try:
            await runtime.start_turn(text="我想学英语", event_id="failed", identity=None)
            await asyncio.wait_for(entered.wait(), 1)
            task = runtime._turn_state.task
            assert task is not None
            await asyncio.wait_for(task, 1)
            assert websocket.json_events[-1]["type"] == "turn_failed"
            assert runtime._dialogue.exchanges[0].assistant_text == ""
            closing = asyncio.create_task(runtime.close())
            release.set()
            await asyncio.wait_for(closing, 1)
            stored = runtime._dialogue_store._history[runtime.session_id]
            assert [(item.exchange.user_text, item.exchange.assistant_text) for item in stored] == [
                ("我想学英语", ""),
            ]
            assert not runtime._pending_input_writes
            assert not runtime._accepted_inputs
        finally:
            release.set()
            if closing is not None:
                await asyncio.gather(closing, return_exceptions=True)
            await runtime.close()

    asyncio.run(scenario())


def test_pending_input_writes_are_bounded_and_overload_does_not_accept_input(monkeypatch):
    import newtalk.transport.runtime as runtime_module
    monkeypatch.setattr(runtime_module, "_MAX_PENDING_INPUT_WRITES", 2)

    async def scenario():
        entered = asyncio.Event()
        release = asyncio.Event()

        class Store(InMemoryDialogueStore):
            async def append(self, **kwargs):
                entered.set()
                await release.wait()
                return await super().append(**kwargs)

        websocket = RecordingWebSocket()
        runtime = make_runtime(websocket, runtime_type=TrackingRuntime)
        runtime._dialogue_store = Store()
        await runtime.start()
        try:
            await runtime.start_turn(text="first", event_id="1", identity=None)
            await asyncio.wait_for(entered.wait(), 1)
            await runtime.start_turn(text="second", event_id="2", identity=None)
            active_id = runtime.active_turn_id
            await asyncio.wait_for(runtime.start_turn(text="rejected", event_id="3", identity=None), 1)
            assert runtime.active_turn_id == active_id
            assert websocket.json_events[-1]["code"] == "dialogue_busy"
            assert [item.user_text for item in runtime._dialogue.exchanges] == ["first", "second"]
            assert len(runtime._pending_input_writes) == 2
        finally:
            release.set()
            await runtime.close()

    asyncio.run(scenario())


def test_shared_cache_copies_unpersisted_input_and_does_not_resurrect_evicted_turns():
    coordinator = DialogueCacheCoordinator()
    first = DialogueSession("session-1", max_turns=1)
    second = DialogueSession("session-1", max_turns=1)
    other = DialogueSession("other", max_turns=1)
    coordinator.register("device-1", "family", first)
    coordinator.register("device-2", "family", other)
    service = ChatService()
    old = service.create_turn(session_id="session-1", user_text="我想学英语")
    coordinator.accept_user_input("device-1", old)
    coordinator.register("device-1", "family", second)
    assert second.exchanges == first.exchanges
    assert other.exchanges == ()
    new = service.create_turn(session_id="session-1", user_text="练口语")
    coordinator.accept_user_input("device-1", new)
    assert not coordinator.commit("device-1", old, "迟到回答")
    assert [item.user_text for item in first.exchanges] == ["练口语"]
    coordinator.clear("device-1")
    assert not coordinator.commit("device-1", new, "清空后的迟到回答")
    assert first.exchanges == second.exchanges == ()


def test_removed_identity_invalidates_input_waiting_for_storage_lock():
    async def scenario():
        runtime = make_runtime(RecordingWebSocket(), runtime_type=TrackingRuntime)
        identity_service = runtime._identity_service
        family = await identity_service.register_device()
        member = await identity_service.create_identity(
            device_id=family.device.device_id, display_name="鱼鱼",
        )
        scope = runtime._dialogue_write_scope
        await runtime.start()
        await scope.lock.acquire()
        try:
            await runtime.start_turn(text="待删除输入", event_id="1", identity=member)
            ticket = runtime._accepted_inputs[runtime.active_turn_id]
            runtime._dialogue_coordinator.remove_identity(runtime.device_id, member.identity_id)
        finally:
            scope.lock.release()
        try:
            assert await asyncio.wait_for(ticket.persistence, 1) == "skipped"
            assert runtime._dialogue.exchanges == ()
            assert runtime._dialogue_store._history.get(runtime.session_id, []) == []
        finally:
            await runtime.close()

    asyncio.run(scenario())
