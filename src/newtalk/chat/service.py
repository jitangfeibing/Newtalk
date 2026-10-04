import asyncio
from collections.abc import AsyncIterator
from contextlib import aclosing
from datetime import datetime, timezone
import json
import logging
from time import perf_counter
from uuid import uuid4

from newtalk.chat.fake_llm import FakeLLM
from newtalk.chat.model import ChatModel
from newtalk.chat.models import (
    AudioCompleted,
    AudioFailed,
    AudioFrame,
    AudioStarted,
    TextDelta,
    ChatMessage,
    ModelToolCall,
    Turn,
    TurnCompleted,
    TurnOutput,
    format_user_message,
)
from newtalk.memory import DisabledMemoryProvider, MemoryProvider
from newtalk.profile import ProfileScope
from newtalk.tts import AudioFormat, FakeTTS, StreamingTextSegmenter, TextToSpeech


logger = logging.getLogger(__name__)


_TEXT_END = object()
_MEMORY_SEARCH_TOOL = {
    "type": "function",
    "function": {
        "name": "memory_search",
        "description": (
            "仅当当前对话和用户画像不足以回答、且问题依赖这位成员更早的个人经历时，"
            "检索当前成员的长期记忆。普通知识问题不要调用。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "需要回忆的过去信息的简洁描述。",
                }
            },
            "required": ["query"],
            "additionalProperties": False,
        },
    },
}


class ChatService:
    def __init__(
        self,
        model: ChatModel | None = None,
        synthesizer: TextToSpeech | None = None,
        memory_provider: MemoryProvider | None = None,
        *,
        memory_search_limit: int = 5,
        memory_search_relativity: float = 0.55,
        memory_result_max_chars: int = 4000,
        memory_query_max_chars: int = 500,
    ) -> None:
        self._model = model or FakeLLM()
        self._synthesizer = synthesizer or FakeTTS()
        self._memory_provider = memory_provider or DisabledMemoryProvider()
        self._memory_search_limit = memory_search_limit
        self._memory_search_relativity = memory_search_relativity
        self._memory_result_max_chars = memory_result_max_chars
        self._memory_query_max_chars = memory_query_max_chars

    @property
    def audio_format(self) -> AudioFormat:
        return self._synthesizer.audio_format

    def create_turn(
        self,
        *,
        session_id: str,
        user_text: str,
        messages: tuple[ChatMessage, ...] | None = None,
        device_id: str = "unknown",
        speaker_identity_id: str | None = None,
        speaker_display_name: str = "Guest",
        speaker_relationship: str | None = None,
        profile_ready: bool = False,
        profile_field_count: int = 0,
    ) -> Turn:
        current_message = format_user_message(
            user_text,
            speaker_identity_id=speaker_identity_id,
            speaker_display_name=speaker_display_name,
            speaker_relationship=speaker_relationship,
        )
        resolved_messages = messages or (ChatMessage("user", current_message),)
        if resolved_messages[-1].role != "user":
            raise ValueError("Turn messages must end with the current user input")
        return Turn(
            turn_id=str(uuid4()),
            session_id=session_id,
            user_text=user_text,
            messages=resolved_messages,
            created_at=datetime.now(timezone.utc),
            device_id=device_id,
            speaker_identity_id=speaker_identity_id,
            speaker_display_name=speaker_display_name,
            speaker_relationship=speaker_relationship,
            profile_ready=profile_ready,
            profile_field_count=profile_field_count,
        )

    async def stream_reply(self, turn: Turn) -> AsyncIterator[str]:
        started_at = perf_counter()
        chunk_count = 0
        model_name = type(self._model).__name__
        messages = turn.messages
        tools: tuple[dict, ...] = (
            (_MEMORY_SEARCH_TOOL,)
            if self._memory_provider.enabled and turn.speaker_identity_id is not None
            else ()
        )
        tool_used = False
        try:
            while True:
                round_chunks = 0
                tool_call: ModelToolCall | None = None
                model_stream = (
                    self._model.stream(messages, tools=tools)
                    if tools
                    else self._model.stream(messages)
                )
                async with aclosing(model_stream) as response_stream:
                    async for event in response_stream:
                        if isinstance(event, ModelToolCall):
                            if tool_call is not None or round_chunks:
                                raise ValueError("Model mixed text with a tool call")
                            tool_call = event
                            continue
                        if not isinstance(event, str) or not event:
                            raise ValueError(
                                "Chat model events must be text or a tool call"
                            )
                        round_chunks += 1
                        chunk_count += 1
                        if chunk_count == 1:
                            logger.info(
                                "llm_first_token turn_id=%s model=%s elapsed_ms=%.1f",
                                turn.turn_id,
                                model_name,
                                (perf_counter() - started_at) * 1000,
                            )
                        yield event

                if tool_call is None:
                    if round_chunks == 0:
                        raise ValueError("Chat model returned no text")
                    break
                if tool_used or not tools:
                    raise ValueError("Model exceeded the memory tool call limit")
                tool_used = True
                result = await self._execute_memory_search(turn, tool_call)
                messages = (
                    *messages,
                    ChatMessage(
                        role="assistant",
                        content="",
                        tool_calls=(tool_call,),
                    ),
                    ChatMessage(
                        role="tool",
                        content=result,
                        tool_call_id=tool_call.call_id,
                    ),
                )
                tools = ()
        except Exception:
            logger.exception(
                "llm_stream_failed turn_id=%s model=%s elapsed_ms=%.1f",
                turn.turn_id,
                model_name,
                (perf_counter() - started_at) * 1000,
            )
            raise
        logger.info(
            "llm_stream_completed turn_id=%s model=%s chunks=%s elapsed_ms=%.1f",
            turn.turn_id,
            model_name,
            chunk_count,
            (perf_counter() - started_at) * 1000,
        )

    async def _execute_memory_search(
        self,
        turn: Turn,
        tool_call: ModelToolCall,
    ) -> str:
        started_at = perf_counter()
        if tool_call.name != "memory_search":
            return _tool_error("unsupported_tool", "不支持这个工具。")
        try:
            arguments = json.loads(tool_call.arguments)
        except json.JSONDecodeError:
            return _tool_error("invalid_arguments", "记忆查询参数不是有效 JSON。")
        query = arguments.get("query") if isinstance(arguments, dict) else None
        if not isinstance(query, str) or not query.strip():
            return _tool_error("invalid_arguments", "记忆查询缺少 query。")
        query = query.strip()
        if len(query) > self._memory_query_max_chars:
            return _tool_error("invalid_arguments", "记忆查询超过长度限制。")
        if turn.speaker_identity_id is None:
            return _tool_error("guest_forbidden", "Guest 不能读取长期记忆。")
        try:
            result = await self._memory_provider.search(
                ProfileScope(
                    device_id=turn.device_id,
                    identity_id=turn.speaker_identity_id,
                ),
                query=query,
                conversation_id=turn.session_id,
                limit=self._memory_search_limit,
                relativity=self._memory_search_relativity,
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning(
                "memory_search_failed turn_id=%s identity_id=%s error_type=%s elapsed_ms=%.1f",
                turn.turn_id,
                turn.speaker_identity_id,
                type(exc).__name__,
                (perf_counter() - started_at) * 1000,
            )
            return _tool_error("unavailable", "长期记忆暂时不可用。")
        logger.info(
            "memory_search_completed turn_id=%s identity_id=%s results=%s elapsed_ms=%.1f",
            turn.turn_id,
            turn.speaker_identity_id,
            len(result.items),
            (perf_counter() - started_at) * 1000,
        )
        return result.to_tool_content(max_chars=self._memory_result_max_chars)

    async def stream_turn(self, turn: Turn) -> AsyncIterator[TurnOutput]:
        started_at = perf_counter()
        stream_id = str(uuid4())
        output_queue: asyncio.Queue[TurnOutput | tuple[str, Exception | None]] = (
            asyncio.Queue(maxsize=64)
        )
        text_queue: asyncio.Queue[str | object] = asyncio.Queue()
        response_parts: list[str] = []
        speech_text_count = 0

        async def text_chunks() -> AsyncIterator[str]:
            nonlocal speech_text_count
            while True:
                item = await text_queue.get()
                if item is _TEXT_END:
                    return
                if isinstance(item, str):
                    speech_text_count += 1
                    yield item

        async def produce_text() -> None:
            segmenter = StreamingTextSegmenter()
            sequence = 0
            failure: Exception | None = None
            try:
                async with aclosing(self.stream_reply(turn)) as reply_stream:
                    async for delta in reply_stream:
                        sequence += 1
                        response_parts.append(delta)
                        await output_queue.put(TextDelta(sequence, delta))
                        for segment in segmenter.push(delta):
                            await text_queue.put(segment)
                final_segment = segmenter.flush()
                if final_segment:
                    await text_queue.put(final_segment)
            except asyncio.CancelledError:
                text_queue.put_nowait(_TEXT_END)
                raise
            except Exception as exc:
                failure = exc
            text_queue.put_nowait(_TEXT_END)
            await output_queue.put(("text", failure))

        async def produce_audio() -> None:
            frame_count = 0
            byte_count = 0
            failure: Exception | None = None
            try:
                async with aclosing(
                    self._synthesizer.stream(text_chunks(), turn_id=turn.turn_id)
                ) as audio_stream:
                    async for frame in audio_stream:
                        if not isinstance(frame, bytes) or not frame:
                            raise ValueError("TTS frames must be non-empty bytes")
                        frame_count += 1
                        byte_count += len(frame)
                        if frame_count == 1:
                            logger.info(
                                "tts_first_audio turn_id=%s provider=%s elapsed_ms=%.1f",
                                turn.turn_id,
                                type(self._synthesizer).__name__,
                                (perf_counter() - started_at) * 1000,
                            )
                            await output_queue.put(
                                AudioStarted(stream_id, self._synthesizer.audio_format)
                            )
                        await output_queue.put(
                            AudioFrame(stream_id, frame_count, frame)
                        )
                if frame_count == 0 and speech_text_count > 0:
                    raise ValueError("TTS returned no audio")
                if frame_count > 0:
                    await output_queue.put(
                        AudioCompleted(stream_id, frame_count, byte_count)
                    )
                    logger.info(
                        "tts_stream_completed turn_id=%s provider=%s frames=%s bytes=%s elapsed_ms=%.1f",
                        turn.turn_id,
                        type(self._synthesizer).__name__,
                        frame_count,
                        byte_count,
                        (perf_counter() - started_at) * 1000,
                    )
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                failure = exc
                logger.exception(
                    "tts_stream_failed turn_id=%s provider=%s elapsed_ms=%.1f",
                    turn.turn_id,
                    type(self._synthesizer).__name__,
                    (perf_counter() - started_at) * 1000,
                )
                await output_queue.put(
                    AudioFailed(stream_id, "Unable to synthesize speech")
                )
            await output_queue.put(("audio", failure))

        tasks = {
            asyncio.create_task(produce_text()),
            asyncio.create_task(produce_audio()),
        }
        stopped: set[str] = set()
        text_failure: Exception | None = None
        try:
            while len(stopped) < 2:
                output = await output_queue.get()
                if isinstance(output, tuple):
                    producer, failure = output
                    stopped.add(producer)
                    if producer == "text":
                        text_failure = failure
                    continue
                yield output
            if text_failure is not None:
                raise text_failure
            yield TurnCompleted("".join(response_parts))
        finally:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)

    async def aclose(self) -> None:
        await asyncio.gather(
            self._model.aclose(),
            self._synthesizer.aclose(),
        )


def _tool_error(code: str, message: str) -> str:
    return json.dumps(
        {"status": "error", "code": code, "message": message},
        ensure_ascii=False,
        separators=(",", ":"),
    )
