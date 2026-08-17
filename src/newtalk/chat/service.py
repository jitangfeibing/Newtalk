import asyncio
from collections.abc import AsyncIterator
from contextlib import aclosing
from datetime import datetime, timezone
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
    Turn,
    TurnCompleted,
    TurnOutput,
)
from newtalk.tts import AudioFormat, FakeTTS, StreamingTextSegmenter, TextToSpeech


logger = logging.getLogger(__name__)


_TEXT_END = object()


class ChatService:
    def __init__(
        self,
        model: ChatModel | None = None,
        synthesizer: TextToSpeech | None = None,
    ) -> None:
        self._model = model or FakeLLM()
        self._synthesizer = synthesizer or FakeTTS()

    @property
    def audio_format(self) -> AudioFormat:
        return self._synthesizer.audio_format

    def create_turn(self, *, session_id: str, user_text: str) -> Turn:
        return Turn(
            turn_id=str(uuid4()),
            session_id=session_id,
            user_text=user_text,
            created_at=datetime.now(timezone.utc),
        )

    async def stream_reply(self, turn: Turn) -> AsyncIterator[str]:
        started_at = perf_counter()
        chunk_count = 0
        model_name = type(self._model).__name__
        try:
            async with aclosing(self._model.stream(turn.user_text)) as model_stream:
                async for chunk in model_stream:
                    if not isinstance(chunk, str) or not chunk:
                        raise ValueError("Chat model chunks must be non-empty strings")
                    chunk_count += 1
                    if chunk_count == 1:
                        logger.info(
                            "llm_first_token turn_id=%s model=%s elapsed_ms=%.1f",
                            turn.turn_id,
                            model_name,
                            (perf_counter() - started_at) * 1000,
                        )
                    yield chunk
            if chunk_count == 0:
                raise ValueError("Chat model returned no text")
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
