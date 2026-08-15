import asyncio

from newtalk.chat import ChatService, FakeLLM


def collect_reply(service: ChatService, turn) -> list[str]:
    async def collect() -> list[str]:
        return [chunk async for chunk in service.stream_reply(turn)]

    return asyncio.run(collect())


def test_chat_service_creates_unique_turns() -> None:
    service = ChatService(FakeLLM(chunk_delay_seconds=0))

    first = service.create_turn(session_id="session", user_text="你好")
    second = service.create_turn(session_id="session", user_text="你好")

    assert first.turn_id != second.turn_id
    assert first.session_id == second.session_id == "session"
    assert first.user_text == second.user_text == "你好"
    assert first.created_at.tzinfo is not None


def test_fake_llm_stream_is_deterministic() -> None:
    service = ChatService(FakeLLM(chunk_delay_seconds=0))
    turn = service.create_turn(session_id="session", user_text="测试消息")

    chunks = collect_reply(service, turn)

    assert chunks == ["我收到了：", "测试消息"]
