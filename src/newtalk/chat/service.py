import asyncio
from collections.abc import AsyncIterator
from contextlib import aclosing
from datetime import datetime, timezone
import json
import logging
import math
from time import perf_counter
from uuid import uuid4

from newtalk.chat.fake_llm import FakeLLM
from newtalk.chat.model import ChatModel
from newtalk.chat.prompts import MEMORY_CONTEXT_PREFIX
from newtalk.tools.models import CLIENT_ACTION_TYPES, DeviceControl, ToolContext, tool_error
from newtalk.media.models import MediaCommands, MediaControl
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
from newtalk.context_debug import ContextDebugger, record_context
from newtalk.tts import AudioFormat, FakeTTS, StreamingTextSegmenter, TextToSpeech
from newtalk.reminders.service import ReminderService
from newtalk.tools.builtin import create_tool_registry
from newtalk.tools.memory import MemorySearchTool
from newtalk.tools.registry import ToolRegistry


logger = logging.getLogger(__name__)


_TEXT_END = object()


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
        context_debug: ContextDebugger | None = None,
        memory_retrieval_mode: str = "tool",
        memory_preload_timeout_seconds: float = 0.8,
        reminder_service: ReminderService | None = None,
        tool_registry: ToolRegistry | None = None,
    ) -> None:
        if memory_retrieval_mode not in {"tool", "preload"}:
            raise ValueError("Memory retrieval mode must be tool or preload")
        if not math.isfinite(memory_preload_timeout_seconds) or memory_preload_timeout_seconds <= 0:
            raise ValueError("Memory preload timeout must be positive")
        self._model = model or FakeLLM()
        self._synthesizer = synthesizer or FakeTTS()
        self._memory_tool = MemorySearchTool(
            memory_provider or DisabledMemoryProvider(),
            search_limit=memory_search_limit,
            search_relativity=memory_search_relativity,
            result_max_chars=memory_result_max_chars,
            query_max_chars=memory_query_max_chars,
        )
        self._memory_retrieval_mode = memory_retrieval_mode
        self._memory_preload_timeout_seconds = memory_preload_timeout_seconds
        self.context_debug = context_debug or ContextDebugger()
        self.tool_registry = tool_registry or create_tool_registry(self._memory_tool, reminder_service)

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

    async def stream_reply(
        self, turn: Turn, *, device_control_enabled: bool = False,
        media_commands: MediaCommands | None = None,
    ) -> AsyncIterator[str | DeviceControl | MediaControl]:
        started_at = perf_counter()
        chunk_count = 0
        model_name = type(self._model).__name__
        messages = turn.messages
        context = ToolContext(turn, device_control_enabled, self._memory_retrieval_mode, media_commands)
        selection = self.tool_registry.select(context)
        tools = selection.definitions
        tool_calls = 0
        memory_tool_offered = self._memory_tool.name in selection.names
        memory_used = False
        trace_token = self.context_debug.activate(turn)
        round_index = 0
        try:
            if (self._memory_retrieval_mode == "preload" and self._memory_tool.enabled_for(turn)):
                memory_used = True
                memory_message = await self._preload_memory_message(turn)
                messages = (*messages[:-1], memory_message, messages[-1])
            while True:
                round_index += 1
                round_started_at = perf_counter()
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
                        if round_chunks == 1:
                            elapsed_ms = (perf_counter() - round_started_at) * 1000
                            record_context("llm_round_first_token", round_index=round_index, elapsed_ms=elapsed_ms)
                            logger.info(
                                "llm_round_first_token turn_id=%s round=%s mode=%s elapsed_ms=%.1f",
                                turn.turn_id, round_index, self._memory_retrieval_mode, elapsed_ms,
                            )
                        if chunk_count == 1:
                            logger.info(
                                "llm_first_token turn_id=%s model=%s elapsed_ms=%.1f",
                                turn.turn_id,
                                model_name,
                                (perf_counter() - started_at) * 1000,
                            )
                        yield event

                if tool_call is not None:
                    elapsed_ms = (perf_counter() - round_started_at) * 1000
                    record_context("llm_tool_decision_completed", round_index=round_index, elapsed_ms=elapsed_ms)
                    logger.info(
                        "llm_tool_decision_completed turn_id=%s round=%s elapsed_ms=%.1f",
                        turn.turn_id, round_index, elapsed_ms,
                    )
                if tool_call is None:
                    if round_chunks == 0:
                        raise ValueError("Chat model returned no text")
                    break
                if tool_calls >= 2 or not tools:
                    raise ValueError("Model exceeded the tool call limit")
                tool_calls += 1
                memory_used = memory_used or tool_call.name == self._memory_tool.name
                result = await selection.execute(tool_call)
                if isinstance(result, CLIENT_ACTION_TYPES):
                    yield result
                    return
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
                # 仅注册表明确声明的后续动作可再执行一次，不能回到任意工具循环。
                selection = selection.following(tool_call.name, result) if tool_calls == 1 else selection.following('')
                tools = selection.definitions
        except Exception:
            logger.exception(
                "llm_stream_failed turn_id=%s model=%s elapsed_ms=%.1f",
                turn.turn_id,
                model_name,
                (perf_counter() - started_at) * 1000,
            )
            raise
        finally:
            record_context(
                "memory_search_usage", offered=memory_tool_offered, used=memory_used,
                mode=self._memory_retrieval_mode,
                skip_reason=("guest" if turn.speaker_identity_id is None else "disabled")
                if not memory_used and not memory_tool_offered else None,
            )
            self.context_debug.reset(trace_token)
        logger.info(
            "llm_stream_completed turn_id=%s model=%s chunks=%s elapsed_ms=%.1f",
            turn.turn_id,
            model_name,
            chunk_count,
            (perf_counter() - started_at) * 1000,
        )

    async def _preload_memory_message(self, turn: Turn) -> ChatMessage:
        """运行于 Turn 的生成 Task；只等待检索，不新增模型决策或后台孤儿任务。"""
        query = turn.user_text.strip()[:self._memory_tool.query_max_chars]
        call = ModelToolCall(
            call_id=f"server-memory-{turn.turn_id}", name="memory_search",
            arguments=json.dumps({"query": query}, ensure_ascii=False),
        )
        started_at = perf_counter()
        timed_out = False
        try:
            async with asyncio.timeout(self._memory_preload_timeout_seconds):
                result = await self._memory_tool.search(
                    ToolContext(turn, memory_retrieval_mode=self._memory_retrieval_mode), call,
                )
        except TimeoutError:
            # 只处理本地截止时间；外部取消继续传播，不能取消后又开始 LLM。
            result = tool_error("timeout", "长期记忆检索超时，请基于当前对话回答。")
            timed_out = True
        elapsed_ms = (perf_counter() - started_at) * 1000
        record_context(
            "memory_search_result", call_id=call.call_id, tool=call.name,
            arguments=call.arguments, result=result, mode="preload", origin="server",
            query_truncated=len(query) < len(turn.user_text.strip()), elapsed_ms=elapsed_ms,
        )
        logger.info(
            "memory_preload_completed turn_id=%s identity_id=%s elapsed_ms=%.1f timed_out=%s",
            turn.turn_id, turn.speaker_identity_id, elapsed_ms, timed_out,
        )
        # 普通资料消息不伪造模型的工具历史；部分思考模型要求工具历史必须
        # 回传真实 reasoning_content，服务端预查没有这些模型输出可供回传。
        return ChatMessage(
            "user", MEMORY_CONTEXT_PREFIX + result, context_kind="memory",
        )

    async def stream_turn(
        self, turn: Turn, *, device_control_enabled: bool = False,
        media_commands: MediaCommands | None = None,
    ) -> AsyncIterator[TurnOutput]:
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
                async with aclosing(self.stream_reply(
                    turn, device_control_enabled=device_control_enabled,
                    media_commands=media_commands,
                )) as reply_stream:
                    async for delta in reply_stream:
                        if isinstance(delta, CLIENT_ACTION_TYPES):
                            await output_queue.put(delta)
                            break
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
                if isinstance(output, CLIENT_ACTION_TYPES):
                    return
            if text_failure is not None:
                raise text_failure
            yield TurnCompleted("".join(response_parts))
        finally:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)

    async def aclose(self) -> None:
        try:
            await asyncio.gather(
                self._model.aclose(),
                self._synthesizer.aclose(),
            )
        finally:
            self.context_debug.close()
