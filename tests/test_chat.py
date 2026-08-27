import asyncio
import logging

import pytest

from newtalk.chat import (
    AudioCompleted,
    AudioFailed,
    AudioFrame,
    AudioStarted,
    ChatService,
    ChatMessage,
    FakeLLM,
    TextDelta,
    TurnCompleted,
)
from newtalk.tts import AudioFormat, FakeTTS


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
    assert first.messages == second.messages == (ChatMessage("user", "你好"),)
    assert first.created_at.tzinfo is not None


def test_fake_llm_stream_is_deterministic() -> None:
    service = ChatService(FakeLLM(chunk_delay_seconds=0))
    turn = service.create_turn(session_id="session", user_text="测试消息")

    chunks = collect_reply(service, turn)

    assert chunks == ["我收到了：", "测试消息"]


def test_chat_service_logs_first_token_and_completion(caplog) -> None:
    service = ChatService(FakeLLM(chunk_delay_seconds=0))
    turn = service.create_turn(session_id="session", user_text="测试消息")

    with caplog.at_level(logging.INFO, logger="newtalk.chat.service"):
        chunks = collect_reply(service, turn)

    assert chunks == ["我收到了：", "测试消息"]
    assert "llm_first_token" in caplog.text
    assert "llm_stream_completed" in caplog.text
    assert f"turn_id={turn.turn_id}" in caplog.text


class EmptyModel:
    async def stream(self, messages):
        if False:
            yield messages

    async def aclose(self) -> None:
        return None


def test_chat_service_rejects_an_empty_model_response(caplog) -> None:
    service = ChatService(EmptyModel())
    turn = service.create_turn(session_id="session", user_text="测试消息")

    with caplog.at_level(logging.ERROR, logger="newtalk.chat.service"):
        with pytest.raises(ValueError, match="returned no text"):
            collect_reply(service, turn)

    assert "llm_stream_failed" in caplog.text


def test_chat_service_streams_text_and_audio_for_one_turn(caplog) -> None:
    service = ChatService(
        FakeLLM(chunk_delay_seconds=0),
        FakeTTS(sample_rate=24000),
    )
    turn = service.create_turn(session_id="session", user_text="测试消息")

    async def collect():
        return [output async for output in service.stream_turn(turn)]

    with caplog.at_level(logging.INFO, logger="newtalk.chat.service"):
        outputs = asyncio.run(collect())

    assert [output.text for output in outputs if isinstance(output, TextDelta)] == [
        "我收到了：",
        "测试消息",
    ]
    assert len([output for output in outputs if isinstance(output, AudioStarted)]) == 1
    assert len([output for output in outputs if isinstance(output, AudioFrame)]) == 1
    assert len([output for output in outputs if isinstance(output, AudioCompleted)]) == 1
    assert isinstance(outputs[-1], TurnCompleted)
    assert outputs[-1].text == "我收到了：测试消息"
    assert "tts_first_audio" in caplog.text
    assert "tts_stream_completed" in caplog.text


class FailingTTS:
    audio_format = AudioFormat(codec="pcm_s16le", sample_rate=24000, channels=1)

    async def stream(self, text_chunks, *, turn_id: str):
        del turn_id
        async for _ in text_chunks:
            raise RuntimeError("deterministic TTS failure")
        if False:
            yield b""

    async def aclose(self) -> None:
        return None


def test_tts_failure_preserves_completed_text_turn() -> None:
    service = ChatService(FakeLLM(chunk_delay_seconds=0), FailingTTS())
    turn = service.create_turn(session_id="session", user_text="仍然显示文本")

    async def collect():
        return [output async for output in service.stream_turn(turn)]

    outputs = asyncio.run(collect())

    assert any(isinstance(output, AudioFailed) for output in outputs)
    assert isinstance(outputs[-1], TurnCompleted)
    assert outputs[-1].text == "我收到了：仍然显示文本"
