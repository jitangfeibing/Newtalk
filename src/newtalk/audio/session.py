import asyncio
from collections import deque
from collections.abc import Awaitable, Callable
from contextlib import aclosing
import logging
from uuid import uuid4

from newtalk.asr import AsrEvent, SpeechRecognizer
from newtalk.audio.model import (
    INPUT_AUDIO_FORMAT,
    SpeechBoundary,
    VoiceActivityStream,
)


_AUDIO_END = object()
logger = logging.getLogger(__name__)


class AudioInputSession:
    def __init__(
        self,
        *,
        vad_stream: VoiceActivityStream,
        recognizer: SpeechRecognizer,
        on_boundary: Callable[[SpeechBoundary], Awaitable[None]],
        on_asr_event: Callable[[str, AsrEvent], Awaitable[None]],
        pre_roll_ms: int = 300,
    ) -> None:
        self._vad_stream = vad_stream
        self._recognizer = recognizer
        self._on_boundary = on_boundary
        self._on_asr_event = on_asr_event
        self._pre_roll_limit = int(
            INPUT_AUDIO_FORMAT.bytes_per_second * pre_roll_ms / 1000
        )
        self._pre_roll: deque[bytes] = deque()
        self._pre_roll_bytes = 0
        self._active_queue: asyncio.Queue[bytes | object] | None = None
        self._active_utterance_id: str | None = None
        self._recognition_tasks: set[asyncio.Task[None]] = set()
        self._closed = False

    async def push(self, pcm: bytes) -> None:
        if self._closed:
            raise RuntimeError("Audio input session is closed")
        if not pcm:
            return
        if len(pcm) % 2 != 0:
            raise ValueError("PCM S16LE frames must contain an even number of bytes")

        queue_at_start = self._active_queue
        if queue_at_start is not None:
            await queue_at_start.put(pcm)
        self._append_pre_roll(pcm)

        for event in self._vad_stream.process(pcm):
            if event.kind == "speech_start":
                await self._start_utterance(event.probability, event.audio_ms)
            else:
                await self._finish_utterance(event.probability, event.audio_ms)

    async def finish(self) -> None:
        if self._closed:
            return
        for event in self._vad_stream.flush():
            if event.kind == "speech_end":
                await self._finish_utterance(event.probability, event.audio_ms)
        if self._active_queue is not None:
            await self._active_queue.put(_AUDIO_END)
            self._active_queue = None
            self._active_utterance_id = None

    async def close(self) -> None:
        if self._closed:
            return
        await self.finish()
        self._closed = True
        if self._recognition_tasks:
            await asyncio.gather(*self._recognition_tasks, return_exceptions=True)
        self._vad_stream.reset()

    async def _start_utterance(self, probability: float, audio_ms: float) -> None:
        if self._active_queue is not None:
            return
        utterance_id = str(uuid4())
        queue: asyncio.Queue[bytes | object] = asyncio.Queue(maxsize=128)
        self._active_queue = queue
        self._active_utterance_id = utterance_id
        task = asyncio.create_task(self._recognize(utterance_id, queue))
        self._recognition_tasks.add(task)
        task.add_done_callback(self._recognition_finished)

        await self._on_boundary(
            SpeechBoundary(
                kind="speech_start",
                utterance_id=utterance_id,
                probability=probability,
                audio_ms=audio_ms,
            )
        )
        for chunk in self._pre_roll:
            await queue.put(chunk)

    async def _finish_utterance(self, probability: float, audio_ms: float) -> None:
        if self._active_queue is None or self._active_utterance_id is None:
            return
        queue = self._active_queue
        utterance_id = self._active_utterance_id
        self._active_queue = None
        self._active_utterance_id = None
        await self._on_boundary(
            SpeechBoundary(
                kind="speech_end",
                utterance_id=utterance_id,
                probability=probability,
                audio_ms=audio_ms,
            )
        )
        await queue.put(_AUDIO_END)

    async def _recognize(
        self,
        utterance_id: str,
        queue: asyncio.Queue[bytes | object],
    ) -> None:
        async def audio_chunks():
            while True:
                chunk = await queue.get()
                if chunk is _AUDIO_END:
                    return
                if isinstance(chunk, bytes):
                    yield chunk

        async with aclosing(
            self._recognizer.stream(audio_chunks(), utterance_id=utterance_id)
        ) as events:
            async for event in events:
                await self._on_asr_event(utterance_id, event)

    def _append_pre_roll(self, pcm: bytes) -> None:
        self._pre_roll.append(pcm)
        self._pre_roll_bytes += len(pcm)
        while self._pre_roll and self._pre_roll_bytes > self._pre_roll_limit:
            removed = self._pre_roll.popleft()
            self._pre_roll_bytes -= len(removed)

    def _recognition_finished(self, task: asyncio.Task[None]) -> None:
        self._recognition_tasks.discard(task)
        if task.cancelled():
            return
        error = task.exception()
        if error is not None:
            logger.error(
                "asr_stream_failed recognizer=%s",
                type(self._recognizer).__name__,
                exc_info=(type(error), error, error.__traceback__),
            )
