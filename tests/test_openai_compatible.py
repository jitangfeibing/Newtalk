import asyncio
from contextlib import aclosing
from types import SimpleNamespace

from newtalk.chat import ChatService, OpenAICompatibleChatModel


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
            session_id="test-session", user_text="介绍一下自己"
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
        chunks = [chunk async for chunk in model.stream("你好")]
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
