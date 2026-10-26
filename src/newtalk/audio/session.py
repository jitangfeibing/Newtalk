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
# 为 ASR 建连预留约 10 秒的 20ms 音频帧，避免短暂握手延迟导致整句失败。
_ASR_QUEUE_MAX_FRAMES = 500
logger = logging.getLogger(__name__)


class AudioInputSession:
    def __init__(
        self,
        *,
        vad_stream: VoiceActivityStream,
        recognizer: SpeechRecognizer,
        on_boundary: Callable[[SpeechBoundary], Awaitable[None]],
        on_asr_event: Callable[[str, AsrEvent], Awaitable[None]],
        on_asr_error: Callable[[str, Exception], Awaitable[None]] | None = None,
        on_utterance_audio: Callable[[str, bytes], Awaitable[None]] | None = None,
        pre_roll_ms: int = 300,
    ) -> None:
        self._vad_stream = vad_stream
        self._recognizer = recognizer
        self._on_boundary = on_boundary
        self._on_asr_event = on_asr_event
        self._on_asr_error = on_asr_error
        self._on_utterance_audio = on_utterance_audio
        self._pre_roll_limit = int(
            INPUT_AUDIO_FORMAT.bytes_per_second * pre_roll_ms / 1000
        )
        self._pre_roll: deque[bytes] = deque()
        self._pre_roll_bytes = 0
        self._active_queue: asyncio.Queue[bytes | object] | None = None
        self._active_utterance_id: str | None = None
        self._active_audio_chunks: list[bytes] | None = None
        self._active_task: asyncio.Task[None] | None = None
        self._recognition_tasks: set[asyncio.Task[None]] = set()
        self._closed = False

    async def push(self, pcm: bytes) -> None:
        if self._closed:
            raise RuntimeError("Audio input session is closed")
        if not pcm:
            return
        if len(pcm) % 2 != 0:
            raise ValueError("PCM S16LE frames must contain an even number of bytes")

        if await self._queue_chunk(pcm) and self._active_audio_chunks is not None:
            self._active_audio_chunks.append(pcm)
        self._append_pre_roll(pcm)

        for event in self._vad_stream.process(pcm):
            if event.kind == "speech_start":
                await self._start_utterance(event.probability, event.audio_ms)
            else:
                await self._finish_utterance(event.probability, event.audio_ms, event.last_voice_audio_ms)

    async def finish(self) -> None:
        if self._closed:
            return
        for event in self._vad_stream.flush():
            if event.kind == "speech_end":
                await self._finish_utterance(event.probability, event.audio_ms, event.last_voice_audio_ms)
        if self._active_utterance_id is not None:
            await self._finish_active_utterance()

    async def close(self) -> None:
        """Gracefully finish the active utterance and wait for its ASR result."""
        if self._closed:
            return
        await self.finish()
        self._closed = True
        if self._recognition_tasks:
            await asyncio.gather(*self._recognition_tasks, return_exceptions=True)
        self._vad_stream.reset()

    async def abort(self) -> None:
        """Cancel recognition immediately when the owning connection is gone."""
        if self._closed:
            return
        self._closed = True
        self._active_queue = None
        self._active_utterance_id = None
        self._active_audio_chunks = None
        self._active_task = None
        self._pre_roll.clear()
        self._pre_roll_bytes = 0

        # 连接已经断开，继续等待 ASR Final 不会产生可交付的结果，反而可能
        # 让 WebSocket 清理被外部服务超时拖住。
        tasks = tuple(self._recognition_tasks)
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._recognition_tasks.clear()
        self._vad_stream.reset()

    async def _start_utterance(self, probability: float, audio_ms: float) -> None:
        if self._active_utterance_id is not None:
            return
        utterance_id = str(uuid4())
        queue: asyncio.Queue[bytes | object] = asyncio.Queue(
            maxsize=_ASR_QUEUE_MAX_FRAMES
        )
        self._active_queue = queue
        self._active_utterance_id = utterance_id
        self._active_audio_chunks = list(self._pre_roll)
        task = asyncio.create_task(self._recognize(utterance_id, queue))
        self._active_task = task
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
            if self._active_utterance_id != utterance_id:
                break
            if not await self._queue_chunk(chunk):
                break

    async def _finish_utterance(
        self, probability: float, audio_ms: float, last_voice_audio_ms: float | None = None,
    ) -> None:
        if self._active_utterance_id is None:
            return
        utterance_id = self._active_utterance_id
        await self._on_boundary(
            SpeechBoundary(
                kind="speech_end",
                utterance_id=utterance_id,
                probability=probability,
                audio_ms=audio_ms,
                last_voice_audio_ms=last_voice_audio_ms,
            )
        )
        if self._active_utterance_id == utterance_id:
            await self._finish_active_utterance()

    async def _finish_active_utterance(self) -> None:
        utterance_id = self._active_utterance_id
        if utterance_id is None:
            return
        audio_chunks = self._active_audio_chunks or []
        queued = await self._queue_chunk(_AUDIO_END)
        self._detach_recognition(utterance_id)
        self._active_utterance_id = None
        if queued and self._on_utterance_audio is not None and audio_chunks:
            await self._on_utterance_audio(utterance_id, b"".join(audio_chunks))

    async def _queue_chunk(self, chunk: bytes | object) -> bool:
        queue = self._active_queue
        task = self._active_task
        utterance_id = self._active_utterance_id
        if queue is None or task is None or utterance_id is None:
            return False
        if task.done() or task.cancelling():
            self._detach_recognition(utterance_id)
            return False
        try:
            queue.put_nowait(chunk)
            return True
        except asyncio.QueueFull:
            # 接收循环不能等待外部 ASR 消费；先摘除队列，保证同一句只报错一次。
            self._detach_recognition(utterance_id)
            task.cancel()
            await self._report_asr_error(
                utterance_id, RuntimeError("ASR audio queue is full")
            )
            return False

    def _detach_recognition(self, utterance_id: str | None) -> None:
        if self._active_utterance_id == utterance_id:
            # 识别结束不代表用户说完，保留编号供 VAD 发送 speech_end。
            self._active_queue = None
            self._active_audio_chunks = None
            self._active_task = None

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

        try:
            async with aclosing(
                self._recognizer.stream(audio_chunks(), utterance_id=utterance_id)
            ) as events:
                async for event in events:
                    await self._on_asr_event(utterance_id, event)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self._detach_recognition(utterance_id)
            await self._report_asr_error(utterance_id, exc)
        finally:
            self._detach_recognition(utterance_id)

    async def _report_asr_error(self, utterance_id: str, error: Exception) -> None:
        logger.error(
            "asr_stream_failed recognizer=%s utterance_id=%s",
            type(self._recognizer).__name__,
            utterance_id,
            exc_info=(type(error), error, error.__traceback__),
        )
        if self._on_asr_error is not None:
            await self._on_asr_error(utterance_id, error)

    def _append_pre_roll(self, pcm: bytes) -> None:
        self._pre_roll.append(pcm)
        self._pre_roll_bytes += len(pcm)
        while self._pre_roll and self._pre_roll_bytes > self._pre_roll_limit:
            removed = self._pre_roll.popleft()
            self._pre_roll_bytes -= len(removed)

    def _recognition_finished(self, task: asyncio.Task[None]) -> None:
        self._recognition_tasks.discard(task)
        if task is self._active_task:
            self._detach_recognition(self._active_utterance_id)
        if task.cancelled():
            return
        error = task.exception()
        if error is not None:
            logger.error(
                "asr_stream_failed recognizer=%s",
                type(self._recognizer).__name__,
                exc_info=(type(error), error, error.__traceback__),
            )
