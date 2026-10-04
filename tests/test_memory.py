import asyncio
import json

from newtalk.chat import ChatMessage, ChatService, ModelToolCall
from newtalk.memory import (
    InMemoryMemoryJobStore,
    MemoryItem,
    MemorySearchResult,
    MemoryWriteReceipt,
    MemoryWriteService,
)
from newtalk.profile import ProfileScope, ProfileSnapshot


class RecordingMemoryProvider:
    enabled = True

    def __init__(self, *, fail_search: bool = False, fail_write: bool = False) -> None:
        self.fail_search = fail_search
        self.fail_write = fail_write
        self.searches: list[tuple[ProfileScope, str, str, int, float]] = []
        self.writes: list[dict] = []

    async def prepare_profile(self, scope: ProfileScope) -> ProfileSnapshot:
        return ProfileSnapshot(scope.identity_id, "template", ())

    async def search(
        self,
        scope: ProfileScope,
        *,
        query: str,
        conversation_id: str,
        limit: int,
        relativity: float,
    ) -> MemorySearchResult:
        self.searches.append((scope, query, conversation_id, limit, relativity))
        if self.fail_search:
            raise RuntimeError("search unavailable")
        return MemorySearchResult(
            (
                MemoryItem(
                    memory_id="memory-1",
                    kind="event",
                    title="上次面试",
                    content="用户认为算法题准备不足",
                    relativity=0.91,
                ),
            )
        )

    async def add_completed_turn(self, scope: ProfileScope, **values) -> MemoryWriteReceipt:
        self.writes.append({"scope": scope, **values})
        if self.fail_write:
            raise RuntimeError("write unavailable")
        return MemoryWriteReceipt("provider-task-1", "running")

    async def aclose(self) -> None:
        return None


class MemoryToolModel:
    def __init__(self) -> None:
        self.requests: list[tuple[tuple[ChatMessage, ...], tuple[dict, ...]]] = []

    async def stream(self, messages, *, tools=()):
        self.requests.append((tuple(messages), tuple(tools)))
        if tools:
            yield ModelToolCall(
                call_id="call-1",
                name="memory_search",
                arguments='{"query":"之前面试的问题"}',
            )
            return
        assert messages[-1].role == "tool"
        yield "你上次提到算法题准备不足。"

    async def aclose(self) -> None:
        return None


def test_member_tool_call_uses_server_owned_scope_and_returns_final_text() -> None:
    async def scenario() -> None:
        provider = RecordingMemoryProvider()
        model = MemoryToolModel()
        service = ChatService(model, memory_provider=provider)
        turn = service.create_turn(
            session_id="session-1",
            user_text="我上次面试哪里没准备好？",
            device_id="02:00:00:00:00:01",
            speaker_identity_id="11111111-1111-1111-1111-111111111111",
            speaker_display_name="小明",
        )

        chunks = [chunk async for chunk in service.stream_reply(turn)]

        assert chunks == ["你上次提到算法题准备不足。"]
        scope, query, conversation_id, limit, relativity = provider.searches[0]
        assert scope.device_id == turn.device_id
        assert scope.identity_id == turn.speaker_identity_id
        assert query == "之前面试的问题"
        assert conversation_id == turn.session_id
        assert limit == 5
        assert relativity == 0.55
        second_messages, second_tools = model.requests[1]
        assert second_tools == ()
        assert second_messages[-2].tool_calls[0].call_id == "call-1"
        tool_result = json.loads(second_messages[-1].content)
        assert tool_result["memories"][0]["content"] == "用户认为算法题准备不足"

    asyncio.run(scenario())


def test_memory_search_failure_becomes_tool_result_instead_of_turn_failure() -> None:
    async def scenario() -> None:
        provider = RecordingMemoryProvider(fail_search=True)
        model = MemoryToolModel()
        service = ChatService(model, memory_provider=provider)
        turn = service.create_turn(
            session_id="session-1",
            user_text="回忆一下",
            device_id="02:00:00:00:00:01",
            speaker_identity_id="11111111-1111-1111-1111-111111111111",
        )

        chunks = [chunk async for chunk in service.stream_reply(turn)]

        assert chunks == ["你上次提到算法题准备不足。"]
        result = json.loads(model.requests[1][0][-1].content)
        assert result["code"] == "unavailable"

    asyncio.run(scenario())


def test_guest_does_not_receive_memory_tool() -> None:
    class DirectModel:
        def __init__(self) -> None:
            self.tools = None

        async def stream(self, messages, *, tools=()):
            self.tools = tuple(tools)
            yield "直接回答"

        async def aclose(self) -> None:
            return None

    async def scenario() -> None:
        model = DirectModel()
        provider = RecordingMemoryProvider()
        service = ChatService(model, memory_provider=provider)
        turn = service.create_turn(session_id="session", user_text="你好")

        assert [chunk async for chunk in service.stream_reply(turn)] == ["直接回答"]
        assert model.tools == ()
        assert provider.searches == []

    asyncio.run(scenario())


async def _wait_for_status(
    store: InMemoryMemoryJobStore,
    expected: str,
    *,
    timeout: float = 1,
) -> None:
    async with asyncio.timeout(timeout):
        while expected not in store.statuses.values():
            await asyncio.sleep(0.001)


def test_memory_write_worker_is_idempotent_and_persists_completed_member_turn() -> None:
    async def scenario() -> None:
        provider = RecordingMemoryProvider()
        store = InMemoryMemoryJobStore()
        service = MemoryWriteService(store, provider, poll_seconds=0.001)
        await service.start()
        turn = ChatService().create_turn(
            session_id="session-1",
            user_text="我准备换工作",
            device_id="02:00:00:00:00:01",
            speaker_identity_id="11111111-1111-1111-1111-111111111111",
            speaker_display_name="小明",
        )
        try:
            assert await service.enqueue_completed_turn(turn, "我会记住这件事。")
            assert not await service.enqueue_completed_turn(turn, "重复写入")
            await _wait_for_status(store, "completed")
        finally:
            await service.close()

        assert len(provider.writes) == 1
        write = provider.writes[0]
        assert write["scope"].identity_id == turn.speaker_identity_id
        assert write["turn_id"] == turn.turn_id
        assert write["user_text"] == turn.user_text
        job_id = next(iter(store.jobs))
        assert store.provider_task_ids[job_id] == "provider-task-1"

    asyncio.run(scenario())


def test_memory_write_worker_marks_final_failure_without_affecting_turn() -> None:
    async def scenario() -> None:
        provider = RecordingMemoryProvider(fail_write=True)
        store = InMemoryMemoryJobStore()
        service = MemoryWriteService(
            store,
            provider,
            poll_seconds=0.001,
            max_attempts=1,
        )
        await service.start()
        turn = ChatService().create_turn(
            session_id="session-1",
            user_text="失败也不能影响聊天",
            device_id="02:00:00:00:00:01",
            speaker_identity_id="11111111-1111-1111-1111-111111111111",
        )
        try:
            assert await service.enqueue_completed_turn(turn, "聊天已经完成")
            await _wait_for_status(store, "failed")
        finally:
            await service.close()

        job_id = next(iter(store.jobs))
        assert "write unavailable" in store.errors[job_id]

    asyncio.run(scenario())
