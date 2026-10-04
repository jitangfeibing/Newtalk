import asyncio
from contextlib import aclosing
from types import SimpleNamespace

import pytest

from newtalk.chat import (
    ChatMessage,
    ChatService,
    ModelToolCall,
    OpenAICompatibleChatModel,
)


class StubStream:
    def __init__(self, events: list[SimpleNamespace]) -> None:
        self._events = iter(events)
        self.exited = False

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, traceback) -> None:
        self.exited = True

    def __aiter__(self):
        return self

    async def __anext__(self):
        try:
            return next(self._events)
        except StopIteration as exc:
            raise StopAsyncIteration from exc


class StubCompletions:
    def __init__(self, stream: StubStream) -> None:
        self._stream = stream
        self.request: dict | None = None

    def stream(self, **request):
        self.request = request
        return self._stream


class StubClient:
    def __init__(self, stream: StubStream) -> None:
        self.chat = SimpleNamespace(completions=StubCompletions(stream))
        self.closed = False

    async def close(self) -> None:
        self.closed = True


class SequencedCompletions:
    def __init__(self, streams: list[StubStream]) -> None:
        self._streams = iter(streams)
        self.requests: list[dict] = []

    def stream(self, **request):
        self.requests.append(request)
        return next(self._streams)


class SequencedClient:
    def __init__(self, streams: list[StubStream]) -> None:
        self.chat = SimpleNamespace(completions=SequencedCompletions(streams))

    async def close(self) -> None:
        return None


def test_openai_compatible_model_uses_chat_service_and_closes_resources() -> None:
    async def exercise() -> tuple[list[str], StubClient, StubStream]:
        stream = StubStream(
            [
                SimpleNamespace(type="content.delta", delta="你好"),
                SimpleNamespace(type="metadata", delta=None),
                SimpleNamespace(type="content.delta", delta="，我是 Newtalk"),
            ]
        )
        client = StubClient(stream)
        model = OpenAICompatibleChatModel(
            api_key="not-used-by-stub",
            base_url="https://example.test/v1",
            model="test-model",
            system_prompt="你是测试助手",
            client=client,
        )

        service = ChatService(model)
        turn = service.create_turn(
            session_id="test-session",
            user_text="介绍一下自己",
            messages=(
                ChatMessage("system", "[当前说话人画像]\n- 兴趣: 科幻小说"),
                ChatMessage("user", "我叫小明"),
                ChatMessage("assistant", "你好，小明"),
                ChatMessage("user", "介绍一下自己"),
            ),
        )
        chunks = [chunk async for chunk in service.stream_reply(turn)]
        await service.aclose()
        return chunks, client, stream

    chunks, client, stream = asyncio.run(exercise())

    assert chunks == ["你好", "，我是 Newtalk"]
    assert client.chat.completions.request == {
        "model": "test-model",
        "messages": [
            {"role": "system", "content": "你是测试助手"},
            {
                "role": "system",
                "content": "[当前说话人画像]\n- 兴趣: 科幻小说",
            },
            {"role": "user", "content": "我叫小明"},
            {"role": "assistant", "content": "你好，小明"},
            {"role": "user", "content": "介绍一下自己"},
        ],
    }
    assert stream.exited
    assert client.closed


def test_openai_compatible_model_omits_empty_system_prompt() -> None:
    async def exercise() -> tuple[list[str], StubClient]:
        stream = StubStream([SimpleNamespace(type="content.delta", delta="回复")])
        client = StubClient(stream)
        model = OpenAICompatibleChatModel(
            api_key="not-used-by-stub",
            model="test-model",
            client=client,
        )
        chunks = [
            chunk
            async for chunk in model.stream((ChatMessage("user", "你好"),))
        ]
        return chunks, client

    chunks, client = asyncio.run(exercise())

    assert chunks == ["回复"]
    assert client.chat.completions.request["messages"] == [
        {"role": "user", "content": "你好"}
    ]


def test_openai_stream_is_closed_when_consumer_stops_early() -> None:
    async def exercise() -> StubStream:
        stream = StubStream(
            [
                SimpleNamespace(type="content.delta", delta="第一段"),
                SimpleNamespace(type="content.delta", delta="第二段"),
            ]
        )
        model = OpenAICompatibleChatModel(
            api_key="not-used-by-stub",
            model="test-model",
            client=StubClient(stream),
        )
        service = ChatService(model)
        turn = service.create_turn(session_id="test-session", user_text="你好")

        async with aclosing(service.stream_reply(turn)) as chunks:
            assert await anext(chunks) == "第一段"
        return stream

    assert asyncio.run(exercise()).exited


def test_openai_compatible_model_serializes_streamed_tool_call_and_result() -> None:
    def tool_chunk(*, call_id=None, name=None, arguments=None):
        function = SimpleNamespace(name=name, arguments=arguments)
        raw_call = SimpleNamespace(index=0, id=call_id, function=function)
        choice = SimpleNamespace(delta=SimpleNamespace(tool_calls=[raw_call]))
        return SimpleNamespace(
            type="chunk",
            chunk=SimpleNamespace(choices=[choice]),
        )

    async def exercise():
        first = StubStream(
            [
                tool_chunk(
                    call_id="call-1",
                    name="memory_search",
                    arguments='{"query":"上次',
                ),
                tool_chunk(arguments='面试"}'),
            ]
        )
        second = StubStream([SimpleNamespace(type="content.delta", delta="记得")])
        client = SequencedClient([first, second])
        model = OpenAICompatibleChatModel(
            api_key="not-used-by-stub",
            model="test-model",
            client=client,
        )
        tools = (
            {
                "type": "function",
                "function": {"name": "memory_search", "parameters": {}},
            },
        )

        first_events = [
            event
            async for event in model.stream(
                (ChatMessage("user", "回忆一下"),),
                tools=tools,
            )
        ]
        call = first_events[0]
        assert isinstance(call, ModelToolCall)
        second_events = [
            event
            async for event in model.stream(
                (
                    ChatMessage("user", "回忆一下"),
                    ChatMessage("assistant", "", tool_calls=(call,)),
                    ChatMessage("tool", '{"status":"ok"}', tool_call_id=call.call_id),
                )
            )
        ]
        return first_events, second_events, client

    first_events, second_events, client = asyncio.run(exercise())

    assert first_events == [
        ModelToolCall("call-1", "memory_search", '{"query":"上次面试"}')
    ]
    assert second_events == ["记得"]
    first_request, second_request = client.chat.completions.requests
    assert first_request["tool_choice"] == "auto"
    assert first_request["tools"][0]["function"]["name"] == "memory_search"
    assert second_request["messages"][-2]["tool_calls"][0]["id"] == "call-1"
    assert second_request["messages"][-1] == {
        "role": "tool",
        "content": '{"status":"ok"}',
        "tool_call_id": "call-1",
    }


def test_openai_compatible_model_rejects_mixed_text_and_tool_call() -> None:
    async def exercise() -> None:
        raw_call = SimpleNamespace(
            index=0,
            id="call-1",
            function=SimpleNamespace(name="memory_search", arguments="{}"),
        )
        stream = StubStream(
            [
                SimpleNamespace(type="content.delta", delta="半截回复"),
                SimpleNamespace(
                    type="chunk",
                    chunk=SimpleNamespace(
                        choices=[
                            SimpleNamespace(
                                delta=SimpleNamespace(tool_calls=[raw_call])
                            )
                        ]
                    ),
                ),
            ]
        )
        model = OpenAICompatibleChatModel(
            api_key="not-used-by-stub",
            model="test-model",
            client=StubClient(stream),
        )
        with pytest.raises(ValueError, match="mixed text"):
            _ = [
                event
                async for event in model.stream(
                    (ChatMessage("user", "回忆一下"),),
                    tools=(
                        {
                            "type": "function",
                            "function": {
                                "name": "memory_search",
                                "parameters": {},
                            },
                        },
                    ),
                )
            ]

    asyncio.run(exercise())
